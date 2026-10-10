# Publishes the loopback Office server on the operator's own tailnet: HTTPS on 443, tailnet
# only, never Funnel. Started by run_office.ps1 next to the server; also fine to run by hand:
#   powershell -File scripts\serve_tailnet.ps1 -Port 8741 -Log office-server.log
# `tailscale serve --bg` is idempotent and tailscaled keeps it across reboots, but the CLI
# waits forever while Serve is not enabled on the tailnet (the operator enables it once, in the
# Tailscale admin console). So each attempt gets 20 seconds, and the loop keeps going for as
# long as the server lives: every minute at first, then every five. Only a change of outcome
# is logged.
param([int]$Port = 8741, [string]$Log = '', [int]$ParentPid = 0)
$ErrorActionPreference = 'Continue'
function Log($m) { if ($Log) { "$(Get-Date -Format s) $m" | Add-Content -Path $Log -Encoding utf8 } else { Write-Host $m } }
$ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (-not (Test-Path $ts)) { $ts = 'tailscale' }
Log "tailscale serve: watching (https 443 -> 127.0.0.1:$Port, tailnet only)"
$last = ''; $n = 0
while ($true) {
    if ($ParentPid -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { Log 'tailscale serve: server gone, stopping'; break }
    $n++
    $out = Join-Path $env:TEMP "office-serve-$PID.txt"
    try {
        $p = Start-Process -FilePath $ts -ArgumentList "serve --bg --https=443 http://127.0.0.1:$Port" `
            -WindowStyle Hidden -PassThru -RedirectStandardOutput $out
        if ($p.WaitForExit(20000) -and $p.ExitCode -eq 0) { Log 'tailscale serve: on'; break }
        try { $p.Kill() } catch {}
        $why = ''
        try { $why = ((Get-Content $out -ErrorAction Stop) -join ' ').Trim() } catch {}
        if ($why -match 'not enabled') { $why = 'Serve is not enabled on the tailnet; the operator enables it once in the Tailscale admin console' }
        if (-not $why) { $why = 'no answer from tailscale in 20s' }
    } catch { $why = "could not run tailscale: $_" }
    if ($why -ne $last) { Log "tailscale serve: not yet ($why)"; $last = $why }
    Start-Sleep -Seconds $(if ($n -lt 5) { 60 } else { 300 })
}
