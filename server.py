"""A small MCP server that exposes public Yahoo Finance market data."""

import os
from typing import Any

import yfinance as yf
from mcp.server.mcpserver import MCPServer, Context
from mcp.types import ToolAnnotations


mcp = MCPServer("finance-data-server")


def _json_value(value: Any) -> Any:
    """Convert pandas/numpy scalar values into JSON-friendly Python values."""
    return value.item() if hasattr(value, "item") else value


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


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
    )
