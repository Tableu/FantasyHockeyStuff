"""The strategy parameters every manager reads, as one object rather than class constants.

The numbers live in `Settings/strategy.json` (beside the league's scoring and roster files),
because they are settings a user tunes, not code: section 11 sweeps them. Season reads the file
and hands the parsed object to `managers.build_field`; nothing here reads a file, so a live runner
can build a `Strategy` from any source it likes.

There are deliberately no defaults. A parameter a file forgets is an error at load, not a value
quietly inherited from whatever the code said last.

Where a value stands in for "unbounded" it is written as the string "inf" (margin, claim_premium)
or "season" (a horizon of the rest of the season), because JSON has neither.
"""

import math
from dataclasses import dataclass, fields

import adddrop
import streaming


@dataclass(frozen=True)
class Strategy:
    name: str
    adddrop: adddrop.AddDropParams            # rung 5 and up: section 9's add/drop rule
    streaming: streaming.StreamParams         # rung 7: section 10's streaming layer
    streamer_horizon_weeks: int | None        # rung 3: how far ahead a swap is priced
    full_system_horizon_weeks: int | None     # rung 4: how far ahead an acquisition is priced
    drop_horizon_weeks: int | None            # rung 4: a forced activation drop's window...
    drop_rate_source: str                     # ...and the rate it is priced on
    z_clip: float                             # rung 4: the matchup z is clipped to +-this
    z_source: str                             # rung 4: "closed_form" or "sampled" week totals
    prior_rate_shrink_games: float            # draft prior: games of league mean mixed in
    goalie_start_share_prior: float           # naive P(start): the share it shrinks toward...
    goalie_start_share_prior_games: float     # ...and how many games of it
    opening_days: int                         # draft board: days of rest-of-season read as "now"
    playoff_eliminated: str                   # "hold" (stop transacting) or "continue"
    playoff_week_weight: str                  # "p_advance" (weight later rounds) or "flat"
    vor_values: str                           # VOR board: "consensus" or "own_model" (reference)
    vor_min_sources: int                      # ...sources a player needs, else last season
    undated_sources: str                      # "include" or "exclude" a source with no publish date
    adp_platform: str                         # whose ADP the draft tools show (display only)
    eligibility_platform: str                 # whose positions the draft tools value players on
    description: str = ""


def _number(value):
    """A JSON number, or "inf" for unbounded."""
    if value == "inf":
        return math.inf
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"expected a number or \"inf\", got {value!r}")
    return value


def _horizon(value):
    """Weeks ahead, or "season" (None: the rest of the season)."""
    if value == "season":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"expected whole weeks >= 0 or \"season\", got {value!r}")
    return value


def _exactly(block, cls, label, convert):
    names = [f.name for f in fields(cls)]
    if set(block) != set(names):
        missing, extra = sorted(set(names) - set(block)), sorted(set(block) - set(names))
        raise ValueError(f"strategy {label}: missing {missing}, unknown {extra}")
    return cls(**{k: convert.get(k, lambda v: v)(block[k]) for k in names})


