"""Small Alpha Vantage HTTP client with a process-wide free-tier quota gate."""

from collections import deque
from datetime import datetime, timedelta, timezone
import json
import os
from threading import Lock
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://www.alphavantage.co/query"
DAILY_LIMIT = 25
MINUTE_LIMIT = 5

_lock = Lock()
_daily_count = 0
_daily_started_at = datetime.now(timezone.utc)
_minute_requests = deque()


def reset_quota_for_tests() -> None:
    """Reset in-memory counters. Intended for deterministic unit tests."""
    global _daily_count, _daily_started_at
    with _lock:
        _daily_count = 0
        _daily_started_at = datetime.now(timezone.utc)
        _minute_requests.clear()


def _reserve_request() -> dict | None:
    """Atomically reserve one request or describe when capacity returns."""
    global _daily_count, _daily_started_at
    now_utc = datetime.now(timezone.utc)
    now_mono = time.monotonic()

    with _lock:
        if now_utc - _daily_started_at >= timedelta(hours=24):
            _daily_started_at = now_utc
            _daily_count = 0

        while _minute_requests and now_mono - _minute_requests[0] >= 60:
            _minute_requests.popleft()

        if _daily_count >= DAILY_LIMIT:
            resets_at = _daily_started_at + timedelta(hours=24)
            return {
                "error": "Alpha Vantage daily quota exhausted.",
                "error_type": "quota_exhausted",
                "quota": "daily",
                "limit": DAILY_LIMIT,
                "resets_at": resets_at.isoformat(),
            }

        if len(_minute_requests) >= MINUTE_LIMIT:
            retry_after = max(1, int(60 - (now_mono - _minute_requests[0])) + 1)
            resets_at = now_utc + timedelta(seconds=retry_after)
            return {
                "error": "Alpha Vantage per-minute quota exhausted.",
                "error_type": "quota_exhausted",
                "quota": "per_minute",
                "limit": MINUTE_LIMIT,
                "retry_after_seconds": retry_after,
                "resets_at": resets_at.isoformat(),
            }

        _daily_count += 1
        _minute_requests.append(now_mono)
        return None


def request(params: dict[str, str], timeout: float = 20) -> dict:
    """Call Alpha Vantage after enforcing the shared process-wide quota."""
    api_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ALPHA_VANTAGE_API_KEY is not set. Configure it before calling an Alpha Vantage tool."
        )

    quota_error = _reserve_request()
    if quota_error:
        return quota_error

    query = dict(params)
    query["apikey"] = api_key
    http_request = Request(
        f"{BASE_URL}?{urlencode(query)}",
        headers={"User-Agent": "Finance-Open-Resource/1.0"},
    )
    with urlopen(http_request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if "Note" in payload or "Information" in payload:
        return {
            "error": payload.get("Note") or payload.get("Information"),
            "error_type": "provider_limit",
        }
    if "Error Message" in payload:
        return {"error": payload["Error Message"], "error_type": "provider_error"}
    return payload
