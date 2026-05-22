import json
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_RUNS_FILE = Path(__file__).parent.parent / "data" / "job_runs.json"

# (start_et, end_et_or_None, job_key, label)
# end_et set → window job: ⬜ before start, 🔄 during window, ✅ after end
# end_et None → point job: ⬜ before start, ✅ after start
_SCHEDULE = [
    ("09:15", None,    "premarket", "Pre-Market Validator — filter gap-downs"),
    ("09:45", None,    "orders",    "Auto-Order — buy validated candidates"),
    ("10:15", "14:45", "intraday",  "Intraday RSI Monitor — every 30 min"),
    ("15:30", None,    "monitor",   "Exit Monitor — EOD signal check (daily bars)"),
    ("17:30", None,    "ingest",    "Alpaca Pull — fetch bars + score signals"),
    ("18:00", None,    "notify",    "Set Watchlist & Alert"),
]


def _parse(hhmm: str) -> dtime:
    h, m = map(int, hhmm.split(":"))
    return dtime(h, m)


def _fmt_time(hhmm: str) -> str:
    t = _parse(hhmm)
    suffix = "AM" if t.hour < 12 else "PM"
    h = t.hour if t.hour <= 12 else t.hour - 12
    h = 12 if h == 0 else h
    return f"{h}:{t.minute:02d} {suffix}"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    m, s = divmod(s, 60)
    return f"{m}m {s}s" if m else f"{s}s"


def _load_runs() -> dict:
    try:
        return json.loads(_RUNS_FILE.read_text()) if _RUNS_FILE.exists() else {}
    except Exception:
        return {}


def build_schedule_message() -> str:
    now = datetime.now(ET)
    now_time = now.time().replace(second=0, microsecond=0)
    date_str = now.strftime("%A %Y-%m-%d")
    time_str = now.strftime("%I:%M %p ET").lstrip("0")
    runs = _load_runs()

    lines = [f"📅 *Momentum Hunter — {date_str}*\n"]

    for start, end, key, label in _SCHEDULE:
        started = now_time >= _parse(start)
        finished = end is None or now_time > _parse(end)

        if not started:
            icon = "⬜"
        elif started and not finished:
            icon = "🔄"   # window job actively running
        else:
            icon = "✅"

        time_label = _fmt_time(start)
        if end:
            time_label = f"{time_label}–{_fmt_time(end)}"

        duration = ""
        if finished and started and key in runs:
            duration = f" _{_fmt_duration(runs[key]['duration_seconds'])}_"

        lines.append(f"{icon}  `{time_label}` {label}{duration}")

    # Top candidates from last scan
    try:
        from data.db import load_signals
        candidates = load_signals("us_equity", min_score=6)
        if candidates:
            symbols = [c["symbol"] for c in candidates[:8]]
            lines.append(f"\n📋 *Watchlist:* {', '.join(symbols)}")
        else:
            lines.append("\n📋 *Watchlist:* No candidates today")
    except Exception:
        pass

    lines.append(f"🕐 *Now:* {time_str}")
    return "\n".join(lines)
