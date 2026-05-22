"""
Tests for the full trade execution layer:
  - trader/order_placer.py   — position sizing, bracket order placement, dedup
  - trader/position_monitor.py — EOD exit logic (fixed TP, warnings, RSI)
  - trader/intraday_monitor.py — intraday 15-min RSI exit logic
"""
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest
from trader._utils import is_transient, log_api_error


class TestIsTransient:
    def test_connection_refused_is_transient(self):
        assert is_transient(Exception("connection refused"))

    def test_timeout_is_transient(self):
        assert is_transient(Exception("request timed out"))

    def test_503_is_transient(self):
        assert is_transient(Exception("503 service unavailable"))

    def test_429_rate_limit_is_transient(self):
        assert is_transient(Exception("429 too many requests"))

    def test_auth_error_is_not_transient(self):
        assert not is_transient(Exception("403 forbidden"))

    def test_invalid_symbol_is_not_transient(self):
        assert not is_transient(Exception("asset not found"))

    def test_log_api_error_warns_on_transient(self):
        mock_log = MagicMock()
        log_api_error(mock_log, "fetch failed", Exception("connection refused"))
        mock_log.warning.assert_called_once()
        mock_log.error.assert_not_called()

    def test_log_api_error_errors_on_real_failure(self):
        mock_log = MagicMock()
        log_api_error(mock_log, "fetch failed", Exception("403 forbidden"))
        mock_log.error.assert_called_once()
        mock_log.warning.assert_not_called()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_position(symbol="AAPL", unrealized_pl="50.00"):
    pos = MagicMock()
    pos.symbol = symbol
    pos.unrealized_pl = unrealized_pl
    return pos


def _mock_order(order_id="order-abc"):
    order = MagicMock()
    order.id = order_id
    return order


def _candidate(
    symbol="AAPL",
    price=25.0,
    exit_mode="trailing_stop",
    atr_min=0.5,
    atr_max=1.0,
    rsi=60.0,
    warning_count=0,
):
    warnings = ["warning signal"] * warning_count
    return {
        "symbol": symbol,
        "market": "stocks",
        "score": 8,
        "days_in_scan": 1,
        "trend": {
            "last_close": price,
            "sma50": price * 0.9, "sma200": price * 0.8,
            "ema9": price * 1.01, "ema21": price * 0.99,
            "above_sma50": True, "above_sma200": True, "ema9_above_ema21": True,
        },
        "momentum": {
            "rsi": rsi, "rsi_overbought": rsi > 70,
            "macd_above_signal": True, "macd_histogram_positive": True,
            "macd_histogram_shrinking": False,
            "adx": 32.0, "adx_strong": True, "adx_falling": False, "atr": atr_min / 1.5,
        },
        "volume": {
            "volume": 1_000_000, "avg_volume": 800_000,
            "volume_ratio": 1.25, "volume_drying_up": False, "volume_above_avg": True,
        },
        "relative_strength": {"rs_return": 20.0, "spy_return": 5.0, "outperforming_spy": True},
        "criteria": {k: True for k in [
            "above_sma50", "above_sma200", "ema9_above_ema21",
            "rsi_in_range", "macd_bullish", "adx_strong", "volume_above_avg", "outperforming_spy",
        ]},
        "exit": {
            "exit_mode": exit_mode,
            "trailing_stop_atr_range": (atr_min, atr_max),
            "warning_count": warning_count,
            "warnings": warnings,
        },
    }


def _signal(symbol="AAPL", exit_mode="trailing_stop", rsi=60.0, warning_count=0):
    warnings = ["w"] * warning_count
    return {
        "symbol": symbol,
        "exit": {
            "exit_mode": exit_mode,
            "warning_count": warning_count,
            "warnings": warnings,
        },
        "momentum": {"rsi": rsi},
    }


# ── position_qty & position_label ────────────────────────────────────────────

