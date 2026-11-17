param([string]$Port = 'COM6', [int]$HttpPort = 8765)
$ErrorActionPreference = 'Stop'
$viewerPython = Join-Path $env:USERPROFILE '.platformio\penv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $viewerPython)) {
    throw 'PlatformIO Python was not found. Use python tools/ppg_viewer.py --port COM6 --open after installing tools/requirements.txt.'
}
& $viewerPython (Join-Path $PSScriptRoot 'ppg_viewer.py') --port $Port --http-port $HttpPort --open
