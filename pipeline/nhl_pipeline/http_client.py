"""Shared HTTP session for NHL API calls: retry/backoff on transient failures,
a small delay between requests to be polite to an unofficial API."""

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

_REQUEST_DELAY_SECONDS = 0.25
_BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

_session = requests.Session()
_retry = Retry(
    total=3,
    backoff_factor=1.0,
    status_forcelist=(500, 502, 503, 504),
    allowed_methods=("GET",),
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://", HTTPAdapter(max_retries=_retry))


def get_json(url: str, params: dict | None = None, extra_headers: dict | None = None) -> dict:
    response = _session.get(url, params=params, headers=extra_headers, timeout=20)
    response.raise_for_status()
    time.sleep(_REQUEST_DELAY_SECONDS)
    return response.json()


def get_text(url: str, params: dict | None = None, extra_headers: dict | None = None) -> str:
    """HTML/text sibling of get_json(), for the static report pages on www.nhl.com (see
    api/html_shift_report.py). Those pages declare their charset only in a <meta> tag, so
    requests' header-based guess (latin-1) would mangle accented names -- the encoding is
    forced to UTF-8 instead. A browser User-Agent is sent because this is an ordinary web
    server, not the API host."""
    headers = {"User-Agent": _BROWSER_USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    response = _session.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"
    time.sleep(_REQUEST_DELAY_SECONDS)
    return response.text
