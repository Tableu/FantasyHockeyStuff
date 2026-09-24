"""Roster state for a whole league, and the rules that make a manager legal.

Section 6 is explicit that this is bigger than "roster plus waiver wire": IR is a
roster-construction lever, the open free-agent pool is where most transactions happen, and the
weekly move counter has to be carried from the first version -- without it every streaming
comparison measures an illegal manager.

The move accounting is the part worth reading twice, because three of the four transaction kinds
are free:

    add (free agent or waiver claim)   costs 1 of the week's 7
    drop                               free
    IR stash                           free
    IR activate                        free

So a full roster's add-plus-drop costs one move, not two, and stashing is unconditionally worth
doing. Unused moves expire on the week boundary; they do not carry over.

Waivers are modelled because twelve managers share one pool and they interfere. A dropped player
is claimable only through the priority queue for `waiver_days`, resolved at the start of a game
day; everyone else is an instant add. A successful claim sends the claimant to the back of the
queue, which is the second scarce resource section 8 describes -- and it still costs a move, so
both stopping rules bind at once.

Every mutation goes through a method that raises on an illegal state rather than returning False.
A silently-refused transaction would show up as a manager who mysteriously stopped streaming, and
that is indistinguishable from a strategy finding.
"""

import logging
from collections import defaultdict

import pandas as pd

log = logging.getLogger("state")


class IllegalMove(RuntimeError):
    """A transaction the league's rules do not allow."""


class Team:
    """One fantasy team's holdings."""

    def __init__(self, team: int, config):
        self.team = team
        self.config = config
        self.roster = []        # ordered by acquisition, for stable tie-breaks
        self.ir = []
        self.moves_used = 0
        self.waiver_priority = team
        self.weekly_points = defaultdict(float)
        self.matchup_wins = 0.0

    @property
    def moves_left(self) -> int:
        return self.config.moves_per_week - self.moves_used

    def holds(self, player_id) -> bool:
        return player_id in self.roster or player_id in self.ir

    def assert_legal(self) -> None:
        if len(self.roster) > self.config.roster_size:
            raise IllegalMove(f"team {self.team} holds {len(self.roster)} of "
                              f"{self.config.roster_size} roster spots")
        if len(self.ir) > self.config.ir:
            raise IllegalMove(f"team {self.team} holds {len(self.ir)} of {self.config.ir} IR")
        if self.moves_used > self.config.moves_per_week:
            raise IllegalMove(f"team {self.team} used {self.moves_used} of "
                              f"{self.config.moves_per_week} moves this week")
        overlap = set(self.roster) & set(self.ir)
        if overlap:
            raise IllegalMove(f"team {self.team} holds {overlap} on both roster and IR")
        if len(set(self.roster)) != len(self.roster):
            raise IllegalMove(f"team {self.team} holds a duplicate on its roster")


