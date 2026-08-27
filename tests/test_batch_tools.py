import time
import unittest
from unittest.mock import patch

import server


class BatchToolTests(unittest.TestCase):
    @staticmethod
    def _quote(ticker):
        values = {
            "AAA": (10.0, 100.0),
            "BBB": (20.0, 300.0),
            "CCC": (30.0, 200.0),
        }
        if ticker not in values:
            return {"error": f"Invalid ticker '{ticker}': no market data was found."}
        price, market_cap = values[ticker]
        return {"ticker": ticker, "current_price": price, "market_cap": market_cap}

    @staticmethod
    def _research(entity, year_from=None, limit=10):
        if entity == "bad entity":
            return {"error": "Could not search OpenAlex: simulated failure"}
        return {
            "query": entity,
            "results": [{"title": f"{entity} paper {index}"} for index in range(limit)],
        }

    def test_market_snapshot_groups_and_ranks_valid_tickers(self):
        with patch.object(server, "get_stock_quote", side_effect=self._quote):
            result = server.get_market_snapshot(["AAA", "BBB", "CCC"])

        self.assertEqual(
            [entry["ticker"] for entry in result["results"]],
            ["BBB", "CCC", "AAA"],
        )

    def test_market_snapshot_isolates_bad_ticker(self):
        with patch.object(server, "get_stock_quote", side_effect=self._quote):
            result = server.get_market_snapshot(["AAA", "BAD", "BBB"])

        self.assertEqual(len(result["results"]), 3)
        self.assertEqual(result["results"][-1]["ticker"], "BAD")
        self.assertIn("error", result["results"][-1])

    def test_research_batch_groups_results_and_isolates_failure(self):
        with patch.object(server, "search_finance_research", side_effect=self._research):
            result = server.search_finance_research_batch(
                ["banking", "bad entity", "accounting"], limit_per_entity=2
            )

        self.assertEqual(
            [entry["entity"] for entry in result["results"]],
            ["banking", "bad entity", "accounting"],
        )
        self.assertEqual(len(result["results"][0]["results"]), 2)
        self.assertIn("error", result["results"][1])
        self.assertEqual(len(result["results"][2]["results"]), 2)

    def test_batch_limits_and_invalid_inputs_return_errors(self):
        cases = [
            server.get_market_snapshot([]),
            server.get_market_snapshot(None),
            server.get_market_snapshot(["AAPL"] * 26),
            server.search_finance_research_batch([]),
            server.search_finance_research_batch(None),
            server.search_finance_research_batch(["finance"] * 11),
            server.search_finance_research_batch(["finance"], 0),
        ]
        for result in cases:
            with self.subTest(result=result):
                self.assertIsInstance(result.get("error"), str)
                self.assertTrue(result["error"])

    def test_market_snapshot_is_faster_than_sequential_calls(self):
        tickers = ["AAA", "BBB", "CCC"]

        def delayed_quote(ticker):
            time.sleep(0.12)
            return self._quote(ticker)

        with patch.object(server, "get_stock_quote", side_effect=delayed_quote):
            sequential_start = time.perf_counter()
            [server._market_snapshot_for_ticker(ticker) for ticker in tickers]
            sequential_seconds = time.perf_counter() - sequential_start

            batch_start = time.perf_counter()
            server.get_market_snapshot(tickers)
            batch_seconds = time.perf_counter() - batch_start

        print(
            f"market timing: sequential={sequential_seconds:.3f}s "
            f"concurrent={batch_seconds:.3f}s"
        )
        self.assertLess(batch_seconds, sequential_seconds * 0.7)

    def test_research_batch_is_faster_than_sequential_calls(self):
        entities = ["banking", "finance", "accounting"]

        def delayed_research(entity, year_from=None, limit=10):
            time.sleep(0.12)
            return self._research(entity, year_from, limit)

        with patch.object(
            server, "search_finance_research", side_effect=delayed_research
        ):
            sequential_start = time.perf_counter()
            [server._finance_research_for_entity(entity, 2) for entity in entities]
            sequential_seconds = time.perf_counter() - sequential_start

            batch_start = time.perf_counter()
            server.search_finance_research_batch(entities, 2)
            batch_seconds = time.perf_counter() - batch_start

        print(
            f"research timing: sequential={sequential_seconds:.3f}s "
            f"concurrent={batch_seconds:.3f}s"
        )
        self.assertLess(batch_seconds, sequential_seconds * 0.7)


if __name__ == "__main__":
    unittest.main()
