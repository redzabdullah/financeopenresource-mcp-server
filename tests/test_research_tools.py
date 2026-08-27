import unittest
from unittest.mock import patch

import server


class ResearchToolTests(unittest.TestCase):
    def test_search_finance_research_success(self):
        payload = {
            "results": [
                {
                    "title": "Corporate Finance Research",
                    "authorships": [{"author": {"display_name": "Ada Author"}}],
                    "publication_year": 2025,
                    "primary_location": {
                        "source": {"display_name": "Journal of Finance"},
                        "pdf_url": None,
                    },
                    "best_oa_location": {"pdf_url": "https://example.org/paper.pdf"},
                    "doi": "https://doi.org/10.1234/example",
                    "abstract_inverted_index": {"Finance": [0], "matters": [1]},
                    "cited_by_count": 12,
                }
            ]
        }
        with patch.object(server, "_research_http_get_json", return_value=payload) as get:
            result = server.search_finance_research("corporate finance", 2020, 5)

        self.assertNotIn("error", result)
        self.assertEqual(result["results"][0]["authors"], ["Ada Author"])
        self.assertEqual(result["results"][0]["abstract_snippet"], "Finance matters")
        params = get.call_args.args[1]
        self.assertEqual(params["per-page"], 5)
        self.assertIn("from_publication_date:2020-01-01", params["filter"])
        self.assertEqual(params["mailto"], server.OPENALEX_MAILTO)

    def test_get_research_paper_success(self):
        payload = {
            "id": "https://openalex.org/W123",
            "doi": "https://doi.org/10.1234/example",
            "title": "A Paper",
            "authorships": [],
            "publication_year": 2024,
            "primary_location": {"source": {"display_name": "Finance Journal"}},
            "abstract_inverted_index": {"Full": [0], "abstract": [1]},
            "topics": [{"display_name": "Finance", "score": 0.9}],
            "concepts": [{"display_name": "Economics", "score": 0.8}],
            "cited_by_count": 7,
            "referenced_works": ["W1", "W2"],
        }
        with patch.object(server, "_research_http_get_json", return_value=payload):
            result = server.get_research_paper("10.1234/example")

        self.assertEqual(result["abstract"], "Full abstract")
        self.assertEqual(result["referenced_works_count"], 2)
        self.assertEqual(result["topics"][0]["name"], "Finance")

    def test_search_finance_preprints_success(self):
        xml = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>https://arxiv.org/abs/2601.00001v1</id>
            <published>2026-01-01T00:00:00Z</published>
            <title>Market microstructure</title>
            <summary>A quantitative finance preprint.</summary>
            <author><name>Ada Author</name></author>
            <link title="pdf" href="https://arxiv.org/pdf/2601.00001v1" type="application/pdf" />
          </entry>
        </feed>"""
        with patch.object(server, "_research_http_get", return_value=xml):
            result = server.search_finance_preprints("market microstructure", 5)

        item = result["results"][0]
        self.assertEqual(item["arxiv_id"], "2601.00001v1")
        self.assertEqual(item["review_status"], "preprint - not peer reviewed")

    def test_check_journal_legitimacy_success_and_not_found(self):
        payload = {
            "results": [
                {
                    "bibjson": {
                        "title": "Open Finance Journal",
                        "identifier": [
                            {"type": "pissn", "id": "1234-5678"},
                            {"type": "eissn", "id": "8765-4321"},
                        ],
                        "subject": [{"term": "Finance"}, {"term": "Economics"}],
                    }
                }
            ]
        }
        with patch.object(server, "_research_http_get_json", return_value=payload):
            found = server.check_journal_legitimacy("1234-5678")
        self.assertTrue(found["found"])
        self.assertEqual(found["subject_areas"], ["Finance", "Economics"])

        with patch.object(server, "_research_http_get_json", return_value={"results": []}):
            missing = server.check_journal_legitimacy("Definitely Not A Journal")
        self.assertEqual(missing, {"query": "Definitely Not A Journal", "found": False})

    def test_empty_and_invalid_inputs_return_structured_errors(self):
        cases = [
            server.search_finance_research(),
            server.search_finance_research(None),
            server.search_finance_research("finance", 999, 10),
            server.search_finance_research("finance", None, 0),
            server.get_research_paper(),
            server.get_research_paper(None),
            server.get_research_paper("not-an-id"),
            server.search_finance_preprints(),
            server.search_finance_preprints(None),
            server.search_finance_preprints("finance", 0),
            server.check_journal_legitimacy(),
            server.check_journal_legitimacy(None),
        ]
        for result in cases:
            with self.subTest(result=result):
                self.assertIsInstance(result.get("error"), str)
                self.assertTrue(result["error"])

    def test_nonexistent_openalex_id_returns_structured_error(self):
        error = server._ResearchAPIError("HTTP 404", status=404)
        with patch.object(server, "_research_http_get_json", side_effect=error):
            result = server.get_research_paper("W999999999999999")
        self.assertEqual(
            result,
            {"error": "No OpenAlex work found for 'W999999999999999'."},
        )

    def test_network_failures_return_structured_errors(self):
        failure = server._ResearchAPIError("timed out")
        cases = [
            ("_research_http_get_json", server.search_finance_research, ("finance",)),
            ("_research_http_get_json", server.get_research_paper, ("W123",)),
            ("_research_http_get", server.search_finance_preprints, ("finance",)),
            ("_research_http_get_json", server.check_journal_legitimacy, ("Finance",)),
        ]
        for helper, function, args in cases:
            with self.subTest(function=function.__name__):
                with patch.object(server, helper, side_effect=failure):
                    result = function(*args)
                self.assertIsInstance(result.get("error"), str)
                self.assertIn("timed out", result["error"])


if __name__ == "__main__":
    unittest.main()
