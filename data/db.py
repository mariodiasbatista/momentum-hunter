import sqlite3
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "momentum.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS assets (
                symbol      TEXT NOT NULL,
                name        TEXT,
                exchange    TEXT,
                asset_class TEXT NOT NULL,
                tradable    INTEGER,
                fractionable INTEGER,
                shortable   INTEGER,
                updated_at  TEXT NOT NULL,
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


def bars_last_date() -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT MAX(date) FROM bars").fetchone()
    return row[0] if row else None


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
