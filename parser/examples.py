"""Few-shot anchors for the LLM parser.

Anonymised messages in the shape of a typical RU-language signal source
(several trader sub-channels, USDT perps). Replace with real messages from
your own sources. Each entry is (raw_message, expected_json_dict) matching
ParsedSignal fields.

Common source template (RU):
    [trader #x] Signal ID: #xNN
    #SYMBOL Лонг|Шорт|long|short
    Вход: <price> лимитка   |   Вход: рынок
    TP: <price>
    SL: <price>
    Риск на сделку 1%
Management messages repeat the symbol and reference the same Signal ID.
"""
from __future__ import annotations

EXAMPLES: list[tuple[str, dict]] = [
    # --- NEW ENTRY, limit, short (trader#c) ---
    (
        "[trader #c] Signal ID: #c21\n#XAUTUSDT Short\nВход: 4240.9 лимитка\n"
        "TP: 4123.5\nSL: 4261.6\nРиск на сделку 1%",
        {
            "intent": "NEW_ENTRY", "symbol": "XAUTUSDT", "side": "short",
            "entries": [4240.9], "stop_loss": 4261.6, "take_profits": [4123.5],
            "leverage": None, "close_pct": None, "sl_to_breakeven": False,
            "confidence": 0.97,
        },
    ),
    # --- NEW ENTRY, market (no entry price), long (trader#c) ---
    (
        "[trader #c] Signal ID: #c18\n#XAUTUSDT long\nВход: рынок\nTP: 4215\n"
        "SL: 4129.2\nРиск на сделку 1%",
        {
            "intent": "NEW_ENTRY", "symbol": "XAUTUSDT", "side": "long",
            "entries": [], "stop_loss": 4129.2, "take_profits": [4215],
            "leverage": None, "close_pct": None, "sl_to_breakeven": False,
            "confidence": 0.95,
        },
    ),
    # --- NEW ENTRY, market, long (trader#c) ---
    (
        "[trader #c] Signal ID: #c23\n#WLDUSDT лонг\nВход: 0.6394 рынок\n"
        "TP: 0.7104\nSL:0.6010\nРиск на сделку 1%",
        {
            "intent": "NEW_ENTRY", "symbol": "WLDUSDT", "side": "long",
            "entries": [0.6394], "stop_loss": 0.6010, "take_profits": [0.7104],
            "leverage": None, "close_pct": None, "sl_to_breakeven": False,
            "confidence": 0.96,
        },
    ),
    # --- NEW ENTRY, limit (trader#c) ---
    (
        "[trader #c] Signal ID: #c17\n#BTCUSDT Long\nВход: 63674 лимитка\n"
        "TP: 66000\nSl: 63471\nРиск на сделку 1%",
        {
            "intent": "NEW_ENTRY", "symbol": "BTCUSDT", "side": "long",
            "entries": [63674], "stop_loss": 63471, "take_profits": [66000],
            "leverage": None, "close_pct": None, "sl_to_breakeven": False,
            "confidence": 0.97,
        },
    ),
    # --- MANAGEMENT: move SL to breakeven (trader#c) ---
    (
        "[trader #c] Signal ID: #c19\n#BTCUSDT Long\n"
        "Закину в б/у, не нравится новостной фон",
        {
            "intent": "MODIFY_SL", "symbol": "BTCUSDT", "side": None,
            "entries": [], "stop_loss": None, "take_profits": [],
            "leverage": None, "close_pct": None, "sl_to_breakeven": True,
            "confidence": 0.93,
        },
    ),
    # --- MANAGEMENT: full close (trader#c) ---
    (
        "[trader #c] Signal ID: #c19\n#BTCUSDT Long\nЗакрыла +10.73R\n"
        "Сомнительная манипуляция, лучше выйду",
        {
            "intent": "FULL_CLOSE", "symbol": "BTCUSDT", "side": None,
            "entries": [], "stop_loss": None, "take_profits": [],
            "leverage": None, "close_pct": 100, "sl_to_breakeven": False,
            "confidence": 0.9,
        },
    ),
    # --- MANAGEMENT: cancel unfilled limit entry (trader#c) ---
    (
        "[trader #c] Signal ID: #c22\n#BTCUSDT Long\nОтменяю лимитку",
        {
            "intent": "FULL_CLOSE", "symbol": "BTCUSDT", "side": None,
            "entries": [], "stop_loss": None, "take_profits": [],
            "leverage": None, "close_pct": 100, "sl_to_breakeven": False,
            "confidence": 0.9,
        },
    ),
    # --- RESULT / INFO: stopped out, no action ---
    (
        "[trader #c] Signal ID: #c18\n#XAUTUSDT long\nСтоп -1%",
        {
            "intent": "INFO", "symbol": "XAUTUSDT", "side": None, "entries": [],
            "stop_loss": None, "take_profits": [], "leverage": None,
            "close_pct": None, "sl_to_breakeven": False, "confidence": 0.85,
        },
    ),
    (
        "#XAUTUSDT long\nВыбило по бу, посмотрю может будет перезаход",
        {
            "intent": "INFO", "symbol": "XAUTUSDT", "side": None, "entries": [],
            "stop_loss": None, "take_profits": [], "leverage": None,
            "close_pct": None, "sl_to_breakeven": False, "confidence": 0.8,
        },
    ),
    # --- strategy forum: NEW ENTRY (different wording) ---
    (
        "🟢 Стратегия «Кросс SMA 21/55» открыла ЛОНГ по ARUSDT интрадей (1Н)\n"
        "Вход 0.08297, стоп 0.079036, цель 0.090051 — риск к прибыли 1.8\n"
        "Это виртуальная сделка в открытом тесте",
        {
            "intent": "NEW_ENTRY", "symbol": "ARUSDT", "side": "long",
            "entries": [0.08297], "stop_loss": 0.079036,
            "take_profits": [0.090051], "leverage": None, "close_pct": None,
            "sl_to_breakeven": False, "confidence": 0.9,
        },
    ),
    # --- strategy forum: result / target reached, no action ---
    (
        "✅ Стратегия «Кросс SMA 21/55» закрыла ШОРТ по ARUSDT — цель достигнута\n"
        "Результат: +1.7R (вход 0.0807 → выход 0.0768)",
        {
            "intent": "FULL_CLOSE", "symbol": "ARUSDT", "side": None,
            "entries": [], "stop_loss": None, "take_profits": [],
            "leverage": None, "close_pct": 100, "sl_to_breakeven": False,
            "confidence": 0.88,
        },
    ),
    # --- Spelled-out fields + "по текущим" market entry (trader#b wording) ---
    (
        "[trader#b] Signal ID: #b66\n$LTCUSDT - Лонг\nВход: по текущим (≈45.12)\n"
        "Тейк профит: 46.7\nСтоп лосс: 44.67\nРиск на сделку 1%\n"
        "Потенциальная прибыль 3.5%",
        {
            "intent": "NEW_ENTRY", "symbol": "LTCUSDT", "side": "long",
            "entries": [], "stop_loss": 44.67, "take_profits": [46.7],
            "leverage": None, "close_pct": None, "sl_to_breakeven": False,
            "confidence": 0.93,
        },
    ),
    # --- MANAGEMENT: close at breakeven (short wording) ---
    (
        "Закрываем пока в БУ",
        {
            "intent": "MODIFY_SL", "symbol": None, "side": None, "entries": [],
            "stop_loss": None, "take_profits": [], "leverage": None,
            "close_pct": None, "sl_to_breakeven": True, "confidence": 0.85,
        },
    ),
    # --- NOISE ---
    (
        "Всем привет, это админ! 👋 У нас в сигнальной сегодня спокойный день.",
        {
            "intent": "NOISE", "symbol": None, "side": None, "entries": [],
            "stop_loss": None, "take_profits": [], "leverage": None,
            "close_pct": None, "sl_to_breakeven": False, "confidence": 0.95,
        },
    ),
]
