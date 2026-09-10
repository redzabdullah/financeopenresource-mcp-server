"""Small Alpha Vantage HTTP client with a shared free-tier quota gate."""

from collections import deque
from datetime import date, datetime, time as datetime_time, timedelta
import json
import os
from pathlib import Path
from threading import Lock
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://www.alphavantage.co/query"
DAILY_LIMIT = 25
MINUTE_LIMIT = 5
DEFAULT_STATE_FILE = Path(__file__).resolve().with_name(".av_quota_state.json")


class AlphaVantageClient:
    """Alpha Vantage client with persistent daily and in-memory minute quotas."""

    def __init__(self, state_file: str | os.PathLike = DEFAULT_STATE_FILE):
        self.state_file = Path(state_file)
        self._lock = Lock()
        self._daily_count = 0
        self._daily_date = date.today()
        self._minute_requests = deque()
        self._load_daily_state()

    def _load_daily_state(self) -> None:
        """Resume today's count, or start at zero for stale/missing state."""
        today = date.today()
        try:
            stored = json.loads(self.state_file.read_text(encoding="utf-8"))
            stored_date = date.fromisoformat(str(stored.get("date", "")))
            stored_count = int(stored.get("count", 0))
        except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
            stored_date = None
            stored_count = 0

        self._daily_date = today
        self._daily_count = max(0, stored_count) if stored_date == today else 0

    def _save_daily_state(self) -> None:
        """Persist daily state atomically using a same-directory temp file."""
        temp_file = self.state_file.with_name(f"{self.state_file.name}.tmp")
        payload = {"date": self._daily_date.isoformat(), "count": self._daily_count}
        temp_file.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp_file, self.state_file)

    def reset_quota_for_tests(self) -> None:
        """Reset counters and remove persisted state for deterministic tests."""
        with self._lock:
            self._daily_count = 0
            self._daily_date = date.today()
            self._minute_requests.clear()
            try:
                self.state_file.unlink()
            except FileNotFoundError:
                pass

    def _reserve_request(self) -> dict | None:
        """Reserve one request or describe when quota capacity returns."""
        with self._lock:
            self._load_daily_state()
            today = date.today()
            now_mono = time.monotonic()

            while self._minute_requests and now_mono - self._minute_requests[0] >= 60:
                self._minute_requests.popleft()

            if self._daily_count >= DAILY_LIMIT:
                resets_at = datetime.combine(
                    today + timedelta(days=1), datetime_time.min
                ).astimezone()
                return {
                    "error": "Alpha Vantage daily quota exhausted.",
                    "error_type": "quota_exhausted",
                    "quota": "daily",
                    "limit": DAILY_LIMIT,
                    "resets_at": resets_at.isoformat(),
                }

            if len(self._minute_requests) >= MINUTE_LIMIT:
                retry_after = max(
                    1, int(60 - (now_mono - self._minute_requests[0])) + 1
                )
                resets_at = datetime.now().astimezone() + timedelta(seconds=retry_after)
                return {
                    "error": "Alpha Vantage per-minute quota exhausted.",
                    "error_type": "quota_exhausted",
                    "quota": "per_minute",
                    "limit": MINUTE_LIMIT,
                    "retry_after_seconds": retry_after,
                    "resets_at": resets_at.isoformat(),
                }

            self._daily_count += 1
            self._save_daily_state()
            self._minute_requests.append(now_mono)
            return None

    def request(self, params: dict[str, str], timeout: float = 20) -> dict:
        """Call Alpha Vantage after enforcing the shared quota."""
        api_key = os.environ.get("ALPHA_VANTAGE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ALPHA_VANTAGE_API_KEY is not set. Configure it before calling an Alpha Vantage tool."
            )

        quota_error = self._reserve_request()
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


_client = AlphaVantageClient()


def reset_quota_for_tests() -> None:
    """Reset the module client's quota state for deterministic unit tests."""
    _client.reset_quota_for_tests()


def request(params: dict[str, str], timeout: float = 20) -> dict:
    """Preserve the public request function used by server.py."""
    return _client.request(params, timeout)
