import json
import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "momentum.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrate_signals(conn: sqlite3.Connection) -> None:
    """Drop signals table if it uses the old PK (symbol, asset_class) without computed_at."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='signals'"
    ).fetchone()
    if row and "computed_at" not in row[0].split("PRIMARY KEY")[-1]:
        conn.execute("DROP TABLE signals")


def init_db() -> None:
    with get_conn() as conn:
        _migrate_signals(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS assets (
                symbol       TEXT NOT NULL,
                name         TEXT,
                exchange     TEXT,
                asset_class  TEXT NOT NULL,
                tradable     INTEGER,
                fractionable INTEGER,
                shortable    INTEGER,
                updated_at   TEXT NOT NULL,
                PRIMARY KEY (symbol, asset_class)
            );

            CREATE TABLE IF NOT EXISTS bars (
                symbol  TEXT NOT NULL,
                date    TEXT NOT NULL,
                open    REAL,
                high    REAL,
                low     REAL,
                close   REAL,
                volume  REAL,
                PRIMARY KEY (symbol, date)
            );

            CREATE INDEX IF NOT EXISTS idx_bars_symbol ON bars (symbol);
            CREATE INDEX IF NOT EXISTS idx_bars_date   ON bars (date);

            CREATE TABLE IF NOT EXISTS signals (
                symbol                   TEXT NOT NULL,
                asset_class              TEXT NOT NULL,
                computed_at              TEXT NOT NULL,
                score                    INTEGER,
                -- criteria flags
                above_sma50              INTEGER,
                above_sma200             INTEGER,
                ema9_above_ema21         INTEGER,
                rsi_in_range             INTEGER,
                macd_bullish             INTEGER,
                adx_strong               INTEGER,
                volume_above_avg         INTEGER,
                outperforming_spy        INTEGER,
                -- trend values
                last_close               REAL,
                sma50                    REAL,
                sma200                   REAL,
                ema9                     REAL,
                ema21                    REAL,
                -- momentum values
                rsi                      REAL,
                rsi_overbought           INTEGER,
                macd_above_signal        INTEGER,
                macd_histogram_positive  INTEGER,
                macd_histogram_shrinking INTEGER,
                adx                      REAL,
                adx_falling              INTEGER,
                atr                      REAL,
                -- volume values
                volume                   REAL,
                avg_volume               REAL,
                volume_ratio             REAL,
                volume_drying_up         INTEGER,
                -- relative strength
                rs_return                REAL,
                spy_return               REAL,
                -- exit
                exit_mode                TEXT,
                warning_count            INTEGER,
                warnings                 TEXT,
                trailing_stop_atr_min    REAL,
                trailing_stop_atr_max    REAL,
                PRIMARY KEY (symbol, asset_class, computed_at)
            );

            CREATE INDEX IF NOT EXISTS idx_signals_score ON signals (score DESC);

            CREATE TABLE IF NOT EXISTS ingestion_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date         TEXT NOT NULL,
                asset_class      TEXT NOT NULL,
                symbol_count     INTEGER,
                bar_count        INTEGER,
                duration_seconds REAL,
                status           TEXT,
                started_at       TEXT,
                completed_at     TEXT
            );
        """)


# --- Assets ---

def save_assets(records: list[dict]) -> None:
    df = pd.DataFrame(records)
    with get_conn() as conn:
        conn.execute("DELETE FROM assets WHERE asset_class = ?", (records[0]["asset_class"],))
        df.to_sql("assets", conn, if_exists="append", index=False)


def load_symbols(asset_class: str) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT symbol FROM assets WHERE asset_class = ? AND tradable = 1 ORDER BY symbol",
            (asset_class,),
        ).fetchall()
    return [r[0] for r in rows]


# --- Bars ---

def save_bars(df: pd.DataFrame, symbol: str) -> None:
    df = df[["open", "high", "low", "close", "volume"]].copy()
    df.index = pd.to_datetime(df.index).date.astype(str)
    df.index.name = "date"
    df = df.reset_index()
    df.insert(0, "symbol", symbol)
    with get_conn() as conn:
        conn.execute("DELETE FROM bars WHERE symbol = ?", (symbol,))
        df.to_sql("bars", conn, if_exists="append", index=False)


