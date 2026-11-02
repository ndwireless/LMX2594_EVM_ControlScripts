from lmx2594 import USB2ANYBackend, LMX2594
import os

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
pll = LMX2594(spi, FOSC_HZ, LMX2594.parse_hex_file(HEX_FILE))
print(f'Running init!')
pll.init()
print(f'Running multiple frequency tests for timing')
import time
freqs_Hz = [(3900 + i)* 1e6 for i in range(200)]
time_start = time.time()
for freq in freqs_Hz:
    pll.set_frequency(fout_hz=freq, recal=True)
time_end = time.time()
print(f'\tTotal time: {time_end - time_start}[s]')
print(f'\tTime Per Hop: {(time_end - time_start) / len(freqs_Hz)}[s]')