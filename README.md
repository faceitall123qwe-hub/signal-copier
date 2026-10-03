# signal-copier

Mirrors trade signals from one or more Telegram sources (including forum
supergroup topics) onto Bybit USDT perpetuals, with risk-based sizing and full
trade management. Three gated run modes: **DRY_RUN → TESTNET → LIVE**.

Stack: async Python, Telethon, python-telegram-bot, pybit v5, pydantic v2,
aiosqlite, Claude Haiku as a fallback parser.

## Try it without any keys

```bash
pip install -r requirements.txt pytest
python demo.py      # scripted signals -> full pipeline -> in-memory exchange
pytest -q           # 31 tests, no network
```

`demo.py` drives the real parser, risk guard, sizing and executor against
`exchange/mock_exchange.py` — an in-memory exchange with the same async
interface as `BybitClient`. It shows limit entry → fill → SL + TP ladder,
TP1 → SL to breakeven, duplicate-message idempotency, market close with
realised PnL, and the `/panic` kill-switch blocking new entries.

## How it works

```
N sources ─(Telethon userbot)─> parse (regex fast-path → Claude Haiku fallback)
  → ParsedSignal{intent,symbol,side,entries,sl,tps,leverage,close_pct,confidence}
  confidence < MIN_CONFIDENCE ─► ping control bot, await /confirm | /reject
  else route ▼
  Risk guard │ Sizing (risk-to-SL) │ Bybit executor │ SQLite state │ Notifier
Bybit private WS (order/position/execution) ─► state machine
  (TP1 filled → SL to breakeven; entry-fill timeout; realized PnL)
Control bot: /status /positions /pnl /pause /resume /panic /risk /mode
             /confirm /reject /sources
```

Two Telegram identities:
- **Telethon userbot** on a *secondary* account that is a member of every source
  channel — reads the signals (Bot API cannot read third-party channels).
- **Control bot** (your own bot token) — how you command the copier.

## Safety model

- **DRY_RUN** (default): parse + log only, zero orders.
- **TESTNET**: places orders on Bybit testnet.
- **LIVE**: refuses to start unless `MODE=LIVE` **and** `CONFIRM_LIVE=I_UNDERSTAND`.
- Idempotent `orderLinkId = sha1(source_channel_id:message_id)` → restarts and
  duplicate messages never double-fire.
- Isolated margin, leverage capped so the SL always triggers before liquidation.
- `/panic` cancels + flattens everything and refuses new entries until `/resume`.
- Max concurrent positions + max-daily-loss halt.
- Secrets only in `.env`; `.env` and `*.session` are gitignored and never logged.

## Setup

```bash
cd signal-copier
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env            # then fill it in
```

Fill `parser/examples.py` with real messages from each sub-channel (+ one
management message) and their expected JSON — this anchors the LLM parser.

### Generate the Telethon session (secondary account)

```bash
python generate_session.py        # prompts for phone + login code (+2FA)
```

### Run

```bash
# 1) DRY_RUN — no orders, just parse + log + notify
#    MODE=DRY_RUN in .env
python main.py

# 2) TESTNET — real orders on Bybit testnet
#    MODE=TESTNET, BYBIT_TESTNET=true, testnet API keys
python main.py

# 3) LIVE — real money. Only after testnet looks correct.
#    MODE=LIVE, CONFIRM_LIVE=I_UNDERSTAND, BYBIT_TESTNET=false, mainnet keys
python main.py
```

### Tests

```bash
pip install pytest
pytest -q
```

- `tests/test_parser.py` — regex fast-path (EN/RU/PL shapes, symbol extraction)
- `tests/test_sizing.py` — risk-to-SL sizing, leverage cap vs liquidation
- `tests/test_state.py` — idempotent order links, scoped management matching
- `tests/test_executor_mock.py` — end-to-end trade lifecycle on `MockExchange`

## Notes / verify against installed libs

- `pybit` v5 `unified_trading.HTTP` / `WebSocket` and `telethon` attribute names
  (`reply_to.reply_to_top_id`, `GetForumTopicsRequest`) can change between
  versions — pinned in `requirements.txt`; re-verify if you upgrade.
- One Bybit account nets per symbol in one-way mode; `ON_SYMBOL_COLLISION`
  controls behaviour when a symbol already has an open position.
