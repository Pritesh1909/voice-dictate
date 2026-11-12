# Removes the Start menu and Startup shortcuts. Settings in %APPDATA%\ParakeetDictate are kept.
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
foreach ($lnk in @((Join-Path $programs 'Parakeet Dictate.lnk'), (Join-Path $programs 'Startup\Parakeet Dictate.lnk'))) {
    if (Test-Path $lnk) { Remove-Item $lnk; "Removed $lnk" }
}
