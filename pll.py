#!/usr/bin/env python3
"""
pll.py - short commands for the LMX2594 (wraps lmx2594.py).

Run via pll.bat (which uses the 32-bit Python):
    pll                      interactive prompt (device stays open between commands)
    pll freq 7.3G            one-shot command
    pll sweep 1G 10G 10M     one-shot sweep

Keep these in the same folder: pll.py, pll.bat, lmx2594.py,
USB2ANY.dll, HexRegisterValues.txt  (edit the settings below if yours differ).
Close TICS Pro first, since it holds the USB2ANY.
"""
import cmd
import os
import sys

from lmx2594 import DryRunBackend, LMX2594, USB2ANYBackend, parse_num

# ----------------------------- settings ------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
HEX_FILE = os.path.join(HERE, "HexRegisterValues.txt")   # TICS Pro register export
DLL_FILE = os.path.join(HERE, "USB2ANY.dll")
FOSC_HZ = 100_000_000                                    # reference oscillator
SPI_KHZ, SPI_PHASE, SPI_POL = 1000, 1, 0                 # USB2ANY SPI settings that worked
DEFAULT_DWELL_S = 0.005                                  # sweep dwell per point
# ---------------------------------------------------------------------------


def fmt_hz(f):
    f = float(f)
    return f"{f/1e9:.6f} GHz" if f >= 1e9 else f"{f/1e6:.6f} MHz"


class PllShell(cmd.Cmd):
    prompt = "pll> "
    intro = "LMX2594 control. Type 'help' for commands, 'quit' to exit."

    def __init__(self, pll, auto_init=True, readback=True):
        super().__init__()
        self.pll, self.auto_init, self.want_readback = pll, auto_init, readback
        self.ready = False

    # connect/initialise the chip the first time a command needs it
    def _ensure(self):
        if self.ready:
            return
        if self.auto_init:
            print("Initialising chip...")
            self.pll.init()
        if self.want_readback:
            self.pll.enable_readback()
        self.ready = True

    def onecmd(self, line):
        try:
            return super().onecmd(line)
        except (ValueError, RuntimeError, IndexError) as e:
            print("error:", e)
            return False

    def emptyline(self):
        pass

    # ---------------------------- commands -----------------------------------
    def do_freq(self, arg):
        """freq <frequency>     set RFout frequency, e.g.  freq 7.3G   freq 950M"""
        self._ensure()
        p = self.pll.set_frequency(parse_num(arg))
        print(f"RFout {fmt_hz(parse_num(arg))}  (N={p['n']} NUM={p['num']} DEN={p['den']} "
              f"chdiv={p['chdiv']} Fvco={fmt_hz(p['fvco'])})")
        if self.pll.readback:
            print("locked" if self.pll.wait_lock(0.2) else "NOT locked")

    def do_sweep(self, arg):
        """sweep <start> <stop> <step> [dwell] [repeat=N] [norecal]
        Stepped sweep. dwell defaults to 5m (5 ms).  repeat=0 loops until Ctrl-C.
        Examples:  sweep 1G 10G 10M     sweep 7G 8G 1M 2m repeat=0"""
        parts = arg.split()
        if len(parts) < 3:
            raise ValueError("usage: sweep <start> <stop> <step> [dwell] [repeat=N] [norecal]")
        start, stop, step = (parse_num(x) for x in parts[:3])
        if step <= 0 or stop < start:
            raise ValueError("need stop >= start and step > 0")
        dwell, repeat, recal = DEFAULT_DWELL_S, 1, True
        for tok in parts[3:]:
            if tok == "norecal":
                recal = False
            elif tok.startswith("repeat="):
                repeat = int(tok.split("=", 1)[1])
            else:
                dwell = parse_num(tok)
        self._ensure()
        n = int(round((stop - start) / step)) + 1
        print(f"Sweeping {fmt_hz(start)} -> {fmt_hz(stop)}, step {fmt_hz(step)}, "
              f"{n} points, dwell {dwell*1e3:g} ms. Ctrl-C to stop.")
        self.pll.sweep(start, stop, step, dwell=dwell, recal=recal, repeat=repeat,
                       on_step=lambda f: print(f"\r  {f/1e6:11.3f} MHz", end="", flush=True))
        print("\ndone")

    def do_status(self, arg):
        """status     show lock state and the current frequency plan registers"""
        self._ensure()
        if not self.pll.readback:
            self.pll.enable_readback()
        ls = self.pll.lock_state()
        r = self.pll.regs
        n = (((r[34] & 7) << 16) | r[36])
        print("lock:", {2: "LOCKED"}.get(ls, f"not locked (state {ls})"))
        print(f"Fpd={fmt_hz(self.pll.fpd())}  N={n}  NUM={(r[42] << 16) | r[43]}  DEN={(r[38] << 16) | r[39]}")

    def do_init(self, arg):
        """init     reset and reload all registers from the baseline file"""
        print("Initialising chip...")
        self.pll.init()
        if self.want_readback:
            self.pll.enable_readback()
        self.ready = True

    def do_outa(self, arg):
        """outa on|off     enable/disable RFoutA"""
        self._set_pd(6, arg)

    def do_outb(self, arg):
        """outb on|off     enable/disable RFoutB"""
        self._set_pd(7, arg)

    def _set_pd(self, bit, arg):
        a = arg.strip().lower()
        if a not in ("on", "off"):
            raise ValueError("say 'on' or 'off'")
        self._ensure()
        self.pll._field(44, bit, bit, 0 if a == "on" else 1)   # R44: OUTA_PD bit 6, OUTB_PD bit 7
        self.pll.write(44)
        print(f"RFout{'A' if bit == 6 else 'B'} {a}")

    def do_power(self, arg):
        """power a|b <0-63>     output power setting (bigger = more power)"""
        parts = arg.split()
        if len(parts) != 2 or parts[0].lower() not in ("a", "b") or not parts[1].isdigit() \
                or not 0 <= int(parts[1]) <= 63:
            raise ValueError("usage: power a|b <0-63>")
        self._ensure()
        v = int(parts[1])
        if parts[0].lower() == "a":
            self.pll._field(44, 13, 8, v)                       # R44: OUTA_PWR
            self.pll.write(44)
        else:
            self.pll._field(45, 5, 0, v)                        # R45: OUTB_PWR
            self.pll.write(45)
        print(f"RFout{parts[0].upper()} power = {v}")

    def do_quit(self, arg):
        """quit     leave the prompt (the chip keeps running)"""
        return True

    do_exit = do_quit

    def do_EOF(self, arg):
        print()
        return True


def main():
    argv = sys.argv[1:]
    auto_init = "--no-init" not in argv
    readback = "--no-readback" not in argv
    argv = [a for a in argv if a not in ("--no-init", "--no-readback")]

    if argv and argv[0] in ("-h", "--help", "help", "?"):
        print(__doc__)
        print("Commands: freq, sweep, status, init, outa, outb, power, quit")
        print("Options : --no-init (don't reset/reload the chip first), --no-readback (skip lock check)")
        return

    if os.environ.get("PLL_BACKEND") == "dryrun":            # for testing without hardware
        spi = DryRunBackend()
    else:
        spi = USB2ANYBackend(DLL_FILE, SPI_KHZ, SPI_PHASE, SPI_POL)
    pll = LMX2594(spi, FOSC_HZ, LMX2594.parse_hex_file(HEX_FILE))
    shell = PllShell(pll, auto_init=auto_init, readback=readback)
    try:
        if argv:
            shell.onecmd(" ".join(argv))
        else:
            shell.cmdloop()
    finally:
        spi.close()


if __name__ == "__main__":
    main()
