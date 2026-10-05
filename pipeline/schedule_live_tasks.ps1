# Registers (or re-registers) the daily pipeline tasks in Windows Task Scheduler, for the current
# user. Times are this PC's local time (Pacific). The PC has to be on and awake at those times;
# a missed run starts as soon as possible afterwards.
#
#   FantasyHockey-LiveInjuries  10:00 and 15:00 daily   Fleaflicker + ESPN injury reports, then the
#                               draft window's Status export (run_live_snapshot.cmd injuries)
#   FantasyHockey-NightlyIngest 04:00 daily               run_daily.py: last night's games, lineups,
#                               season totals, then the live injury spells
#   FantasyHockey-PlayerTeams   05:00 daily               import_player_teams.py: new signings, then
#                               each projected / league-pool player's NHL team (~12 min)
#   FantasyHockey-CatchUp       on wake from sleep and at logon (2 min later): catch_up.ps1 runs any
#                               of the three above whose last scheduled time passed while the PC
#                               slept -- Task Scheduler's own catch-up did not fire after a wake
#                               (2026-10-04). Re-registering a task here clears its last run time,
#                               so the next catch-up re-runs it once (harmless: each job only adds
#                               what is missing).
#
# The line-chart and starting-goalie snapshots are not scheduled: the plan server takes them
# (Fantasy Hockey AI/Server/server.py, via planpass.py) on each refresh, with an injury
# snapshot. Their old tasks -- FantasyHockey-LiveLines, -LiveGoalies -- are still removed here if
# present.
#
# Usage:  powershell -ExecutionPolicy Bypass -File schedule_live_tasks.ps1
#         powershell -ExecutionPolicy Bypass -File schedule_live_tasks.ps1 -Remove

param([switch]$Remove)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$names = "FantasyHockey-LiveInjuries", "FantasyHockey-LiveLines", "FantasyHockey-LiveGoalies",
    "FantasyHockey-NightlyIngest", "FantasyHockey-PlayerTeams", "FantasyHockey-CatchUp"

foreach ($name in $names) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}
if ($Remove) { Write-Output "Removed: $($names -join ', ')"; return }
Write-Output "Removed if present: FantasyHockey-LiveLines, FantasyHockey-LiveGoalies"

# Injury reports twice a day, each followed by the draft window's Status export.
$injuries = New-ScheduledTaskAction -Execute (Join-Path $here "run_live_snapshot.cmd") -Argument "injuries" `
    -WorkingDirectory $here
$injurySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "FantasyHockey-LiveInjuries" -Action $injuries `
    -Trigger @((New-ScheduledTaskTrigger -Daily -At 10:00), (New-ScheduledTaskTrigger -Daily -At 15:00)) `
    -Settings $injurySettings -Description "Fantasy Hockey AI live snapshot (injuries): pipeline\snapshot_live.py" | Out-Null
Write-Output "Registered FantasyHockey-LiveInjuries"

# Nightly ingest: a whole slate can take a while, so it gets two hours.
$nightly = New-ScheduledTaskAction -Execute (Join-Path $here "run_nightly_ingest.cmd") -WorkingDirectory $here
$nightlySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "FantasyHockey-NightlyIngest" -Action $nightly `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 04:00) -Settings $nightlySettings `
    -Description "Fantasy Hockey AI nightly ingest: pipeline\run_daily.py" | Out-Null
Write-Output "Registered FantasyHockey-NightlyIngest"

# Player teams: about 1,500 NHL player pages at the client's pacing, ~12 minutes.
$teams = New-ScheduledTaskAction -Execute (Join-Path $here "run_player_teams.cmd") -WorkingDirectory $here
$teamsSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "FantasyHockey-PlayerTeams" -Action $teams `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 05:00) -Settings $teamsSettings `
    -Description "Fantasy Hockey AI player teams: pipeline\import_player_teams.py" | Out-Null
Write-Output "Registered FantasyHockey-PlayerTeams"

# Catch-up: on every wake from sleep (System log, Power-Troubleshooter event 1 -- "The system has
# returned from a low power state") and at logon, two minutes later for the network.
$catchUp = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $here 'catch_up.ps1')`"" `
    -WorkingDirectory $here
$wake = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler |
    New-CimInstance -ClientOnly
$wake.Enabled = $true
$wake.Delay = "PT2M"
$wake.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">' +
    "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]" +
    '</Select></Query></QueryList>'
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT2M"
$catchUpSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "FantasyHockey-CatchUp" -Action $catchUp -Trigger @($wake, $logon) `
    -Settings $catchUpSettings -Description "Fantasy Hockey AI: run missed daily tasks after a wake or logon (pipeline\catch_up.ps1)" | Out-Null
Write-Output "Registered FantasyHockey-CatchUp"