def from_dict(payload: dict, name: str = "") -> Strategy:
    """Parse a strategy file's contents. Every key is required; unknown keys are refused."""
    sections = {"description", "adddrop", "streaming", "rung3_streamer", "rung4_full_system",
                "priors", "playoffs", "draft"}
    if set(payload) - sections or sections - {"description"} - set(payload):
        raise ValueError(f"strategy {name}: sections must be {sorted(sections)}; "
                         f"got {sorted(payload)}")
    add = _exactly(payload["adddrop"], adddrop.AddDropParams, "adddrop",
                   {"horizon_weeks": _horizon, "margin": _number, "claim_premium": _number})
    stream = _exactly(payload["streaming"], streaming.StreamParams, "streaming",
                      {"lam": _number, "margin": _number})
    rung3, rung4, priors = (payload["rung3_streamer"], payload["rung4_full_system"],
                            payload["priors"])
    for label, block, keys in (
            ("rung3_streamer", rung3, {"horizon_weeks"}),
            ("rung4_full_system", rung4, {"horizon_weeks", "drop_horizon_weeks",
                                          "drop_rate_source", "z_clip", "z_source"}),
            ("priors", priors, {"prior_rate_shrink_games", "goalie_start_share_prior",
                                "goalie_start_share_prior_games", "opening_days"})):
        if set(block) != keys:
            raise ValueError(f"strategy {name} {label}: needs exactly {sorted(keys)}; "
                             f"got {sorted(block)}")
    playoffs = payload["playoffs"]
    if set(playoffs) != {"eliminated", "future_week_weight"}:
        raise ValueError(f"strategy {name} playoffs: needs exactly eliminated, future_week_weight; "
                         f"got {sorted(playoffs)}")
    if playoffs["eliminated"] not in ("hold", "continue"):
        raise ValueError(f"strategy {name}: playoffs.eliminated {playoffs['eliminated']!r}")
    if playoffs["future_week_weight"] not in ("p_advance", "flat"):
        raise ValueError(f"strategy {name}: playoffs.future_week_weight "
                         f"{playoffs['future_week_weight']!r}")
    draft = payload["draft"]
    draft_keys = {"vor_values", "min_sources", "undated_sources", "adp_platform",
                  "eligibility_platform"}
    if set(draft) != draft_keys:
        raise ValueError(f"strategy {name} draft: needs exactly {sorted(draft_keys)}; "
                         f"got {sorted(draft)}")
    if draft["vor_values"] not in ("own_model", "consensus"):
        raise ValueError(f"strategy {name}: draft.vor_values {draft['vor_values']!r}")
    if draft["undated_sources"] not in ("include", "exclude"):
        raise ValueError(f"strategy {name}: draft.undated_sources {draft['undated_sources']!r}")
    for key in ("adp_platform", "eligibility_platform"):
        if not isinstance(draft[key], str) or not draft[key].strip():
            raise ValueError(f"strategy {name}: draft.{key} {draft[key]!r}")
    min_sources = draft["min_sources"]
    if isinstance(min_sources, bool) or not isinstance(min_sources, int) or min_sources < 1:
        raise ValueError(f"strategy {name}: draft.min_sources {draft['min_sources']!r}")
    if rung4["z_source"] not in ("closed_form", "sampled"):
        raise ValueError(f"strategy {name}: z_source {rung4['z_source']!r}; use closed_form or sampled")
    for source in (add.rate_source, rung4["drop_rate_source"]):
        if source not in ("ros", "per_game"):
            raise ValueError(f"strategy {name}: rate source {source!r}; use ros or per_game")
    return Strategy(
        name=name,
        adddrop=add,
        streaming=stream,
        streamer_horizon_weeks=_horizon(rung3["horizon_weeks"]),
        full_system_horizon_weeks=_horizon(rung4["horizon_weeks"]),
        drop_horizon_weeks=_horizon(rung4["drop_horizon_weeks"]),
        drop_rate_source=rung4["drop_rate_source"],
        z_clip=float(_number(rung4["z_clip"])),
        z_source=rung4["z_source"],
        prior_rate_shrink_games=float(_number(priors["prior_rate_shrink_games"])),
        goalie_start_share_prior=float(_number(priors["goalie_start_share_prior"])),
        goalie_start_share_prior_games=float(_number(priors["goalie_start_share_prior_games"])),
        opening_days=int(priors["opening_days"]),
        playoff_eliminated=playoffs["eliminated"],
        playoff_week_weight=playoffs["future_week_weight"],
        vor_values=draft["vor_values"],
        vor_min_sources=draft["min_sources"],
        undated_sources=draft["undated_sources"],
        adp_platform=draft["adp_platform"].strip().lower(),
        eligibility_platform=draft["eligibility_platform"].strip().lower(),
        description=payload.get("description", ""),
    )
