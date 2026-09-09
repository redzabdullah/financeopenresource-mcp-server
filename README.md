# Finance Data MCP Server

A read-only remote Model Context Protocol (MCP) server for public market data
and finance literature. Yahoo Finance is the primary market-data provider;
OpenAlex, arXiv, and DOAJ provide research and journal-directory records. The
server uses Streamable HTTP transport.

## Canonical repository

The canonical GitHub repository is
[`redzabdullah/Finance-Open-Resource`](https://github.com/redzabdullah/Finance-Open-Resource).

Clone it with:

```bash
git clone https://github.com/redzabdullah/Finance-Open-Resource.git
```

## Tools

Market tools: `get_stock_quote`, `get_historical_prices`,
`get_financial_statements`, `get_corporate_actions`, `get_holders`,
`get_sector_data`, `get_stock_screener`, `get_news`, `get_sustainability`, and
`get_market_snapshot`.

Research tools: `search_finance_research`, `get_research_paper`,
`search_finance_preprints`, `check_journal_legitimacy`, and
`search_finance_research_batch`.

## Coverage contract

Every tool response includes a `coverage_audit` without changing its original
result fields. Its `status` is one of:

- `complete`: all requested observations were returned;
- `partial`: only some requested entities or fields were returned;
- `unavailable`: the primary source returned no usable observations or failed;
- `truncated`: the connector limit may have excluded observations;
- `not_supported`: the requested data type is outside the tool capability;
- `conflicting`: reserved for unreconciled observations from multiple sources.

Empty collections mean unavailable data, never a measured zero. Malformed
parameters retain an `error` and include `error_type: "validation"` so clients
can distinguish invalid requests from legitimate empty results.

When coverage is incomplete, `fallback_recommendation` identifies the narrowest
configured authoritative supplementary source. It always includes
`requires_user_confirmation: true`. The server does not retrieve supplementary
data automatically. Clients must obtain explicit user confirmation before using
the recommendation, must label supplementary observations separately, and must
never overwrite primary-source values while differences remain unreconciled.

The audit records the primary provider, retrieval method, and retrieval date.
Future supplementary observations should also retain their provider, underlying
source, stable URL/accession/DOI, reporting or measurement date, retrieval date,
retrieval method, source status, and any transformation or reconciliation note.

Historical ownership is not supplied by `get_holders`; requesting it returns
`not_supported` and recommends the appropriate SEC EDGAR filings. A DEF 14A
table must retain its stated measurement date, 13F positions must not be treated
as a complete shareholder register, and truncated holder lists must not be used
to calculate total institutional ownership. Any later SEC workflow must verify
issuer identity (including CIK and CUSIP), account for amendments and duplicate
manager filings, and flag predecessor, SPAC, reverse-merger, and pre-listing
periods where relevant.

## Set up locally

Python 3.10 or newer is recommended.

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

On macOS or Linux, activate it with:

```bash
source .venv/bin/activate
```

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

## Run the server

Start it locally with:

```bash
python server.py
```

The server binds to `0.0.0.0` on port `8000` by default and exposes its
Streamable HTTP endpoint at `http://localhost:8000/mcp`. Set the `PORT`
environment variable to use a different port (Render supplies this variable
automatically).

No authentication, API key, or OAuth configuration is needed because the
tool only reads public Yahoo Finance data.
