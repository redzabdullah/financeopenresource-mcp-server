import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

import server


class CoverageContractTests(unittest.TestCase):
    def test_complete_response(self):
        @server._coverage_tool("prices", "Yahoo Finance", "prices", 250)
        def retrieve():
            return {"prices": [{"date": "2026-01-02", "close": 10.0}]}

        result = retrieve()
        self.assertEqual(result["coverage_audit"]["status"], "complete")
        self.assertNotIn("fallback_recommendation", result)

    def test_partial_compound_response(self):
        @server._coverage_tool("corporate_actions", "Yahoo Finance")
        def retrieve(action_type="all"):
            return {"ticker": "TEST", "dividends": [{"amount": 1.0}], "splits": []}

        result = retrieve()
        self.assertEqual(result["coverage_audit"]["status"], "partial")
        self.assertEqual(result["coverage_audit"]["missing"]["fields"], ["splits"])
        self.assertTrue(result["fallback_recommendation"]["requires_user_confirmation"])

    def test_empty_response_is_unavailable_not_zero(self):
        @server._coverage_tool("ownership", "Yahoo Finance", "data", 20)
        def retrieve(ticker="TEST"):
            return {"ticker": ticker, "holder_type": "institutional", "data": []}

        result = retrieve()
        self.assertEqual(result["coverage_audit"]["status"], "unavailable")
        self.assertEqual(result["coverage_audit"]["returned"]["row_count"], 0)
        self.assertEqual(result["fallback_recommendation"]["recommended_source"], "SEC EDGAR")

    def test_limit_hit_is_truncated(self):
        @server._coverage_tool("prices", "Yahoo Finance", "prices", 2)
        def retrieve():
            return {"prices": [{"date": "2026-01-01"}, {"date": "2026-01-02"}]}

        result = retrieve()
        self.assertEqual(result["coverage_audit"]["status"], "truncated")
        self.assertIn("connector limit", result["coverage_audit"]["missing"]["reason"])

    def test_historical_ownership_is_not_supported(self):
        result = server.get_holders("IONQ", "historical_ownership")
        self.assertEqual(result["coverage_audit"]["status"], "not_supported")
        self.assertNotEqual(result.get("error_type"), "validation")
        self.assertTrue(result["fallback_recommendation"]["requires_user_confirmation"])

    def test_empty_major_and_institutional_holders_for_quantum_tickers(self):
        for ticker in ("IONQ", "RGTI", "QBTS", "QUBT"):
            for holder_type in ("institutional", "major"):
                stock = MagicMock()
                stock.history.return_value = pd.DataFrame({"Close": [1.0]})
                stock.institutional_holders = pd.DataFrame()
                stock.major_holders = pd.DataFrame()
                with self.subTest(ticker=ticker, holder_type=holder_type):
                    with patch.object(server.yf, "Ticker", return_value=stock):
                        result = server.get_holders(ticker, holder_type)
                    self.assertEqual(result["data"], [])
                    self.assertEqual(result["coverage_audit"]["status"], "unavailable")
                    self.assertEqual(result["coverage_audit"]["missing"]["tickers"], [ticker])
                    self.assertEqual(result["fallback_recommendation"]["recommended_source"], "SEC EDGAR")

    def test_validation_error_remains_distinct(self):
        result = server.get_holders("IONQ", "invalid-kind")
        self.assertEqual(result["error_type"], "validation")
        self.assertEqual(result["coverage_audit"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
