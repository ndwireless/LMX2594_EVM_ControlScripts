#!/usr/bin/env python3
"""
lmx2594.py - control a TI LMX2594 PLL from Python without TICS Pro.

Workflow
--------
1. In TICS Pro, get the board locked the way you like, then export the
   register map (File -> Export Hex Registers, or similar). That .txt/.hex
   file is used as the *baseline*; this script only edits the fields it needs
   (N, fraction, channel divider, output mux, FCAL adjust bits).
2. Close TICS Pro (it holds the USB2ANY exclusively).
3. Run, e.g.:
       python lmx2594.py --hex baseline.txt --fosc 100M --backend usb2any init
       python lmx2594.py --hex baseline.txt --fosc 100M --backend usb2any freq 7.3G
       python lmx2594.py --hex baseline.txt --fosc 100M --backend usb2any sweep 7G 8G 10M --dwell 5m
   Use --backend dryrun to just print the SPI frames without hardware.

SPI frame: 24 bits, MSB first, SPI mode 0 (CPOL=0, CPHA=0), CS active low:
   [R/W(0=write,1=read) | addr(7)] [data(16)]

!! Register bit positions below were written from the datasheet from memory.
!! Check them against the LMX2594 register map before trusting them.
"""
from __future__ import annotations

import argparse
import re
import time
from fractions import Fraction
import os

CHDIV_TABLE = [2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 72, 96, 128, 192, 256, 384, 512, 768]
VCO_MIN, VCO_MAX = 7.5e9, 15.0e9


# --------------------------------------------------------------------------
# SPI backends
# --------------------------------------------------------------------------
class SpiBackend:
    def transfer(self, data: bytes) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass


class DryRunBackend(SpiBackend):
    def transfer(self, data: bytes) -> bytes:
        print("  SPI ->", data.hex())
        return bytes(len(data))


class FT232HBackend(SpiBackend):
    """Adafruit/any FT232H breakout. pip install pyftdi
    Wiring: AD0=SCK, AD1=MOSI, AD2=MISO, AD3=CS -> LMX2594 SCK/SDI/MUXOUT(SDO)/CSB."""

    def __init__(self, url="ftdi://ftdi:232h/1", freq=1_000_000, cs=0):
        from pyftdi.spi import SpiController

        self._ctrl = SpiController()
        self._ctrl.configure(url)
        self._port = self._ctrl.get_port(cs=cs, freq=freq, mode=0)

    def transfer(self, data: bytes) -> bytes:
        return bytes(self._port.exchange(data, len(data), duplex=True))

    def close(self):
        self._ctrl.terminate()


