# Requires PowerShell.
# Creates Windows scheduled task from PIPELINE_SCHEDULE_TIMES in .env, e.g.:
#   PIPELINE_SCHEDULE_TIMES=09:00,18:00,21:00
# Default if unset: 09:00,18:00,21:00.
# Times use the Windows system clock (set Windows timezone for your city).

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = (Resolve-Path (Join-Path $here "..")).Path
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Error "Command py not found in PATH. Install Python and the py launcher."
    exit 1
}

$envFile = Join-Path $repoRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^\s*#") { return }
        if ($_ -match "^\s*([^=]+)\s*=\s*(.*)\s*$") {
            $k = $Matches[1].Trim()
            $v = $Matches[2].Trim().Trim('"').Trim("'")
            if ($k -and $v -and -not [Environment]::GetEnvironmentVariable($k)) {
                [Environment]::SetEnvironmentVariable($k, $v)
            }
        }
    }
}

$timesRaw = [Environment]::GetEnvironmentVariable("PIPELINE_SCHEDULE_TIMES")
if ([string]::IsNullOrWhiteSpace($timesRaw)) { $timesRaw = "09:00,18:00,21:00" }
$times = @($timesRaw.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -match "^\d{1,2}:\d{2}$" })
if ($times.Count -eq 0) {
    Write-Error "PIPELINE_SCHEDULE_TIMES is empty or invalid. Example: 09:00,18:00,21:00"
    exit 1
}

$launcher = Join-Path $repoRoot "tools\launch_scheduled_pipeline.py"
$action = New-ScheduledTaskAction -Execute "py" -Argument "-3 `"$launcher`"" -WorkingDirectory $repoRoot
$triggers = @()
foreach ($t in $times) {
    $triggers += New-ScheduledTaskTrigger -Daily -At $t
}
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
try {
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "AutoBotEISPipeline" -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null
} catch {
    Register-ScheduledTask -TaskName "AutoBotEISPipeline" -Action $action -Trigger $triggers -Principal $principal -Force | Out-Null
}
$timesStr = $times -join ", "
Write-Host "OK: scheduled task AutoBotEISPipeline at $timesStr (local Windows time)."
Write-Host "Check: taskschd.msc -> Task Scheduler Library -> AutoBotEISPipeline"
