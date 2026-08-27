"""A small MCP server that exposes public Yahoo Finance market data."""

import os
import json
import re
from datetime import date, datetime, timezone
from math import isnan
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import yfinance as yf
from yfinance.const import SECTOR_INDUSTY_MAPPING_LC
from mcp.server.mcpserver import MCPServer, Context
from mcp.types import ToolAnnotations


mcp = MCPServer("finance-data-server")


def _json_value(value: Any) -> Any:
    """Convert pandas/numpy scalar values into JSON-friendly Python values."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    converted = value.item() if hasattr(value, "item") else value
    if isinstance(converted, float) and isnan(converted):
        return None
    return converted


def _iso_datetime(value: Any) -> str | None:
    """Convert Yahoo timestamps or date strings to an ISO-formatted string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _normalize_text(value: Any) -> str:
    """Normalize a string parameter without raising on malformed input."""
    return value.strip() if isinstance(value, str) else ""


def _ticker_validation_error(stock: yf.Ticker, symbol: str) -> dict | None:
    """Return a structured error when Yahoo has no market data for a symbol."""
    try:
        if stock.history(period="5d").empty:
            return {"error": f"Invalid ticker '{symbol}': no market data was found."}
    except Exception as exc:
        return {"error": f"Could not validate ticker '{symbol}': {exc}"}
    return None


OPENALEX_MAILTO = "redzuana@smu.edu.sg"
RESEARCH_HTTP_TIMEOUT = 15


class _ResearchAPIError(Exception):
    """Represent an expected failure from an external research API."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _research_http_get(url: str, params: dict | None = None) -> bytes:
    """Fetch an external research API response with a bounded timeout."""
    if params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "finance-research-mcp/1.0"})
    try:
        with urlopen(request, timeout=RESEARCH_HTTP_TIMEOUT) as response:
            return response.read()
    except HTTPError as exc:
        raise _ResearchAPIError(f"HTTP {exc.code}", status=exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise _ResearchAPIError(str(exc)) from exc


def _research_http_get_json(url: str, params: dict | None = None) -> dict:
    """Fetch and decode a JSON response from a research API."""
    try:
        return json.loads(_research_http_get(url, params).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ResearchAPIError("the service returned invalid JSON") from exc


def _openalex_abstract(inverted_index: Any) -> str | None:
    """Reconstruct OpenAlex's inverted-index abstract representation."""
    if not isinstance(inverted_index, dict) or not inverted_index:
        return None
    positioned_words = []
    for word, positions in inverted_index.items():
        if isinstance(positions, list):
            positioned_words.extend((position, word) for position in positions)
    return " ".join(word for _, word in sorted(positioned_words)) or None


def _snippet(text: str | None, length: int = 500) -> str | None:
    """Return a compact single-line text preview."""
    if not text:
        return None
    compact = " ".join(text.split())
    return compact if len(compact) <= length else f"{compact[: length - 1].rstrip()}…"


def _openalex_authors(work: dict) -> list[str]:
    """Extract display names from an OpenAlex work."""
    return [
        author.get("author", {}).get("display_name")
        for author in work.get("authorships", [])
        if author.get("author", {}).get("display_name")
    ]


