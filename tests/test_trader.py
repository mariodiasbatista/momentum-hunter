"""
Tests for the full trade execution layer:
  - trader/order_placer.py   — position sizing, bracket order placement, dedup
  - trader/position_monitor.py — EOD exit logic (fixed TP, warnings, RSI)
  - trader/intraday_monitor.py — intraday 15-min RSI exit logic
"""
import json
import logging
import math
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

def _mock_position(symbol="AAPL", unrealized_pl="50.00", unrealized_plpc="0.0",
                   current_price="105.0", avg_entry_price="100.0", qty="10"):
    pos = MagicMock()
    pos.symbol = symbol
    pos.unrealized_pl = unrealized_pl
    pos.unrealized_plpc = unrealized_plpc
    pos.current_price = current_price
    pos.avg_entry_price = avg_entry_price
    pos.qty = qty
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
    adx=32.0,
    warning_count=0,
):
    warnings = ["warning signal"] * warning_count
    return {
        "symbol": symbol,
        "market": "stocks",
        "score": 8,
        "days_in_scan": 2,
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
            "adx": adx, "adx_strong": adx >= 30, "adx_falling": False, "atr": atr_min / 1.5,
            "roc_pass": True,
        },
        "volume": {
            "volume": 1_000_000, "avg_volume": 800_000,
            "volume_ratio": 1.25, "volume_drying_up": False, "volume_above_avg": True,
        },
        "relative_strength": {"rs_return": 20.0, "spy_return": 5.0, "outperforming_spy": True, "dual_rs": True},
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

    def test_open_position_within_cooldown_is_blocked(self, tmp_path):
        self._write_orders(tmp_path, days_ago=1, symbols=["AAPL"])
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "orders.json"), \
             patch.object(__import__("config"), "ORDER_COOLDOWN_DAYS", 1):
            from trader.order_placer import _open_positions_in_cooldown
            assert "AAPL" in _open_positions_in_cooldown({"AAPL"})

    def test_closed_position_not_blocked_even_within_cooldown(self, tmp_path):
        # AAPL bought yesterday but position is now closed — not in open_symbols
        self._write_orders(tmp_path, days_ago=1, symbols=["AAPL"])
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "orders.json"), \
             patch.object(__import__("config"), "ORDER_COOLDOWN_DAYS", 1):
            from trader.order_placer import _open_positions_in_cooldown
            assert "AAPL" not in _open_positions_in_cooldown(set())  # not open

    def test_open_position_always_blocked_regardless_of_age(self, tmp_path):
        # Any open position blocks re-entry regardless of how long it's been held
        self._write_orders(tmp_path, days_ago=3, symbols=["AAPL"])
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "orders.json"), \
             patch.object(__import__("config"), "ORDER_COOLDOWN_DAYS", 1):
            from trader.order_placer import _open_positions_in_cooldown
            assert "AAPL" in _open_positions_in_cooldown({"AAPL"})

    def test_open_position_blocked_regardless_of_cooldown_setting(self, tmp_path):
        # cooldown=0 does not unblock currently-open positions
        self._write_orders(tmp_path, days_ago=1, symbols=["AAPL"])
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "orders.json"), \
             patch.object(__import__("config"), "ORDER_COOLDOWN_DAYS", 0):
            from trader.order_placer import _open_positions_in_cooldown
            assert "AAPL" in _open_positions_in_cooldown({"AAPL"})

    def test_place_orders_skips_open_position_in_cooldown(self, tmp_path):
        from datetime import date, timedelta
        today = date.today().isoformat()
        orders_file = tmp_path / "orders.json"
        orders_file.write_text(json.dumps({today: {"AAPL": {"qty": 10, "entry_price": 100.0}}}))
        mock_client = MagicMock()
        mock_client.submit_order.return_value = _mock_order()
        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_client.get_all_positions.return_value = [mock_pos]
        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE", orders_file), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("trader.order_placer._get_spy_open_return", return_value=None):
            from trader.order_placer import place_orders
            placed = place_orders([_candidate(symbol="AAPL")])
        assert len(placed) == 0
        mock_client.submit_order.assert_not_called()

    def test_place_orders_blocks_any_open_position_regardless_of_age(self, tmp_path):
        from datetime import date, timedelta
        # Bought 5 days ago but still open — re-entry is always blocked while position is open
        old_date = (date.today() - timedelta(days=5)).isoformat()
        orders_file = tmp_path / "orders.json"
        orders_file.write_text(json.dumps({old_date: {"AAPL": {"qty": 10, "entry_price": 100.0}}}))
        mock_client = MagicMock()
        mock_client.submit_order.return_value = _mock_order()
        mock_pos = MagicMock()
        mock_pos.symbol = "AAPL"
        mock_client.get_all_positions.return_value = [mock_pos]
        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE", orders_file), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("trader.order_placer._get_spy_open_return", return_value=None):
            from trader.order_placer import place_orders
            placed = place_orders([_candidate(symbol="AAPL")])
        assert len(placed) == 0
        mock_client.submit_order.assert_not_called()


