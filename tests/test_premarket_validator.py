"""
Tests for trader/premarket_validator.py — pre-market gap and trend validation.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _candidate(symbol="AAPL", last_close=100.0, sma50=90.0, sma200=80.0, atr=2.0):
    return {
        "symbol": symbol,
        "trend": {
            "last_close": last_close,
            "sma50": sma50,
            "sma200": sma200,
            "ema9": last_close * 1.01,
            "ema21": last_close * 0.99,
        },
        "momentum": {"rsi": 60.0, "adx": 30.0, "atr": atr},
        "volume": {"volume_ratio": 1.3},
        "relative_strength": {"rs_return": 15.0, "spy_return": 5.0},
        "exit": {"exit_mode": "trailing_stop",
                 "trailing_stop_atr_range": (atr * 1.5, atr * 3.0),
                 "warning_count": 0, "warnings": []},
        "score": 8,
    }


def _run_validate(candidates, prices, tmp_path):
    with patch("data.alpaca_client.fetch_latest_prices", return_value=prices), \
         patch("trader.premarket_validator._FILTER_FILE", tmp_path / "filter.json"):
        from trader.premarket_validator import validate
        return validate(candidates)


class TestValidateKeep:
    def test_keeps_healthy_symbol(self, tmp_path):
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=80.0)
        result = _run_validate([c], {"AAPL": 101.0}, tmp_path)
        assert "AAPL" in result["approved"]
        assert "AAPL" not in result["dropped"]

    def test_keeps_small_gap_down(self, tmp_path):
        # Gap down 1% — within ATR×2 range, still above SMAs
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=80.0, atr=2.0)
        result = _run_validate([c], {"AAPL": 99.0}, tmp_path)
        assert "AAPL" in result["approved"]


class TestValidateDrop:
    def test_drops_when_below_sma50(self, tmp_path):
        c = _candidate("AAPL", last_close=100.0, sma50=105.0, sma200=80.0)
        result = _run_validate([c], {"AAPL": 102.0}, tmp_path)
        assert "AAPL" in result["dropped"]
        assert "AAPL" not in result["approved"]
        assert "SMA50" in result["dropped"]["AAPL"]["reason"]

    def test_drops_when_below_sma200(self, tmp_path):
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=105.0)
        result = _run_validate([c], {"AAPL": 102.0}, tmp_path)
        assert "AAPL" in result["dropped"]
        assert "SMA200" in result["dropped"]["AAPL"]["reason"]

    def test_drops_on_excessive_gap_down(self, tmp_path):
        # ATR=2, drop > ATR×2=4 → drop
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=80.0, atr=2.0)
        result = _run_validate([c], {"AAPL": 95.0}, tmp_path)  # -5% = 2.5×ATR
        assert "AAPL" in result["dropped"]
        assert "gap down" in result["dropped"]["AAPL"]["reason"]

    def test_sma50_check_takes_priority_over_gap_down(self, tmp_path):
        c = _candidate("AAPL", last_close=100.0, sma50=98.0, sma200=80.0, atr=2.0)
        result = _run_validate([c], {"AAPL": 97.0}, tmp_path)
        assert "SMA50" in result["dropped"]["AAPL"]["reason"]


class TestValidateWarn:
    def test_warns_on_gap_up_over_5pct(self, tmp_path):
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=80.0)
        result = _run_validate([c], {"AAPL": 106.0}, tmp_path)  # +6%
        assert "AAPL" in result["approved"]   # still approved
        assert "AAPL" in result["warned"]
        assert "gap up" in result["warned"]["AAPL"]["reason"]

    def test_warns_on_exactly_5pct(self, tmp_path):
        # boundary: >= 5% triggers warn
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=80.0)
        result = _run_validate([c], {"AAPL": 105.0}, tmp_path)  # exactly +5%
        assert "AAPL" in result["warned"]

    def test_no_warn_below_5pct(self, tmp_path):
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=80.0)
        result = _run_validate([c], {"AAPL": 104.9}, tmp_path)  # just under +5%
        assert "AAPL" not in result["warned"]

    def test_warn_symbol_still_in_approved(self, tmp_path):
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=80.0)
        result = _run_validate([c], {"AAPL": 108.0}, tmp_path)
        assert "AAPL" in result["approved"]


class TestValidateMissingPrice:
    def test_keeps_symbol_when_no_live_price(self, tmp_path):
        # Benefit of the doubt — no price data → keep
        c = _candidate("AAPL")
        result = _run_validate([c], {}, tmp_path)
        assert "AAPL" in result["approved"]
        assert "AAPL" not in result["dropped"]


class TestValidateMultipleSymbols:
    def test_mixed_results(self, tmp_path):
        candidates = [
            _candidate("KEEP", last_close=100.0, sma50=90.0, sma200=80.0),
            _candidate("DROP", last_close=100.0, sma50=105.0, sma200=80.0),
            _candidate("WARN", last_close=100.0, sma50=90.0, sma200=80.0),
        ]
        prices = {"KEEP": 101.0, "DROP": 102.0, "WARN": 107.0}
        result = _run_validate(candidates, prices, tmp_path)
        assert "KEEP" in result["approved"]
        assert "DROP" in result["dropped"]
        assert "WARN" in result["warned"]
        assert "WARN" in result["approved"]

    def test_saves_filter_to_file(self, tmp_path):
        c = _candidate("AAPL", last_close=100.0, sma50=90.0, sma200=80.0)
        _run_validate([c], {"AAPL": 101.0}, tmp_path)
        filter_file = tmp_path / "filter.json"
        assert filter_file.exists()
        data = json.loads(filter_file.read_text())
        import datetime
        today = datetime.date.today().isoformat()
        assert today in data
        assert "approved" in data[today]


class TestLoadApprovedToday:
    def test_returns_none_when_no_file(self, tmp_path):
        with patch("trader.premarket_validator._FILTER_FILE", tmp_path / "missing.json"):
            from trader.premarket_validator import load_approved_today
            assert load_approved_today() is None

    def test_returns_approved_set(self, tmp_path):
        import datetime
        today = datetime.date.today().isoformat()
        filter_file = tmp_path / "filter.json"
        filter_file.write_text(json.dumps({
            today: {"approved": ["AAPL", "MSFT"], "warned": {}, "dropped": {}}
        }))
        with patch("trader.premarket_validator._FILTER_FILE", filter_file):
            from trader.premarket_validator import load_approved_today
            result = load_approved_today()
        assert result == {"AAPL", "MSFT"}

    def test_includes_warned_symbols_in_approved(self, tmp_path):
        import datetime
        today = datetime.date.today().isoformat()
        filter_file = tmp_path / "filter.json"
        filter_file.write_text(json.dumps({
            today: {
                "approved": ["AAPL"],
                "warned": {"ALLR": {"reason": "gap up +7%"}},
                "dropped": {}
            }
        }))
        with patch("trader.premarket_validator._FILTER_FILE", filter_file):
            from trader.premarket_validator import load_approved_today
            result = load_approved_today()
        assert "AAPL" in result
        assert "ALLR" in result


class TestSendPremarketSummary:
    def test_sends_approved_symbols(self):
        result = {"approved": ["AAPL", "MSFT"], "warned": {}, "dropped": {}}
        with patch("notifier.telegram._send") as mock_send:
            from trader.premarket_validator import send_premarket_summary
            send_premarket_summary(result)
            text = mock_send.call_args[0][0]
            assert "AAPL" in text
            assert "MSFT" in text

    def test_shows_drop_reason(self):
        result = {
            "approved": [],
            "warned": {},
            "dropped": {"MDAI": {"reason": "below SMA50", "current": 2.1, "prev_close": 2.64}}
        }
        with patch("notifier.telegram._send") as mock_send:
            from trader.premarket_validator import send_premarket_summary
            send_premarket_summary(result)
            text = mock_send.call_args[0][0]
            assert "MDAI" in text
            assert "below SMA50" in text

    def test_shows_warn_reason(self):
        result = {
            "approved": ["ALLR"],
            "warned": {"ALLR": {"reason": "gap up +7.2%", "current": 1.78, "prev_close": 1.66}},
            "dropped": {}
        }
        with patch("notifier.telegram._send") as mock_send:
            from trader.premarket_validator import send_premarket_summary
            send_premarket_summary(result)
            text = mock_send.call_args[0][0]
            assert "ALLR" in text
            assert "gap up" in text
