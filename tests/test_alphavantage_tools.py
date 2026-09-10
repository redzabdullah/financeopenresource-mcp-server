from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import alphavantage_client
import server


class AlphaVantageQuotaTests(unittest.TestCase):
    def setUp(self):
        alphavantage_client.reset_quota_for_tests()

    @patch.dict(os.environ, {"ALPHA_VANTAGE_API_KEY": "test-key"})
    def test_quota_exhausted_does_not_attempt_http(self):
        with (
            patch.object(alphavantage_client, "DAILY_LIMIT", 0),
            patch.object(alphavantage_client, "urlopen") as mocked_urlopen,
        ):
            result = alphavantage_client.request(
                {"function": "GLOBAL_QUOTE", "symbol": "IBM"}
            )

        self.assertEqual(result["error_type"], "quota_exhausted")
        self.assertEqual(result["quota"], "daily")
        self.assertIn("resets_at", result)
        mocked_urlopen.assert_not_called()

    def test_missing_api_key_is_checked_only_when_called(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ALPHA_VANTAGE_API_KEY"):
                alphavantage_client.request({"function": "GLOBAL_QUOTE"})

    def test_daily_quota_resumes_from_persisted_state_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            state_file = Path(temp_directory) / ".av_quota_state.json"
            state_file.write_text(
                json.dumps({"date": date.today().isoformat(), "count": 20}),
                encoding="utf-8",
            )

            restarted_client = alphavantage_client.AlphaVantageClient(state_file)

            self.assertEqual(restarted_client._daily_count, 20)
            self.assertEqual(
                alphavantage_client.DAILY_LIMIT - restarted_client._daily_count,
                5,
            )

    def test_stale_daily_quota_resets_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_directory:
            state_file = Path(temp_directory) / ".av_quota_state.json"
            yesterday = date.today() - timedelta(days=1)
            state_file.write_text(
                json.dumps({"date": yesterday.isoformat(), "count": 20}),
                encoding="utf-8",
            )

            restarted_client = alphavantage_client.AlphaVantageClient(state_file)

            self.assertEqual(restarted_client._daily_count, 0)
            self.assertEqual(
                alphavantage_client.DAILY_LIMIT - restarted_client._daily_count,
                25,
            )


class AlphaVantageToolTests(unittest.TestCase):
    def test_stock_quote_parses_provider_response(self):
        payload = {
            "Global Quote": {
                "05. price": "123.45",
                "08. previous close": "120.00",
                "03. high": "125.00",
                "04. low": "119.00",
                "06. volume": "1000",
                "07. latest trading day": "2026-09-08",
                "09. change": "3.45",
                "10. change percent": "2.875%",
            }
        }
        with patch.object(server, "alphavantage_request", return_value=payload):
            result = server.get_stock_quote_av("ibm")

        self.assertEqual(result["ticker"], "IBM")
        self.assertEqual(result["current_price"], 123.45)
        self.assertEqual(result["coverage_audit"]["status"], "complete")

    def test_tool_quota_error_has_unavailable_coverage_reason(self):
        quota = {
            "error": "Alpha Vantage per-minute quota exhausted.",
            "error_type": "quota_exhausted",
            "quota": "per_minute",
            "limit": 5,
            "resets_at": "2026-09-09T01:02:03+00:00",
        }
        with patch.object(server, "alphavantage_request", return_value=quota):
            result = server.get_stock_quote_av("IBM")

        self.assertEqual(result["coverage_audit"]["status"], "unavailable")
        self.assertIn("per_minute", result["coverage_audit"]["missing"]["reason"])
        self.assertIn("2026-09-09", result["coverage_audit"]["missing"]["reason"])


class AlphaVantageFallbackTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _yahoo_partial():
        return {
            "ticker": "IBM",
            "current_price": None,
            "coverage_audit": {
                "status": "partial",
                "missing": {"fields": ["current_price"]},
            },
        }

    @staticmethod
    def _av_complete():
        return {
            "ticker": "IBM",
            "current_price": 123.45,
            "coverage_audit": {"status": "complete", "missing": {}},
        }

    @staticmethod
    def _context(supports_elicitation, action="accept", use=True):
        ctx = MagicMock()
        ctx.session.check_client_capability.return_value = supports_elicitation
        ctx.elicit = AsyncMock(
            return_value=SimpleNamespace(
                action=action,
                data=(
                    server.AlphaVantageFallbackConfirmation(
                        use_alpha_vantage=use
                    )
                    if action == "accept"
                    else None
                ),
            )
        )
        return ctx

    async def test_elicitation_accept_calls_av_and_merges_gap(self):
        ctx = self._context(True, "accept")
        with (
            patch.object(server, "_get_stock_quote_yahoo", return_value=self._yahoo_partial()),
            patch.object(server, "get_stock_quote_av", return_value=self._av_complete()) as av,
        ):
            result = await server.get_stock_quote("IBM", ctx)

        ctx.elicit.assert_awaited_once()
        av.assert_called_once_with("IBM")
        self.assertEqual(result["current_price"], 123.45)
        self.assertEqual(result["coverage_audit"]["status"], "complete")
        self.assertEqual(result["coverage_audit"]["alpha_vantage_offer"]["outcome"], "accepted_filled")

    async def test_elicitation_decline_returns_yahoo_unchanged(self):
        ctx = self._context(True, "decline")
        with (
            patch.object(server, "_get_stock_quote_yahoo", return_value=self._yahoo_partial()),
            patch.object(server, "get_stock_quote_av") as av,
        ):
            result = await server.get_stock_quote("IBM", ctx)

        av.assert_not_called()
        self.assertIsNone(result["current_price"])
        self.assertEqual(result["coverage_audit"]["alpha_vantage_offer"]["outcome"], "declined")

    async def test_no_elicitation_support_adds_suggestion(self):
        ctx = self._context(False)
        with (
            patch.object(server, "_get_stock_quote_yahoo", return_value=self._yahoo_partial()),
            patch.object(server, "get_stock_quote_av") as av,
        ):
            result = await server.get_stock_quote("IBM", ctx)

        ctx.elicit.assert_not_awaited()
        av.assert_not_called()
        self.assertEqual(result["coverage_audit"]["suggestion"]["provider"], "Alpha Vantage")
        self.assertIn("25 daily requests", result["coverage_audit"]["suggestion"]["message"])


if __name__ == "__main__":
    unittest.main()
