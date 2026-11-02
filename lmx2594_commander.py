#!/usr/bin/env python3
"""
LMX2594 Windows USB Controller
Talks to the LMX2594EVM via the Reference Pro board using USB2ANY.dll
"""

import ctypes
import os
import time

# Path to TI's official USB driver installed with TICS Pro
DLL_PATH = r"C:\Program Files (x86)\Texas Instruments\TICS Pro\USB2ANY.dll"


class USB2ANYController:

    def __init__(self):
        if not os.path.exists(DLL_PATH):
            raise FileNotFoundError(
                f"Could not find USB2ANY.dll at {DLL_PATH}.\n"
                "Please make sure TICS Pro is installed."
            )

        self.dll = ctypes.WinDLL(DLL_PATH)
        self.handle = None
        self._init_hardware()

    def _init_hardware(self):
        # Scan for Reference Pro / USB2ANY boards
        dev_count = self.dll.u2aFindControllers()
        if dev_count <= 0:
            raise RuntimeError(
                "No Reference Pro board detected! Check USB cable & 3.3V power."
            )

        # Open connection to the first board
        self.handle = self.dll.u2aOpen(0)
        if self.handle < 0:
            raise RuntimeError("Failed to open connection to Reference Pro board.")

        print("[USB2ANY] Successfully connected to Reference Pro board!")

        # Initialize SPI interface: 3-wire/4-wire, speed (~5 MHz), MSB first
        # Config params: Handle, ClockPhase/Polarity, BitOrder, Speed
        self.dll.u2aSPI_Control(self.handle, 0, 0, 0, 5000000)

    def write_register(self, addr: int, data: int):
        """Sends a 24-bit SPI packet [Addr(7b) + Data(16b)] over USB2ANY."""
        addr &= 0x7F
        data &= 0xFFFF

        # Construct 3-byte payload
        b0 = addr & 0x7F
        b1 = (data >> 8) & 0xFF
        b2 = data & 0xFF
        buffer = (ctypes.c_ubyte * 3)(b0, b1, b2)

        # Send via SPI Write API
        # API: u2aSPI_Write(handle, count, buffer)
        self.dll.u2aSPI_Write(self.handle, 3, buffer)

    def close(self):
        if self.handle is not None and self.handle >= 0:
            self.dll.u2aClose(self.handle)
            print("[USB2ANY] Connection closed.")


class LMX2594Commander:

    def __init__(self, f_osc_mhz=100.0, use_doubler=True):
        self.hw = USB2ANYController()
        self.f_osc = f_osc_mhz
        self.f_pd = (self.f_osc * 2) if use_doubler else self.f_osc

        # Default Register Map from TICS Pro setup
        self.regs = {
            0x00: 0x2618,
            0x01: 0x0808,
            0x02: 0x0500,
            0x03: 0x0642,
            0x04: 0x1A43,
            0x05: 0x00C8,
            0x06: 0xC802,
            0x07: 0x00B2,
            0x08: 0x2000,
            0x09: 0x0604,
            0x0A: 0x10D8,
            0x0B: 0x0018,
            0x0C: 0x5001,
            0x0D: 0x4000,
            0x0E: 0x1E50,
            0x0F: 0x064F,
            0x10: 0x0000,
            0x11: 0x012C,
            0x12: 0x0064,
            0x13: 0x2700,
            0x14: 0xF848,
            0x15: 0x0401,
            0x16: 0x0001,
            0x17: 0x007C,
            0x18: 0x071A,
            0x19: 0x0C2B,
            0x1A: 0x0DB0,
            0x1B: 0x0002,
            0x1C: 0x0488,
            0x1D: 0x318C,
            0x1E: 0x318C,
            0x1F: 0x03EC,
            0x20: 0x0393,
            0x21: 0x1E21,
            0x22: 0x0000,
            0x23: 0x0004,
            0x24: 0x0500,
            0x25: 0x0004,
            0x26: 0x0000,
            0x27: 0x0001,
            0x28: 0x0000,
            0x29: 0x0000,
            0x2A: 0x0000,
            0x2B: 0x0000,
            0x2C: 0x1F80,
            0x2D: 0xCEC0,
            0x2E: 0x07FD,
            0x2F: 0x0300,
            0x30: 0x0300,
            0x31: 0x4180,
            0x32: 0x0000,
            0x33: 0x0080,
            0x34: 0x0820,
            0x35: 0x0000,
            0x36: 0x0000,
            0x37: 0x0000,
            0x38: 0x0000,
            0x39: 0x0020,
            0x3A: 0x0001,
            0x3B: 0x0000,
            0x3C: 0x0000,
            0x3D: 0x00A8,
            0x3E: 0x0322,
            0x3F: 0x0000,
            0x40: 0x1388,
            0x41: 0x0000,
            0x42: 0x01F4,
            0x43: 0x0000,
            0x44: 0x03E8,
            0x45: 0x0000,
            0x46: 0x0000,
            0x47: 0x0001,
            0x48: 0x0000,
            0x49: 0x0000,
            0x4A: 0x0066,
            0x4B: 0x0800,
            0x4C: 0x0000,
            0x4D: 0x0000,
            0x4E: 0x016F,
        }
        self.flush_registers()

    def write_register(self, addr: int, data: int):
        self.regs[addr & 0x7F] = data & 0xFFFF
        self.hw.write_register(addr, data)

    def flush_registers(self):
        """Programs all registers in reverse order (highest address to 0)."""
        sorted_addrs = sorted(self.regs.keys(), reverse=True)
        for addr in sorted_addrs:
            self.hw.write_register(addr, self.regs[addr])

    def trigger_calibration(self):
        """Toggles FCAL_EN to lock frequency."""
        r0 = self.regs.get(0x00, 0x2618)
        self.write_register(0x00, r0 | 0x0001)
        time.sleep(0.005)
        self.write_register(0x00, r0 & ~0x0001)
        time.sleep(0.01)

    def set_frequency(self, freq_mhz: float):
        if not (10.0 <= freq_mhz <= 15000.0):
            raise ValueError("Frequency must be between 10 MHz and 15 GHz.")

        chdiv = 2
        vco_freq = freq_mhz * chdiv
        while vco_freq < 7500.0 and chdiv < 76:
            chdiv += 2
            vco_freq = freq_mhz * chdiv

        n_val = int(vco_freq / self.f_pd)
        remainder = (vco_freq / self.f_pd) - n_val
        den = 4294967293
        num = int(remainder * den)

        print(
            f"[PLL] Setting: {freq_mhz} MHz | VCO: {vco_freq} MHz | CHDIV: {chdiv} | N: {n_val}"
        )

        self.write_register(0x36, n_val & 0xFFFF)
        self.write_register(0x2B, (num >> 24) & 0xFF)
        self.write_register(0x2C, (num >> 8) & 0xFFFF)

        self.trigger_calibration()
        print(f"Successfully locked hardware to {freq_mhz} MHz!")

    def sweep_frequency(
        self,
        start_mhz: float,
        stop_mhz: float,
        step_mhz: float,
        dwell_sec: float = 0.5,
    ):
        curr = start_mhz
        direction = 1 if stop_mhz >= start_mhz else -1
        try:
            while curr <= stop_mhz if direction > 0 else curr >= stop_mhz:
                self.set_frequency(curr)
                time.sleep(dwell_sec)
                curr += step_mhz * direction
            print("Sweep complete!")
        except KeyboardInterrupt:
            print("\nSweep stopped.")


if __name__ == "__main__":
    pll = LMX2594Commander()