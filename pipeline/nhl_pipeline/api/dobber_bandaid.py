"""Dobber's Band-Aid Boys -- DobberHockey's yearly list of players at risk of missing games.

One public article per season (2026: https://dobberhockey.com/2026/07/22/dobbers-band-aid-boys-2026/).
Each group is an <h2> heading followed by a table of player links:

- "Certified Band-Aid Boys": "virtually guaranteed to miss games and a significant risk to miss
  12 or more in a given season";
- "Band-Aid Boy Trainees": "probably going to miss six or seven games ... and has some risk of
  suffering an injury that will cost him 12 or more";
- "Goalies": listed on their own, with no tier.

The tier is kept as the article's own heading ("Certified", "Trainee", "Goalie"); nothing is
inferred beyond it.
"""

import html
import re

from nhl_pipeline.http_client import get_text

URLS = {"2026-27": "https://dobberhockey.com/2026/07/22/dobbers-band-aid-boys-2026/"}

_TIERS = (("certified", "Certified"), ("trainee", "Trainee"), ("goalie", "Goalie"))
_HEADING = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S | re.I)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


def _tier(heading: str):
    lowered = heading.lower()
    return next((tier for key, tier in _TIERS if key in lowered), None)


def parse(page: str) -> list:
    """[{"name", "tier"}] in the article's order. A heading that is not one of the three groups
    ends the previous group's table and is skipped; a group with no players is an error, since the
    page layout has changed."""
    out = []
    headings = list(_HEADING.finditer(page))
    for i, heading in enumerate(headings):
        tier = _tier(_TAGS.sub("", html.unescape(heading.group(1))))
        if tier is None:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(page)
        section = page[heading.end():end]
        section = section[:section.find("</table>")] if "</table>" in section else ""
        names = [html.unescape(_TAGS.sub("", c)).replace("\xa0", " ").strip()
                 for c in _CELL.findall(section)]
        names = [n for n in names if n]
        if not names:
            raise ValueError(f"Band-Aid Boys: no players under the {tier!r} heading")
        out.extend({"name": n, "tier": tier} for n in names)
    found = {r["tier"] for r in out}
    missing = [t for _, t in _TIERS if t not in found]
    if missing:
        raise ValueError(f"Band-Aid Boys: no {missing} group on the page")
    return out


def fetch(season: str, url: str | None = None) -> list:
    url = url or URLS.get(season)
    if url is None:
        raise ValueError(f"no Band-Aid Boys article known for {season}; pass its url")
    return parse(get_text(url))
