# Starts The Office at logon: the server on 127.0.0.1 only, published to the operator's
# own tailnet (HTTPS, private) by `tailscale serve`. Never Funnel, never the public internet.
# Launched hidden by Task Scheduler through system\run_hidden.vbs; see scripts\install_office_task.ps1.
$ErrorActionPreference = 'Continue'
$here = Split-Path -Parent $PSScriptRoot
$py = 'C:\Users\faroo\AppData\Local\Python\bin\python.exe'
$port = 8741
$log = Join-Path $here 'office-server.log'

function Log($m) { "$(Get-Date -Format s) $m" | Add-Content -Path $log -Encoding utf8 }

# Tailnet-only HTTPS in front of the loopback server. Idempotent; tailscaled keeps the config
# across reboots. Done in a background job with a timeout per attempt: `tailscale serve` waits
# forever when Serve is not yet enabled on the tailnet, and that must not hold the server back.
$null = Start-Job -ArgumentList $port, $log -ScriptBlock {
    param($port, $log)
    for ($i = 0; $i -lt 60; $i++) {
        $p = Start-Process -FilePath 'tailscale' -ArgumentList "serve --bg --https=443 http://127.0.0.1:$port" `
            -WindowStyle Hidden -PassThru
        if ($p.WaitForExit(20000)) {
            if ($p.ExitCode -eq 0) { "$(Get-Date -Format s) tailscale serve ok" | Add-Content $log; break }
        } else { try { $p.Kill() } catch {} }
        "$(Get-Date -Format s) tailscale serve not ready (is Serve enabled on the tailnet?); retrying" | Add-Content $log
        Start-Sleep -Seconds 60
    }
}

# The server, in the foreground so the task shows as Running while it lives.
$env:OFFICE_PORT = "$port"
Set-Location $here
Log "starting office.py"
& $py (Join-Path $here 'office.py') --port $port --allow-spawn *>> $log
Log "office.py exited ($LASTEXITCODE)"
exit $LASTEXITCODE
