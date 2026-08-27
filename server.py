"""A small MCP server that exposes public Yahoo Finance market data."""

import os
from datetime import date, datetime
from math import isnan
from typing import Any

import yfinance as yf
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


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