class USB2ANYBackend(SpiBackend):
    """TI USB2ANY / ReferencePRO via USB2ANY.dll (stdcall, 32-bit -> needs 32-bit Python).

    Signatures follow TI's "API Reference for the USB2ANY SDK 2.8":
      u2aSPI_Control(handle, ClockPhase, ClockPolarity, BitDirection, CharacterLength,
                     CSType, CSPolarity, DividerHigh, DividerLow)
      u2aSPI_WriteAndRead(handle, nBytes, buf)   # buf is sent, then overwritten with the reply
    Constants: phase 1 = capture on leading edge; polarity 0 = clock idles low;
               bit dir 1 = MSB first; char length 0 = 8 bit; CSType 1 = CS low for whole packet;
               CS polarity 1 = active low; divider = 24e6 / bit rate.
    TICS Pro must be closed (it holds the device).
    """

    def __init__(self, dll_path="USB2ANY.dll", spi_khz=1000, phase=1, polarity=0):
        import ctypes
        import os

        self.ct = ctypes
        folder = os.path.dirname(os.path.abspath(dll_path))
        if hasattr(os, "add_dll_directory") and os.path.isdir(folder):
            os.add_dll_directory(folder)         # so the DLL's own dependencies load
        if not os.path.exists(dll_path):
            raise FileNotFoundError(
                f"{dll_path} not found. Find it in the TICS Pro install folder and pass --dll <full path>")
        self.dll = ctypes.WinDLL(dll_path)       # stdcall

        # Windows' default timer tick is 15.6 ms, and the DLL appears to wait one tick per call.
        # Ask for a 1 ms tick for this process (undone in close()).
        self._winmm = None
        try:
            self._winmm = ctypes.WinDLL("winmm")
            self._winmm.timeBeginPeriod(15)
        except Exception:
            self._winmm = None

        c_int, c_char_p, c_ubyte = ctypes.c_int, ctypes.c_char_p, ctypes.c_ubyte
        self._find = self._fn("u2aFindControllers", 0)
        self._serial = self._fn("u2aGetSerialNumber", 2, [c_int, ctypes.c_char_p])
        self._open = self._fn("u2aOpen", 1, [c_char_p])
        self._close = self._fn("u2aClose", 1, [c_int])
        self._spi_ctl = self._fn("u2aSPI_Control", 9, [c_int] * 9)
        self._spi_wr = self._fn("u2aSPI_WriteAndRead", 3,
                                [c_int, c_int, ctypes.POINTER(c_ubyte)])
        try:
            self._fn("u2aSuppressSplash", 1, [c_int])(1)
        except Exception:
            pass

        n = self._find()
        if n < 1:
            raise RuntimeError("No USB2ANY found (is TICS Pro still open? is the board connected?)")
        sn = ctypes.create_string_buffer(80)
        self._check(self._serial(0, sn), "u2aGetSerialNumber")
        print("USB2ANY serial:", sn.value.decode(errors="replace"))
        self.handle = self._open(sn.value)
        self._check(self.handle, "u2aOpen")

        divider = int(24000 / spi_khz)           # 24 MHz / bit rate
        self._check(self._spi_ctl(self.handle, phase, polarity,
                                  1,             # MSB first
                                  0,             # 8-bit characters
                                  1,             # CS asserted for the whole packet
                                  1,             # CS active low
                                  (divider >> 8) & 0xFF, divider & 0xFF), "u2aSPI_Control")

    def _fn(self, name, nargs, argtypes=None):
        try:
            f = getattr(self.dll, name)
        except AttributeError:
            f = self.dll[f"_{name}@{4 * nargs}"]
        f.restype = self.ct.c_int
        if argtypes is not None:
            f.argtypes = argtypes
        return f

    @staticmethod
    def _check(rc, what):
        if rc < 0:
            raise RuntimeError(f"{what} failed with USB2ANY error code {rc}")

    def transfer(self, data: bytes) -> bytes:
        buf = (self.ct.c_ubyte * len(data))(*data)
        rc = self._spi_wr(self.handle, len(data), buf)
        self._check(rc, "u2aSPI_WriteAndRead")
        return bytes(buf)

    def close(self):
        if getattr(self, "handle", 0) > 0:
            self._close(self.handle)


