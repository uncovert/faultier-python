"""
SWIM host-side tests against a fake Faultier that speaks the control-channel
framing ("FLTR" + little-endian u32 length + protobuf) the way the firmware
does: header and body in separate USB packets, at most 64 bytes per packet.

These cover the Python side only (framing, chunking, errors, timeouts). The
SWIM protocol itself is exercised by the firmware's swim/test/swim_pio_sim.py
and on hardware.
"""
import struct

import pytest
import usb.core

import faultier
from faultier.Faultier import Faultier, SWIMError, SWIM_MAX_READ, SWIM_MAX_WRITE, SWIM_REPLY_TIMEOUT_MS
from faultier.faultier_pb2 import (
    Command, Response, ResponseHello, ResponseOk, ResponseError, ResponseSWIM,
    FAULTIER_VERSION, SWIM_ENTER, SWIM_EXIT, SWIM_SRST, SWIM_READ, SWIM_WRITE,
)

# The firmware drops commands longer than this (main.c).
FIRMWARE_MAX_COMMAND = 256


class FakeFirmware:
    def __init__(self, supports_swim=True, answer_swim=True):
        self.supports_swim = supports_swim
        self.answer_swim = answer_swim
        self.packets = []
        self.commands = []
        self.read_timeouts = []
        self.active = False
        self.memory = bytearray(range(256)) * 0x10000  # whole 24-bit space
        self.fail_next = None

    # ep_out
    def write(self, message):
        assert message[:4] == b"FLTR"
        length = struct.unpack("<I", message[4:8])[0]
        assert len(message) == 8 + length
        assert length <= FIRMWARE_MAX_COMMAND
        cmd = Command()
        cmd.ParseFromString(bytes(message[8:]))
        self.commands.append(cmd)
        reply = self.handle(cmd)
        if reply is not None:
            body = reply.SerializeToString()
            self.packets.append(b"FLTR" + struct.pack("<I", len(body)))
            for i in range(0, len(body), 64):
                self.packets.append(body[i:i + 64])
        return len(message)

    # ep_in
    def read(self, size, timeout=None):
        self.read_timeouts.append(timeout)
        if not self.packets:
            raise usb.core.USBTimeoutError("timeout")
        packet = self.packets.pop(0)
        assert len(packet) <= size
        return packet

    def handle(self, cmd):
        kind = cmd.WhichOneof("cmd")
        if kind == "hello":
            return Response(hello=ResponseHello(version=FAULTIER_VERSION,
                                                supports_swim=self.supports_swim))
        if kind == "configure_adc":
            return Response(ok=ResponseOk())
        if kind != "swim" or not self.answer_swim:
            return None                       # old firmware: "Invalid command!", no reply
        swim = cmd.swim
        if self.fail_next:
            message, self.fail_next = self.fail_next, None
            return Response(error=ResponseError(message=message))
        if swim.function == SWIM_ENTER:
            self.active = True
            return Response(swim=ResponseSWIM(sync_ns=15984))
        if swim.function == SWIM_EXIT:
            self.active = False
            return Response(ok=ResponseOk())
        if not self.active:
            return Response(error=ResponseError(message="SWIM: not active, call swim_enter()"))
        if swim.function == SWIM_SRST:
            return Response(ok=ResponseOk())
        if swim.function == SWIM_READ:
            assert 1 <= swim.length <= SWIM_MAX_READ
            data = bytes(self.memory[swim.address:swim.address + swim.length])
            return Response(swim=ResponseSWIM(data=data))
        if swim.function == SWIM_WRITE:
            assert 1 <= len(swim.data) <= SWIM_MAX_WRITE
            self.memory[swim.address:swim.address + len(swim.data)] = swim.data
            return Response(ok=ResponseOk())
        raise AssertionError(swim)


def make_faultier(fw):
    ft = Faultier.__new__(Faultier)
    ft.ep_out = fw
    ft.ep_in = fw
    ft._buffer = b""
    ft._send_hello()
    return ft


def swim_commands(fw):
    return [c.swim for c in fw.commands if c.WhichOneof("cmd") == "swim"]


def test_api_is_exported():
    for name in ("SWIMError", "SWIM_ENTER", "SWIM_READ"):
        assert hasattr(faultier, name)
    for name in ("swim_enter", "swim_read", "swim_write", "swim_reset", "swim_exit"):
        assert callable(getattr(faultier.Faultier, name))


def test_hello_reports_swim_support():
    assert make_faultier(FakeFirmware()).supports_swim is True
    assert make_faultier(FakeFirmware(supports_swim=False)).supports_swim is False


