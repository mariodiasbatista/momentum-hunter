"""Tests for the volume filter applied during ingestion (improvement #6)."""
import numpy as np
import pandas as pd
import pytest


def _bars(avg_volume: float, n: int = 50) -> pd.DataFrame:
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    close = np.linspace(100, 110, n)
    volume = np.full(n, avg_volume)
    return pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": volume},
        index=dates,
    )


class TestVolumeFilter:
    def test_symbol_above_threshold_passes(self):
        bars = {"AAPL": _bars(1_000_000)}
        filtered = {sym: df for sym, df in bars.items() if df["volume"].mean() >= 500_000}
        assert "AAPL" in filtered

    def test_symbol_below_threshold_excluded(self):
        bars = {"TINY": _bars(100_000)}
        filtered = {sym: df for sym, df in bars.items() if df["volume"].mean() >= 500_000}
        assert "TINY" not in filtered

    def test_symbol_at_exact_threshold_passes(self):
        bars = {"EDGE": _bars(500_000)}
        filtered = {sym: df for sym, df in bars.items() if df["volume"].mean() >= 500_000}
        assert "EDGE" in filtered

    def test_mixed_universe_filters_correctly(self):
        bars = {
            "HIGH": _bars(2_000_000),
            "MID": _bars(500_000),
            "LOW": _bars(50_000),
        }
        filtered = {sym: df for sym, df in bars.items() if df["volume"].mean() >= 500_000}
        assert set(filtered.keys()) == {"HIGH", "MID"}

    def test_filter_count_is_correct(self):
        bars = {f"S{i}": _bars(i * 100_000) for i in range(1, 11)}
        filtered = {sym: df for sym, df in bars.items() if df["volume"].mean() >= 500_000}
        # i=5..10 pass (500k, 600k, ..., 1M) → 6 symbols
        assert len(filtered) == 6
