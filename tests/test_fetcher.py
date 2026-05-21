"""Tests for data/fetcher.py — NYSE trading calendar and cache freshness."""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from data.fetcher import _last_trading_date, _cache_is_fresh


class TestLastTradingDate:
    def test_returns_string_in_iso_format(self):
        result = _last_trading_date()
        # Should parse without error
        date.fromisoformat(result)

    def test_not_in_future(self):
        result = _last_trading_date()
        assert result <= date.today().isoformat()

    def test_skips_weekend(self):
        # Simulate calling on a Saturday — last trading day should be Friday
        saturday = date(2026, 5, 16)
        friday = date(2026, 5, 15)
        import pandas_market_calendars as mcal
        nyse = mcal.get_calendar("NYSE")
        schedule = nyse.schedule(
            start_date=(saturday - timedelta(days=10)).isoformat(),
            end_date=friday.isoformat(),  # schedule only goes up to Friday
        )
        with patch("data.fetcher._nyse") as mock_cal, \
             patch("data.fetcher.date") as mock_date:
            mock_date.today.return_value = saturday
            mock_cal.schedule.return_value = schedule
            result = _last_trading_date()
        assert result == "2026-05-15"

    def test_returns_today_if_market_open_day(self):
        # 2026-05-21 is a Thursday (trading day)
        trading_day = date(2026, 5, 21)
        with patch("data.fetcher._nyse") as mock_cal:
            import pandas as pd
            import pandas_market_calendars as mcal
            nyse = mcal.get_calendar("NYSE")
            schedule = nyse.schedule(
                start_date=(trading_day - timedelta(days=10)).isoformat(),
                end_date=trading_day.isoformat(),
            )
            mock_cal.schedule.return_value = schedule
            result = _last_trading_date()
        assert result == "2026-05-21"


class TestCacheIsFresh:
    def test_fresh_when_bars_match_last_trading_date(self):
        with patch("data.fetcher.bars_last_date", return_value="2026-05-21"), \
             patch("data.fetcher._last_trading_date", return_value="2026-05-21"):
            assert _cache_is_fresh() is True

    def test_stale_when_bars_behind_last_trading_date(self):
        with patch("data.fetcher.bars_last_date", return_value="2026-05-19"), \
             patch("data.fetcher._last_trading_date", return_value="2026-05-21"):
            assert _cache_is_fresh() is False

    def test_stale_when_no_bars(self):
        with patch("data.fetcher.bars_last_date", return_value=None):
            assert _cache_is_fresh() is False

    def test_fresh_on_weekend_with_friday_bars(self):
        # Saturday — last trading day is Friday
        with patch("data.fetcher.bars_last_date", return_value="2026-05-15"), \
             patch("data.fetcher._last_trading_date", return_value="2026-05-15"):
            assert _cache_is_fresh() is True
