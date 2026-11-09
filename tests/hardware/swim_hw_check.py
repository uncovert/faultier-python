#!/usr/bin/env python3
"""
SWIM smoke test against a real STM8 on a Faultier.

Wiring: STM8 SWIM -> IO8, STM8 NRST -> RST on the 5-pin SWD header, GND -> GND,
~1 kOhm pull-up from SWIM to the target's VDD (3.3 V; Faultier pins are not 5 V
tolerant).

Non-destructive: reads registers, option bytes and flash, and writes RAM only
(original contents restored). Never writes flash, EEPROM or option bytes.
Ends with swim_exit(), so the target runs its firmware again.

Usage: python3 swim_hw_check.py [--flash-size BYTES] [--skip-hot-attach] [--mux0-power-cycle]
"""
import argparse
import sys
import time

import faultier
from faultier import Faultier, SWIMError

SWIM_CSR = 0x7F80
DM_CSR2 = 0x7F99
CPU_REGS = 0x7F00          # A, PCE, PCH, PCL, XH, XL, YH, YL, SPH, SPL, CC
OPTION_BYTES = 0x4800
FLASH = 0x8000
RAM_TEST = 0x0010

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    print(f"[{'PASS' if condition else 'FAIL'}] {name}{': ' + detail if detail else ''}")
    return condition


def expect_swim_error(name, func, contains):
    try:
        func()
    except SWIMError as e:
        return check(name, contains in str(e), f"SWIMError({e})")
    return check(name, False, "no exception")


