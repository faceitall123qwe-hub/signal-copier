"""Regex fast-path tests (no network / no LLM)."""
from parser import regex_fastpath as fp
from parser.schema import Intent, Side


def test_new_entry_long_en():
    s = fp.parse("LONG BTCUSDT Entry: 64000 SL: 62000 TP1: 65000 TP2: 66000")
    assert s and s.intent == Intent.NEW_ENTRY
    assert s.side == Side.LONG
    assert s.symbol == "BTCUSDT"
    assert s.entries == [64000]
    assert s.stop_loss == 62000
    assert s.take_profits == [65000, 66000]
    assert s.confidence >= 0.8


def test_new_entry_short_ru():
    s = fp.parse("ШОРТ ETH вход 3200 стоп 3300 цель 3100")
    assert s and s.intent == Intent.NEW_ENTRY
    assert s.side == Side.SHORT
    assert s.symbol == "ETHUSDT"
    assert s.stop_loss == 3300


def test_move_to_breakeven():
    s = fp.parse("BTC move SL to breakeven")
    assert s and s.intent == Intent.MODIFY_SL
    assert s.symbol == "BTCUSDT"
    assert s.sl_to_breakeven is True


def test_full_close():
    s = fp.parse("Close BTC position now")
    assert s and s.intent == Intent.FULL_CLOSE
    assert s.symbol == "BTCUSDT"       # not "CLOSE"
    assert s.close_pct == 100.0


def test_partial_close_pct():
    s = fp.parse("SOL закрыть частично 50%")
    assert s and s.intent == Intent.PARTIAL_CLOSE
    assert s.close_pct == 50.0


def test_empty_is_noise():
    s = fp.parse("   ")
    assert s and s.intent == Intent.NOISE


def test_ambiguous_defers_to_llm():
    # side present but no entry/sl structure -> None (LLM fallback)
    s = fp.parse("thinking about going long on bitcoin soon maybe")
    assert s is None


def test_real_source_limit_entry():
    msg = ("[trader #c] Signal ID: #c21\n#XAUTUSDT Short\n"
           "Вход: 4240.9 лимитка\nTP: 4123.5\nSL: 4261.6\nРиск на сделку 1%")
    s = fp.parse(msg)
    assert s and s.intent == Intent.NEW_ENTRY
    assert s.symbol == "XAUTUSDT"      # not "TRADER"/"SIGNAL"
    assert s.side == Side.SHORT
    assert s.entries == [4240.9]
    assert s.stop_loss == 4261.6
    assert s.take_profits == [4123.5]


def test_real_source_market_entry_no_price():
    msg = ("[trader #c] Signal ID: #c18\n#XAUTUSDT long\nВход: рынок\n"
           "TP: 4215\nSL: 4129.2\nРиск на сделку 1%")
    s = fp.parse(msg)
    assert s and s.intent == Intent.NEW_ENTRY
    assert s.symbol == "XAUTUSDT"
    assert s.side == Side.LONG
    assert s.entries == []             # market: no entry price
    assert s.stop_loss == 4129.2


def test_management_msg_not_misread_as_entry():
    # has a side line but no entry/sl -> defers to LLM (returns None)
    msg = "[trader #c] Signal ID: #c19\n#BTCUSDT Long\nЗакину в б/у"
    assert fp.parse(msg) is None


def test_symbol_normalisation():
    from parser.schema import ParsedSignal
    sig = ParsedSignal(intent=Intent.NEW_ENTRY, symbol="btc/usdt", side=Side.LONG,
                       entries=[1.0], stop_loss=0.9, confidence=0.9)
    assert sig.symbol == "BTCUSDT"
    sig2 = ParsedSignal(intent=Intent.NEW_ENTRY, symbol="ETH", side=Side.LONG,
                        entries=[1.0], stop_loss=0.9, confidence=0.9)
    assert sig2.symbol == "ETHUSDT"