class TestPositionSizing:
    def test_high_price_gives_one_position(self):
        from trader.order_placer import position_qty
        # $250 / $200 = 1 share
        assert position_qty(200.0) == 1

    def test_low_price_gives_three_positions(self):
        from trader.order_placer import position_qty
        # 3 × $250 / $5 = 150 shares
        assert position_qty(5.0) == 150

    def test_price_just_above_threshold_is_one_position(self):
        from trader.order_placer import position_qty
        assert position_qty(51.0) == 4   # floor(250/51)

    def test_price_just_below_threshold_is_three_positions(self):
        from trader.order_placer import position_qty
        assert position_qty(49.0) == 15  # floor(750/49)

    def test_very_high_price_minimum_one_share(self):
        from trader.order_placer import position_qty
        # $250 / $500 = 0 → forced to 1
        assert position_qty(500.0) == 1

    def test_label_high_price(self):
        from trader.order_placer import position_label
        label = position_label(100.0)
        assert "1 pos" in label
        assert "$250" in label

    def test_label_low_price(self):
        from trader.order_placer import position_label
        label = position_label(10.0)
        assert "3 pos" in label
        assert "$750" in label


# ── place_orders ─────────────────────────────────────────────────────────────

class TestCooldown:
    def _write_orders(self, tmp_path, days_ago: int, symbols: list):
        from datetime import date, timedelta
        d = (date.today() - timedelta(days=days_ago)).isoformat()
        data = {d: {s: {"qty": 10, "entry_price": 100.0} for s in symbols}}
        (tmp_path / "orders.json").write_text(json.dumps(data))

    def test_symbols_bought_yesterday_in_cooldown(self, tmp_path):
        self._write_orders(tmp_path, days_ago=1, symbols=["AAPL"])
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "orders.json"), \
             patch.object(__import__("config"), "ORDER_COOLDOWN_DAYS", 1):
            from trader.order_placer import _symbols_in_cooldown
            assert "AAPL" in _symbols_in_cooldown()

    def test_symbols_bought_today_not_in_cooldown(self, tmp_path):
        self._write_orders(tmp_path, days_ago=0, symbols=["AAPL"])
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "orders.json"), \
             patch.object(__import__("config"), "ORDER_COOLDOWN_DAYS", 1):
            from trader.order_placer import _symbols_in_cooldown
            assert "AAPL" not in _symbols_in_cooldown()  # today handled by _orders_today()

    def test_symbols_outside_cooldown_window_not_blocked(self, tmp_path):
        self._write_orders(tmp_path, days_ago=3, symbols=["AAPL"])
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "orders.json"), \
             patch.object(__import__("config"), "ORDER_COOLDOWN_DAYS", 1):
            from trader.order_placer import _symbols_in_cooldown
            assert "AAPL" not in _symbols_in_cooldown()

    def test_zero_cooldown_returns_empty(self, tmp_path):
        self._write_orders(tmp_path, days_ago=1, symbols=["AAPL"])
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "orders.json"), \
             patch.object(__import__("config"), "ORDER_COOLDOWN_DAYS", 0):
            from trader.order_placer import _symbols_in_cooldown
            assert _symbols_in_cooldown() == set()

    def test_place_orders_skips_cooldown_symbol(self, tmp_path):
        from datetime import date, timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        orders_file = tmp_path / "orders.json"
        orders_file.write_text(json.dumps({yesterday: {"AAPL": {"qty": 10, "entry_price": 100.0}}}))
        mock_client = MagicMock()
        mock_client.submit_order.return_value = _mock_order()
        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE", orders_file), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None):
            from trader.order_placer import place_orders
            placed = place_orders([_candidate(symbol="AAPL")])
        assert len(placed) == 0
        mock_client.submit_order.assert_not_called()


