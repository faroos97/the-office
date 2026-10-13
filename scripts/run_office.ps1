# Starts The Office at logon: the server on 127.0.0.1 only, published to the operator's
# own tailnet (HTTPS, private) by `tailscale serve`. Never Funnel, never the public internet.
# Launched hidden by Task Scheduler through system\run_hidden.vbs; see scripts\install_office_task.ps1.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $PSScriptRoot
$py = 'C:\Users\faroo\AppData\Local\Python\bin\python.exe'
$port = 8741
# A fresh log per start (the last five are kept): a handle left open on one log file by a
# crashed process once stopped the whole task from starting.
$log = Join-Path $here ('office-server-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
Get-ChildItem -Path $here -Filter 'office-server*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip 4 |
    ForEach-Object { try { Remove-Item $_.FullName -Force -ErrorAction Stop } catch {} }

function Log($m) { try { "$(Get-Date -Format s) $m" | Add-Content -Path $log -Encoding utf8 -ErrorAction Stop } catch {} }

# Tailnet-only HTTPS in front of the loopback server: scripts\serve_tailnet.ps1, as its own
# hidden process, so that `tailscale serve` waiting on the operator never holds the server back.
# It stops by itself when this script (and so the server) is gone. Its own log file: the
# server's log is held open by the redirection below, and a second writer is refused.
Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'serve_tailnet.ps1'),
    '-Port', $port, '-Log', (Join-Path $here 'office-serve.log'), '-ParentPid', $PID)

# The server, in the foreground so the task shows as Running while it lives.
$env:OFFICE_PORT = "$port"
Set-Location $here
Log "starting office.py"
# Through cmd.exe so the redirection appends plain UTF-8 bytes (PowerShell 5.1's own
# *>> writes UTF-16, which makes the log unreadable next to Add-Content's lines).
$script = Join-Path $here 'office.py'
& cmd.exe /c "`"$py`" -u `"$script`" --port $port --allow-spawn >> `"$log`" 2>&1"
Log "office.py exited ($LASTEXITCODE)"
exit $LASTEXITCODE