def load_bars(symbol: str) -> pd.DataFrame | None:
    with get_conn() as conn:
        df = pd.read_sql(
            "SELECT date, open, high, low, close, volume FROM bars WHERE symbol = ? ORDER BY date",
            conn,
            params=(symbol,),
            parse_dates=["date"],
            index_col="date",
        )
    return df if not df.empty else None


def load_all_bars(asset_class: str) -> dict[str, pd.DataFrame]:
    """Load all bars for an asset class in a single query, split by symbol."""
    with get_conn() as conn:
        df = pd.read_sql(
            """SELECT b.symbol, b.date, b.open, b.high, b.low, b.close, b.volume
               FROM bars b
               JOIN assets a ON a.symbol = b.symbol AND a.asset_class = ?
               WHERE a.tradable = 1
               ORDER BY b.symbol, b.date""",
            conn,
            params=(asset_class,),
            parse_dates=["date"],
        )
    if df.empty:
        return {}
    result = {}
    for symbol, group in df.groupby("symbol"):
        result[symbol] = group.drop(columns="symbol").set_index("date")
    return result


def bars_last_date() -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(date) FROM bars").fetchone()
    return row[0] if row else None


# --- Signals ---

def save_signals(records: list[dict]) -> None:
    if not records:
        return
    asset_class = records[0]["asset_class"]
    rows = []
    for r in records:
        atr_min, atr_max = r["exit"]["trailing_stop_atr_range"]
        rows.append({
            "symbol": r["symbol"],
            "asset_class": asset_class,
            "computed_at": r["computed_at"],
            "score": r["score"],
            "above_sma50": int(r["criteria"]["above_sma50"]),
            "above_sma200": int(r["criteria"]["above_sma200"]),
            "ema9_above_ema21": int(r["criteria"]["ema9_above_ema21"]),
            "rsi_in_range": int(r["criteria"]["rsi_in_range"]),
            "macd_bullish": int(r["criteria"]["macd_bullish"]),
            "adx_strong": int(r["criteria"]["adx_strong"]),
            "volume_above_avg": int(r["criteria"]["volume_above_avg"]),
            "outperforming_spy": int(r["criteria"]["outperforming_spy"]),
            "last_close": r["trend"]["last_close"],
            "sma50": r["trend"]["sma50"],
            "sma200": r["trend"]["sma200"],
            "ema9": r["trend"]["ema9"],
            "ema21": r["trend"]["ema21"],
            "rsi": r["momentum"]["rsi"],
            "rsi_overbought": int(r["momentum"]["rsi_overbought"]),
            "macd_above_signal": int(r["momentum"]["macd_above_signal"]),
            "macd_histogram_positive": int(r["momentum"]["macd_histogram_positive"]),
            "macd_histogram_shrinking": int(r["momentum"]["macd_histogram_shrinking"]),
            "adx": r["momentum"]["adx"],
            "adx_falling": int(r["momentum"]["adx_falling"]),
            "atr": r["momentum"]["atr"],
            "volume": r["volume"]["volume"],
            "avg_volume": r["volume"]["avg_volume"],
            "volume_ratio": r["volume"]["volume_ratio"],
            "volume_drying_up": int(r["volume"]["volume_drying_up"]),
            "rs_return": r["relative_strength"]["rs_return"],
            "spy_return": r["relative_strength"]["spy_return"],
            "exit_mode": r["exit"]["exit_mode"],
            "warning_count": r["exit"]["warning_count"],
            "warnings": json.dumps(r["exit"]["warnings"]),
            "trailing_stop_atr_min": atr_min,
            "trailing_stop_atr_max": atr_max,
        })
    df = pd.DataFrame(rows)
    run_date = rows[0]["computed_at"]
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM signals WHERE asset_class = ? AND computed_at = ?",
            (asset_class, run_date),
        )
        df.to_sql("signals", conn, if_exists="append", index=False)