class TestPlaceOrders:
    def _run(self, candidates, already_ordered=None, orders_file=None, asks=None):
        mock_client = MagicMock()
        mock_client.submit_order.return_value = _mock_order()

        if orders_file:
            today = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).date().isoformat()
            orders_file.write_text(json.dumps({today: list(already_ordered or [])}))

        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE",
                   orders_file or Path("/tmp/orders_test_noop.json")), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("data.alpaca_client.fetch_latest_asks", return_value=asks or {}), \
             patch("trader.order_placer._get_spy_open_return", return_value=None):
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
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("data.alpaca_client.fetch_latest_asks", return_value={}):
            with caplog.at_level(logging.ERROR, logger="trader.orders"):
                from trader.order_placer import place_orders
                placed = place_orders([_candidate()])
        assert len(placed) == 0
        assert any("Failed" in r.message for r in caplog.records)

    def test_stop_price_capped_at_one_percent_below(self, tmp_path):
        # ATR too small to provide 1% cushion → stop floors at price × 0.99
        # price=100, atr_min=0.05 → raw stop 99.94 > 99.00 → capped at 99.00
        # stop_distance_pct = 0.05% well within volatility filter
        c = _candidate(price=100.0, atr_min=0.05, atr_max=0.10)
        placed, _ = self._run([c], orders_file=tmp_path / "o.json")
        assert placed[0]["stop_price"] == pytest.approx(99.00)

    def test_respects_auto_order_top_n(self, tmp_path):
        candidates = [_candidate(symbol=f"S{i}") for i in range(20)]
        placed, client = self._run(candidates, orders_file=tmp_path / "o.json")
        import config
        assert client.submit_order.call_count <= config.AUTO_ORDER_TOP_N

    def test_continues_past_filtered_candidates_to_fill_order_limit(self, tmp_path):
        # First 8 candidates are fading (fixed_take_profit) — old code would stop at 10
        # and only place 2. New code should walk past them and place up to AUTO_ORDER_TOP_N.
        fading   = [_candidate(symbol=f"F{i}", exit_mode="fixed_take_profit") for i in range(8)]
        good     = [_candidate(symbol=f"G{i}") for i in range(6)]
        placed, client = self._run(fading + good, orders_file=tmp_path / "o.json")
        assert len(placed) == 6  # all 6 good ones placed (below AUTO_ORDER_TOP_N cap)
        assert client.submit_order.call_count == 6

    def test_stops_at_order_limit_even_with_more_valid_candidates(self, tmp_path):
        # 20 valid candidates — must stop at AUTO_ORDER_TOP_N regardless
        import config
        candidates = [_candidate(symbol=f"S{i}") for i in range(20)]
        placed, client = self._run(candidates, orders_file=tmp_path / "o.json")
        assert len(placed) == config.AUTO_ORDER_TOP_N
        assert client.submit_order.call_count == config.AUTO_ORDER_TOP_N

    def test_skips_fixed_take_profit_mode(self, tmp_path):
        c = _candidate(exit_mode="fixed_take_profit")
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_skips_weak_adx(self, tmp_path):
        c = _candidate(adx=24.0)
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_skips_volume_drying_up(self, tmp_path):
        c = _candidate()
        c["volume"]["volume_drying_up"] = True
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_places_order_when_adx_meets_threshold(self, tmp_path):
        c = _candidate(adx=30.0)
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 1

    def test_skips_macd_histogram_shrinking(self, tmp_path):
        c = _candidate()
        c["momentum"]["macd_histogram_shrinking"] = True
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_places_order_when_macd_building(self, tmp_path):
        c = _candidate()
        c["momentum"]["macd_histogram_shrinking"] = False
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 1

    def test_skips_when_dual_rs_fails(self, tmp_path):
        c = _candidate()
        c["relative_strength"]["dual_rs"] = False
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_skips_when_roc_pass_fails(self, tmp_path):
        c = _candidate()
        c["momentum"]["roc_pass"] = False
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_skips_when_gapped_up_above_threshold(self, tmp_path):
        # price=25.0, last_close=25.0 → fetch returns ask 26.1 → gap 4.4% > 4%
        c = _candidate(price=25.0)
        placed, client = self._run(
            [c], orders_file=tmp_path / "o.json",
            asks={"AAPL": 26.1},
        )
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_places_order_when_gap_within_threshold(self, tmp_path):
        # price=25.0, ask=25.9 → gap 3.6% < 4%
        c = _candidate(price=25.0)
        placed, client = self._run(
            [c], orders_file=tmp_path / "o.json",
            asks={"AAPL": 25.9},
        )
        assert len(placed) == 1

    def test_skips_when_stop_distance_too_volatile(self, tmp_path):
        # price=5.36, atr_min=1.71 → stop distance 31.9% > 15% cap (CUPR-like micro-cap)
        c = _candidate(price=5.36, atr_min=1.71)
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_places_order_when_stop_distance_within_limit(self, tmp_path):
        # price=25.0, atr_min=3.0 → stop distance 12% < 15% cap
        c = _candidate(price=25.0, atr_min=3.0)
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 1

    def test_spy_bear_day_skips_all_orders(self, tmp_path):
        from unittest.mock import patch
        with patch("trader.order_placer._get_spy_open_return", return_value=-1.2), \
             patch("trader.order_placer._get_client", return_value=MagicMock()), \
             patch("trader.order_placer._ORDERS_FILE", tmp_path / "o.json"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("data.alpaca_client.fetch_latest_asks", return_value={}), \
             patch("notifier.telegram._send"):
            from trader.order_placer import place_orders
            placed = place_orders([_candidate(), _candidate(symbol="NVDA")])
        assert placed == []

    def test_spy_flat_day_allows_orders(self, tmp_path):
        c = _candidate()
        c["momentum"]["macd_histogram_shrinking"] = False
        # _run already mocks _get_spy_open_return to return None (no block)
        placed, client = self._run([c], orders_file=tmp_path / "o.json")
        assert len(placed) == 1

    def test_spy_near_threshold_does_not_block(self, tmp_path):
        with patch("trader.order_placer._get_spy_open_return", return_value=-0.4), \
             patch("trader.order_placer._get_client", return_value=MagicMock(
                 submit_order=MagicMock(return_value=_mock_order()),
                 get_all_positions=MagicMock(return_value=[]))), \
             patch("trader.order_placer._ORDERS_FILE", tmp_path / "o.json"), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("data.alpaca_client.fetch_latest_asks", return_value={}):
            from trader.order_placer import place_orders
            placed = place_orders([_candidate()])
        assert len(placed) == 1

    def test_retries_with_adjusted_stop_on_42210000(self, tmp_path):
        # fill=25.00, atr_min=0.5 → raw_atr=0.333, 2% of fill=0.50
        # buffer = max(0.333, 0.50) = 0.50 → stop = 25.00 - 0.50 = 24.50
        error_msg = '{"base_price":"25.00","code":42210000,"message":"stop too high"}'
        retry_order = _mock_order()
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = []
        mock_client.submit_order.side_effect = [Exception(error_msg), retry_order]
        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE", tmp_path / "o.json"), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("data.alpaca_client.fetch_latest_asks", return_value={}), \
             patch("trader.order_placer._get_spy_open_return", return_value=None):
            from trader.order_placer import place_orders
            placed = place_orders([_candidate(price=25.0, atr_min=0.5)])
        assert len(placed) == 1
        assert mock_client.submit_order.call_count == 2
        assert placed[0]["stop_price"] == pytest.approx(24.50)  # fill(25.00) - max(ATR×1.0=0.33, 2%=0.50)

    def test_retries_with_atr_buffer_when_atr_exceeds_two_pct(self, tmp_path):
        # fill=25.00, atr_min=1.5 → raw_atr=1.0, 2% of fill=0.50
        # buffer = max(1.0, 0.50) = 1.0 → stop = 25.00 - 1.0 = 24.00
        error_msg = '{"base_price":"25.00","code":42210000,"message":"stop too high"}'
        retry_order = _mock_order()
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = []
        mock_client.submit_order.side_effect = [Exception(error_msg), retry_order]
        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE", tmp_path / "o.json"), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("data.alpaca_client.fetch_latest_asks", return_value={}), \
             patch("trader.order_placer._get_spy_open_return", return_value=None):
            from trader.order_placer import place_orders
            placed = place_orders([_candidate(price=25.0, atr_min=1.5)])
        assert len(placed) == 1
        assert placed[0]["stop_price"] == pytest.approx(24.00)  # fill(25.00) - max(ATR×1.0=1.0, 2%=0.50)

    def test_retry_failure_skips_symbol(self, tmp_path, caplog):
        error_msg = '{"base_price":"25.00","code":42210000,"message":"stop too high"}'
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = []
        mock_client.submit_order.side_effect = [Exception(error_msg), Exception("rejected again")]
        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE", tmp_path / "o.json"), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("data.alpaca_client.fetch_latest_asks", return_value={}), \
             patch("trader.order_placer._get_spy_open_return", return_value=None):
            with caplog.at_level(logging.ERROR, logger="trader.orders"):
                from trader.order_placer import place_orders
                placed = place_orders([_candidate(price=25.0)])
        assert len(placed) == 0


# ── _parse_fill_from_stop_error ───────────────────────────────────────────────

class TestParseFilFromStopError:
    def _call(self, msg):
        from trader.order_placer import _parse_fill_from_stop_error
        return _parse_fill_from_stop_error(Exception(msg))

    def test_returns_none_for_non_42210000_error(self):
        assert self._call("some other error 500") is None

    def test_returns_none_when_no_base_price_in_body(self):
        assert self._call("{code:42210000,message:stop too high}") is None

    def test_returns_fill_price_with_double_quotes(self):
        result = self._call('{"base_price":"108.64","code":42210000}')
        assert result == pytest.approx(108.64)

    def test_returns_fill_price_without_quotes(self):
        result = self._call('{"base_price":108.64,"code":42210000}')
        assert result == pytest.approx(108.64)

    def test_parses_baseprice_key_variant(self):
        result = self._call('{"baseprice":"18.5424","code":42210000}')
        assert result == pytest.approx(18.5424)

    def test_returns_raw_fill_not_adjusted(self):
        result = self._call('{"base_price":"50.00","code":42210000}')
        assert result == pytest.approx(50.00)  # caller applies ATR/pct buffer


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
        mock_client.get_orders.return_value = []
        with patch("trader.position_monitor._get_client", return_value=mock_client), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]), \
             patch("trader.trade_recorder.record_manual_close", return_value={}):
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
        mock_client.get_orders.return_value = []
        mock_client.close_position.side_effect = Exception("rejected")
        with patch("trader.position_monitor._get_client", return_value=mock_client), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]), \
             patch("trader.trade_recorder.record_manual_close", return_value={}):
            with caplog.at_level(logging.ERROR, logger="trader.monitor"):
                from trader.position_monitor import check_and_exit
                closed = check_and_exit({"FAIL": sig})
        assert len(closed) == 0
        assert any("Failed to close" in r.message for r in caplog.records)

    def test_handles_position_fetch_error(self, caplog):
        mock_client = MagicMock()
        mock_client.get_all_positions.side_effect = Exception("timeout")
        with patch("trader.position_monitor._get_client", return_value=mock_client), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]):
            with caplog.at_level(logging.ERROR, logger="trader.monitor"):
                from trader.position_monitor import check_and_exit
                closed = check_and_exit({})
        assert closed == []

    def test_one_warning_does_not_trigger_close(self):
        pos = _mock_position("SAFE")
        sig = _signal("SAFE", exit_mode="trailing_stop", rsi=60.0, warning_count=1)
        closed, client = self._run([pos], {"SAFE": sig})
        assert len(closed) == 0

    def test_closes_on_max_loss_exceeded(self):
        pos = _mock_position("AAPL", unrealized_plpc="-0.06")  # -6% > MAX_LOSS_PCT=5%
        sig = _signal("AAPL", exit_mode="trailing_stop", rsi=60.0)
        closed, _ = self._run([pos], {"AAPL": sig})
        assert len(closed) == 1
        assert any("loss" in r.lower() for r in closed[0]["reasons"])

    def test_closes_on_min_gain_at_eod(self):
        pos = _mock_position("AAPL", unrealized_plpc="0.09")  # 9% >= MIN_GAIN_TAKE_PCT=8%
        sig = _signal("AAPL", exit_mode="trailing_stop", rsi=60.0)
        closed, _ = self._run([pos], {"AAPL": sig})
        assert len(closed) == 1
        assert any("gain" in r.lower() for r in closed[0]["reasons"])


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
        mock_client.get_orders.return_value = []

        def fake_rsi(series, length):
            vals = rsi_values or [60.0] * 5
            return pd.Series(vals)

        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("data.alpaca_client.fetch_intraday_bars", return_value=bars_map), \
             patch("trader.intraday_monitor.ta.rsi", side_effect=fake_rsi), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]), \
             patch("trader.trade_recorder.record_manual_close", return_value={}):
            from trader.intraday_monitor import run_intraday_check
            closed = run_intraday_check()

        return closed, mock_client

    def test_no_positions_returns_empty(self):
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = []
        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]):
            from trader.intraday_monitor import run_intraday_check
            assert run_intraday_check() == []

    def test_closes_when_intraday_rsi_above_threshold(self):
        # RSI_OVERBOUGHT = 65; RSI=72 > 65 → close
        pos = _mock_position("AAPL")
        closed, client = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[50.0, 72.0])
        assert len(closed) == 1
        assert closed[0]["symbol"] == "AAPL"
        client.close_position.assert_called_once_with("AAPL")

    def test_closes_when_intraday_rsi_between_65_and_70(self):
        # RSI_OVERBOUGHT changed from 70 → 65; RSI=68 now triggers exit
        pos = _mock_position("AAPL")
        closed, client = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[68.0])
        assert len(closed) == 1

    def test_holds_when_intraday_rsi_below_threshold(self):
        # RSI=64 < 65 → hold
        pos = _mock_position("AAPL")
        closed, client = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[50.0, 64.0])
        assert len(closed) == 0
        client.close_position.assert_not_called()

    def test_holds_exactly_at_rsi_overbought_threshold(self):
        # RSI=65 is NOT strictly > 65 → hold (threshold is exclusive)
        pos = _mock_position("AAPL")
        closed, client = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[65.0])
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
        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]):
            with caplog.at_level(logging.WARNING, logger="trader.intraday"):
                from trader.intraday_monitor import run_intraday_check
                result = run_intraday_check()
        assert result == []
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_handles_real_position_fetch_error(self, caplog):
        mock_client = MagicMock()
        mock_client.get_all_positions.side_effect = Exception("403 forbidden")
        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]):
            with caplog.at_level(logging.ERROR, logger="trader.intraday"):
                from trader.intraday_monitor import run_intraday_check
                result = run_intraday_check()
        assert result == []
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_handles_close_position_error(self, caplog):
        pos = _mock_position("FAIL")
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [pos]
        mock_client.get_orders.return_value = []
        mock_client.close_position.side_effect = Exception("rejected")

        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("data.alpaca_client.fetch_intraday_bars",
                   return_value={"FAIL": _make_bars()}), \
             patch("trader.intraday_monitor.ta.rsi",
                   return_value=pd.Series([75.0])), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]), \
             patch("trader.trade_recorder.record_manual_close", return_value={}):
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
        mock_client.get_orders.return_value = []
        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("data.alpaca_client.fetch_intraday_bars", return_value=bars), \
             patch("trader.intraday_monitor.ta.rsi", side_effect=rsi_by_symbol), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]), \
             patch("trader.trade_recorder.record_manual_close", return_value={}):
            from trader.intraday_monitor import run_intraday_check
            closed = run_intraday_check()

        assert len(closed) == 1
        assert closed[0]["symbol"] == "HIGH"

    def test_closes_on_max_loss_exceeded(self):
        pos = _mock_position("AAPL", unrealized_plpc="-0.06")  # -6% > MAX_LOSS_PCT=5%
        closed, _ = self._run([pos], {"AAPL": _make_bars()})
        assert len(closed) == 1
        assert "loss" in closed[0]["reason"]

    def test_holds_on_min_gain_with_rising_rsi(self):
        pos = _mock_position("AAPL", unrealized_plpc="0.09")  # 9% >= MIN_GAIN_TAKE_PCT=8%
        # RSI >= 50 → hold (momentum still good)
        closed, _ = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[55.0])
        assert len(closed) == 0

    def test_closes_on_min_gain_with_fading_momentum(self):
        pos = _mock_position("AAPL", unrealized_plpc="0.09")  # 9% >= MIN_GAIN_TAKE_PCT=8%
        # RSI < 50 → close (momentum fading)
        closed, _ = self._run([pos], {"AAPL": _make_bars()}, rsi_values=[45.0])
        assert len(closed) == 1
        assert "fading momentum" in closed[0]["reason"]


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


