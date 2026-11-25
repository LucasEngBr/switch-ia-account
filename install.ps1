# Switch IA Account - installer (per-user, no admin)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dst = Join-Path $env:LOCALAPPDATA 'SwitchIAAccount'

$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { throw 'Python 3.10+ nao encontrado. Instale em https://www.python.org/downloads/ e marque "Add to PATH".' }
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'

Write-Host 'Instalando dependencias...'
& $py -m pip install --user -q -r (Join-Path $here 'requirements.txt')

New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item (Join-Path $here 'src\*') $dst -Force

$ws = New-Object -ComObject WScript.Shell
$sm = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
foreach ($lnk in (Join-Path $sm 'Switch IA Account.lnk'), (Join-Path $sm 'Startup\Switch IA Account.lnk')) {
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = $pyw
    $s.Arguments = '"' + (Join-Path $dst 'switcher.pyw') + '"'
    $s.WorkingDirectory = $dst
    $s.IconLocation = (Join-Path $dst 'icon.ico')
    $s.Save()
}

Start-Process $pyw ('"' + (Join-Path $dst 'switcher.pyw') + '"') -WorkingDirectory $dst
Write-Host "Pronto. O icone esta na bandeja e abre junto com o Windows. Instalado em $dst"
