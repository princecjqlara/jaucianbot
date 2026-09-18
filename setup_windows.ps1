$ErrorActionPreference = 'Stop'

$tokenPath = Join-Path $PSScriptRoot '.telegram-token.dpapi'
$secureToken = Read-Host 'Paste the BotFather token (input is hidden)' -AsSecureString
if ($secureToken.Length -eq 0) { throw 'No token entered.' }
ConvertFrom-SecureString $secureToken | Set-Content -LiteralPath $tokenPath -NoNewline
Write-Host 'Token saved with Windows user encryption.'

& (Join-Path $PSScriptRoot 'telegram_windows.ps1') verify
if ($LASTEXITCODE -ne 0) { throw 'Bot verification failed. Check the token, then rerun setup.' }

$taskName = 'TelegramGroupInsights'
$script = Join-Path $PSScriptRoot 'telegram_windows.ps1'
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask -and $existingTask.Actions.Arguments -notlike "*$script*") {
    throw "A different scheduled task named '$taskName' already exists. Rename or remove it before rerunning setup."
}
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" collect"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Collector task '$taskName' started and will run when you log in."
Write-Host 'Add the bot to your Telegram groups, then run: py telegram_insights.py groups'
