"""
Tests for trader/trade_recorder.py — TP/SL fill detection, trade recording, Telegram notification.
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_order(symbol, order_type, filled_avg_price, qty=10, status="filled"):
    o = MagicMock()
    o.symbol = symbol
    o.order_type = order_type        # "limit" → TP, "stop" → SL
    o.filled_avg_price = filled_avg_price
    o.qty = str(qty)
    o.status = status
    o.filled_at = datetime(2026, 5, 22, 11, 30, tzinfo=timezone.utc)
    return o


def _today():
    return date.today().isoformat()


def _orders_data(symbol="AAPL", qty=10, entry=150.0, stop=147.0, take=153.0):
    return {_today(): {symbol: {"qty": qty, "entry_price": entry,
                                "stop_price": stop, "take_price": take,
                                "exit_mode": "trailing_stop"}}}


# ── scan_for_fills ────────────────────────────────────────────────────────────

class TestScanForFills:
    def _run(self, closed_orders, orders_data, trades_data=None, tmp_path=None):
        trades_file = tmp_path / "trades.json" if tmp_path else Path("/tmp/trades_noop.json")
        if trades_data and tmp_path:
            trades_file.write_text(json.dumps(trades_data))

        mock_client = MagicMock()
        mock_client.get_orders.return_value = closed_orders

        with patch("trader.trade_recorder._get_client", return_value=mock_client), \
             patch("trader.order_placer.load_orders_today", return_value=orders_data.get(_today(), {})), \
             patch("trader.trade_recorder._TRADES_FILE", trades_file):
            from trader.trade_recorder import scan_for_fills
            return scan_for_fills()

    def test_detects_take_profit_fill(self, tmp_path):
        order = _mock_order("AAPL", order_type="limit", filled_avg_price=153.0)
        fills = self._run([order], _orders_data("AAPL", entry=150.0), tmp_path=tmp_path)
        assert len(fills) == 1
        assert fills[0]["reason"] == "take_profit"

    def test_detects_stop_loss_fill(self, tmp_path):
        order = _mock_order("AAPL", order_type="stop", filled_avg_price=147.0)
        fills = self._run([order], _orders_data("AAPL", entry=150.0), tmp_path=tmp_path)
        assert len(fills) == 1
        assert fills[0]["reason"] == "stop_loss"

    def test_computes_pnl_correctly_for_profit(self, tmp_path):
        order = _mock_order("AAPL", order_type="limit", filled_avg_price=153.0, qty=10)
        fills = self._run([order], _orders_data("AAPL", qty=10, entry=150.0), tmp_path=tmp_path)
        assert fills[0]["pnl"] == pytest.approx(30.0)
        assert fills[0]["pnl_pct"] == pytest.approx(2.0)

    def test_computes_pnl_correctly_for_loss(self, tmp_path):
        order = _mock_order("AAPL", order_type="stop", filled_avg_price=147.0, qty=10)
        fills = self._run([order], _orders_data("AAPL", qty=10, entry=150.0), tmp_path=tmp_path)
        assert fills[0]["pnl"] == pytest.approx(-30.0)
        assert fills[0]["pnl_pct"] == pytest.approx(-2.0)

    def test_skips_symbol_not_in_our_orders(self, tmp_path):
        order = _mock_order("NVDA", order_type="limit", filled_avg_price=900.0)
        fills = self._run([order], _orders_data("AAPL"), tmp_path=tmp_path)
        assert len(fills) == 0

    def test_skips_unfilled_orders(self, tmp_path):
        order = _mock_order("AAPL", order_type="limit", filled_avg_price=None, status="cancelled")
        order.filled_avg_price = None
        fills = self._run([order], _orders_data("AAPL"), tmp_path=tmp_path)
        assert len(fills) == 0

    def test_skips_already_notified_symbols(self, tmp_path):
        trades_data = {_today(): [{"symbol": "AAPL", "notified": True, "reason": "take_profit",
                                   "entry_price": 150.0, "exit_price": 153.0, "qty": 10,
                                   "pnl": 30.0, "pnl_pct": 2.0, "exited_at": "..."}]}
        order = _mock_order("AAPL", order_type="limit", filled_avg_price=153.0)
        fills = self._run([order], _orders_data("AAPL"), trades_data=trades_data, tmp_path=tmp_path)
        assert len(fills) == 0

    def test_returns_empty_when_no_orders_placed_today(self, tmp_path):
        fills = self._run([], {}, tmp_path=tmp_path)
        assert fills == []

    def test_handles_alpaca_api_error_gracefully(self, tmp_path):
        mock_client = MagicMock()
        mock_client.get_orders.side_effect = Exception("timeout")
        with patch("trader.trade_recorder._get_client", return_value=mock_client), \
             patch("trader.order_placer.load_orders_today",
                   return_value=_orders_data().get(_today(), {})), \
             patch("trader.trade_recorder._TRADES_FILE", tmp_path / "t.json"):
            from trader.trade_recorder import scan_for_fills
            result = scan_for_fills()
        assert result == []

    def test_saves_trade_to_file(self, tmp_path):
        order = _mock_order("AAPL", order_type="limit", filled_avg_price=153.0)
        self._run([order], _orders_data("AAPL", entry=150.0), tmp_path=tmp_path)
        trades_file = tmp_path / "trades.json"
        assert trades_file.exists()
        saved = json.loads(trades_file.read_text())
        assert len(saved[_today()]) == 1
        assert saved[_today()][0]["symbol"] == "AAPL"


# ── send_fill_notifications ───────────────────────────────────────────────────

class TestSendFillNotifications:
    def _fill(self, symbol="AAPL", reason="take_profit", entry=150.0, exit_p=153.0,
              qty=10, pnl=30.0, pnl_pct=2.0):
        return {"symbol": symbol, "reason": reason, "entry_price": entry,
                "exit_price": exit_p, "qty": qty, "pnl": pnl, "pnl_pct": pnl_pct,
                "exited_at": "2026-05-22T11:30:00+00:00"}

    def test_sends_tp_message_with_target_icon(self):
        with patch("notifier.telegram._send") as mock_send:
            from trader.trade_recorder import send_fill_notifications
            send_fill_notifications([self._fill(reason="take_profit")])
            text = mock_send.call_args[0][0]
            assert "🎯" in text
            assert "Take Profit" in text

    def test_sends_sl_message_with_shield_icon(self):
        with patch("notifier.telegram._send") as mock_send:
            from trader.trade_recorder import send_fill_notifications
            send_fill_notifications([self._fill(reason="stop_loss", exit_p=147.0,
                                                pnl=-30.0, pnl_pct=-2.0)])
            text = mock_send.call_args[0][0]
            assert "🛡" in text
            assert "Stop Loss" in text

    def test_shows_positive_pnl_with_plus_sign(self):
        with patch("notifier.telegram._send") as mock_send:
            from trader.trade_recorder import send_fill_notifications
            send_fill_notifications([self._fill(pnl=30.0, pnl_pct=2.0)])
            text = mock_send.call_args[0][0]
            assert "+$30.00" in text
            assert "+2.0%" in text

    def test_shows_negative_pnl_with_minus_sign(self):
        with patch("notifier.telegram._send") as mock_send:
            from trader.trade_recorder import send_fill_notifications
            send_fill_notifications([self._fill(reason="stop_loss", exit_p=147.0,
                                                pnl=-30.0, pnl_pct=-2.0)])
            text = mock_send.call_args[0][0]
            assert "-$30.00" in text

    def test_shows_entry_and_exit_price(self):
        with patch("notifier.telegram._send") as mock_send:
            from trader.trade_recorder import send_fill_notifications
            send_fill_notifications([self._fill(entry=150.0, exit_p=153.0)])
            text = mock_send.call_args[0][0]
            assert "150.00" in text
            assert "153.00" in text

    def test_sends_one_message_per_fill(self):
        fills = [self._fill("AAPL"), self._fill("MSFT", exit_p=155.0, pnl=10.0, pnl_pct=7.0)]
        with patch("notifier.telegram._send") as mock_send:
            from trader.trade_recorder import send_fill_notifications
            send_fill_notifications(fills)
            assert mock_send.call_count == 2


# ── load_orders_today (new format) ───────────────────────────────────────────

class TestLoadOrdersToday:
    def test_loads_new_dict_format(self, tmp_path):
        orders_file = tmp_path / "orders.json"
        data = {_today(): {"AAPL": {"qty": 10, "entry_price": 150.0}}}
        orders_file.write_text(json.dumps(data))
        with patch("trader.order_placer._ORDERS_FILE", orders_file):
            from trader.order_placer import load_orders_today
            result = load_orders_today()
        assert "AAPL" in result
        assert result["AAPL"]["entry_price"] == 150.0

    def test_migrates_old_list_format(self, tmp_path):
        orders_file = tmp_path / "orders.json"
        data = {_today(): ["AAPL", "MSFT"]}
        orders_file.write_text(json.dumps(data))
        with patch("trader.order_placer._ORDERS_FILE", orders_file):
            from trader.order_placer import load_orders_today
            result = load_orders_today()
        assert "AAPL" in result
        assert "MSFT" in result

    def test_returns_empty_when_no_file(self, tmp_path):
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "missing.json"):
            from trader.order_placer import load_orders_today
            assert load_orders_today() == {}
