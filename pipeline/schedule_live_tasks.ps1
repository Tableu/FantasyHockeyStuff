# Registers (or re-registers) the live snapshot tasks in Windows Task Scheduler, for the current
# user. Times are this PC's local time (Pacific). The PC has to be on and awake at those times;
# a missed run starts as soon as possible afterwards.
#
#   FantasyHockey-LiveInjuries  10:00 and 15:00 daily   Fleaflicker + ESPN injury reports
#   FantasyHockey-LiveLines     10:05 and 15:05 daily   Daily Faceoff line charts (+ their injury flags)
#   FantasyHockey-LiveGoalies   every 10 min, 08:00-24:00 daily
#                               (exits without fetching once every game that day has started)
#   FantasyHockey-NightlyIngest 04:00 daily               run_daily.py: last night's games, lineups,
#                               season totals, then the live injury spells
#   FantasyHockey-PlayerTeams   05:00 daily               import_player_teams.py: new signings, then
#                               each projected / league-pool player's NHL team (~12 min)
#
# Usage:  powershell -ExecutionPolicy Bypass -File schedule_live_tasks.ps1
#         powershell -ExecutionPolicy Bypass -File schedule_live_tasks.ps1 -Remove

param([switch]$Remove)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $here "run_live_snapshot.cmd"
$names = "FantasyHockey-LiveInjuries", "FantasyHockey-LiveLines", "FantasyHockey-LiveGoalies",
    "FantasyHockey-NightlyIngest", "FantasyHockey-PlayerTeams"

foreach ($name in $names) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}
if ($Remove) { Write-Output "Removed: $($names -join ', ')"; return }

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

function Register-Snapshot($name, $kind, $triggers) {
    $action = New-ScheduledTaskAction -Execute $runner -Argument $kind -WorkingDirectory $here
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers -Settings $settings `
        -Description "Fantasy Hockey AI live snapshot ($kind): pipeline\snapshot_live.py" | Out-Null
    Write-Output "Registered $name"
}

Register-Snapshot "FantasyHockey-LiveInjuries" "injuries" @(
    (New-ScheduledTaskTrigger -Daily -At 10:00), (New-ScheduledTaskTrigger -Daily -At 15:00))
Register-Snapshot "FantasyHockey-LiveLines" "lines" @(
    (New-ScheduledTaskTrigger -Daily -At 10:05), (New-ScheduledTaskTrigger -Daily -At 15:05))

# A daily trigger that repeats every 10 minutes for 16 hours.
$goalies = New-ScheduledTaskTrigger -Daily -At 08:00
$goalies.Repetition = (New-ScheduledTaskTrigger -Once -At 08:00 -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Hours 16)).Repetition
Register-Snapshot "FantasyHockey-LiveGoalies" "goalies" @($goalies)

# Nightly ingest: a whole slate can take a while, so it gets a longer limit than a snapshot.
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
