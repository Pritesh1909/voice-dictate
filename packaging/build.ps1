# Builds build\installer\ParakeetDictate-Setup-<version>.exe
#   1. PyInstaller bundles the app with its own Python into build\dist\ParakeetDictate\
#   2. Inno Setup wraps that folder into a per-user installer (no admin needed)
# Needs: the project's .venv with requirements + pyinstaller, and Inno Setup 6 (ISCC.exe).
$ErrorActionPreference = 'Stop'
$root  = Split-Path -Parent $PSScriptRoot
$py    = Join-Path $root '.venv\Scripts\python.exe'
$build = Join-Path $root 'build'
New-Item -ItemType Directory -Force $build | Out-Null

$version = (& $py -c "import sys; sys.path.insert(0, r'$root'); import parakeet_dictate as p; print(p.__version__)").Trim()
$v = ($version.Split('.') + @('0','0','0'))[0..3] -join ', '
Write-Host "Building Parakeet Dictate $version"

# Icon (same microphone as the tray icon)
& $py -c "import sys; sys.path.insert(0, r'$root'); from parakeet_dictate.tray import icon_image; icon_image('ready', 256).save(r'$build\icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE) { throw 'icon failed' }

# Windows version resource for the .exe
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($v), prodvers=($v), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('FileDescription', 'Parakeet Dictate - offline dictation'),
    StringStruct('ProductName', 'Parakeet Dictate'),
    StringStruct('FileVersion', '$version'),
    StringStruct('ProductVersion', '$version'),
    StringStruct('OriginalFilename', 'ParakeetDictate.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@ | Set-Content (Join-Path $build 'version.txt') -Encoding utf8

& $py -m PyInstaller --noconfirm --clean --log-level WARN --windowed --name ParakeetDictate `
    --icon (Join-Path $build 'icon.ico') --version-file (Join-Path $build 'version.txt') `
    --add-data "$root\parakeet_dictate\config.default.toml;parakeet_dictate" `
    --add-data "$root\parakeet_dictate\fonts;parakeet_dictate\fonts" `
    --collect-all transcribe_cpp --collect-all transcribe_cpp_native --collect-all _sounddevice_data `
    --collect-all sv_ttk --collect-submodules tomlkit --hidden-import parakeet_dictate.settings_ui `
    --exclude-module pytest --exclude-module PyInstaller `
    --paths $root --distpath (Join-Path $build 'dist') --workpath (Join-Path $build 'work') --specpath $build `
    (Join-Path $root 'packaging\launcher.py')
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'Inno Setup 6 (ISCC.exe) not found' }
& $iscc /Q "/DAppVersion=$version" "/DRoot=$root" (Join-Path $root 'packaging\installer.iss')
if ($LASTEXITCODE) { throw 'Inno Setup failed' }

$setup = Join-Path $build "installer\ParakeetDictate-Setup-$version.exe"
Write-Host ("Installer: {0} ({1:N0} MB)" -f $setup, ((Get-Item $setup).Length / 1MB))