# All tools on this server are read-only data lookups. Use this annotations
# pattern for every tool added here so clients classify them correctly.
@mcp.tool(
    title="Get Stock Quote",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_stock_quote(ticker: str = "") -> dict:
    """Return the latest public Yahoo Finance quote for a ticker symbol."""
    symbol = _normalize_text(ticker).upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        stock = yf.Ticker(symbol)
        history = stock.history(period="5d")

        # Yahoo returns an empty history for unknown or delisted symbols.
        if history.empty:
            return {"error": f"Invalid ticker '{symbol}': no market data was found."}

        latest = history.iloc[-1]
        fast_info = stock.fast_info

        current_price = _json_value(latest["Close"])
        previous_close = fast_info.get("regularMarketPreviousClose")
        if previous_close is None:
            previous_close = fast_info.get("previousClose")
        if previous_close is None and len(history) > 1:
            previous_close = history.iloc[-2]["Close"]

        market_cap = fast_info.get("marketCap")
        if market_cap is None:
            shares = fast_info.get("shares")
            if shares is not None and current_price is not None:
                market_cap = shares * current_price

        # Use the latest trading row for intraday fields and fast_info for
        # quote metadata that is not included in the history table.
        return {
            "ticker": symbol,
            "current_price": current_price,
            "previous_close": _json_value(previous_close),
            "day_high": _json_value(latest["High"]),
            "day_low": _json_value(latest["Low"]),
            "volume": _json_value(latest["Volume"]),
            "market_cap": _json_value(market_cap),
        }
    except Exception as exc:
        # Keep network errors and malformed Yahoo responses from crashing MCP.
        return {"error": f"Could not fetch data for ticker '{symbol}': {exc}"}


@mcp.tool(
    title="Get Historical Prices",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_historical_prices(
    ticker: str = "", period: str = "1mo", interval: str = "1d"
) -> dict:
    """Return historical OHLCV prices from Yahoo Finance.

    Results are limited to the most recent 250 rows, so older data may be
    truncated when the requested range and interval would exceed that limit.
    """
    valid_periods = {
        "1d",
        "5d",
        "1mo",
        "3mo",
        "6mo",
        "1y",
        "2y",
        "5y",
        "10y",
        "ytd",
        "max",
    }
    valid_intervals = {"1d", "1wk", "1mo"}

    symbol = _normalize_text(ticker).upper()
    selected_period = _normalize_text(period).lower()
    selected_interval = _normalize_text(interval).lower()

    if not symbol:
        return {"error": "Please provide a ticker symbol."}
    if selected_period not in valid_periods:
        return {
            "error": (
                f"Invalid period '{period}'. Valid periods are: "
                f"{', '.join(sorted(valid_periods))}."
            )
        }
    if selected_interval not in valid_intervals:
        return {
            "error": (
                f"Invalid interval '{interval}'. Valid intervals are: "
                f"{', '.join(sorted(valid_intervals))}."
            )
        }

    try:
        history = yf.Ticker(symbol).history(
            period=selected_period, interval=selected_interval
        )
        if history.empty:
            return {"error": f"Invalid ticker '{symbol}': no market data was found."}

        prices = []
        for timestamp, row in history.tail(250).iterrows():
            prices.append(
                {
                    "date": timestamp.date().isoformat(),
                    "open": _json_value(row["Open"]),
                    "high": _json_value(row["High"]),
                    "low": _json_value(row["Low"]),
                    "close": _json_value(row["Close"]),
                    "volume": _json_value(row["Volume"]),
                }
            )

        return {
            "ticker": symbol,
            "period": selected_period,
            "interval": selected_interval,
            "prices": prices,
        }
    except Exception as exc:
        return {
            "error": f"Could not fetch historical data for ticker '{symbol}': {exc}"
        }


@mcp.tool(
    title="Get Financial Statements",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_financial_statements(
    ticker: str = "", statement: str = "income", period: str = "annual"
) -> dict:
    """Return financial statement periods and line items from Yahoo Finance.

    Results are limited to the 8 most recent reporting periods, so older
    statement periods may be truncated.
    """
    statement_attributes = {
        ("income", "annual"): "income_stmt",
        ("income", "quarterly"): "quarterly_income_stmt",
        ("balance", "annual"): "balance_sheet",
        ("balance", "quarterly"): "quarterly_balance_sheet",
        ("cashflow", "annual"): "cashflow",
        ("cashflow", "quarterly"): "quarterly_cashflow",
    }
    valid_statements = {"income", "balance", "cashflow"}
    valid_periods = {"annual", "quarterly"}

    symbol = _normalize_text(ticker).upper()
    selected_statement = _normalize_text(statement).lower()
    selected_period = _normalize_text(period).lower()

    if not symbol:
        return {"error": "Please provide a ticker symbol."}
    if selected_statement not in valid_statements:
        return {
            "error": (
                f"Invalid statement '{statement}'. Valid statements are: "
                f"{', '.join(sorted(valid_statements))}."
            )
        }
    if selected_period not in valid_periods:
        return {
            "error": (
                f"Invalid period '{period}'. Valid periods are: "
                f"{', '.join(sorted(valid_periods))}."
            )
        }

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        attribute = statement_attributes[(selected_statement, selected_period)]
        statement_data = getattr(stock, attribute)
        data = []
        if statement_data is not None and not statement_data.empty:
            periods = statement_data.T.sort_index(ascending=False).head(8)
            for period_ending, row in periods.iterrows():
                record = {"period_ending": period_ending.date().isoformat()}
                record.update(
                    {str(line_item): _json_value(value) for line_item, value in row.items()}
                )
                data.append(record)

        return {
            "ticker": symbol,
            "statement": selected_statement,
            "period": selected_period,
            "data": data,
        }
    except Exception as exc:
        return {
            "error": f"Could not fetch financial statements for ticker '{symbol}': {exc}"
        }


@mcp.tool(
    title="Get Corporate Actions",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_corporate_actions(ticker: str = "", action_type: str = "all") -> dict:
    """Return dividends and stock splits from Yahoo Finance.

    Each requested action type is limited to its 100 most recent entries, so
    older corporate actions may be truncated.
    """
    valid_action_types = {"all", "dividends", "splits"}
    symbol = _normalize_text(ticker).upper()
    selected_action_type = _normalize_text(action_type).lower()

    if not symbol:
        return {"error": "Please provide a ticker symbol."}
    if selected_action_type not in valid_action_types:
        return {
            "error": (
                f"Invalid action_type '{action_type}'. Valid action types are: "
                f"{', '.join(sorted(valid_action_types))}."
            )
        }

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        dividends = None
        splits = None

        if selected_action_type in {"all", "dividends"}:
            dividend_series = stock.dividends
            dividends = []
            if dividend_series is not None and not dividend_series.empty:
                dividends = [
                    {
                        "date": timestamp.date().isoformat(),
                        "amount": _json_value(amount),
                    }
                    for timestamp, amount in dividend_series.sort_index(ascending=False)
                    .head(100)
                    .items()
                ]

        if selected_action_type in {"all", "splits"}:
            split_series = stock.splits
            splits = []
            if split_series is not None and not split_series.empty:
                splits = [
                    {"date": timestamp.date().isoformat(), "ratio": _json_value(ratio)}
                    for timestamp, ratio in split_series.sort_index(ascending=False)
                    .head(100)
                    .items()
                ]

        return {"ticker": symbol, "dividends": dividends, "splits": splits}
    except Exception as exc:
        return {
            "error": f"Could not fetch corporate actions for ticker '{symbol}': {exc}"
        }


@mcp.tool(
    title="Get Holders",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_holders(ticker: str = "", holder_type: str = "major") -> dict:
    """Return holder or insider-transaction data from Yahoo Finance.

    Institutional holders and insider transactions are limited to the top 20
    rows, so additional rows may be truncated.
    """
    holder_attributes = {
        "major": "major_holders",
        "institutional": "institutional_holders",
        "insider_transactions": "insider_transactions",
    }
    symbol = _normalize_text(ticker).upper()
    selected_holder_type = _normalize_text(holder_type).lower()

    if not symbol:
        return {"error": "Please provide a ticker symbol."}
    if selected_holder_type not in holder_attributes:
        return {
            "error": (
                f"Invalid holder_type '{holder_type}'. Valid holder types are: "
                f"{', '.join(sorted(holder_attributes))}."
            )
        }

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        holder_data = getattr(stock, holder_attributes[selected_holder_type])
        data = []
        if holder_data is not None and not holder_data.empty:
            if selected_holder_type in {"institutional", "insider_transactions"}:
                holder_data = holder_data.head(20)
            for row_index, row in holder_data.iterrows():
                record = {
                    str(column): _json_value(value) for column, value in row.items()
                }
                # Current yfinance versions store major-holder categories in
                # the dataframe index rather than in a regular column.
                if selected_holder_type == "major":
                    record = {"Breakdown": str(row_index), **record}
                data.append(record)

        return {"ticker": symbol, "holder_type": selected_holder_type, "data": data}
    except Exception as exc:
        return {"error": f"Could not fetch holder data for ticker '{symbol}': {exc}"}


@mcp.tool(
    title="Get Sector Data",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_sector_data(key: str = "", level: str = "sector") -> dict:
    """Return a Yahoo Finance sector or industry overview and top companies.

    Valid sector key examples include ``technology``, ``financial-services``,
    and ``healthcare``. Valid industry examples include ``semiconductors``,
    ``computer-hardware``, and ``solar``. Companies are limited to the top 15,
    so additional companies may be truncated.
    """
    valid_levels = {"sector", "industry"}
    sector_keys = set(SECTOR_INDUSTY_MAPPING_LC)
    industry_keys = {
        industry
        for industries in SECTOR_INDUSTY_MAPPING_LC.values()
        for industry in industries
    }
    selected_key = _normalize_text(key).lower()
    selected_level = _normalize_text(level).lower()

    if not selected_key:
        return {"error": "Please provide a sector or industry key."}
    if selected_level not in valid_levels:
        return {
            "error": (
                f"Invalid level '{level}'. Valid levels are: "
                f"{', '.join(sorted(valid_levels))}."
            )
        }

    valid_keys = sector_keys if selected_level == "sector" else industry_keys
    if selected_key not in valid_keys:
        examples = (
            "technology, financial-services, healthcare"
            if selected_level == "sector"
            else "semiconductors, computer-hardware, solar"
        )
        return {
            "error": (
                f"Invalid {selected_level} key '{key}'. Examples of valid keys are: "
                f"{examples}."
            )
        }

    try:
        domain = (
            yf.Sector(selected_key)
            if selected_level == "sector"
            else yf.Industry(selected_key)
        )
        overview = {
            "name": domain.name,
            **{str(field): _json_value(value) for field, value in domain.overview.items()},
        }
        companies = []
        top_companies = domain.top_companies
        if top_companies is not None and not top_companies.empty:
            for ticker_symbol, row in top_companies.head(15).iterrows():
                companies.append(
                    {
                        "ticker": str(ticker_symbol),
                        "name": _json_value(row.get("name")),
                        "market_weight": _json_value(row.get("market weight")),
                    }
                )

        return {
            "key": selected_key,
            "level": selected_level,
            "overview": overview,
            "companies": companies,
        }
    except Exception as exc:
        return {
            "error": f"Could not fetch {selected_level} data for key '{selected_key}': {exc}"
        }


@mcp.tool(
    title="Get Stock Screener",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_stock_screener(screen_name: str = "day_gainers") -> dict:
    """Return results from a predefined Yahoo Finance stock screener.

    Results are limited to the top 25 entries, so additional matches may be
    truncated.
    """
    selected_screen = _normalize_text(screen_name).lower()
    valid_screens = set(yf.PREDEFINED_SCREENER_QUERIES)
    if selected_screen not in valid_screens:
        return {
            "error": (
                f"Invalid screen_name '{screen_name}'. Valid screens are: "
                f"{', '.join(sorted(valid_screens))}."
            )
        }

    try:
        response = yf.screen(selected_screen, count=25)
        results = []
        for quote in response.get("quotes", [])[:25]:
            results.append(
                {
                    "ticker": quote.get("symbol"),
                    "name": quote.get("longName") or quote.get("shortName"),
                    "price": _json_value(quote.get("regularMarketPrice")),
                    "change": _json_value(quote.get("regularMarketChange")),
                    "percent_change": _json_value(
                        quote.get("regularMarketChangePercent")
                    ),
                    "volume": _json_value(quote.get("regularMarketVolume")),
                    "market_cap": _json_value(quote.get("marketCap")),
                }
            )
        return {"screen_name": selected_screen, "results": results}
    except Exception as exc:
        return {"error": f"Could not run stock screener '{selected_screen}': {exc}"}


@mcp.tool(
    title="Get News",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_news(ticker: str = "") -> dict:
    """Return the 10 most recent Yahoo Finance news items for a ticker.

    Results are limited to 10 items, so older news may be truncated.
    """
    symbol = _normalize_text(ticker).upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        items = []
        for raw_item in (stock.news or [])[:10]:
            content = raw_item.get("content") or raw_item
            provider = content.get("provider") or {}
            link_data = content.get("canonicalUrl") or content.get("clickThroughUrl")
            link = link_data.get("url") if isinstance(link_data, dict) else link_data
            items.append(
                {
                    "title": content.get("title"),
                    "publisher": provider.get("displayName")
                    or content.get("publisher"),
                    "link": link or content.get("link"),
                    "published_time": _iso_datetime(
                        content.get("pubDate") or content.get("providerPublishTime")
                    ),
                }
            )
        return {"ticker": symbol, "news": items}
    except Exception as exc:
        return {"error": f"Could not fetch news for ticker '{symbol}': {exc}"}


@mcp.tool(
    title="Get Sustainability",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_sustainability(ticker: str = "") -> dict:
    """Return available Yahoo Finance ESG and sustainability scores."""
    symbol = _normalize_text(ticker).upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        stock = yf.Ticker(symbol)
        if ticker_error := _ticker_validation_error(stock, symbol):
            return ticker_error
        sustainability = stock.sustainability
        if sustainability is None or sustainability.empty:
            return {
                "ticker": symbol,
                "message": f"No sustainability data is available for ticker '{symbol}'.",
                "data": {},
            }

        scores = {}
        if len(sustainability.columns) == 1:
            value_column = sustainability.columns[0]
            scores = {
                str(score): _json_value(value)
                for score, value in sustainability[value_column].items()
            }
        else:
            for score, row in sustainability.iterrows():
                for column, value in row.items():
                    scores[f"{score} ({column})"] = _json_value(value)

        return {"ticker": symbol, "data": scores}
    except Exception as exc:
        return {
            "error": f"Could not fetch sustainability data for ticker '{symbol}': {exc}"
        }


@mcp.tool(
    title="Search Finance Research",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def search_finance_research(
    query: str = "", year_from: int = None, limit: int = 10
) -> dict:
    """Search OpenAlex for business, economics, finance, and accounting works.

    Results are limited to at most 50 works, so additional matches may be
    truncated. Replace ``OPENALEX_MAILTO`` with a deployment contact email.
    """
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return {"error": "Please provide a research search query."}
    if year_from is not None and (
        isinstance(year_from, bool)
        or not isinstance(year_from, int)
        or year_from < 1000
        or year_from > datetime.now().year
    ):
        return {
            "error": (
                f"Invalid year_from '{year_from}'. Provide a year from 1000 through "
                f"{datetime.now().year}."
            )
        }
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return {"error": "Invalid limit. Provide an integer from 1 through 50."}

    filters = ["topics.field.id:14|20"]
    if year_from is not None:
        filters.append(f"from_publication_date:{year_from}-01-01")
    try:
        payload = _research_http_get_json(
            "https://api.openalex.org/works",
            {
                "search": normalized_query,
                "filter": ",".join(filters),
                "per-page": limit,
                "mailto": OPENALEX_MAILTO,
            },
        )
        works = payload.get("results") or []
        if not works:
            return {"error": f"No finance research found for query '{normalized_query}'."}

        results = []
        for work in works[:limit]:
            location = work.get("primary_location") or {}
            source = location.get("source") or {}
            oa_location = work.get("best_oa_location") or {}
            abstract = _openalex_abstract(work.get("abstract_inverted_index"))
            results.append(
                {
                    "title": work.get("title") or work.get("display_name"),
                    "authors": _openalex_authors(work),
                    "year": work.get("publication_year"),
                    "journal": source.get("display_name"),
                    "doi": work.get("doi"),
                    "open_access_pdf_url": oa_location.get("pdf_url")
                    or location.get("pdf_url"),
                    "abstract_snippet": _snippet(abstract),
                    "cited_by_count": work.get("cited_by_count", 0),
                }
            )
        return {"query": normalized_query, "results": results}
    except Exception as exc:
        return {"error": f"Could not search OpenAlex: {exc}"}


@mcp.tool(
    title="Get Research Paper",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def get_research_paper(doi_or_openalex_id: str = "") -> dict:
    """Return full OpenAlex details for one DOI or OpenAlex work ID."""
    identifier = _normalize_text(doi_or_openalex_id)
    if not identifier:
        return {"error": "Please provide a DOI or OpenAlex work ID."}

    if identifier.lower().startswith("doi:"):
        identifier = identifier[4:].strip()
    if identifier.lower().startswith("https://doi.org/"):
        identifier = f"https://doi.org/{identifier.split('/', 3)[-1]}"
    elif re.fullmatch(r"10\.\d{4,9}/\S+", identifier, flags=re.IGNORECASE):
        identifier = f"https://doi.org/{identifier}"
    elif identifier.lower().startswith("https://openalex.org/"):
        identifier = identifier.rstrip("/").rsplit("/", 1)[-1]
    elif not re.fullmatch(r"W\d+", identifier, flags=re.IGNORECASE):
        return {
            "error": (
                f"Invalid paper identifier '{doi_or_openalex_id}'. Provide a DOI or "
                "an OpenAlex work ID such as W2741809807."
            )
        }

    try:
        work = _research_http_get_json(
            f"https://api.openalex.org/works/{quote(identifier, safe=':/')}",
            {"mailto": OPENALEX_MAILTO},
        )
        topics = []
        for topic in work.get("topics") or []:
            topics.append(
                {
                    "name": topic.get("display_name"),
                    "score": topic.get("score"),
                    "subfield": (topic.get("subfield") or {}).get("display_name"),
                    "field": (topic.get("field") or {}).get("display_name"),
                    "domain": (topic.get("domain") or {}).get("display_name"),
                }
            )
        concepts = [
            {"name": concept.get("display_name"), "score": concept.get("score")}
            for concept in (work.get("concepts") or [])
        ]
        location = work.get("primary_location") or {}
        source = location.get("source") or {}
        return {
            "openalex_id": work.get("id"),
            "doi": work.get("doi"),
            "title": work.get("title") or work.get("display_name"),
            "authors": _openalex_authors(work),
            "year": work.get("publication_year"),
            "journal": source.get("display_name"),
            "abstract": _openalex_abstract(work.get("abstract_inverted_index")),
            "topics": topics,
            "concepts": concepts,
            "cited_by_count": work.get("cited_by_count", 0),
            "referenced_works_count": len(work.get("referenced_works") or []),
        }
    except _ResearchAPIError as exc:
        if exc.status == 404:
            return {"error": f"No OpenAlex work found for '{doi_or_openalex_id}'."}
        return {"error": f"Could not fetch the OpenAlex work: {exc}"}
    except Exception as exc:
        return {"error": f"Could not fetch the OpenAlex work: {exc}"}


@mcp.tool(
    title="Search Finance Preprints",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def search_finance_preprints(query: str = "", limit: int = 10) -> dict:
    """Search arXiv quantitative-finance and economics preprints.

    Results are limited to at most 50 preprints, so additional matches may be
    truncated.
    """
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return {"error": "Please provide a preprint search query."}
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        return {"error": "Invalid limit. Provide an integer from 1 through 50."}

    try:
        xml_payload = _research_http_get(
            "https://export.arxiv.org/api/query",
            {
                "search_query": (
                    f'(all:"{normalized_query}") AND (cat:q-fin.* OR cat:econ.*)'
                ),
                "start": 0,
                "max_results": limit,
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        root = ElementTree.fromstring(xml_payload)
        atom = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", atom)
        if not entries:
            return {"error": f"No finance preprints found for query '{normalized_query}'."}

        results = []
        for entry in entries[:limit]:
            entry_id = entry.findtext("atom:id", default="", namespaces=atom)
            pdf_link = None
            for link in entry.findall("atom:link", atom):
                if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                    pdf_link = link.get("href")
                    break
            results.append(
                {
                    "title": " ".join(
                        entry.findtext("atom:title", default="", namespaces=atom).split()
                    ),
                    "authors": [
                        author.findtext("atom:name", default="", namespaces=atom)
                        for author in entry.findall("atom:author", atom)
                    ],
                    "submission_date": entry.findtext(
                        "atom:published", default=None, namespaces=atom
                    ),
                    "arxiv_id": entry_id.rstrip("/").rsplit("/", 1)[-1],
                    "abstract_snippet": _snippet(
                        entry.findtext("atom:summary", default=None, namespaces=atom)
                    ),
                    "pdf_link": pdf_link,
                    "review_status": "preprint - not peer reviewed",
                }
            )
        return {"query": normalized_query, "results": results}
    except ElementTree.ParseError:
        return {"error": "Could not search arXiv: the service returned invalid XML."}
    except Exception as exc:
        return {"error": f"Could not search arXiv: {exc}"}


@mcp.tool(
    title="Check Journal Legitimacy",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
)
def check_journal_legitimacy(journal_name_or_issn: str = "") -> dict:
    """Check whether a journal is listed in DOAJ's vetted directory."""
    search_value = _normalize_text(journal_name_or_issn)
    if not search_value:
        return {"error": "Please provide a journal name or ISSN."}

    compact_issn = search_value.replace("-", "")
    is_issn = bool(re.fullmatch(r"\d{7}[\dXx]", compact_issn))
    query_field = "index.issn.exact" if is_issn else "bibjson.title"
    doaj_query = f'{query_field}:"{search_value.replace(chr(34), "")}"'
    try:
        payload = _research_http_get_json(
            f"https://doaj.org/api/search/journals/{quote(doaj_query, safe='')}",
            {"pageSize": 1},
        )
        results = payload.get("results") or []
        if not results:
            return {"query": search_value, "found": False}

        journal = results[0].get("bibjson") or {}
        issns = [
            identifier.get("id")
            for identifier in (journal.get("identifier") or [])
            if identifier.get("type") in {"pissn", "eissn"} and identifier.get("id")
        ]
        for issn_field in ("pissn", "eissn"):
            if journal.get(issn_field) and journal[issn_field] not in issns:
                issns.append(journal[issn_field])
        subjects = []
        for subject in journal.get("subject") or []:
            value = subject.get("term") or subject.get("code")
            if value and value not in subjects:
                subjects.append(value)
        return {
            "query": search_value,
            "found": True,
            "title": journal.get("title"),
            "issn": issns,
            "subject_areas": subjects,
        }
    except Exception as exc:
        return {"error": f"Could not check the DOAJ directory: {exc}"}


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