def test_session_round_trip():
    fw = FakeFirmware()
    ft = make_faultier(fw)
    ft.swim_enter()
    assert ft.swim_sync_ns == 15984
    assert ft.swim_read(0x4800, 4) == bytes(fw.memory[0x4800:0x4804])
    ft.swim_write(0x0000, b"\xde\xad\xbe\xef")
    assert ft.swim_read(0x0000, 4) == b"\xde\xad\xbe\xef"
    ft.swim_reset()
    ft.swim_exit()
    functions = [c.function for c in swim_commands(fw)]
    assert functions == [SWIM_ENTER, SWIM_READ, SWIM_WRITE, SWIM_READ, SWIM_SRST, SWIM_EXIT]
    assert swim_commands(fw)[0].hot_attach is False


def test_hot_attach_flag_is_sent():
    fw = FakeFirmware()
    make_faultier(fw).swim_enter(hot_attach=True)
    assert swim_commands(fw)[0].hot_attach is True


def test_long_read_is_split_and_reassembled():
    fw = FakeFirmware()
    ft = make_faultier(fw)
    ft.swim_enter()
    data = ft.swim_read(0x8000, 2500)        # replies span many 64-byte packets
    assert data == bytes(fw.memory[0x8000:0x8000 + 2500])
    reads = [c for c in swim_commands(fw) if c.function == SWIM_READ]
    assert [(c.address, c.length) for c in reads] == [
        (0x8000, 1024), (0x8400, 1024), (0x8800, 452)]


def test_long_write_is_split_and_fits_the_firmware_buffer():
    fw = FakeFirmware()
    ft = make_faultier(fw)
    ft.swim_enter()
    payload = bytes((i * 13) & 0xFF for i in range(300))
    ft.swim_write(0xFFFF00 - 300, payload)   # also checks a high 24-bit address
    assert bytes(fw.memory[0xFFFF00 - 300:0xFFFF00]) == payload
    writes = [c for c in swim_commands(fw) if c.function == SWIM_WRITE]
    assert [len(c.data) for c in writes] == [128, 128, 44]
    assert [c.address for c in writes] == [0xFFFF00 - 300, 0xFFFF00 - 172, 0xFFFF00 - 44]


def test_largest_command_fits_the_firmware_buffer():
    cmd = Command()
    cmd.swim.function = SWIM_WRITE
    cmd.swim.address = 0xFFFFFF
    cmd.swim.length = 0xFFFFFFFF
    cmd.swim.data = b"\xff" * SWIM_MAX_WRITE
    cmd.swim.hot_attach = True
    assert len(cmd.SerializeToString()) <= FIRMWARE_MAX_COMMAND


def test_zero_length_does_not_talk_to_the_device():
    fw = FakeFirmware()
    ft = make_faultier(fw)
    assert ft.swim_read(0x100, 0) == b""
    ft.swim_write(0x100, b"")
    assert swim_commands(fw) == []


def test_firmware_errors_raise_swim_error_with_its_message():
    fw = FakeFirmware()
    ft = make_faultier(fw)
    fw.fail_next = "SWIM: no sync frame from target"
    with pytest.raises(SWIMError, match="no sync frame"):
        ft.swim_enter()
    with pytest.raises(SWIMError, match="not active"):
        ft.swim_read(0x8000, 1)
    # The channel stays in sync after errors.
    ft.swim_enter()
    assert ft.swim_read(0x10, 2) == bytes(fw.memory[0x10:0x12])


def test_swim_error_is_a_value_error():
    # Existing callers catch ValueError for Faultier errors.
    assert issubclass(SWIMError, ValueError)


def test_old_firmware_is_refused_without_sending_anything():
    fw = FakeFirmware(supports_swim=False, answer_swim=False)
    ft = make_faultier(fw)
    with pytest.raises(SWIMError, match="no SWIM support"):
        ft.swim_enter()
    assert swim_commands(fw) == []


def test_missing_reply_times_out_instead_of_hanging():
    fw = FakeFirmware(answer_swim=False)
    ft = make_faultier(fw)
    with pytest.raises(SWIMError, match="No reply"):
        ft.swim_enter()
    assert fw.read_timeouts[-1] == SWIM_REPLY_TIMEOUT_MS


def test_bad_ranges_are_rejected_on_the_host():
    fw = FakeFirmware()
    ft = make_faultier(fw)
    ft.swim_enter()
    with pytest.raises(ValueError):
        ft.swim_read(0xFFFFFF, 2)
    with pytest.raises(ValueError):
        ft.swim_read(-1, 1)
    with pytest.raises(ValueError):
        ft.swim_write(0x1000000, b"\x00")
    assert [c.function for c in swim_commands(fw)] == [SWIM_ENTER]


def test_existing_commands_still_use_the_default_timeout():
    fw = FakeFirmware()
    ft = make_faultier(fw)
    ft.configure_adc(faultier.ADC_CROWBAR, 1000)
    assert fw.read_timeouts[-1] is None
