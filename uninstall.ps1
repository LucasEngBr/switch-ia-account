# Switch IA Account - uninstaller. Keeps your saved accounts in ~/.codex-accounts and ~/.claude-accounts.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'SwitchIAAccount' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
$sm = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
Remove-Item (Join-Path $sm 'Switch IA Account.lnk'), (Join-Path $sm 'Startup\Switch IA Account.lnk') -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:LOCALAPPDATA 'SwitchIAAccount') -Recurse -Force -ErrorAction SilentlyContinue
Write-Host 'Removido. Contas salvas continuam em ~/.codex-accounts e ~/.claude-accounts (apague manualmente se quiser).'
