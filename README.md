# faultier-python: Uncovert fork

This is [Uncovert](https://uncovert.eu)'s fork of Hextree's
[faultier-python](https://github.com/hextreeio/faultier-python), the Python
library for the Faultier. It adds the host side of what our
[Faultier firmware fork](https://github.com/uncovert/faultier) adds:

- **STM8 SWIM debug access**: `swim_enter()`, `swim_read()`, `swim_write()`,
  `swim_reset()`, `swim_exit()`. Failures raise `faultier.SWIMError` with the
  reason (no target, missing pull-up, ...) instead of hanging. See
  [docs/source/swim.rst](docs/source/swim.rst).
- **`faultier bootloader`** (`Faultier.reboot_bootloader()`): firmware updates
  without the BOOTSEL button.

Everything else is unchanged. With firmware that lacks these features the new
calls fail with a clear error, and everything else keeps working.

```python
import faultier

ft = faultier.Faultier()
ft.swim_enter()                      # STM8 on IO8 (SWIM) and RST (NRST)
print(ft.swim_read(0x4800, 16).hex())   # option bytes
ft.swim_exit()                       # target runs its firmware again
```

Install from this fork:

```
pip install --upgrade git+https://github.com/uncovert/faultier-python
```

## Branches

- `main`: this fork, i.e. upstream plus the changes above.
- `stm8-swim`: the same changes without this notice, kept clean so they can be
  offered upstream.

---

# Faultier Python Library

This library is used to control Faultier-based glitchers.

STM8 SWIM support: see docs/source/swim.rst.
