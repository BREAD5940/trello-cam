# Practice mode on the shop laptop (docs/practice.md): show people how to use AutoCAM live, keep nothing.
#   powershell -ExecutionPolicy Bypass -File C:\dev\frc-autocam\ops\practice.ps1 on
#   powershell -ExecutionPolicy Bypass -File C:\dev\frc-autocam\ops\practice.ps1 off
#   powershell -ExecutionPolicy Bypass -File C:\dev\frc-autocam\ops\practice.ps1        (says which is running)
# on:  stops the real service, archives the practice board's cards, starts practice's records fresh, and starts the
#      service on the practice board (a task of its own, "AutoCAM practice", made the first time).
# off: stops practice, puts back the names, Slack sign-ins and Onshape call count it gathered, takes its unstarted
#      jobs off the queue, and starts the real service again. A reboot always comes back in the real mode.
param([ValidateSet("on", "off", "status")][string]$Mode = "status")
$ErrorActionPreference = "Stop"
$repo = "C:\dev\frc-autocam"
$autocam = "C:\dev\venvs\frc-autocam\Scripts\autocam.exe"
$real = "AutoCAM service"
$practice = "AutoCAM practice"

function ServiceProcs([bool]$isPractice) {
    # the service's own processes only (autocam.exe and its python child), never Fusion or the scouting app
    Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and ($_.CommandLine -match 'autocam(\.exe|_service)') -and ($_.CommandLine -match '\brun\b') -and
        (($_.CommandLine -match '--practice') -eq $isPractice) }
}
function StopService([string]$task, [bool]$isPractice) {
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    ServiceProcs $isPractice | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 30; $i++) {
        if (-not (Get-NetTCPConnection -LocalPort 8790 -State Listen -ErrorAction SilentlyContinue)) { return }
        Start-Sleep -Seconds 1
    }
    throw "Port 8790 is still in use; nothing was switched."
}
function Running {
    $p = @(ServiceProcs $true).Count; $r = @(ServiceProcs $false).Count
    if ($p) { "Practice mode is ON ($p process(es))." } elseif ($r) { "The real service is running (practice is off)." }
    else { "Neither the real service nor practice is running." }
}

switch ($Mode) {
    "status" { Running }
    "on" {
        if (@(ServiceProcs $true).Count) { "Practice mode is already on."; break }
        if (-not (Get-ScheduledTask -TaskName $practice -ErrorAction SilentlyContinue)) {
            $t = Get-ScheduledTask -TaskName $real
            $action = New-ScheduledTaskAction -Execute $autocam -Argument "run --verbose --practice" -WorkingDirectory $repo
            Register-ScheduledTask -TaskName $practice -Action $action -Principal $t.Principal -Settings $t.Settings `
                -Description "AutoCAM in practice mode (ops\practice.ps1 on/off). Never started by itself." | Out-Null
            "Made the '$practice' task."
        }
        StopService $real $false
        & $autocam practice-reset
        if ($LASTEXITCODE -ne 0) { Start-ScheduledTask -TaskName $real; throw "practice-reset failed; the real service is back on." }
        Start-ScheduledTask -TaskName $practice
        Start-Sleep -Seconds 8
        Running
    }
    "off" {
        StopService $practice $true
        & $autocam practice-done
        Start-ScheduledTask -TaskName $real
        Start-Sleep -Seconds 8
        Running
    }
}