# ── cancel_open_orders ────────────────────────────────────────────────────────

class TestCancelOpenOrders:
    def _make_order(self, order_id="ord-1"):
        o = MagicMock()
        o.id = order_id
        return o

    def test_cancels_all_open_orders(self):
        mock_client = MagicMock()
        mock_client.get_orders.return_value = [self._make_order("a"), self._make_order("b")]
        from trader._utils import cancel_open_orders
        count = cancel_open_orders(mock_client, "AAPL")
        assert count == 2
        assert mock_client.cancel_order_by_id.call_count == 2

    def test_returns_zero_when_no_open_orders(self):
        mock_client = MagicMock()
        mock_client.get_orders.return_value = []
        from trader._utils import cancel_open_orders
        count = cancel_open_orders(mock_client, "AAPL")
        assert count == 0
        mock_client.cancel_order_by_id.assert_not_called()

    def test_handles_cancel_error_gracefully(self):
        mock_client = MagicMock()
        mock_client.get_orders.return_value = [self._make_order()]
        mock_client.cancel_order_by_id.side_effect = Exception("already cancelled")
        from trader._utils import cancel_open_orders
        count = cancel_open_orders(mock_client, "AAPL")  # must not raise
        assert count == 1  # order was in the list

    def test_handles_get_orders_error_gracefully(self):
        mock_client = MagicMock()
        mock_client.get_orders.side_effect = Exception("timeout")
        from trader._utils import cancel_open_orders
        count = cancel_open_orders(mock_client, "AAPL")
        assert count == 0


