# Installs 32-bit Python 3.11.9 side by side with your existing 64-bit Python.
# It does NOT touch PATH, so nothing else on your machine changes.
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File .\install_python32.ps1

$ErrorActionPreference = "Stop"
$url     = "https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe"   # 32-bit installer
$exe     = "$env:TEMP\python-3.11.9-32bit.exe"
$target  = "$env:LOCALAPPDATA\Programs\Python\Python311-32"
$python  = "$target\python.exe"
$dll     = "$env:USERPROFILE\Downloads\USB2ANY.dll"

if (-not (Test-Path $python)) {
    Write-Host "Downloading 32-bit Python 3.11.9..."
    Invoke-WebRequest -Uri $url -OutFile $exe

    Write-Host "Installing to $target ..."
    Start-Process -FilePath $exe -Wait -ArgumentList @(
        "/quiet",
        "InstallAllUsers=0",
        "TargetDir=`"$target`"",
        "PrependPath=0",
        "Include_launcher=0",
        "Include_test=0",
        "Include_doc=0"
    )
} else {
    Write-Host "32-bit Python already installed at $target"
}

if (-not (Test-Path $python)) { throw "Install failed: $python not found" }

& $python -c "import struct; print('Installed Python is', struct.calcsize('P')*8, 'bit')"
& $python -m pip install --quiet --upgrade pip pefile

if (Test-Path $dll) {
    Write-Host "`nUSB2ANY.dll exported functions:"
    & $python -c "import pefile,sys; pe=pefile.PE(r'$dll'); print('DLL is', '32-bit' if pe.FILE_HEADER.Machine==0x14c else '64-bit'); print([e.name.decode() for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name])"
}

Write-Host "`nDone. Run the PLL script with:"
Write-Host "  & `"$python`" lmx2594.py --hex HexRegisterValues.txt --fosc 100M --backend usb2any --dll `"$dll`" init"
