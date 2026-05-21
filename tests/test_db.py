"""Tests for db.py — historical signals, persistence, migration, helpers."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

import data.db as db_module
from data.db import (
    init_db,
    save_assets,
    save_bars,
    save_signals,
    load_signals,
    signals_last_computed_date,
    signal_persistence,
    signals_computed_today,
    bars_last_date,
)


@pytest.fixture(autouse=True)
def tmp_db(tmp_path):
    """Redirect DB_PATH to a temp file for every test."""
    db_path = tmp_path / "test.db"
    with patch.object(db_module, "DB_PATH", db_path):
        init_db()
        yield db_path


def _asset(symbol="AAPL", asset_class="us_equity"):
    return {
        "symbol": symbol,
        "name": symbol,
        "exchange": "NASDAQ",
        "asset_class": asset_class,
        "tradable": 1,
        "fractionable": 1,
        "shortable": 1,
        "updated_at": "2026-05-21",
    }


def _signal(symbol="AAPL", asset_class="us_equity", score=8, computed_at="2026-05-21"):
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "computed_at": computed_at,
        "score": score,
        "criteria": {
            "above_sma50": True, "above_sma200": True, "ema9_above_ema21": True,
            "rsi_in_range": True, "macd_bullish": True, "adx_strong": True,
            "volume_above_avg": True, "outperforming_spy": True,
        },
        "trend": {"last_close": 100.0, "sma50": 90.0, "sma200": 80.0, "ema9": 101.0, "ema21": 99.0},
        "momentum": {
            "rsi": 60.0, "rsi_overbought": False, "macd_above_signal": True,
            "macd_histogram_positive": True, "macd_histogram_shrinking": False,
            "adx": 30.0, "adx_falling": False, "atr": 2.0,
        },
        "volume": {"volume": 1_000_000.0, "avg_volume": 800_000.0, "volume_ratio": 1.25, "volume_drying_up": False},
        "relative_strength": {"rs_return": 5.0, "spy_return": 2.0, "outperforming_spy": True},
        "exit": {
            "exit_mode": "trailing_stop", "warning_count": 0, "warnings": [],
            "trailing_stop_atr_range": (1.5, 3.0),
        },
    }


class TestSaveAndLoadSignals:
    def test_load_returns_latest_run_only(self):
        save_signals([_signal(computed_at="2026-05-20")])
        save_signals([_signal(computed_at="2026-05-21")])
        results = load_signals("us_equity")
        assert len(results) == 1
        assert results[0]["symbol"] == "AAPL"

    def test_history_preserved_across_runs(self, tmp_path):
        """Both run dates should exist in the DB."""
        save_signals([_signal(computed_at="2026-05-20")])
        save_signals([_signal(computed_at="2026-05-21")])
        with db_module.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert count == 2

    def test_same_day_rerun_does_not_duplicate(self):
        save_signals([_signal(computed_at="2026-05-21")])
        save_signals([_signal(computed_at="2026-05-21")])
        with db_module.get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        assert count == 1

    def test_min_score_filter(self):
        save_signals([_signal("AAPL", score=9), _signal("MSFT", score=5)])
        results = load_signals("us_equity", min_score=7)
        assert len(results) == 1
        assert results[0]["symbol"] == "AAPL"

    def test_results_ordered_by_score_desc(self):
        save_signals([_signal("A", score=6), _signal("B", score=9), _signal("C", score=7)])
        results = load_signals("us_equity")
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)


class TestSignalsLastComputedDate:
    def test_returns_none_when_empty(self):
        assert signals_last_computed_date("us_equity") is None

    def test_returns_latest_date(self):
        save_signals([_signal(computed_at="2026-05-19")])
        save_signals([_signal(computed_at="2026-05-21")])
        assert signals_last_computed_date("us_equity") == "2026-05-21"

    def test_isolated_per_asset_class(self):
        save_signals([_signal("BTC/USD", asset_class="crypto", computed_at="2026-05-20")])
        assert signals_last_computed_date("us_equity") is None
        assert signals_last_computed_date("crypto") == "2026-05-20"


class TestSignalPersistence:
    def test_symbol_appearing_multiple_days(self):
        save_signals([_signal("AAPL", computed_at="2026-05-19")])
        save_signals([_signal("AAPL", computed_at="2026-05-20")])
        save_signals([_signal("AAPL", computed_at="2026-05-21")])
        p = signal_persistence("us_equity", days=5)
        assert p["AAPL"] == 3

    def test_symbol_appearing_once(self):
        save_signals([_signal("AAPL", computed_at="2026-05-21")])
        p = signal_persistence("us_equity", days=5)
        assert p["AAPL"] == 1

    def test_days_cap_limits_lookback(self):
        for d in range(1, 8):
            save_signals([_signal(computed_at=f"2026-05-{d:02d}")])
        p = signal_persistence("us_equity", days=3)
        assert p["AAPL"] == 3

    def test_returns_empty_when_no_signals(self):
        assert signal_persistence("us_equity") == {}


class TestMigration:
    def test_old_schema_is_dropped_and_recreated(self, tmp_path):
        db_path = tmp_path / "old.db"
        # Create table with old PK (symbol, asset_class) — no computed_at in PK
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE signals (
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                computed_at TEXT,
                score INTEGER,
                PRIMARY KEY (symbol, asset_class)
            )
        """)
        conn.execute("INSERT INTO signals VALUES ('AAPL', 'us_equity', '2026-05-20', 8)")
        conn.commit()
        conn.close()

        with patch.object(db_module, "DB_PATH", db_path):
            init_db()
            with db_module.get_conn() as c:
                count = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        # Old data wiped, new empty table with correct schema
        assert count == 0


class TestBarsLastDate:
    def test_returns_none_when_empty(self):
        assert bars_last_date() is None

    def test_returns_max_date(self):
        save_assets([_asset()])
        df = pd.DataFrame(
            {"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [1000.0]},
            index=pd.to_datetime(["2026-05-21"]),
        )
        df.index.name = "date"
        save_bars(df, "AAPL")
        assert bars_last_date() == "2026-05-21"