def signals_last_computed_date(asset_class: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(computed_at) FROM signals WHERE asset_class = ?",
            (asset_class,),
        ).fetchone()
    return row[0] if row else None


def signal_persistence(asset_class: str, days: int = 5) -> dict[str, int]:
    """Return how many of the last N run dates each symbol appeared in signals."""
    with get_conn() as conn:
        dates = conn.execute(
            "SELECT DISTINCT computed_at FROM signals WHERE asset_class = ? ORDER BY computed_at DESC LIMIT ?",
            (asset_class, days),
        ).fetchall()
        if not dates:
            return {}
        date_list = [d[0] for d in dates]
        placeholders = ",".join("?" * len(date_list))
        rows = conn.execute(
            f"SELECT symbol, COUNT(*) FROM signals WHERE asset_class = ? AND computed_at IN ({placeholders}) GROUP BY symbol",
            [asset_class] + date_list,
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def load_signals(asset_class: str, min_score: int = 0) -> list[dict]:
    with get_conn() as conn:
        df = pd.read_sql(
            """SELECT * FROM signals
               WHERE asset_class = ?
                 AND computed_at = (SELECT MAX(computed_at) FROM signals WHERE asset_class = ?)
                 AND score >= ?
               ORDER BY score DESC""",
            conn,
            params=(asset_class, asset_class, min_score),
        )
    if df.empty:
        return []

    results = []
    for _, row in df.iterrows():
        results.append({
            "symbol": row["symbol"],
            "market": "crypto" if asset_class == "crypto" else "stocks",
            "score": int(row["score"]),
            "criteria": {
                "above_sma50": bool(row["above_sma50"]),
                "above_sma200": bool(row["above_sma200"]),
                "ema9_above_ema21": bool(row["ema9_above_ema21"]),
                "rsi_in_range": bool(row["rsi_in_range"]),
                "macd_bullish": bool(row["macd_bullish"]),
                "adx_strong": bool(row["adx_strong"]),
                "volume_above_avg": bool(row["volume_above_avg"]),
                "outperforming_spy": bool(row["outperforming_spy"]),
            },
            "trend": {
                "last_close": row["last_close"],
                "sma50": row["sma50"],
                "sma200": row["sma200"],
                "ema9": row["ema9"],
                "ema21": row["ema21"],
            },
            "momentum": {
                "rsi": row["rsi"],
                "rsi_overbought": bool(row["rsi_overbought"]),
                "macd_above_signal": bool(row["macd_above_signal"]),
                "macd_histogram_positive": bool(row["macd_histogram_positive"]),
                "macd_histogram_shrinking": bool(row["macd_histogram_shrinking"]),
                "adx": row["adx"],
                "adx_falling": bool(row["adx_falling"]),
                "atr": row["atr"],
            },
            "volume": {
                "volume": row["volume"],
                "avg_volume": row["avg_volume"],
                "volume_ratio": row["volume_ratio"],
                "volume_drying_up": bool(row["volume_drying_up"]),
            },
            "relative_strength": {
                "rs_return": row["rs_return"],
                "spy_return": row["spy_return"],
                "outperforming_spy": bool(row["outperforming_spy"]),
            },
            "exit": {
                "exit_mode": row["exit_mode"],
                "warning_count": int(row["warning_count"]),
                "warnings": json.loads(row["warnings"]),
                "trailing_stop_atr_range": (row["trailing_stop_atr_min"], row["trailing_stop_atr_max"]),
            },
        })
    return results


def signals_computed_today() -> bool:
    from datetime import date
    today = date.today().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE computed_at >= ?", (today,)
        ).fetchone()
    return row[0] > 0


# --- Ingestion log ---

def log_ingestion(run_date: str, asset_class: str, symbol_count: int,
                  bar_count: int, duration: float, status: str,
                  started_at: str, completed_at: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO ingestion_log
               (run_date, asset_class, symbol_count, bar_count, duration_seconds,
                status, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_date, asset_class, symbol_count, bar_count,
             duration, status, started_at, completed_at),
        )