# ── close_position_with_retry ─────────────────────────────────────────────────

class TestClosePositionWithRetry:
    def test_cancels_orders_then_closes(self):
        mock_client = MagicMock()
        mock_client.get_orders.return_value = []
        from trader._utils import close_position_with_retry
        close_position_with_retry(mock_client, "AAPL")
        mock_client.close_position.assert_called_once_with("AAPL")

    def test_retries_once_on_insufficient_qty(self):
        mock_client = MagicMock()
        mock_client.get_orders.return_value = []
        mock_client.close_position.side_effect = [
            Exception("insufficient qty available"), None
        ]
        with patch("time.sleep"):
            from trader._utils import close_position_with_retry
            close_position_with_retry(mock_client, "AAPL")
        assert mock_client.close_position.call_count == 2

    def test_raises_on_non_retryable_error(self):
        mock_client = MagicMock()
        mock_client.get_orders.return_value = []
        mock_client.close_position.side_effect = Exception("position not found")
        from trader._utils import close_position_with_retry
        with pytest.raises(Exception, match="position not found"):
            close_position_with_retry(mock_client, "AAPL")

    def test_cancels_bracket_legs_before_close(self):
        o = MagicMock()
        o.id = "bracket-leg-1"
        mock_client = MagicMock()
        mock_client.get_orders.return_value = [o]
        from trader._utils import close_position_with_retry
        close_position_with_retry(mock_client, "AAPL")
        mock_client.cancel_order_by_id.assert_called_once_with("bracket-leg-1")
        mock_client.close_position.assert_called_once_with("AAPL")


