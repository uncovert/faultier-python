STM8 SWIM
=========

The Faultier can talk to STM8 microcontrollers over SWIM, ST's single-wire
debug interface (UM0470): enter debug mode, read and write memory, and reset
the target. This needs Faultier firmware with SWIM support
(``Faultier.supports_swim`` is ``True``).

Wiring
------

======  ===============================  ======
Signal  Faultier pin                     RP2040
======  ===============================  ======
SWIM    ``IO8`` on the 20-pin header     GP13
NRST    ``RST`` on the 5-pin SWD header  GP17
GND     any GND
======  ===============================  ======

* Add a pull-up of about 1 kOhm (470 Ohm to 2.2 kOhm) from SWIM to the
  target's VDD (or the Faultier's 3.3 V pin when both run at 3.3 V). The
  line has to rise within 250 ns; the built-in pull-ups take 400 ns or more.
  ``swim_enter()`` measures the rise time and refuses to start without one.
* Faultier pins are 3.3 V only. Run the STM8 at 3.3 V, or use open-drain level
  shifters on SWIM and NRST.

Example
-------

.. code-block:: python

    import faultier

    ft = faultier.Faultier()
    ft.swim_enter()                        # NRST held low during entry; core stays stalled
    print(ft.swim_sync_ns)                 # ~16000 ns: the target's SWIM clock is ~8 MHz
    print(ft.swim_rise_ns)                 # ~70 ns with a 1 kOhm pull-up
    option_bytes = ft.swim_read(0x4800, 16)
    flash = ft.swim_read(0x8000, 8192)
    ft.swim_write(0x0000, b"\x12\x34")     # RAM / registers
    ft.swim_reset()                        # SRST, session stays up
    ft.swim_exit()                         # target resets and runs its firmware

Every failure (no target, no answer to the entry sequence, missing pull-up,
target not acknowledging, calling ``swim_read`` before ``swim_enter``) raises
:class:`faultier.SWIMError` with the firmware's explanation. Nothing blocks:
the firmware bounds every wait for the target, and the host gives up on a
reply after 5 seconds.

``swim_enter(hot_attach=True)`` attaches without touching NRST, so running
firmware keeps running. That only works if the firmware has not turned the
SWIM pin into a GPIO; if it has, the error says the line is held low, and the
default (connect under reset) still gets in.

On a read-out protected STM8, flash reads succeed but return ``0x71`` for
every byte, and the core stays stalled while attached.

``swim_enter()`` also takes two tuning parameters that are normally left
alone: ``entry_low_us`` (first pulse of the entry sequence, default 1000) and
``max_rise_ns`` (rise-time limit, default 250).

``swim_write`` is a raw memory write. Flash and EEPROM need the target's own
unlock and programming sequence (``FLASH_PUKR``, ``FLASH_DUKR``,
``FLASH_CR2`` ...) written through it.

Glitching while SWIM is active is allowed: the glitcher and SWIM use different
hardware. If a glitch or power cycle resets the target, its SWIM session is
gone and the next SWIM call raises; call ``swim_enter()`` again.