class TestPlaceOrders:
    def _run(self, candidates, already_ordered=None, orders_file=None):
        mock_client = MagicMock()
        mock_client.submit_order.return_value = _mock_order()

        if orders_file:
            today = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).date().isoformat()
            orders_file.write_text(json.dumps({today: list(already_ordered or [])}))

        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE",
                   orders_file or Path("/tmp/orders_test_noop.json")), \
             patch("trader.order_placer._record_order"):
            from trader.order_placer import place_orders
            placed = place_orders(candidates)

        return placed, mock_client

    def test_places_bracket_order_for_candidate(self, tmp_path):
        placed, client = self._run([_candidate()], orders_file=tmp_path / "o.json")
        assert len(placed) == 1
        client.submit_order.assert_called_once()

    def test_stop_and_take_profit_in_bracket_order(self, tmp_path):
        from alpaca.trading.requests import MarketOrderRequest
        placed, client = self._run([_candidate(price=25.0, atr_min=0.5, atr_max=1.0)],
                                   orders_file=tmp_path / "o.json")
        req = client.submit_order.call_args[0][0]
        assert req.stop_loss is not None
        assert req.take_profit is not None

    def test_trailing_stop_mode_uses_wide_take_profit(self, tmp_path):
        # take_profit = price + atr_max × 2
        c = _candidate(price=10.0, atr_min=0.3, atr_max=0.6, exit_mode="trailing_stop")
        placed, _ = self._run([c], orders_file=tmp_path / "o.json")
        expected_tp = round(10.0 + 0.6 * 2, 2)
        assert placed[0]["take_price"] == expected_tp

    def test_fixed_mode_uses_tight_take_profit(self, tmp_path):
        # take_profit = price + atr_max × 1
        c = _candidate(price=10.0, atr_min=0.3, atr_max=0.6, exit_mode="fixed_take_profit")
        placed, _ = self._run([c], orders_file=tmp_path / "o.json")
        expected_tp = round(10.0 + 0.6, 2)
        assert placed[0]["take_price"] == expected_tp

    def test_skips_already_ordered_symbol(self, tmp_path):
        orders_file = tmp_path / "o.json"
        placed, client = self._run(
            [_candidate(symbol="AAPL")],
            already_ordered={"AAPL"},
            orders_file=orders_file,
        )
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_api_error_is_logged_not_raised(self, tmp_path, caplog):
        mock_client = MagicMock()
        mock_client.submit_order.side_effect = Exception("API down")
        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE", tmp_path / "o.json"), \
             patch("trader.order_placer._record_order"):
            with caplog.at_level(logging.ERROR, logger="trader.orders"):
                from trader.order_placer import place_orders
                placed = place_orders([_candidate()])
        assert len(placed) == 0
        assert any("Failed" in r.message for r in caplog.records)

    def test_stop_price_capped_at_one_percent_below(self, tmp_path):
        # atr_min larger than 1% of price → stop capped at price × 0.99
        c = _candidate(price=10.0, atr_min=5.0, atr_max=10.0)
        placed, _ = self._run([c], orders_file=tmp_path / "o.json")
        assert placed[0]["stop_price"] <= round(10.0 * 0.99, 2)

    def test_respects_auto_order_top_n(self, tmp_path):
        candidates = [_candidate(symbol=f"S{i}") for i in range(20)]
        placed, client = self._run(candidates, orders_file=tmp_path / "o.json")
        import config
        assert client.submit_order.call_count <= config.AUTO_ORDER_TOP_N


# ── send_order_summary ───────────────────────────────────────────────────────

class TestSendOrderSummary:
    def test_sends_no_orders_message_when_empty(self):
        with patch("notifier.telegram._send") as mock_send:
            from trader.order_placer import send_order_summary
            send_order_summary([])
            text = mock_send.call_args[0][0]
            assert "No new orders" in text

    def test_sends_symbol_and_qty_for_each_order(self):
        orders = [{"symbol": "AAPL", "qty": 10, "stop_price": 24.0,
                   "take_price": 27.0, "exit_mode": "trailing_stop",
                   "pos_label": "1 pos · $250", "order_id": "abc"}]
        with patch("notifier.telegram._send") as mock_send:
            from trader.order_placer import send_order_summary
            send_order_summary(orders)
            text = mock_send.call_args[0][0]
            assert "AAPL" in text
            assert "x10" in text
            assert "24.00" in text
            assert "27.00" in text


# ── check_and_exit (EOD monitor) ─────────────────────────────────────────────

