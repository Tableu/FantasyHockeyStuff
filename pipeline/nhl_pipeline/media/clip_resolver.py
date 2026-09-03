"""Resolves a goal's stable clip id (Game.Goals.HighlightClipID / DiscreteClipID) into a
downloadable video URL, on demand. Verified live against a real goal during planning:
https://nhl.com/video/mtl-buf-caufield-scores-goal-against-colten-ellis-6387782787112

Resolution chain: NHL's goal clips are hosted on Brightcove, account 6415718365001. The
account's "policy key" is public by Brightcove's design (every visitor's browser sends it)
and is embedded in the account's player bundle -- extracted fresh from there each call
rather than hardcoded, so a future key rotation self-heals without a code change.

IMPORTANT: the resolved MP4/HLS/DASH URLs are signed and expire in ~1 hour
(manifest_url_ttl in the API response). Never persist a resolved URL -- only the clip id
is durable (already stored on Game.Goals). Resolve immediately before use.
"""

import re

import requests

ACCOUNT_ID = "6415718365001"
PLAYER_ID = "default_default"
PLAYER_JS_URL = f"https://players.brightcove.net/{ACCOUNT_ID}/{PLAYER_ID}/index.min.js"
PLAYBACK_API_URL = f"https://edge.api.brightcove.com/playback/v1/accounts/{ACCOUNT_ID}/videos/{{clip_id}}"

_POLICY_KEY_PATTERN = re.compile(r'policyKey:"(BCpk[A-Za-z0-9_.\-]+)"')


def get_policy_key() -> str:
    response = requests.get(PLAYER_JS_URL, timeout=20)
    response.raise_for_status()
    match = _POLICY_KEY_PATTERN.search(response.text)
    if not match:
        raise RuntimeError(
            "Brightcove policy key not found in the player bundle -- "
            "NHL may have changed accounts/player ids, or rotated the key format."
        )
    return match.group(1)


def resolve_clip(clip_id: int, policy_key: str | None = None) -> dict:
    """Full Brightcove Playback API response for one clip id: name, duration, and a
    sources[] list of MP4/HLS/DASH renditions."""
    if policy_key is None:
        policy_key = get_policy_key()
    response = requests.get(
        PLAYBACK_API_URL.format(clip_id=clip_id),
        headers={"Accept": f"application/json;pk={policy_key}"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def get_mp4_url(clip_id: int, policy_key: str | None = None) -> str:
    """The direct, immediately-downloadable MP4 URL for a clip id. Expires in ~1 hour --
    use it right away (e.g. as the target of a streaming download), don't store it."""
    data = resolve_clip(clip_id, policy_key)
    for source in data.get("sources", []):
        if source.get("container") == "MP4" and source.get("src", "").startswith("https"):
            return source["src"]
    raise RuntimeError(f"No downloadable MP4 source found for clip {clip_id}")
