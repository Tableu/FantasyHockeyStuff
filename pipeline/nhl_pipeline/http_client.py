"""Shared HTTP session for NHL API calls: retry/backoff on transient failures,
a small delay between requests to be polite to an unofficial API."""

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

_REQUEST_DELAY_SECONDS = 0.25

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
