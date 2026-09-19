"""
Global test fixtures.

Two things every test is protected from, regardless of which fixtures it declares:
sending a real Telegram message, and writing to the production files in data/.
"""
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def no_telegram_sends():
    """TelegramHandler calls _send_safe in a background thread — patch it globally
    so no test can ever fire a real message regardless of handler attachment."""
    with patch("notifier.telegram_handler._send_safe"):
        yield


# Every module-level path pointing into data/. Modules that intentionally share a
# file (summary and trade_recorder both read trades.json) keep sharing it, because
# the redirect preserves the original filename.
_DATA_PATHS = [
    ("scheduler", "_RUNS_FILE"),
    ("crypto.trader", "_STATE_FILE"),
    ("crypto.trader", "_TRADES_FILE"),
    ("crypto.trader", "_WATCHLIST_FILE"),
    ("notifier.feature_flags", "_FILE"),
    ("notifier.log_config", "_FILE"),
    ("notifier.schedule_display", "_RUNS_FILE"),
    ("notifier.summary", "_TRADES_FILE"),
    ("trader.order_placer", "_ORDERS_FILE"),
    ("trader.premarket_validator", "_FILTER_FILE"),
    ("trader.trade_recorder", "_TRADES_FILE"),
]


@pytest.fixture(autouse=True)
def isolate_data_files(tmp_path, monkeypatch):
    """Redirect every data/ path into a per-test directory.

    Without this a test only needs to call a function that records something —
    manage_exits, _save_trade — to append to the real ledger. That happened: 46
    fixture trades at $100 entry reached data/crypto_trades.json and showed up in
    /summary as real P&L. Individual fixtures patching one path each are not
    enough, since the leak comes from whichever path a test forgot to patch.
    """
    import importlib

    sandbox = tmp_path / "data"
    sandbox.mkdir(exist_ok=True)
    for module_name, attr in _DATA_PATHS:
        module = importlib.import_module(module_name)
        original = getattr(module, attr)
        monkeypatch.setattr(module, attr, sandbox / Path(original).name)
    yield