# ── load_entry_date_for_symbol ────────────────────────────────────────────────

class TestLoadEntryDateForSymbol:
    def _write_orders(self, tmp_path, data: dict):
        f = tmp_path / "orders.json"
        f.write_text(json.dumps(data))
        return f

    def test_returns_none_when_file_missing(self, tmp_path):
        with patch("trader.order_placer._ORDERS_FILE", tmp_path / "missing.json"):
            from trader.order_placer import load_entry_date_for_symbol
            assert load_entry_date_for_symbol("AAPL") is None

    def test_returns_none_when_symbol_not_found(self, tmp_path):
        f = self._write_orders(tmp_path, {"2026-06-01": {"MSFT": {"qty": 1}}})
        with patch("trader.order_placer._ORDERS_FILE", f):
            from trader.order_placer import load_entry_date_for_symbol
            assert load_entry_date_for_symbol("AAPL") is None

    def test_returns_date_of_entry(self, tmp_path):
        f = self._write_orders(tmp_path, {"2026-06-01": {"AAPL": {"qty": 5}}})
        with patch("trader.order_placer._ORDERS_FILE", f):
            from trader.order_placer import load_entry_date_for_symbol
            assert load_entry_date_for_symbol("AAPL") == "2026-06-01"

    def test_returns_most_recent_date_across_multiple_days(self, tmp_path):
        f = self._write_orders(tmp_path, {
            "2026-05-20": {"AAPL": {"qty": 5}},
            "2026-06-01": {"AAPL": {"qty": 3}},
        })
        with patch("trader.order_placer._ORDERS_FILE", f):
            from trader.order_placer import load_entry_date_for_symbol
            assert load_entry_date_for_symbol("AAPL") == "2026-06-01"

    def test_returns_correct_symbol_among_multiple(self, tmp_path):
        f = self._write_orders(tmp_path, {
            "2026-06-10": {"AAPL": {"qty": 1}, "NVDA": {"qty": 2}},
        })
        with patch("trader.order_placer._ORDERS_FILE", f):
            from trader.order_placer import load_entry_date_for_symbol
            assert load_entry_date_for_symbol("NVDA") == "2026-06-10"
            assert load_entry_date_for_symbol("AAPL") == "2026-06-10"


