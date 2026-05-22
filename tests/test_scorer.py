"""Tests for signals/scorer.py — weighted scoring."""
import numpy as np
import pandas as pd
import pytest

from signals.scorer import score_ticker, MAX_SCORE, _WEIGHTS


def _make_bars(n=220, trend="up") -> pd.DataFrame:
    """Generate synthetic OHLCV bars with a clear trend."""
    np.random.seed(42)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    if trend == "up":
        close = np.linspace(50, 150, n) + np.random.normal(0, 1, n)
    else:
        close = np.linspace(150, 50, n) + np.random.normal(0, 1, n)
    high = close + np.abs(np.random.normal(1, 0.5, n))
    low = close - np.abs(np.random.normal(1, 0.5, n))
    open_ = close + np.random.normal(0, 0.5, n)
    volume = np.random.randint(600_000, 2_000_000, n).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=dates)


class TestMaxScore:
    def test_max_score_is_8(self):
        assert MAX_SCORE == 8

    def test_weights_sum_to_max_score(self):
        assert sum(_WEIGHTS.values()) == MAX_SCORE

    def test_all_criteria_have_equal_weight(self):
        assert _WEIGHTS["adx_strong"] == 1
        assert _WEIGHTS["outperforming_spy"] == 1

    def test_all_other_weights_are_1(self):
        double_weighted = {"adx_strong", "outperforming_spy"}
        for k, v in _WEIGHTS.items():
            if k not in double_weighted:
                assert v == 1, f"{k} should have weight 1, got {v}"


class TestScoreTicker:
    def test_returns_none_when_insufficient_bars(self):
        df = _make_bars(n=100)
        spy = _make_bars(n=100)
        assert score_ticker(df, spy) is None

    def test_returns_none_at_exactly_209_bars(self):
        df = _make_bars(n=209)
        spy = _make_bars(n=209)
        assert score_ticker(df, spy) is None

    def test_returns_result_at_210_bars(self):
        df = _make_bars(n=210)
        spy = _make_bars(n=210)
        result = score_ticker(df, spy)
        assert result is not None

    def test_score_within_valid_range(self):
        df = _make_bars(n=220)
        spy = _make_bars(n=220)
        result = score_ticker(df, spy)
        assert 0 <= result["score"] <= MAX_SCORE

    def test_result_has_required_keys(self):
        df = _make_bars(n=220)
        spy = _make_bars(n=220)
        result = score_ticker(df, spy)
        assert result is not None
        for key in ("score", "criteria", "trend", "momentum", "volume", "relative_strength", "exit"):
            assert key in result

    def test_criteria_keys_match_weights(self):
        df = _make_bars(n=220)
        spy = _make_bars(n=220)
        result = score_ticker(df, spy)
        assert set(result["criteria"].keys()) == set(_WEIGHTS.keys())

    def test_score_matches_weighted_sum_of_criteria(self):
        df = _make_bars(n=220)
        spy = _make_bars(n=220)
        result = score_ticker(df, spy)
        expected = sum(_WEIGHTS[k] for k, v in result["criteria"].items() if v)
        assert result["score"] == expected

    def test_downtrend_scores_lower_than_uptrend(self):
        spy = _make_bars(n=220, trend="up")
        up_result = score_ticker(_make_bars(n=220, trend="up"), spy)
        down_result = score_ticker(_make_bars(n=220, trend="down"), spy)
        assert up_result["score"] > down_result["score"]