class TestCheckAndExit:
    def _run(self, positions, signals_map):
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = positions
        with patch("trader.position_monitor._get_client", return_value=mock_client):
            from trader.position_monitor import check_and_exit
            return check_and_exit(signals_map), mock_client

    def test_closes_on_fixed_take_profit_mode(self):
        pos = _mock_position("AAPL")
        sig = _signal("AAPL", exit_mode="fixed_take_profit", rsi=60.0)
        closed, client = self._run([pos], {"AAPL": sig})
        assert len(closed) == 1
        client.close_position.assert_called_once_with("AAPL")

    def test_closes_on_two_or_more_warnings(self):
        pos = _mock_position("MSFT")
        sig = _signal("MSFT", exit_mode="trailing_stop", rsi=60.0, warning_count=2)
        closed, client = self._run([pos], {"MSFT": sig})
        assert len(closed) == 1

    def test_closes_on_rsi_overbought(self):
        pos = _mock_position("NVDA")
        sig = _signal("NVDA", exit_mode="trailing_stop", rsi=75.0)
        closed, client = self._run([pos], {"NVDA": sig})
        assert len(closed) == 1

    def test_holds_on_clean_signal(self):
        pos = _mock_position("GOOG")
        sig = _signal("GOOG", exit_mode="trailing_stop", rsi=60.0, warning_count=0)
        closed, client = self._run([pos], {"GOOG": sig})
        assert len(closed) == 0
        client.close_position.assert_not_called()

    def test_skips_symbol_with_no_signal(self, caplog):
        pos = _mock_position("UNKNOWN")
        with caplog.at_level(logging.WARNING, logger="trader.monitor"):
            closed, client = self._run([pos], {})
        assert len(closed) == 0
        assert any("no signal data" in r.message for r in caplog.records)

    def test_handles_close_position_error(self, caplog):
        pos = _mock_position("FAIL")
        sig = _signal("FAIL", exit_mode="fixed_take_profit")
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [pos]
        mock_client.close_position.side_effect = Exception("rejected")
        with patch("trader.position_monitor._get_client", return_value=mock_client):
            with caplog.at_level(logging.ERROR, logger="trader.monitor"):
                from trader.position_monitor import check_and_exit
                closed = check_and_exit({"FAIL": sig})
        assert len(closed) == 0
        assert any("Failed to close" in r.message for r in caplog.records)

    def test_handles_position_fetch_error(self, caplog):
        mock_client = MagicMock()
        mock_client.get_all_positions.side_effect = Exception("timeout")
        with patch("trader.position_monitor._get_client", return_value=mock_client):
            with caplog.at_level(logging.ERROR, logger="trader.monitor"):
                from trader.position_monitor import check_and_exit
                closed = check_and_exit({})
        assert closed == []

    def test_one_warning_does_not_trigger_close(self):
        pos = _mock_position("SAFE")
        sig = _signal("SAFE", exit_mode="trailing_stop", rsi=60.0, warning_count=1)
        closed, client = self._run([pos], {"SAFE": sig})
        assert len(closed) == 0


# ── send_monitor_summary ─────────────────────────────────────────────────────

class TestSendMonitorSummary:
    def test_sends_none_closed_message(self):
        with patch("notifier.telegram._send") as mock_send:
            from trader.position_monitor import send_monitor_summary
            send_monitor_summary([], total_positions=3)
            text = mock_send.call_args[0][0]
            assert "3 position" in text
            assert "none closed" in text.lower()

    def test_sends_closed_position_details(self):
        closed = [{"symbol": "AAPL", "reasons": ["RSI=75.0"], "rsi": 75.0,
                   "exit_mode": "trailing_stop", "unrealized_pl": "42.50"}]
        with patch("notifier.telegram._send") as mock_send:
            from trader.position_monitor import send_monitor_summary
            send_monitor_summary(closed, total_positions=1)
            text = mock_send.call_args[0][0]
            assert "AAPL" in text
            assert "42.50" in text


# ── run_intraday_check ────────────────────────────────────────────────────────

def _make_bars(n=30, close_values=None):
    closes = close_values or [100.0] * n
    return pd.DataFrame({"close": closes, "open": closes, "high": closes, "low": closes, "volume": [1e6] * n})