# ── days_in_scan filter in place_orders ───────────────────────────────────────

class TestDaysInScanFilter:
    def _run(self, candidate, orders_file):
        mock_client = MagicMock()
        mock_client.submit_order.return_value = _mock_order()
        mock_client.get_all_positions.return_value = []
        with patch("trader.order_placer._get_client", return_value=mock_client), \
             patch("trader.order_placer._ORDERS_FILE", orders_file), \
             patch("trader.order_placer._record_order"), \
             patch("trader.premarket_validator.load_approved_today", return_value=None), \
             patch("data.alpaca_client.fetch_latest_asks", return_value={}), \
             patch("trader.order_placer._get_spy_open_return", return_value=None):
            from trader.order_placer import place_orders
            placed = place_orders([candidate])
        return placed, mock_client

    def test_skips_candidate_with_one_day_in_scan(self, tmp_path):
        c = _candidate()
        c["days_in_scan"] = 1
        placed, client = self._run(c, tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()

    def test_places_order_for_candidate_with_two_days_in_scan(self, tmp_path):
        c = _candidate()
        c["days_in_scan"] = 2
        placed, client = self._run(c, tmp_path / "o.json")
        assert len(placed) == 1

    def test_places_order_for_candidate_with_many_days_in_scan(self, tmp_path):
        c = _candidate()
        c["days_in_scan"] = 5
        placed, client = self._run(c, tmp_path / "o.json")
        assert len(placed) == 1

    def test_missing_days_in_scan_defaults_to_one_and_skips(self, tmp_path):
        c = _candidate()
        del c["days_in_scan"]
        placed, client = self._run(c, tmp_path / "o.json")
        assert len(placed) == 0
        client.submit_order.assert_not_called()


# ── max-hold exit in intraday monitor ────────────────────────────────────────

class TestIntradayMaxHold:
    def _run_with_entry_date(self, entry_date_str, rsi_values=None):
        pos = _mock_position("AAPL", unrealized_plpc="0.02")
        mock_client = MagicMock()
        mock_client.get_all_positions.return_value = [pos]
        mock_client.get_orders.return_value = []

        def fake_rsi(series, length):
            return pd.Series(rsi_values or [55.0])

        with patch("trader.intraday_monitor._get_trading_client", return_value=mock_client), \
             patch("data.alpaca_client.fetch_intraday_bars",
                   return_value={"AAPL": _make_bars()}), \
             patch("trader.intraday_monitor.ta.rsi", side_effect=fake_rsi), \
             patch("trader.trade_recorder.scan_for_fills", return_value=[]), \
             patch("trader.trade_recorder.record_manual_close", return_value={}), \
             patch("trader.order_placer.load_entry_date_for_symbol",
                   return_value=entry_date_str):
            from trader.intraday_monitor import run_intraday_check
            closed = run_intraday_check()
        return closed, mock_client

    def test_closes_position_held_at_max_hold_days(self):
        from datetime import date, timedelta
        import config
        entry = (date.today() - timedelta(days=config.MAX_HOLD_DAYS)).isoformat()
        closed, client = self._run_with_entry_date(entry)
        assert len(closed) == 1
        assert "max hold" in closed[0]["reason"]
        client.close_position.assert_called_once_with("AAPL")

    def test_closes_position_held_beyond_max_hold_days(self):
        from datetime import date, timedelta
        import config
        entry = (date.today() - timedelta(days=config.MAX_HOLD_DAYS + 3)).isoformat()
        closed, client = self._run_with_entry_date(entry)
        assert len(closed) == 1
        assert "max hold" in closed[0]["reason"]

    def test_holds_position_under_max_hold_days(self):
        from datetime import date, timedelta
        import config
        entry = (date.today() - timedelta(days=config.MAX_HOLD_DAYS - 1)).isoformat()
        closed, client = self._run_with_entry_date(entry)
        assert len(closed) == 0
        client.close_position.assert_not_called()

    def test_holds_position_entered_today(self):
        from datetime import date
        entry = date.today().isoformat()
        closed, client = self._run_with_entry_date(entry)
        assert len(closed) == 0

    def test_max_hold_takes_priority_over_rsi_exit(self):
        # Position at max hold days AND RSI below threshold — max hold fires first
        from datetime import date, timedelta
        import config
        entry = (date.today() - timedelta(days=config.MAX_HOLD_DAYS)).isoformat()
        closed, client = self._run_with_entry_date(entry, rsi_values=[55.0])
        assert len(closed) == 1
        assert "max hold" in closed[0]["reason"]

    def test_skips_max_hold_when_no_entry_date_recorded(self):
        # No entry date in orders file → max-hold check skipped, RSI check proceeds
        closed, client = self._run_with_entry_date(None, rsi_values=[55.0])
        assert len(closed) == 0  # RSI=55 < 65, no other exit → holds
