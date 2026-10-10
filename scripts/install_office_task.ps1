# Registers the logon task "TheOffice" (run once; safe to re-run). Same hidden-launch pattern
# as the other scheduled jobs: wscript system\run_hidden.vbs <script.ps1>.
$vbs = 'C:\Users\faroo\Documents\PORTFOLIO\system\run_hidden.vbs'
$ps1 = Join-Path $PSScriptRoot 'run_office.ps1'
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`" `"$ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'TheOffice' -Action $action -Trigger $trigger -Settings $settings `
    -Description 'The Office board: loopback server + tailscale serve (tailnet only)' -Force | Out-Null
Get-ScheduledTask -TaskName 'TheOffice' | Select-Object TaskName, State
