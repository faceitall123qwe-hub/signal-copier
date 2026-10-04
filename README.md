# telegram-bybit-copier

[![ci](https://github.com/faceitall123qwe-hub/telegram-bybit-copier/actions/workflows/ci.yml/badge.svg)](https://github.com/faceitall123qwe-hub/telegram-bybit-copier/actions/workflows/ci.yml)

Reads trade signals from Telegram channels and places them on Bybit (USDT perpetuals). It
manages the whole trade: entry, stop loss, a take-profit ladder, moving the stop to breakeven
after TP1, and partial or full closes when the channel posts a follow-up.

It starts in `DRY_RUN`. It only trades real money with `MODE=LIVE` **and**
`CONFIRM_LIVE=I_UNDERSTAND`.

## Trying it without any keys

```bash
pip install -r requirements.txt pytest
python demo.py
pytest -q
```

`demo.py` pushes a few scripted messages through the real parser, risk checks, sizing and
executor, against an in-memory exchange (`exchange/mock_exchange.py`) that has the same
interface as the Bybit client. Shortened output:

```text
>>> message #1: LONG BTCUSDT Entry: 64000 SL: 63000 TP1: 65000 TP2: 66000
  ENTRY BTCUSDT long qty=0.1 @ 64000.0 | SL=63000.0 TP=[65000.0, 66000.0]
>>> exchange: fill entry
  SL+TP set BTCUSDT: SL=63000.0 TP=[65000.0, 66000.0]
>>> exchange: fill tp1
  PARTIAL CLOSE BTCUSDT leg filled @ 65000.0
  SL -> breakeven BTCUSDT after TP1
>>> message #3: (the first message again)
  duplicate entry, skipping
>>> operator: /panic
>>> message #6: LONG SOLUSDT Entry: 150 SL: 145 TP: 165
  SKIP SOLUSDT: panic active (use /resume)
```

## How it works

Messages come in through a Telethon user account, because bots can't read channels they don't
own. A separate bot that I control is used for commands and notifications.

Parsing tries regular expressions first (English, Russian and Polish wording). If they don't
match with enough confidence, the message goes to Claude Haiku with a set of examples from the
real channels. Anything below 0.75 confidence is held until I reply `/confirm` or `/reject`.

Position size comes from risk, not leverage: `equity * RISK_PCT / distance to stop`. Leverage
only changes the margin, and it's capped so the liquidation price is always past the stop.

Every order gets `orderLinkId = hash(channel_id, message_id)`. A restart or a message delivered
twice can't open a second position.

Fills come back over Bybit's private WebSocket and move the trade through its states, which
are stored in SQLite. Follow-up messages ("move SL to BE", "close half") are matched to a trade
from the same channel and topic, never across channels.

Other limits: max open positions, max daily loss, and `/panic`, which cancels all orders,
closes all positions and blocks new entries until `/resume`.

## Commands

`/status` `/positions` `/pnl` `/pause` `/resume` `/panic` `/risk` `/mode` `/confirm` `/reject`
`/sources`. Only the configured Telegram user ID can use them.

## Layout

```
parser/     regex parser, LLM fallback, signal schema (pydantic), examples
core/       executor and trade states, risk guard, notifications
exchange/   Bybit client, position sizing, mock exchange
storage/    SQLite (aiosqlite)
tests/      parser, sizing, state matching, full trade flow on the mock exchange
```

## Running it for real

```bash
cp .env.example .env         # Telegram API, control bot token, Bybit and Anthropic keys
python generate_session.py   # log in the Telethon account
python main.py               # DRY_RUN, then TESTNET, then LIVE
```

Put real messages from your channels into `parser/examples.py` before relying on the LLM
parser. Run on testnet first. This is not financial advice.

## License

MIT
