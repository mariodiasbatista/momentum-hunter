"""Tests for scheduler.py — market holiday guard."""
from unittest.mock import patch, MagicMock

import pytest

import scheduler


class TestMarketClosedToday:
    def test_returns_false_on_open_day(self):
        with patch("data.fetcher.is_market_open_today", return_value=True):
            assert scheduler._market_closed_today() is False

    def test_returns_true_on_holiday(self):
        with patch("data.fetcher.is_market_open_today", return_value=False):
            assert scheduler._market_closed_today() is True


class TestHolidayGuards:
    """Each trading job must exit early on a market holiday without placing orders."""

    def test_run_orders_skips_on_holiday(self):
        with patch("scheduler._market_closed_today", return_value=True), \
             patch("data.db.load_signals") as mock_load, \
             patch("trader.order_placer.place_orders") as mock_place:
            scheduler.run_orders()
            mock_load.assert_not_called()
            mock_place.assert_not_called()

    def test_run_premarket_skips_on_holiday(self):
        with patch("scheduler._market_closed_today", return_value=True), \
             patch("trader.premarket_validator.validate") as mock_validate:
            scheduler.run_premarket()
            mock_validate.assert_not_called()

    def test_run_intraday_monitor_skips_on_holiday(self):
        with patch("scheduler._market_closed_today", return_value=True), \
             patch("trader.intraday_monitor.run_intraday_check") as mock_check:
            scheduler.run_intraday_monitor()
            mock_check.assert_not_called()

    def test_run_monitor_skips_on_holiday(self):
        with patch("scheduler._market_closed_today", return_value=True), \
             patch("trader.position_monitor.check_and_exit") as mock_exit:
            scheduler.run_monitor()
            mock_exit.assert_not_called()

    def test_run_orders_proceeds_on_trading_day(self):
        mock_candidates = [MagicMock()]
        with patch("scheduler._market_closed_today", return_value=False), \
             patch("data.db.load_signals", return_value=mock_candidates), \
             patch("data.db.signal_persistence", return_value={}), \
             patch("trader.order_placer.place_orders", return_value=[]) as mock_place, \
             patch("trader.order_placer.send_order_summary"):
            scheduler.run_orders()
            mock_place.assert_called_once()
