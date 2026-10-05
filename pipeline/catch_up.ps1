# Runs any daily pipeline task whose last scheduled time passed while the PC was asleep or off.
# Task Scheduler's own "run as soon as possible after a missed start" did not fire after a wake on
# 2026-10-04 (the 4:00 nightly ingest and the 10:00 injuries stayed missed for 25+ minutes), so
# FantasyHockey-CatchUp runs this on every wake from sleep and at logon (schedule_live_tasks.ps1).
#
# For each task: its most recent scheduled time (today's if it has passed, else yesterday's). If the
# task last ran before that and is not running now, it is started and waited for -- one at a time,
# nightly ingest first, so the database jobs never overlap. Output: logs\live\catch_up.log.
#
# Usage:  powershell -ExecutionPolicy Bypass -File catch_up.ps1          (what the task runs)
#         powershell -ExecutionPolicy Bypass -File catch_up.ps1 -WhatIf  (report, start nothing)

param([switch]$WhatIf)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $here "logs\live"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$log = Join-Path $logDir "catch_up.log"

function Say($text) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Output $line
}

function Last-Scheduled($task, $now) {
    # The latest of the task's daily trigger times that has already passed.
    $latest = $null
    foreach ($trigger in $task.Triggers) {
        if (-not $trigger.StartBoundary) { continue }
        $time = ([datetime]$trigger.StartBoundary).TimeOfDay
        $at = $now.Date + $time
        if ($at -gt $now) { $at = $at.AddDays(-1) }
        if ($latest -eq $null -or $at -gt $latest) { $latest = $at }
    }
    return $latest
}

$now = Get-Date
foreach ($name in "FantasyHockey-NightlyIngest", "FantasyHockey-PlayerTeams", "FantasyHockey-LiveInjuries") {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $task) { Say "$name : not registered, skipped"; continue }
    $due = Last-Scheduled $task $now
    $last = (Get-ScheduledTaskInfo -TaskName $name).LastRunTime
    if ($task.State -eq "Running") { Say "$name : running now"; continue }
    if ($due -eq $null -or $last -ge $due) {
        Say ("{0} : up to date (last run {1:yyyy-MM-dd HH:mm}, due {2:yyyy-MM-dd HH:mm})" -f $name, $last, $due)
        continue
    }
    if ($WhatIf) {
        Say ("{0} : MISSED (last run {1:yyyy-MM-dd HH:mm}, due {2:yyyy-MM-dd HH:mm}) -- would start it" -f $name, $last, $due)
        continue
    }
    Say ("{0} : missed (last run {1:yyyy-MM-dd HH:mm}, due {2:yyyy-MM-dd HH:mm}) -- starting it" -f $name, $last, $due)
    Start-ScheduledTask -TaskName $name
    Start-Sleep -Seconds 5
    while ((Get-ScheduledTask -TaskName $name).State -eq "Running") { Start-Sleep -Seconds 15 }
    Say ("{0} : finished, result {1}" -f $name, (Get-ScheduledTaskInfo -TaskName $name).LastTaskResult)
}
