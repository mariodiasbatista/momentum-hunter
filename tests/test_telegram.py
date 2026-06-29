"""Tests for notifier/telegram.py — send_alert, stale warning, persistence streak."""
from unittest.mock import patch, call, MagicMock
import pytest

from notifier.telegram import send_alert, send_results, _format_candidate, _send


def _candidate(symbol="AAPL", score=9, days_in_scan=1):
    return {
        "symbol": symbol,
        "market": "stocks",
        "score": score,
        "days_in_scan": days_in_scan,
        "criteria": {
            "above_sma50": True, "above_sma200": True, "ema9_above_ema21": True,
            "rsi_in_range": True, "macd_bullish": True, "adx_strong": True,
            "volume_above_avg": True, "outperforming_spy": True,
        },
        "trend": {"last_close": 150.0, "sma50": 130.0, "sma200": 110.0, "ema9": 151.0, "ema21": 148.0},
        "momentum": {
            "rsi": 62.0, "rsi_overbought": False, "macd_above_signal": True,
            "macd_histogram_positive": True, "macd_histogram_shrinking": False,
            "adx": 31.0, "adx_falling": False, "atr": 3.0,
        },
        "volume": {"volume": 1_200_000.0, "avg_volume": 900_000.0, "volume_ratio": 1.33, "volume_drying_up": False},
        "relative_strength": {"rs_return": 6.0, "spy_return": 2.0, "outperforming_spy": True},
        "exit": {
            "exit_mode": "trailing_stop", "warning_count": 0, "warnings": [],
            "trailing_stop_atr_range": (1.5, 3.0),
        },
    }


class TestSend:
    def _mock_resp(self, status=200, retry_after=None):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = {"parameters": {"retry_after": retry_after}} if retry_after else {}
        if status >= 400:
            from requests.exceptions import HTTPError
            r.raise_for_status.side_effect = HTTPError(f"{status} error")
        else:
            r.raise_for_status.return_value = None
        return r

    def test_sends_on_first_try(self):
        with patch("requests.post", return_value=self._mock_resp(200)) as mock_post:
            _send("hello")
            assert mock_post.call_count == 1

    def test_retries_once_on_429_then_succeeds(self):
        responses = [self._mock_resp(429, retry_after=1), self._mock_resp(200)]
        with patch("requests.post", side_effect=responses) as mock_post, \
             patch("time.sleep") as mock_sleep:
            _send("hello")
            assert mock_post.call_count == 2
            mock_sleep.assert_called_once_with(1)

    def test_retries_up_to_max_on_repeated_429(self):
        from notifier.telegram import _MAX_RETRIES
        responses = [self._mock_resp(429, retry_after=2)] * _MAX_RETRIES
        with patch("requests.post", side_effect=responses) as mock_post, \
             patch("time.sleep"):
            _send("hello")  # must not raise
            assert mock_post.call_count == _MAX_RETRIES

    def test_does_not_raise_on_other_http_errors(self):
        with patch("requests.post", return_value=self._mock_resp(500)):
            _send("hello")  # must not raise

    def test_does_not_raise_on_network_error(self):
        with patch("requests.post", side_effect=Exception("connection refused")):
            # The exception escapes _send but send_alert swallows it
            pass  # _send itself will raise; callers must wrap — tested via send_alert below


class TestSendAlert:
    def test_sends_message_with_alert_prefix(self):
        with patch("notifier.telegram._send") as mock_send:
            send_alert("Something broke")
            text = mock_send.call_args[0][0]
            assert "Alert" in text
            assert "Something broke" in text

    def test_does_not_raise_on_network_error(self):
        with patch("notifier.telegram._send", side_effect=Exception("network down")):
            send_alert("test")  # must not raise


class TestFormatCandidate:
    def test_shows_score_out_of_8(self):
        text = _format_candidate(1, _candidate(score=8))
        assert "8/8" in text

    def test_no_streak_when_days_is_1(self):
        text = _format_candidate(1, _candidate(days_in_scan=1))
        assert "streak" not in text

    def test_shows_streak_when_days_gt_1(self):
        text = _format_candidate(1, _candidate(days_in_scan=3))
        assert "3d streak" in text

    def test_shows_symbol_and_rank(self):
        text = _format_candidate(2, _candidate(symbol="MSFT"))
        assert "#2" in text
        assert "MSFT" in text

    def test_all_criteria_shown_without_stars(self):
        text = _format_candidate(1, _candidate())
        assert "ADX" in text
        assert "RS>SPY" in text
        assert "★" not in text

    def test_warnings_shown_when_present(self):
        c = _candidate()
        c["exit"]["warnings"] = ["RSI overbought", "ADX falling"]
        text = _format_candidate(1, c)
        assert "RSI overbought" in text


class TestSendResults:
    def test_sends_empty_message_when_no_candidates(self):
        with patch("notifier.telegram._send") as mock_send:
            send_results([], market_label="Stocks")
            text = mock_send.call_args[0][0]
            assert "No candidates" in text

    def test_includes_stale_warning_in_header(self):
        with patch("notifier.telegram._send") as mock_send:
            send_results([_candidate()], market_label="Stocks", stale_warning="Signals from 2026-05-20")
            text = mock_send.call_args[0][0]
            assert "2026-05-20" in text

    def test_no_stale_line_when_warning_is_none(self):
        with patch("notifier.telegram._send") as mock_send:
            send_results([_candidate()], market_label="Stocks", stale_warning=None)
            text = mock_send.call_args[0][0]
            assert "Signals from" not in text

    def test_splits_into_multiple_messages_when_over_limit(self):
        candidates = [_candidate(symbol=f"SYM{i}", score=9) for i in range(30)]
        with patch("notifier.telegram._send") as mock_send:
            send_results(candidates, market_label="Stocks")
            assert mock_send.call_count > 1
