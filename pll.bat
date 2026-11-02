@echo off
rem Launcher: runs pll.py with the 32-bit Python (needed for USB2ANY.dll).
"%LOCALAPPDATA%\Programs\Python\Python311-32\python.exe" "%~dp0pll.py" %*