# --------------------------------------------------------------------------
# LMX2594 driver
# --------------------------------------------------------------------------
class LMX2594:
    def __init__(self, spi: SpiBackend, fosc_hz: int, regs: dict[int, int] | None = None):
        self.spi = spi
        self.fosc = int(fosc_hz)
        self.regs: dict[int, int] = dict(regs or {})
        self.readback = False

    # ---- baseline file ----------------------------------------------------
    @staticmethod
    def parse_hex_file(path: str) -> dict[int, int]:
        """Accepts lines like 'R0<TAB>0x00251C' or 'R0 0x251C' (TICS Pro export)."""
        regs = {}
        pat = re.compile(r"^\s*R?(\d+)\s*[\t ,=]+\s*(?:0x)?([0-9A-Fa-f]+)")
        with open(path) as f:
            for line in f:
                m = pat.match(line)
                if m:
                    regs[int(m.group(1))] = int(m.group(2), 16) & 0xFFFF
        if 0 not in regs or 112 not in regs:
            raise ValueError("Hex file doesn't look like a full R0..R112 export")
        return regs

    # ---- raw SPI ----------------------------------------------------------
    def write(self, addr: int, value: int | None = None):
        if value is None:
            value = self.regs[addr]
        self.regs[addr] = value & 0xFFFF
        self.spi.transfer(bytes([addr & 0x7F, (value >> 8) & 0xFF, value & 0xFF]))

    def read(self, addr: int) -> int:
        r = self.spi.transfer(bytes([0x80 | (addr & 0x7F), 0, 0]))
        return (r[1] << 8) | r[2]

    def _field(self, addr: int, hi: int, lo: int, val: int):
        """Edit bits [hi:lo] of the shadow copy of a register (does not write)."""
        mask = ((1 << (hi - lo + 1)) - 1) << lo
        self.regs[addr] = (self.regs.get(addr, 0) & ~mask & 0xFFFF) | ((val << lo) & mask)

    # ---- bring-up ---------------------------------------------------------
    def init(self):
        """Reset, then program R112..R0 (R0 last, with FCAL_EN=1)."""
        r0 = self.regs[0]
        self.write(0, r0 | (1 << 1))             # RESET=1
        self.write(0, r0 & ~(1 << 1))            # RESET=0
        for a in range(112, 0, -1):
            self.write(a)
        time.sleep(0.01)
        self._field(0, 3, 3, 1)                  # FCAL_EN
        self.write(0)

    # ---- frequency plan ---------------------------------------------------
    def fpd(self) -> Fraction:
        dbl = 2 if (self.regs[9] >> 12) & 1 else 1
        mult = ((self.regs[10] >> 7) & 0x1F) or 1
        pre_r = self.regs[12] & 0xFFF
        r = (self.regs[11] >> 4) & 0xFF
        return Fraction(self.fosc) * dbl * mult / (pre_r * r)

    def plan(self, fout_hz: float):
        fout = Fraction(int(round(fout_hz)))
        if VCO_MIN <= fout <= VCO_MAX:
            chdiv = 1
        else:
            for d in CHDIV_TABLE:
                if VCO_MIN <= fout * d <= VCO_MAX:
                    chdiv = d
                    break
            else:
                raise ValueError(f"{float(fout)/1e6:.3f} MHz is outside 10 MHz..15 GHz")
        fvco = fout * chdiv
        ratio = fvco / self.fpd()
        n = int(ratio)
        frac = (ratio - n).limit_denominator(2**32 - 1)
        if frac >= 1:
            n, frac = n + 1, Fraction(0)
        return dict(chdiv=chdiv, fvco=fvco, n=n, num=frac.numerator,
                    den=frac.denominator, fpd=self.fpd())

    def set_frequency(self, fout_hz: float, recal: bool = True) -> dict:
        p = self.plan(fout_hz)
        n, num, den, chdiv = p["n"], p["num"], p["den"], p["chdiv"]
        if n < 28:
            print(f"warning: N={n} is low; check datasheet min-N for your MASH order/Fvco")

        # N and fraction
        self._field(34, 2, 0, n >> 16)
        self._field(36, 15, 0, n & 0xFFFF)
        self.regs[38], self.regs[39] = den >> 16, den & 0xFFFF
        self.regs[42], self.regs[43] = num >> 16, num & 0xFFFF

        # Channel divider / output mux (OUTx_MUX: 0=channel divider, 1=VCO)
        if chdiv == 1:
            mux = 1
        else:
            mux = 0
            self._field(75, 10, 6, CHDIV_TABLE.index(chdiv))
            self._field(31, 14, 14, 1)           # CHDIV_DIV2 / segment enable
        self._field(45, 12, 11, mux)             # OUTA_MUX
        self._field(46, 1, 0, mux)               # OUTB_MUX

        # VCO-cal helper bits depend on Fpd
        fpd = float(p["fpd"])
        hp = 0 if fpd <= 100e6 else 1 if fpd <= 150e6 else 2 if fpd <= 200e6 else 3
        lp = 0 if fpd >= 10e6 else 1 if fpd >= 5e6 else 2 if fpd >= 2.5e6 else 3
        self._field(0, 8, 7, hp)
        self._field(0, 6, 5, lp)

        for a in (75, 31, 45, 46, 34, 36, 38, 39, 42, 43):
            self.write(a)
        self._field(0, 3, 3, 1 if recal else 0)  # FCAL_EN triggers VCO calibration
        self.write(0)
        return p

    # ---- status -----------------------------------------------------------
    def enable_readback(self):
        """MUXOUT becomes SDO (MUXOUT_LD_SEL=0), so the LD pin no longer shows lock."""
        self._field(0, 2, 2, 0)
        self.write(0)
        self.readback = True

    def lock_state(self) -> int:
        return (self.read(110) >> 9) & 0x3        # 2 = locked

    def is_locked(self) -> bool:
        return self.lock_state() == 2

    def wait_lock(self, timeout=0.1, poll=0.0005) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.is_locked():
                return True
            time.sleep(poll)
        return False

    # ---- sweep ------------------------------------------------------------
    def sweep(self, start, stop, step, dwell=0.001, recal=True, repeat=1,
              on_step=None):
        """Software-timed stepped sweep. repeat=0 -> run until Ctrl-C."""
        n_pts = int(round((stop - start) / step)) + 1
        freqs = [start + i * step for i in range(n_pts)]
        k = 0
        try:
            while repeat == 0 or k < repeat:
                for f in freqs:
                    self.set_frequency(f, recal=recal)
                    if self.readback:
                        ok = self.wait_lock(timeout=max(dwell, 0.05))
                        if not ok:
                            print(f"  no lock at {f/1e6:.3f} MHz")
                    if dwell:
                        time.sleep(dwell)
                    if on_step:
                        on_step(f)
                k += 1
        except KeyboardInterrupt:
            print("sweep stopped")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def parse_num(s: str) -> float:
    s = s.strip()
    mult = {"k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "m": 1e-3, "u": 1e-6}
    if s and s[-1] in mult:
        return float(s[:-1]) * mult[s[-1]]
    return float(s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hex", required=True, help="register export from TICS Pro (baseline)")
    ap.add_argument("--fosc", default="100M", help="reference frequency, e.g. 100M")
    ap.add_argument("--backend", choices=["usb2any", "ft232h", "dryrun"], default="dryrun")
    ap.add_argument("--dll", default="USB2ANY.dll")
    ap.add_argument("--spi-khz", type=int, default=1000, help="USB2ANY SPI clock in kHz")
    ap.add_argument("--spi-phase", type=int, default=1, help="USB2ANY ClockPhase (1=capture on leading edge)")
    ap.add_argument("--spi-pol", type=int, default=0, help="USB2ANY ClockPolarity (0=idle low)")
    ap.add_argument("--readback", action="store_true", help="use SPI readback for lock detect")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="reset + full register load")
    p = sub.add_parser("freq"); p.add_argument("f")
    p = sub.add_parser("sweep")
    p.add_argument("start"); p.add_argument("stop"); p.add_argument("step")
    p.add_argument("--dwell", default="1m"); p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--no-recal", action="store_true")
    sub.add_parser("status")
    a = ap.parse_args()

    # ----------------------------- settings ------------------------------------
    HERE = os.path.dirname(os.path.abspath(__file__))
    HEX_FILE = os.path.join(HERE, "HexRegisterValues.txt")  # TICS Pro register export
    DLL_FILE = os.path.join(HERE, "USB2ANY.dll")
    FOSC_HZ = 100_000_000  # reference oscillator
    SPI_KHZ, SPI_PHASE, SPI_POL = 1000, 1, 0  # USB2ANY SPI settings that worked
    DEFAULT_DWELL_S = 0.005  # sweep dwell per point

    spi = USB2ANYBackend(DLL_FILE, SPI_KHZ, SPI_PHASE, SPI_POL)
    # spi = {"usb2any": lambda: USB2ANYBackend(a.dll, a.spi_khz, a.spi_phase, a.spi_pol),
    #        "ft232h": FT232HBackend,
    #        "dryrun": DryRunBackend}[a.backend]()
    pll = LMX2594(spi, int(parse_num(a.fosc)), LMX2594.parse_hex_file(a.hex))
    try:
        if a.cmd == "init":
            pll.init()
        if a.readback or a.cmd == "status":
            pll.enable_readback()
        if a.cmd == "freq":
            p = pll.set_frequency(parse_num(a.f))
            print(f"N={p['n']} NUM={p['num']} DEN={p['den']} chdiv={p['chdiv']} "
                  f"Fvco={float(p['fvco'])/1e6:.3f} MHz Fpd={float(p['fpd'])/1e6:.3f} MHz")
            if pll.readback:
                print("locked" if pll.wait_lock(0.2) else "NOT locked")
        elif a.cmd == "sweep":
            pll.sweep(parse_num(a.start), parse_num(a.stop), parse_num(a.step),
                      dwell=parse_num(a.dwell), recal=not a.no_recal, repeat=a.repeat)
        elif a.cmd == "status":
            print("lock state:", pll.lock_state(), "(2 = locked)")
    finally:
        spi.close()


if __name__ == "__main__":
    main()
