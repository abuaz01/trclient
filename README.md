# trclient: Trade Republic API and MCP server

Python client and local MCP server for the private API of the Trade Republic online brokerage.

> [!CAUTION]
> Not affiliated with Trade Republic Bank GmbH. Unofficial, may break at any time and may violate their terms.
> It can place real orders with real money. No warranty, no liability. **Use at your own risk.**

* [Quickstart](#quickstart)
* [Command line](#command-line)
* [MCP server](#mcp-server)
* [Python](#python)
* [Configuration](#configuration)
* [License](#license)

## Quickstart

```sh
git clone https://github.com/abuaz01/trclient.git && cd trclient
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/trclient login
```

`login` asks for your phone number and PIN, then you approve the login in the Trade Republic app.
The PIN is never stored. The session is kept in the OS keychain.

## Command line

```console
trclient login | status | logout
trclient portfolio | cash | orders [--terminated] | transactions
trclient quote ISIN | instrument ISIN | history ISIN [--range 1y] | search QUERY
trclient documents [--days 90 | --since 2025-01-01] [--source postbox] [--match Kontoauszug] [--list]
trclient buy|sell ISIN --size N [--limit P | --stop P] [--execute]
trclient cancel ORDER_ID [--execute]
```

- `documents` downloads account statements, trade confirmations, tax documents and other postbox
  PDFs to `~/trclient-documents`. Files that already exist are skipped.
- Orders are a dry run unless `TR_TRADING_ENABLED=1` is set and `--execute` is passed.

Run `trclient <command> --help` for all options.

## MCP server

`trclient-mcp` is a local stdio MCP server. Log in with `trclient login` first.

```sh
claude mcp add trade-republic -- /ABSOLUTE/PATH/trclient/.venv/bin/trclient-mcp
```

Add `-e TR_TRADING_ENABLED=1` to allow orders. Without it the server is read-only.

| Area | Tools |
| --- | --- |
| Account | `get_account_status`, `get_portfolio`, `get_cash`, `get_orders`, `get_transactions`, `get_available_size` |
| Documents | `list_documents`, `download_documents` |
| Market data | `get_quote`, `get_instrument`, `get_stock_details`, `search_instruments`, `get_price_history`, `get_performance`, `get_news`, `get_market_status`, `wait_for_market`, `wait_for_price` |
| Orders | `preview_order` → `place_order(confirmation_id)`, `cancel_order`, `get_budget_status` |

An order is always previewed first. `place_order` only accepts the `confirmation_id` of that preview.

## Python

```python
import asyncio
from trclient import TRSession, TradeRepublic, OrderRequest

async def main():
    session = TRSession("+4917012345678")
    if not await session.resume():
        raise SystemExit("run: trclient login")

    async with TradeRepublic(session) as tr:
        print(await tr.portfolio())
        for doc in await tr.documents(sources=("postbox",)):
            await tr.download_document(doc, "~/trclient-documents")

        order = OrderRequest(isin="DE0007164600", side="buy", size=1, mode="limit", limit=180.0)
        print(await tr.place_order(order))                 # dry run
        # await tr.place_order(order, execute=True)        # real order, needs TR_TRADING_ENABLED=1

asyncio.run(main())
```

| | Methods of `TradeRepublic` |
| --- | --- |
| Account | `portfolio()`, `cash()`, `available_cash()`, `available_size(isin)`, `orders(terminated=False)`, `timeline_transactions(after)`, `timeline_activity_log(after)`, `timeline_detail(id)` |
| Documents | `documents(since, sources, title_contains)`, `download_document(doc, directory)` |
| Market data | `ticker(isin)`, `stream_ticker(isin)`, `price_for_order(isin, side)`, `history(isin, range_)`, `instrument(isin)`, `stock_details(isin)`, `performance(isin)`, `search(query, asset_type)`, `news(isin)`, `market_status(isin)` |
| Orders | `place_order(order, execute=False)`, `buy(...)`, `sell(...)`, `cancel_order(order_id, execute=False)` |

Errors derive from `TRError`. `OrderStateUnknown` means there was no answer in time: check `orders()`
before sending again. Orders are never retried automatically.

## Configuration

Environment variables, all optional:

| Variable | Default | |
| --- | --- | --- |
| `TR_TRADING_ENABLED` | off | `1` allows real orders |
| `TR_MAX_ORDER_EUR` | `500` | Maximum value of one buy |
| `TR_MAX_ORDERS_PER_DAY` | `10` | |
| `TR_BUDGET_EUR` | – | Total amount orders may use |
| `TR_ALLOWED_ISINS` | all | Comma-separated allow list |
| `TR_ORDER_FEE_EUR` | `1.0` | Fee per order |
| `TR_DOCUMENTS_DIR` | `~/trclient-documents` | Download folder |
| `TR_LOCALE` | `de` | Language of news and document titles |
| `TR_APP_VERSION` | built in | Set if login fails with `426 CLIENT_VERSION_OUTDATED` |

Every order attempt is logged to `~/.local/state/trclient/audit.jsonl`.

## License

MIT. Based on research from [pytr](https://github.com/pytr-org/pytr) and
[TradeRepublicApi](https://github.com/Zarathustra2/TradeRepublicApi).
