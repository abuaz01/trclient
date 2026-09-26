# trclient: unofficial Trade Republic API and MCP server

> [!CAUTION]
> **This project is NOT affiliated with, endorsed by, or connected to Trade Republic Bank GmbH in any way.**
> "Trade Republic" is a trademark of its owner and is used here only to describe what this software talks to.
>
> - Trade Republic offers **no public API**. This project uses the private, reverse-engineered
>   interface of their web app. It can break at any time without notice.
> - Using it **may violate Trade Republic's terms of service**. Your account could be restricted or closed.
> - This software can place **real orders with real money**. Bugs, API changes, network problems
>   or a misbehaving AI agent can cause **financial losses**.
> - Nothing here is **financial or investment advice**.
> - The software is provided **"as is", without any warranty**. The authors and contributors accept
>   **no liability** for losses, damages, account restrictions or any other consequences.
> - **You use it entirely at your own risk and responsibility.**

---

`trclient` is a Python client for your own Trade Republic account and a local **MCP server**
that exposes it to AI agents such as Claude. It logs in the same way
[app.traderepublic.com](https://app.traderepublic.com) does, reads account and market data,
downloads your documents, and places and cancels orders. Everything runs on your machine.

It provides the API and the MCP server only. It contains no trading strategy and no analysis.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Login and session security](#login-and-session-security)
- [Configuration](#configuration)
- [Command line](#command-line)
- [Documents (postbox, statements)](#documents-postbox-statements)
- [MCP server](#mcp-server)
- [Python API reference](#python-api-reference)
- [Guardrails, fees and budget](#guardrails-fees-and-budget)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Credits](#credits)
- [License](#license)

## Features

| Area | What you get |
| --- | --- |
| **Login** | Web login v2 (approve in the app, or authenticator code) and the v1 code flow. The PIN is **never stored**. |
| **Session** | Cookies in the OS keychain (macOS Keychain, Windows Credential Locker, Secret Service), auto-refresh, clean logout |
| **Account** | Portfolio, cash, open/terminated orders, transactions, activity timeline, sellable size |
| **Documents** | Account statements, trade confirmations, cost information, tax documents and postbox items as PDF |
| **Market data** | Quotes, live ticker stream, price history, instrument details, search, Trade Republic news |
| **Trading** | Market, limit and stop-market orders (buy/sell, fractional), `gfd`/`gtd`/`gtc`, cancel. Stocks/ETFs on LSX, crypto on BHS (venue chosen automatically) |
| **Safety** | Dry run by default, preview-then-confirm, value/fee/budget/daily limits, price-typo guard, append-only audit log |
| **MCP** | Local stdio MCP server with 22 tools for Claude Code, Claude Desktop or any MCP client |

## How it works

```
 Claude Code / Claude Desktop / any MCP client
            │  stdio (local only)
            ▼
   trclient-mcp  ──  guardrails + audit log (~/.local/state/trclient/audit.jsonl)
            │
   trclient (Python) ── session cookies from the OS keychain
            │  HTTPS + WebSocket
            ▼
   api.traderepublic.com
```

- **Login** uses the same v2 endpoints as the web app (`/api/v2/auth/web/login`, with the
  `X-TR-Device-Info` / `X-TR-App-Version` / `X-Tr-Platform` headers). No AWS WAF token is needed.
- **Data and orders** go over the web app's WebSocket (`connect 31`, cookie-authenticated).
  Each request unsubscribes when done, so stale updates can never be mistaken for an answer.
- **Orders** use the `simpleCreateOrder` payload that others confirmed on real accounts:
  numbers as JSON numbers, a UUID `clientProcessId`, and `warningsShown` plus `acceptedWarnings`.
- **Documents** are attached to timeline events (`timelineTransactions`, `timelineActivityLog`)
  and listed in the `documents` section of `timelineDetailV2`.

## Installation

Requirements: Python ≥ 3.11 and a Trade Republic account. For the MCP part you also need an MCP client,
e.g. [Claude Code](https://claude.com/claude-code) or Claude Desktop.

```bash
git clone https://github.com/abuaz01/trclient.git
cd trclient
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                             # offline tests, no account needed
```

## Login and session security

```bash
.venv/bin/trclient login     # phone number once, then PIN (hidden prompt)
.venv/bin/trclient status
.venv/bin/trclient logout    # ends the session at Trade Republic and deletes it locally
```

- **Second factor:** Trade Republic decides how you confirm the login. You **approve it in the
  Trade Republic app** (the client waits). Accounts with an **authenticator app** first enter the
  6-digit code and then still approve the login in the app, exactly like the web app.
  `--code-flow` uses the older v1 flow with a code sent by Trade Republic. It only works with an
  `aws-waf-token` taken from a real browser session (`--waf-token`).
- **PIN / password:** it is read with `getpass`, used for one request and never written anywhere.
  It never passes through the MCP server or an AI agent.
- **Session:** the cookies (`tr_session`, `tr_refresh`, ...) and a random, stable device id are
  stored in the **OS keychain**. On headless Linux without a keyring you can opt in explicitly
  with `TR_SESSION_STORE=file`. That writes a `0600` file and refuses to load it if others can read it.
- The session is refreshed automatically while a client runs. When Trade Republic no longer accepts it,
  the stored cookies are wiped and you log in again.
- **Logs are redacted:** cookie values, PIN and codes never appear, even with `-v`.

## Configuration

All settings are environment variables. None of them are secrets.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TR_TRADING_ENABLED` | off | Master switch. Without `1`, every order is a dry run. |
| `TR_BUDGET_EUR` | unset | Money an agent may commit in total (see below) |
| `TR_BUDGET_SINCE` | all time | Start date of the budget ledger (`YYYY-MM-DD`). Move it to reset. |
| `TR_ORDER_FEE_EUR` | `1.0` | Flat fee per order (Trade Republic: 1 € external cost per buy and per sell) |
| `TR_MAX_FEE_PCT` | `1.0` (off) | Refuse buys where the fee is a larger share of the order than this, e.g. `0.03` |
| `TR_MAX_ORDER_EUR` | `500` | Maximum estimated value of one buy |
| `TR_MAX_ORDERS_PER_DAY` | `10` | Counted from the audit log |
| `TR_MAX_PRICE_DEVIATION` | `0.20` | A limit/stop price more than 20 % from the quote is treated as a typo |
| `TR_ALLOWED_ISINS` | all | Comma-separated allow list |
| `TR_AUDIT_PATH` | `~/.local/state/trclient/audit.jsonl` | Append-only audit log (`0600`) |
| `TR_DOCUMENTS_DIR` | `~/trclient-documents` | Target folder for downloaded documents |
| `TR_PHONE` | last login | Phone number in international format |
| `TR_SESSION_STORE` | `keyring` | `keyring` or `file` |
| `TR_LOCALE` | `de` | Language of Trade Republic texts (news, descriptions, document titles) |
| `TR_MCP_PREVIEW_TTL` | `120` | Seconds a `preview_order` confirmation stays valid |
| `TR_APP_VERSION` | built in | Override if login answers `426 CLIENT_VERSION_OUTDATED` |
| `TR_WS_PROTOCOL_VERSION` | `31` | Override if the WebSocket answers `failed <n>` |
| `TR_USER_AGENT` | Chrome UA | Override if the login looks bot-filtered |
| `TR_TRACE_PATH` | off | If set, the MCP server logs every tool call with a short summary to this file (`0600`) |

## Command line

```bash
trclient portfolio | cash | orders [--terminated] | transactions [--after CURSOR]
trclient quote DE0007164600 [--side sell]          # no login needed
trclient history DE0007164600 --range 1y           # no login needed
trclient instrument DE0007164600
trclient search "SAP" [--type stock|fund|derivative|crypto|bond]
trclient budget                                     # budget ledger (TR_BUDGET_EUR)
trclient documents [--days 90] [--list]             # see below

trclient buy  DE0007164600 --size 0.25 --limit 180            # dry run: shows the payload
TR_TRADING_ENABLED=1 trclient buy DE0007164600 --size 0.25 --limit 180 --execute
trclient sell DE0007164600 --size 0.25 --sell-fractions --execute
trclient cancel <orderId> --execute
```

With `--execute` you still have to type `BUY` / `SELL` / `CANCEL` to confirm, unless you add `--yes`.
Run `trclient <command> --help` for all options.

## Documents (postbox, statements)

Trade Republic attaches PDFs to timeline events:

- **postbox** (activity timeline): account statements (Kontoauszug), tax reports, notices, contract documents
- **transactions**: trade confirmations (Abrechnung), cost information (Kosteninformation), dividend and interest statements

```bash
trclient documents --list                           # last 90 days, list only
trclient documents                                  # download the last 90 days
trclient documents --since 2025-01-01               # everything since a date
trclient documents --source postbox --match Kontoauszug --days 365
trclient documents --out ~/Documents/TradeRepublic  # other folder (or TR_DOCUMENTS_DIR)
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--days N` | `90` | Events of the last N days (ignored with `--since`) |
| `--since YYYY-MM-DD` | – | Events on or after this date |
| `--source` | `all` | `all`, `postbox` or `transactions` |
| `--match TEXT` | – | Only documents whose title or event title contains TEXT (case-insensitive) |
| `--out DIR` | `TR_DOCUMENTS_DIR` | Target folder (created with `0700`, files `0600`) |
| `--list` | – | Print the documents as JSON, download nothing |
| `--overwrite` | – | Replace files that already exist (otherwise they are skipped) |

File names: `YYYY-MM-DD - <document title> - <event title> - <event subtitle> [<id>].pdf`.
Repeated runs only fetch new documents. Titles follow `TR_LOCALE`.

Each event is opened with one request, so long periods take a while. Download links are
short-lived pre-signed URLs; they are fetched without your session cookies and are never printed.

## MCP server

`trclient-mcp` is a local **stdio** MCP server. It does not open a network port. Only the client
that starts it can talk to it. It uses the session from your keychain, so run `trclient login`
first. The server never asks for the PIN.

### Claude Code

```bash
claude mcp add trade-republic --scope user \
  -e TR_TRADING_ENABLED=1 -e TR_MAX_ORDER_EUR=50 \
  -- /ABSOLUTE/PATH/trclient/.venv/bin/trclient-mcp

claude mcp list           # should show trade-republic ... ✓ Connected
```

Then start `claude` and type `/mcp` to see the tools. Claude Code asks for permission before each
tool call unless you allow it. Leave out `TR_TRADING_ENABLED=1` for read-only use.

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "trade-republic": {
      "command": "/ABSOLUTE/PATH/trclient/.venv/bin/trclient-mcp",
      "env": { "TR_TRADING_ENABLED": "1", "TR_MAX_ORDER_EUR": "50" }
    }
  }
}
```

### Tools

| Tool | Login | Parameters | What it does |
| --- | :-: | --- | --- |
| `get_account_status` | ✓ | – | Login state, account number, guardrails, fee, budget |
| `get_portfolio` | ✓ | – | Positions |
| `get_cash` | ✓ | – | Cash balance |
| `get_orders` | ✓ | `include_terminated` | Open (and optionally terminated) orders |
| `get_transactions` | ✓ | `after` | Transaction timeline, paged with the `after` cursor |
| `get_available_size` | ✓ | `isin`, `exchange` | Sellable units of an instrument |
| `list_documents` | ✓ | `since_days` (90), `source`, `title_contains` | Documents without download links |
| `download_documents` | ✓ | `document_ids`, `since_days`, `source`, `title_contains` | Saves PDFs to `TR_DOCUMENTS_DIR`; all matching documents when `document_ids` is omitted |
| `get_quote` | – | `isin`, `side`, `exchange` | Order price (ask/bid) |
| `get_instrument` | – | `isin` | Master data |
| `get_stock_details` | – | `isin` | Company details |
| `search_instruments` | – | `query`, `asset_type` | Search |
| `get_price_history` | – | `isin`, `range`, `exchange` | Candles (`1d`, `5d`, `1m`, `3m`, `1y`, `max`) |
| `get_performance` | – | `isin`, `exchange` | Performance figures |
| `get_news` | – | `isin`, `limit` | Trade Republic news (`TR_LOCALE`) |
| `get_market_status` | – | `isin`, `exchange` | Whether the instrument is tradable now |
| `wait_for_market` | – | `isin`, `exchange`, `timeout_seconds` | Wait until tradable (max 300 s) |
| `wait_for_price` | – | `isin`, `above`/`below`, `field`, `exchange`, `timeout_seconds` | Wait for a price level (max 300 s) |
| `get_budget_status` | – | – | Budget ledger |
| `preview_order` | – | `isin`, `side`, `size`, `order_type`, `limit_price`, `stop_price`, `expiry`, `expiry_date`, `exchange`, `sell_fractions` | Validates, checks guardrails, returns value, fee, total and a single-use `confirmation_id` |
| `place_order` | ✓ | `confirmation_id` | Sends a previewed order (**real money**) |
| `cancel_order` | ✓ | `order_id` | Cancels an open order |

Tool annotations: `place_order` and `cancel_order` are marked destructive, `download_documents`
writes local files, all others are read-only.
The two-step order flow means an agent cannot change an order between preview and execution.

## Python API reference

Everything is `async`. The package exports `TRSession`, `TradeRepublic`, `OrderRequest`,
`OrderResult`, `Guardrails`, `Settings`, the stores and the error classes.

### Example

```python
import asyncio
from datetime import date
from trclient import TRSession, TradeRepublic, OrderRequest

async def main():
    session = TRSession("+4917012345678")
    if not await session.resume():
        challenge = await session.start_login(input("PIN: "))
        if challenge.method != "app" and not await session.complete_with_code(challenge, input("Code: ")):
            print("Code accepted - now approve the login in the Trade Republic app")
        if not session.has_session:
            await session.wait_for_app_confirmation(challenge)

    async with TradeRepublic(session) as tr:
        print(await tr.portfolio())

        for doc in await tr.documents(since=date(2026, 1, 1), sources=("postbox",)):
            print(await tr.download_document(doc, "~/trclient-documents"))

        order = OrderRequest(isin="DE0007164600", side="buy", size=0.25, mode="limit", limit=180.0)
        print(await tr.place_order(order))                      # dry run
        async with tr.stream_ticker("DE0007164600") as ticks:   # unsubscribes on exit
            async for tick in ticks:
                print(tick["last"]["price"])
                break

asyncio.run(main())
```

### `TRSession(phone, *, store=None, settings=None, read_only=False)`

Owns the HTTP client and the cookies of one account. Default store: OS keychain.

| Method | Returns | Description |
| --- | --- | --- |
| `resume()` | `bool` | Continue a stored session; `False` means a new login is needed |
| `start_login(pin)` | `LoginChallenge` | Start the web login v2. `challenge.method` is `"app"` or a code method |
| `complete_with_code(challenge, code)` | `bool` | Send an authenticator code. `False` = app approval still required |
| `wait_for_app_confirmation(challenge)` | – | Wait until the login is approved in the app |
| `start_login_code_flow(pin, waf_token)` / `resend_code(challenge)` | – | Legacy v1 flow |
| `ensure_fresh(force=False)` | – | Refresh the session cookie if needed |
| `request(method, path, **kw)` | `httpx.Response` | Authenticated REST call to `api.traderepublic.com` |
| `has_session` | `bool` | Session cookie present |
| `logout()` / `aclose()` | – | End the session / close the HTTP client |

### `TradeRepublic(session, *, guardrails=None)`

Use as `async with`. Methods marked *public* work without login.

**Account**

| Method | Description |
| --- | --- |
| `account()` | Account settings (REST) |
| `sec_acc_no()` | Securities account number |
| `portfolio()` | Positions (`compactPortfolioByType`) |
| `cash()` / `available_cash()` | Cash balance / cash available for orders |
| `available_size(isin, exchange=None)` | Sellable units |
| `orders(terminated=False)` | Open or terminated orders |
| `timeline_transactions(after=None)` | Transaction timeline page; `cursors.after` gives the next page |
| `timeline_activity_log(after=None)` | Activity / postbox timeline page |
| `timeline_detail(item_id)` | Details of one timeline event (`timelineDetailV2`) |

**Documents**

| Method | Description |
| --- | --- |
| `documents(since=None, sources=("transactions", "postbox"), title_contains=None)` | `list[Document]`, newest first |
| `download_document(doc, directory, overwrite=False)` | Saves the PDF; returns `{"id", "file", "status": "downloaded" \| "exists", "bytes"}` |

`Document` fields: `id`, `title`, `detail`, `date` (`YYYY-MM-DD`), `event_id`, `event_title`,
`event_subtitle`, `event_type`, `source` (`transactions` \| `postbox`), `payload` (download link).
`doc.public()` returns the fields without `payload`; `doc.filename()` the file name used.

**Market data** (*public*)

| Method | Description |
| --- | --- |
| `instrument(isin)` / `stock_details(isin)` | Master data / company details |
| `ticker(isin, exchange=None)` | Current quote (prices are strings) |
| `stream_ticker(isin, exchange=None)` | Live quotes, `async with ... as sub: async for tick in sub` |
| `price_for_order(isin, side="buy", exchange=None)` | Ask/bid used for orders |
| `performance(isin, exchange=None)` | Performance figures |
| `history(isin, range_="1y", exchange=None)` | Raw candles (`aggregateHistoryLight`) |
| `search(query, asset_type="stock", page=1, page_size=20, jurisdiction=None)` | Instrument search |
| `news(isin)` | Trade Republic news |
| `market_status(isin, exchange=None)` | Quote freshness, spread, trading status |
| `wait_for_market(...)` / `wait_for_price(...)` | Wait for tradability / a price level |
| `stream(payload)` | Any raw WebSocket subscription |

`exchange=None` means the home venue: `LSX` for securities, `BHS` for crypto.

**Trading**

| Method | Description |
| --- | --- |
| `place_order(order, execute=False)` | Checks the guardrails; sends only with `execute=True` and `TR_TRADING_ENABLED=1`. Returns `OrderResult` |
| `buy(isin, size, execute=False, **kw)` / `sell(...)` | Shortcuts that build an `OrderRequest` |
| `cancel_order(order_id, execute=False)` | Cancel an open order |
| `order_cost(order)` | Trade Republic's cost information for an order |
| `reconcile_budget()` | Release the budget of cancelled/expired orders |

`OrderRequest(isin, side, size, mode="market", limit=None, stop=None, expiry="gfd", expiry_date=None, exchange=None, sell_fractions=False)`
validates on creation (`mode`: `market` \| `limit` \| `stopMarket`; `expiry`: `gfd` \| `gtd` \| `gtc`).

`OrderResult`: `status` (`accepted` \| `dry_run`), `client_process_id`, `order_id`, `estimated_price`,
`estimated_value`, `fee_eur`, `budget_remaining_eur`, `payload`, `response`.

### Errors

All errors derive from `TRError`:

| Error | Meaning |
| --- | --- |
| `LoginError`, `SessionExpired` | Login failed / session no longer accepted, log in again |
| `OrderValidationError` | Invalid order parameters (also a `ValueError`) |
| `GuardrailViolation` | A configured limit refused the order |
| `OrderRejected` | Trade Republic rejected the order |
| `OrderStateUnknown` | No answer in time; the order may or may not exist. Check `orders()` before retrying |

## Guardrails, fees and budget

- **Dry run by default:** nothing is sent unless `TR_TRADING_ENABLED=1` **and** the call explicitly executes.
- **Fees:** Trade Republic charges a flat **1 € per order** (buy and sell). Previews show the fee,
  total cost and net proceeds. Optionally, `TR_MAX_FEE_PCT` refuses buys where the fee is a larger share.
- **Budget (`TR_BUDGET_EUR`, optional):**
  - Accepted buys use up value + fee.
  - Accepted sells give back value − fee.
  - Resting sells (limit/stop) are credited only once they are executed.
  - Cancelled, expired or rejected orders release their share automatically (partly filled orders keep the filled part).
  - In budget mode, only units bought through the budget can be sold, so other holdings stay untouched.
- **Exits are never blocked.** Sells ignore the value and fee limits.
- **Price-typo guard, allow list, daily order limit.**
- **Audit log** of every dry run, submission, acceptance, rejection and unknown state.
- **No automatic retries of orders.** A timeout returns `ORDER STATE UNKNOWN` together with the
  `clientProcessId`, so you check the orders before trying again.

## Known limitations

- **Accepted ≠ executed.** Trade Republic may route an order to another venue (e.g. LSX → TIB),
  and accepted orders can expire seconds later. Always verify.
- **Limit orders cannot use `gtc` at LSX.** Use `gtd` with a date.
- **Changing an order is not supported.** Cancel it and place a new one.
- **No volume data** in Trade Republic's price history.
- `tradingStatus` needs a login. Without it, `get_market_status` uses a quote-freshness heuristic.
- **The successful answer to a cancellation** has not been documented publicly yet. The raw answer is returned.
- **Document titles and categories** come from Trade Republic and may change.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `426 CLIENT_VERSION_OUTDATED` | Set `TR_APP_VERSION` to the current web app version |
| `MISSING_REQUIRED_HEADER` | Update trclient. Trade Republic changed the login headers. |
| `WAF_BLOCKED` (405 awselb) | Use the default v2 login, or pass a browser `aws-waf-token` for `--code-flow` |
| WebSocket `failed <n>` | `TR_WS_PROTOCOL_VERSION=<n>` |
| "Not logged in" | Run `trclient login` in a terminal and retry |
| No OS keyring on Linux | Install one (e.g. gnome-keyring), or `TR_SESSION_STORE=file` |
| `wait_for_*` or document tools time out in Claude | Raise the client's MCP tool timeout (Claude Code: `MCP_TOOL_TIMEOUT`) or use shorter periods |

## Development

```bash
.venv/bin/pytest                 # unit + end-to-end tests against a local fake Trade Republic server
```

- The tests use a mock HTTP transport and a real local WebSocket server that speaks the Trade Republic protocol.
- No test touches a real account.

Contributions are welcome. Please add tests, and never commit session data, documents or audit logs.

## Credits

This project builds on research from these MIT-licensed projects and their communities:

- [pytr-org/pytr](https://github.com/pytr-org/pytr): web login v2, WebSocket protocol, delta decoding, timeline documents
- [Zarathustra2/TradeRepublicApi](https://github.com/Zarathustra2/TradeRepublicApi) and the fixes in
  its open pull requests by LudwigJMarx: the order payload verified on real accounts, and the subscription handling

## License

MIT, see [LICENSE](LICENSE). Again: **not affiliated with Trade Republic, no warranty, use at your own risk.**
