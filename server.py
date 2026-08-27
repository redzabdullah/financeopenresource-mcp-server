"""A small MCP server that exposes public Yahoo Finance market data."""

import os
from datetime import date, datetime, timezone
from math import isnan
from typing import Any

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
def get_stock_quote(ticker: str) -> dict:
    """Return the latest public Yahoo Finance quote for a ticker symbol."""
    symbol = ticker.strip().upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        stock = yf.Ticker(symbol)
        history = stock.history(period="5d")

        # Yahoo returns an empty history for unknown or delisted symbols.
        if history.empty:
            return {"error": f"No market data found for ticker '{symbol}'."}

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
    ticker: str, period: str = "1mo", interval: str = "1d"
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

    symbol = ticker.strip().upper()
    selected_period = period.strip().lower()
    selected_interval = interval.strip().lower()

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
            return {
                "error": (
                    f"No historical data found for ticker '{symbol}' with "
                    f"period '{selected_period}' and interval "
                    f"'{selected_interval}'."
                )
            }

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
    ticker: str, statement: str = "income", period: str = "annual"
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

    symbol = ticker.strip().upper()
    selected_statement = statement.strip().lower()
    selected_period = period.strip().lower()

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
def get_corporate_actions(ticker: str, action_type: str = "all") -> dict:
    """Return dividends and stock splits from Yahoo Finance.

    Each requested action type is limited to its 100 most recent entries, so
    older corporate actions may be truncated.
    """
    valid_action_types = {"all", "dividends", "splits"}
    symbol = ticker.strip().upper()
    selected_action_type = action_type.strip().lower()

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
        dividends = None
        splits = None

        if selected_action_type in {"all", "dividends"}:
            dividend_series = stock.dividends
            dividends = [
                {"date": timestamp.date().isoformat(), "amount": _json_value(amount)}
                for timestamp, amount in dividend_series.sort_index(ascending=False)
                .head(100)
                .items()
            ]

        if selected_action_type in {"all", "splits"}:
            split_series = stock.splits
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
def get_holders(ticker: str, holder_type: str = "major") -> dict:
    """Return holder or insider-transaction data from Yahoo Finance.

    Institutional holders and insider transactions are limited to the top 20
    rows, so additional rows may be truncated.
    """
    holder_attributes = {
        "major": "major_holders",
        "institutional": "institutional_holders",
        "insider_transactions": "insider_transactions",
    }
    symbol = ticker.strip().upper()
    selected_holder_type = holder_type.strip().lower()

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
        holder_data = getattr(yf.Ticker(symbol), holder_attributes[selected_holder_type])
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
def get_sector_data(key: str, level: str = "sector") -> dict:
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
    selected_key = key.strip().lower()
    selected_level = level.strip().lower()

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
    selected_screen = screen_name.strip().lower()
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
def get_news(ticker: str) -> dict:
    """Return the 10 most recent Yahoo Finance news items for a ticker.

    Results are limited to 10 items, so older news may be truncated.
    """
    symbol = ticker.strip().upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        items = []
        for raw_item in (yf.Ticker(symbol).news or [])[:10]:
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
def get_sustainability(ticker: str) -> dict:
    """Return available Yahoo Finance ESG and sustainability scores."""
    symbol = ticker.strip().upper()
    if not symbol:
        return {"error": "Please provide a ticker symbol."}

    try:
        sustainability = yf.Ticker(symbol).sustainability
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


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
