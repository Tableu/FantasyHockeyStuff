# Registers (or re-registers) the daily pipeline tasks in Windows Task Scheduler, for the current
# user. Times are this PC's local time (Pacific). The PC has to be on and awake at those times;
# a missed run starts as soon as possible afterwards.
#
#   FantasyHockey-NightlyIngest 04:00 daily               run_daily.py: last night's games, lineups,
#                               season totals, then the live injury spells
#   FantasyHockey-PlayerTeams   05:00 daily               import_player_teams.py: new signings, then
#                               each projected / league-pool player's NHL team (~12 min)
#
# The live snapshots (injuries, line charts, starting goalies; snapshot_live.py) are not scheduled:
# they run only while the plan window is open (Fantasy Hockey AI/Live/plan_gui.py, via planpass.py).
# Their old tasks -- FantasyHockey-LiveInjuries, -LiveLines, -LiveGoalies -- are still removed here
# if present.
#
# Usage:  powershell -ExecutionPolicy Bypass -File schedule_live_tasks.ps1
#         powershell -ExecutionPolicy Bypass -File schedule_live_tasks.ps1 -Remove

param([switch]$Remove)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$names = "FantasyHockey-LiveInjuries", "FantasyHockey-LiveLines", "FantasyHockey-LiveGoalies",
    "FantasyHockey-NightlyIngest", "FantasyHockey-PlayerTeams"

foreach ($name in $names) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}
if ($Remove) { Write-Output "Removed: $($names -join ', ')"; return }
Write-Output "Removed if present: FantasyHockey-LiveInjuries, FantasyHockey-LiveLines, FantasyHockey-LiveGoalies"

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
