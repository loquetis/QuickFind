# Builds dist\QuickFind.exe with the shared build venv.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$venv = 'C:\Users\corey\workspace\buildtools\pyenv315'
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    python -m venv $venv
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check pyinstaller
}
& "$venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath dist --workpath build QuickFind.spec
Get-FileHash dist\QuickFind.exe -Algorithm SHA256