class LeagueState:
    """Every team's holdings, the free-agent pool and the waiver queue."""

    def __init__(self, config, player_pool, eligibility):
        self.config = config
        self.eligibility = eligibility
        self.teams = [Team(t, config) for t in range(config.teams)]
        self.pool = set(player_pool)
        self.owner = {}                 # player_id -> team index
        self.waived = {}                # player_id -> date it clears waivers
        self.pending_claims = defaultdict(list)   # player_id -> [team index], until he clears
        self.claim_drops = {}                     # (team index, player_id) -> player to drop
        self.claims_submitted = defaultdict(int)  # team index -> claims entered
        self.failed_claims = []                   # {date, team, player_id, reason}
        self.week = None
        self.transactions = []

    # ---------- queries ----------

    def free_agents(self) -> set:
        return self.pool

    def on_waivers(self, player_id, today) -> bool:
        clears = self.waived.get(player_id)
        return clears is not None and pd.Timestamp(today) < clears

    def team_of(self, player_id):
        return self.owner.get(player_id)

    # ---------- the week boundary ----------

    def start_week(self, week: int) -> None:
        """Reset the move budget. Unused moves expire; they do not carry over."""
        self.week = week
        for team in self.teams:
            if self.config.moves_carry_over:
                team.moves_used = max(0, team.moves_used - self.config.moves_per_week)
            else:
                team.moves_used = 0

    # ---------- transactions ----------

    def draft(self, team_index: int, player_id) -> None:
        """Draft-time acquisition: no move cost, no roster-size check until the end."""
        if player_id not in self.pool:
            raise IllegalMove(f"{player_id} is not available to draft")
        team = self.teams[team_index]
        if len(team.roster) >= self.config.roster_size:
            raise IllegalMove(f"team {team_index} is full and cannot draft")
        self.pool.discard(player_id)
        team.roster.append(player_id)
        self.owner[player_id] = team_index
        team.assert_legal()

    def add(self, team_index: int, player_id, today, drop=None, reason="add") -> None:
        """Acquire a free agent. Costs one move; the accompanying drop does not."""
        team = self.teams[team_index]
        if player_id not in self.pool:
            raise IllegalMove(f"{player_id} is not a free agent (held by "
                              f"{self.owner.get(player_id)})")
        if self.on_waivers(player_id, today):
            raise IllegalMove(f"{player_id} is on waivers until "
                              f"{self.waived[player_id].date()}; use claim()")
        if team.moves_left <= 0:
            raise IllegalMove(f"team {team_index} has no moves left this week")
        self._acquire(team_index, player_id, today, drop, reason)

    def _acquire(self, team_index: int, player_id, today, drop, reason) -> None:
        """Take a player from the pool for one move: the part `add` and a waiver award share.
        A claim award skips `add`'s on-waivers refusal -- awarding a player from waivers is the
        one thing that refusal must not block."""
        team = self.teams[team_index]
        if drop is not None:
            self.drop(team_index, drop, today)
        if len(team.roster) >= self.config.roster_size:
            raise IllegalMove(f"team {team_index} must drop before adding")

        self.pool.discard(player_id)
        team.roster.append(player_id)
        self.owner[player_id] = team_index
        team.moves_used += 1
        self.waived.pop(player_id, None)
        team.assert_legal()
        self.transactions.append({"date": pd.Timestamp(today), "week": self.week,
                                  "team": team_index, "kind": reason,
                                  "player_id": player_id, "dropped": drop})

    def drop(self, team_index: int, player_id, today) -> None:
        """Release a player. Free, and he lands on waivers for the configured window."""
        team = self.teams[team_index]
        if player_id in team.roster:
            team.roster.remove(player_id)
        elif player_id in team.ir:
            team.ir.remove(player_id)
        else:
            raise IllegalMove(f"team {team_index} does not hold {player_id}")
        self.owner.pop(player_id, None)
        self.pool.add(player_id)
        self.waived[player_id] = pd.Timestamp(today) + pd.Timedelta(days=self.config.waiver_days)
        team.assert_legal()

    def stash(self, team_index: int, player_id, ir_eligible: set) -> None:
        """Move an injured player to IR. Free, so the only question is eligibility."""
        team = self.teams[team_index]
        if player_id not in team.roster:
            raise IllegalMove(f"team {team_index} cannot stash {player_id}: not on its roster")
        if player_id not in ir_eligible:
            raise IllegalMove(f"{player_id} is not IR-eligible today")
        if len(team.ir) >= self.config.ir:
            raise IllegalMove(f"team {team_index} has no IR slot free")
        team.roster.remove(player_id)
        team.ir.append(player_id)
        team.assert_legal()

    def activate(self, team_index: int, player_id, drop=None, stash=None, ir_eligible=None,
                 today=None) -> None:
        """Bring a player off IR. Free, but it needs a roster spot -- so on a full roster it forces
        a drop, or a swap with a newly injured player going the other way.

        `drop` may be the returning player himself (he is released straight off IR). `stash`
        swaps him with a rostered player who is IR-eligible, atomically, so a full IR does not
        block it. Neither spends a move, and neither is logged as a transaction: a forced drop is
        graded by the IR log, not by the move metrics.
        """
        team = self.teams[team_index]
        if player_id not in team.ir:
            raise IllegalMove(f"team {team_index} does not hold {player_id} on IR")
        if drop is not None:
            self.drop(team_index, drop, today)
            if drop == player_id:
                return
        if stash is not None:
            if stash not in team.roster:
                raise IllegalMove(f"team {team_index} cannot stash {stash}: not on its roster")
            if ir_eligible is None or stash not in ir_eligible:
                raise IllegalMove(f"{stash} is not IR-eligible today")
            team.ir.remove(player_id)
            team.roster.remove(stash)
            team.ir.append(stash)
            team.roster.append(player_id)
            team.assert_legal()
            return
        if len(team.roster) >= self.config.roster_size:
            raise IllegalMove(f"team {team_index} must drop before activating {player_id}")
        team.ir.remove(player_id)
        team.roster.append(player_id)
        team.assert_legal()

    def assert_ir_resolved(self, team_index: int, injured: set) -> None:
        """No healthy player on IR once a team's transactions are done. The league enforces it
        (a platform blocks every other move until it is fixed), so a manager that leaves one
        there is illegal, not merely lazy."""
        healthy = [p for p in self.teams[team_index].ir if p not in injured]
        if healthy:
            raise IllegalMove(f"team {team_index} left healthy {healthy} on IR")

    # ---------- waivers ----------

    def submit_claim(self, team_index: int, player_id, drop=None, today=None) -> None:
        """Enter a claim on a player on waivers. It resolves when his waiver window ends, not
        before (`process_waivers`).

        `drop` is who goes if the claim is awarded, chosen when the claim is priced. If he has
        left the roster by then, the award asks the manager again rather than failing silently.
        """
        if self.teams[team_index].moves_left <= 0:
            raise IllegalMove(f"team {team_index} has no moves left to claim with")
        if player_id not in self.waived or player_id not in self.pool:
            raise IllegalMove(f"{player_id} is not on waivers; add() him instead")
        if today is not None and not self.on_waivers(player_id, today):
            raise IllegalMove(f"{player_id} has already cleared waivers; add() him instead")
        if team_index not in self.pending_claims[player_id]:
            self.pending_claims[player_id].append(team_index)
            self.claims_submitted[team_index] += 1
        if drop is not None:
            self.claim_drops[(team_index, player_id)] = drop

    def process_waivers(self, today, redrop=None) -> list:
        """Award every claim whose player's waiver window has ended, by rolling priority.

        Claims used to be processed the next game day, while the player was usually still inside
        his window, and the award went through `add()` -- which refuses a player on waivers. So a
        claim almost never succeeded (one of two in a whole 2025-26 replay), and the refusal was
        a DEBUG line followed by the claim being cleared. Now:

          * a claim waits until its player clears (`clears <= today`), then resolves;
          * the claimant with the best priority who can still take him wins -- if he cannot (no
            moves left, no legal drop), the next claimant is tried, as a real league does;
          * a drop that has left the roster is re-chosen through `redrop(team, player)` (the
            engine passes the manager's rule), and None from it abandons the claim;
          * every failure is recorded in `failed_claims` with its reason.

        The winner spends one move and goes to the back of the priority queue. Returns the
        awarded (team, player_id) pairs.
        """
        today = pd.Timestamp(today)
        awarded = []
        due = sorted(p for p in self.pending_claims if not self.on_waivers(p, today))
        for player_id in due:
            claimants = sorted(set(self.pending_claims.pop(player_id)),
                               key=lambda t: self.teams[t].waiver_priority)
            for team_index in claimants:
                reason = self._claim_blocker(team_index, player_id, today, redrop)
                if reason is None:
                    try:
                        self._acquire(team_index, player_id, today,
                                      self.claim_drops.get((team_index, player_id)), "claim")
                    except IllegalMove as error:
                        reason = str(error)
                if reason is not None:
                    self.failed_claims.append({"date": today, "team": team_index,
                                               "player_id": player_id, "reason": reason})
                    continue
                self._to_back_of_queue(team_index)
                awarded.append((team_index, player_id))
                break
            for team_index in claimants:
                self.claim_drops.pop((team_index, player_id), None)
        self.waived = {p: d for p, d in self.waived.items() if today < d and p in self.pool}
        return awarded

    def _claim_blocker(self, team_index, player_id, today, redrop):
        """Why this team cannot be awarded this player now, or None. Re-chooses a stale drop."""
        team = self.teams[team_index]
        if player_id not in self.pool:
            return "no longer in the pool"
        if team.moves_left <= 0:
            return "no moves left this week"
        drop = self.claim_drops.get((team_index, player_id))
        if drop is not None and not team.holds(drop):
            drop = redrop(team_index, player_id) if redrop else None
            if drop is None:
                self.claim_drops.pop((team_index, player_id), None)
                return "the chosen drop had left the roster and no replacement drop was chosen"
            self.claim_drops[(team_index, player_id)] = drop
        if drop is None and len(team.roster) >= self.config.roster_size:
            return "a full roster and no drop"
        return None

    def _to_back_of_queue(self, winner: int) -> None:
        """Rolling priority: a successful claim drops you behind everyone who did not claim."""
        worst = max(t.waiver_priority for t in self.teams)
        for team in self.teams:
            if team.waiver_priority > self.teams[winner].waiver_priority:
                team.waiver_priority -= 1
        self.teams[winner].waiver_priority = worst

    # ---------- invariants ----------

    def assert_legal(self) -> None:
        for team in self.teams:
            team.assert_legal()
        held = [p for team in self.teams for p in team.roster + team.ir]
        if len(held) != len(set(held)):
            raise IllegalMove("a player is held by two teams at once")
        if set(held) & self.pool:
            raise IllegalMove("a player is both rostered and a free agent")
        for player_id, team_index in self.owner.items():
            if not self.teams[team_index].holds(player_id):
                raise IllegalMove(f"owner index says team {team_index} holds {player_id}, "
                                  f"but it does not")

    def summary(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "team": t.team, "roster": len(t.roster), "ir": len(t.ir),
            "moves_used": t.moves_used, "waiver_priority": t.waiver_priority,
            "points": sum(t.weekly_points.values()), "matchup_wins": t.matchup_wins,
        } for t in self.teams])
