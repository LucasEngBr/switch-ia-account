# Switch IA Account - installer (per-user, no admin)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dst = Join-Path $env:LOCALAPPDATA 'SwitchIAAccount'

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw 'Python 3.10+ nao encontrado. Instale em https://www.python.org/downloads/ e marque "Add to PATH".' }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'

Write-Host 'Instalando dependencias...'
& $py -m pip install --user -q -r (Join-Path $here 'requirements.txt')

# stop a running copy so the new files are used
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'SwitchIAAccount\\switcher' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item (Join-Path $here 'src\*') $dst -Force

# Claude's own icon, saved next to the app so it survives Claude updates
$claudeIco = Join-Path $dst 'claude.ico'
try {
    $pkg = Get-AppxPackage -Name Claude -ErrorAction SilentlyContinue | Select-Object -First 1
    $exe = if ($pkg) { Join-Path $pkg.InstallLocation 'app\Claude.exe' }
    if ($exe -and (Test-Path $exe)) {
        Add-Type -AssemblyName System.Drawing
        $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($exe)
        $fs = [IO.File]::Create($claudeIco); $icon.Save($fs); $fs.Close()
    }
} catch { }

$ws = New-Object -ComObject WScript.Shell
$sm = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
function New-Link($path, $script, $icon, $desc) {
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $pyw
    $s.Arguments = '"' + (Join-Path $dst $script) + '"'
    $s.WorkingDirectory = $dst
    if (Test-Path $icon) { $s.IconLocation = $icon }
    $s.Description = $desc
    $s.Save()
}
foreach ($lnk in (Join-Path $sm 'Switch IA Account.lnk'), (Join-Path $sm 'Startup\Switch IA Account.lnk')) {
    New-Link $lnk 'switcher.pyw' (Join-Path $dst 'icon.ico') 'Trocar de conta no Claude e no Codex'
}
# "Claude (ultima conta)": opens Claude in the account used last, not always the default one
$desk = [Environment]::GetFolderPath('Desktop')
$claudeLinks = @((Join-Path $sm 'Claude (ultima conta).lnk'), (Join-Path $desk 'Claude.lnk'))
foreach ($lnk in $claudeLinks) {
    New-Link $lnk 'open_claude.pyw' $claudeIco 'Abre o Claude na ultima conta usada'
}
# same AppUserModelID as the Claude app: its window groups under this shortcut, so a taskbar pin keeps it
try { & (Join-Path $here 'set-aumid.ps1') -Links $claudeLinks | Out-Null } catch { Write-Host "Aviso: AppUserModelID nao aplicado ($_)" }

Start-Process $pyw ('"' + (Join-Path $dst 'switcher.pyw') + '"') -WorkingDirectory $dst
Write-Host "Pronto. Switch IA Account na bandeja; atalho 'Claude' na Area de Trabalho abre a ultima conta. Instalado em $dst"