def hexdump(data):
    return " ".join(f"{b:02X}" for b in data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flash-size", type=lambda s: int(s, 0), default=8192)
    parser.add_argument("--skip-hot-attach", action="store_true")
    parser.add_argument("--mux0-power-cycle", action="store_true",
                        help="target is powered from MUX0: also power-cycle it during a session")
    args = parser.parse_args()

    ft = Faultier()
    check("firmware reports SWIM support", ft.supports_swim)
    expect_swim_error("read before swim_enter() is refused", lambda: ft.swim_read(SWIM_CSR, 1), "not active")

    t = time.monotonic()
    ft.swim_enter()
    check("swim_enter() under reset", True,
          f"{(time.monotonic() - t) * 1000:.1f} ms, sync frame {ft.swim_sync_ns} ns "
          f"-> SWIM clock {128e3 / ft.swim_sync_ns:.3f} MHz")

    csr = ft.swim_read(SWIM_CSR, 1)[0]
    check("SWIM_CSR has SAFE_MASK and SWIM_DM", csr & 0xA0 == 0xA0, f"0x{csr:02X}")
    dm_csr2 = ft.swim_read(DM_CSR2, 1)[0]
    print(f"       DM_CSR2 = 0x{dm_csr2:02X} (STALL={dm_csr2 >> 3 & 1})")
    regs = ft.swim_read(CPU_REGS, 11)
    pc = regs[1] << 16 | regs[2] << 8 | regs[3]
    print(f"       CPU: A={regs[0]:02X} PC={pc:06X} X={regs[4]:02X}{regs[5]:02X} "
          f"Y={regs[6]:02X}{regs[7]:02X} SP={regs[8]:02X}{regs[9]:02X} CC={regs[10]:02X}")
    opt = ft.swim_read(OPTION_BYTES, 16)
    print(f"       option bytes @4800: {hexdump(opt)}")
    vectors = ft.swim_read(FLASH, 32)
    print(f"       flash @8000: {hexdump(vectors)}")
    if all(b == 0x71 for b in vectors):
        # A read-out protected STM8 answers every flash read over SWIM with
        # 0x71. (ROP is option byte 0x4800: protected unless 0xAA on STM8L,
        # protected if 0xAA on STM8S.)
        print(f"       flash reads 0x71: read-out protection is on (ROP byte 0x{opt[0]:02X})")
        check("flash at 0x8000 readable (read-out protected chip: all 0x71)", True)
    else:
        check("vector table at 0x8000 (0x82 every 4 bytes, or erased)",
              all(b == 0x82 for b in vectors[::4]) or all(b == 0x00 for b in vectors)
              or all(b == 0xFF for b in vectors))

    original = ft.swim_read(RAM_TEST, 16)
    pattern = bytes((0xA5 ^ (i * 0x1D)) & 0xFF for i in range(16))
    ft.swim_write(RAM_TEST, pattern)
    readback = ft.swim_read(RAM_TEST, 16)
    ft.swim_write(RAM_TEST, original)
    restored = ft.swim_read(RAM_TEST, 16)
    check("RAM write/read back", readback == pattern, hexdump(readback))
    check("RAM restored", restored == original)

    t = time.monotonic()
    first = ft.swim_read(FLASH, args.flash_size)
    elapsed = time.monotonic() - t
    second = ft.swim_read(FLASH, args.flash_size)
    check(f"read {args.flash_size} bytes of flash twice, identical", first == second,
          f"{elapsed * 1000:.0f} ms ({args.flash_size / elapsed / 1024:.1f} KiB/s)")
    with open("stm8_flash_dump.bin", "wb") as f:
        f.write(first)

    ft.swim_reset()
    csr = ft.swim_read(SWIM_CSR, 1)[0]
    check("swim_reset() keeps the session", csr & 0x20, f"SWIM_CSR=0x{csr:02X} "
          f"DM_CSR2=0x{ft.swim_read(DM_CSR2, 1)[0]:02X}")

    # A glitch run while SWIM is active: the glitcher is on PIO0 and other pins,
    # so the session must survive one that does not touch the target.
    ft.configure_glitcher(trigger_type=faultier.TRIGGER_NONE, trigger_source=faultier.TRIGGER_IN_NONE,
                          glitch_output=faultier.OUT_NONE, power_cycle_output=faultier.OUT_NONE,
                          delay=1000, pulse=0)
    ft.glitch()
    check("glitch() with no output while SWIM is active, session survives",
          ft.swim_read(SWIM_CSR, 1)[0] & 0x20)

    if args.mux0_power_cycle:
        # The target is powered from MUX0: a power-cycle "glitch" in the middle
        # of a session takes SWIM down on the target side.
        ft.configure_glitcher(power_cycle_output=faultier.OUT_MUX0, power_cycle_length=25_000_000)
        ft.power_cycle()
        ft.configure_glitcher(power_cycle_output=faultier.OUT_NONE, power_cycle_length=0)
        t = time.monotonic()
        try:
            ft.swim_read(SWIM_CSR, 1)
            check("power cycle during a session: next SWIM call raises", False, "no exception")
        except SWIMError as e:
            check("power cycle during a session: next SWIM call raises", True,
                  f"{(time.monotonic() - t) * 1000:.1f} ms, SWIMError({e})")
        ft.swim_enter()
        check("swim_enter() again after the power cycle", ft.swim_read(SWIM_CSR, 1)[0] & 0x20)

    ft.swim_exit()
    expect_swim_error("read after swim_exit() is refused", lambda: ft.swim_read(SWIM_CSR, 1), "not active")

    if not args.skip_hot_attach:
        time.sleep(0.2)
        ft.swim_enter(hot_attach=True)
        dm_csr2 = ft.swim_read(DM_CSR2, 1)[0]
        ram = ft.swim_read(0x0000, 16)
        check("hot attach: RAM readable", True,
              f"STALL={dm_csr2 >> 3 & 1}, RAM@0000: {hexdump(ram)}")
        flash = ft.swim_read(FLASH, 16)
        check("hot attach: flash read matches dump", flash == first[:16], hexdump(flash))
        ft.swim_exit()

    failed = [name for name, ok in results if not ok]
    print("ALL PASS" if not failed else f"{len(failed)} FAILED: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
