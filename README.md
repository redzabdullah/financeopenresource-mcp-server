# Finance Data MCP Server

A minimal remote Model Context Protocol (MCP) server that reads public stock
quote data from Yahoo Finance. It uses Streamable HTTP transport and currently
provides one tool: `get_stock_quote`.

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