class TestIntradayMonitor:
    def _run(self, positions, bars_map, rsi_values=None):
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = positions

        def fake_rsi(series, length):
            vals = rsi_values or [60.0] * 5
            return pd.Series(vals)

        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("data.alpaca_client.fetch_intraday_bars", return_value=bars_map), \
             patch("trader.intraday_monitor.ta.rsi", side_effect=fake_rsi):
            from trader.intraday_monitor import run_intraday_check
            closed = run_intraday_check()

        return closed, mock_client

    def test_no_positions_returns_empty(self):
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = []
        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client):
            from trader.intraday_monitor import run_intraday_check
            assert run_intraday_check() == []

    def test_closes_when_intraday_rsi_above_70(self):
        pos = _mock_position("AAPL")
        closed, client = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[50.0, 72.0])
        assert len(closed) == 1
        assert closed[0]["symbol"] == "AAPL"
        client.close_position.assert_called_once_with("AAPL")

    def test_holds_when_intraday_rsi_below_70(self):
        pos = _mock_position("AAPL")
        closed, client = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[50.0, 65.0])
        assert len(closed) == 0
        client.close_position.assert_not_called()

    def test_holds_exactly_at_rsi_70(self):
        pos = _mock_position("AAPL")
        closed, client = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[70.0])
        assert len(closed) == 0

    def test_skips_symbol_with_no_bar_data(self, caplog):
        pos = _mock_position("NODATA")
        with caplog.at_level(logging.WARNING, logger="trader.intraday"):
            closed, _ = self._run([pos], {})
        assert len(closed) == 0
        assert any("no 15-min bar data" in r.message for r in caplog.records)

    def test_handles_transient_position_fetch_error(self, caplog):
        mock_client = MagicMock()
        mock_client.get_all_positions.side_effect = Exception("connection refused")
        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client):
            with caplog.at_level(logging.WARNING, logger="trader.intraday"):
                from trader.intraday_monitor import run_intraday_check
                result = run_intraday_check()
        assert result == []
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_handles_real_position_fetch_error(self, caplog):
        mock_client = MagicMock()
        mock_client.get_all_positions.side_effect = Exception("403 forbidden")
        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client):
            with caplog.at_level(logging.ERROR, logger="trader.intraday"):
                from trader.intraday_monitor import run_intraday_check
                result = run_intraday_check()
        assert result == []
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_handles_close_position_error(self, caplog):
        pos = _mock_position("FAIL")
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [pos]
        mock_client.close_position.side_effect = Exception("rejected")

        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("data.alpaca_client.fetch_intraday_bars",
                   return_value={"FAIL": _make_bars()}), \
             patch("trader.intraday_monitor.ta.rsi",
                   return_value=pd.Series([75.0])):
            with caplog.at_level(logging.ERROR, logger="trader.intraday"):
                from trader.intraday_monitor import run_intraday_check
                closed = run_intraday_check()
        assert len(closed) == 0
        assert any("Failed to close" in r.message for r in caplog.records)

    def test_multiple_positions_independent(self):
        positions = [_mock_position("HIGH"), _mock_position("LOW")]
        bars = {"HIGH": _make_bars(), "LOW": _make_bars()}

        call_count = [0]
        def rsi_by_symbol(series, length):
            val = 75.0 if call_count[0] == 0 else 55.0
            call_count[0] += 1
            return pd.Series([val])

        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = positions
        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("data.alpaca_client.fetch_intraday_bars", return_value=bars), \
             patch("trader.intraday_monitor.ta.rsi", side_effect=rsi_by_symbol):
            from trader.intraday_monitor import run_intraday_check
            closed = run_intraday_check()

        assert len(closed) == 1
        assert closed[0]["symbol"] == "HIGH"


# ── send_intraday_summary ─────────────────────────────────────────────────────

class TestSendIntradaySummary:
    def test_silent_when_nothing_closed(self):
        with patch("notifier.telegram._send") as mock_send:
            from trader.intraday_monitor import send_intraday_summary
            send_intraday_summary([], total_checked=3)
            mock_send.assert_not_called()

    def test_sends_when_position_closed(self):
        closed = [{"symbol": "AAPL", "intraday_rsi": 74.5,
                   "unrealized_pl": "35.00", "reason": "15-min RSI=74.5 > 70"}]
        with patch("notifier.telegram._send") as mock_send:
            from trader.intraday_monitor import send_intraday_summary
            send_intraday_summary(closed, total_checked=1)
            mock_send.assert_called_once()
            text = mock_send.call_args[0][0]
            assert "AAPL" in text
            assert "35.00" in text
            assert "74.5" in text
