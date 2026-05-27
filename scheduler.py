"""
Long-running scheduler service — keeps ingest.py and main.py running on a cron schedule.

Jobs (all Mon–Fri, times ET unless noted):
  - orders  : 09:45 ET  — place bracket orders for top candidates (after 1st 15-min candle)
  - monitor : 15:30 ET  — exit monitor: close positions where RSI>70 or 2+ warnings
  - ingest  : 21:30 UTC — fetch Alpaca bars + compute signals (30 min after market close)
  - notify  : 22:00 UTC — send top 10 watchlist + P&L to Telegram
  - bot     : background thread — long-polls Telegram for /schedule, /loglevel, /setlevel

Run as a service:
  .venv/bin/python scheduler.py
"""
import json
import logging
import sys
import threading
import time
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config

_RUNS_FILE = Path(__file__).parent / "data" / "job_runs.json"


def _save_run(job: str, duration_seconds: float) -> None:
    try:
        data = json.loads(_RUNS_FILE.read_text()) if _RUNS_FILE.exists() else {}
        data[job] = {"duration_seconds": duration_seconds}
        _RUNS_FILE.write_text(json.dumps(data))
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def _market_closed_today() -> bool:
    from data.fetcher import is_market_open_today
    return not is_market_open_today()


def run_ingest() -> None:
    import ingest
    log.info("=== Starting daily ingestion ===")
    t0 = time.monotonic()
    try:
        ingest.main()
        _save_run("ingest", time.monotonic() - t0)
        log.info("=== Ingestion complete ===")
    except Exception as exc:
        log.exception("Ingestion failed")
        from notifier.telegram import send_alert
        send_alert(f"Daily ingestion failed: `{exc}`\nCheck `journalctl -u momentum-hunter` for details.")


def run_premarket() -> None:
    if _market_closed_today():
        log.info("=== Pre-market skipped — market holiday ===")
        return
    log.info("=== Pre-market validator (9:15 AM ET) ===")
    t0 = time.monotonic()
    try:
        from data.db import load_signals, signal_persistence
        from trader.premarket_validator import validate, send_premarket_summary
        candidates = load_signals("us_equity", min_score=config.MIN_SCORE)
        persistence = signal_persistence("us_equity")
        for c in candidates:
            c["days_in_scan"] = persistence.get(c["symbol"], 1)
        result = validate(candidates[:config.AUTO_ORDER_TOP_N])
        _save_run("premarket", time.monotonic() - t0)
        send_premarket_summary(result)
        log.info("=== Pre-market done: %d approved, %d warned, %d dropped ===",
                 len(result["approved"]), len(result.get("warned", {})),
                 len(result.get("dropped", {})))
    except Exception as exc:
        log.exception("Pre-market validator failed")
        from notifier.telegram import send_alert
        send_alert(f"Pre-market validator failed: `{exc}`")


def run_stop_update() -> None:
    if _market_closed_today():
        log.info("=== Stop update skipped — market holiday ===")
        return
    log.info("=== Trailing stop update (9:40 AM ET) ===")
    try:
        from trader.stop_updater import update_trailing_stops, send_stop_summary
        updated = update_trailing_stops()
        send_stop_summary(updated)
        log.info("=== Stop update done: %d raised ===", len(updated))
    except Exception as exc:
        log.exception("Stop update failed")
        from notifier.telegram import send_alert
        send_alert(f"Stop update failed: `{exc}`")


def run_orders() -> None:
    if _market_closed_today():
        log.info("=== Orders skipped — market holiday ===")
        return
    log.info("=== Placing morning orders (9:45 AM ET) ===")
    try:
        from data.db import load_signals, signal_persistence
        from trader.order_placer import place_orders, send_order_summary
        candidates = load_signals("us_equity", min_score=config.MIN_SCORE)
        persistence = signal_persistence("us_equity")
        for c in candidates:
            c["days_in_scan"] = persistence.get(c["symbol"], 1)
        placed = place_orders(candidates)
        send_order_summary(placed)
        log.info("=== Orders complete: %d placed ===", len(placed))
    except Exception as exc:
        log.exception("Order placement failed")
        from notifier.telegram import send_alert
        send_alert(f"Auto-order failed: `{exc}`")


def run_intraday_monitor() -> None:
    if _market_closed_today():
        log.info("=== Intraday monitor skipped — market holiday ===")
        return
    log.info("=== Intraday RSI monitor (15-min bars) ===")
    t0 = time.monotonic()
    try:
        from trader.intraday_monitor import run_intraday_check, send_intraday_summary
        closed = run_intraday_check()
        total = len(closed)  # positions checked is logged inside run_intraday_check
        _save_run("intraday", time.monotonic() - t0)
        send_intraday_summary(closed, total)
        log.info("=== Intraday monitor done: %d closed ===", len(closed))
    except Exception as exc:
        log.exception("Intraday monitor failed")
        from notifier.telegram import send_alert
        send_alert(f"Intraday monitor failed: `{exc}`")


def run_monitor() -> None:
    if _market_closed_today():
        log.info("=== Exit monitor skipped — market holiday ===")
        return
    log.info("=== Exit monitor (3:30 PM ET) ===")
    t0 = time.monotonic()
    try:
        from data.db import load_signals
        from trader.position_monitor import check_and_exit, send_monitor_summary
        from alpaca.trading.client import TradingClient
        paper = "paper-api" in config.ALPACA_BASE_URL
        client = TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)
        total_positions = len(client.get_all_positions())
        all_signals = load_signals("us_equity", min_score=0)
        signals_map = {s["symbol"]: s for s in all_signals}
        closed = check_and_exit(signals_map)
        _save_run("monitor", time.monotonic() - t0)
        send_monitor_summary(closed, total_positions)
        log.info("=== Exit monitor done: %d closed ===", len(closed))
    except Exception as exc:
        log.exception("Exit monitor failed")
        from notifier.telegram import send_alert
        send_alert(f"Exit monitor failed: `{exc}`")


def run_notify() -> None:
    import subprocess, sys
    log.info("=== Sending Telegram notifications ===")
    t0 = time.monotonic()
    result = subprocess.run(
        [sys.executable, "main.py", "--market", "all", "--top", "10"],
        capture_output=True, text=True,
    )
    if result.stdout:
        log.info(result.stdout.strip())
    if result.stderr:
        log.warning(result.stderr.strip())
    if result.returncode != 0:
        log.error("Notification run exited with code %d", result.returncode)
        from notifier.telegram import send_alert
        send_alert(f"Notification run failed (exit {result.returncode}).\n`{result.stderr.strip()[:500]}`")
    else:
        _save_run("notify", time.monotonic() - t0)
        log.info("=== Notifications sent ===")


def main() -> None:
    from notifier.telegram_handler import setup_telegram_logging
    setup_telegram_logging()

    # Start Telegram bot listener in a daemon thread
    from notifier.bot_listener import run_forever
    bot_thread = threading.Thread(target=run_forever, daemon=True, name="bot-listener")
    bot_thread.start()

    scheduler = BlockingScheduler(timezone="UTC")

    # 21:30 UTC = 30 min after US market close (4pm ET)
    scheduler.add_job(
        run_ingest,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=30, timezone="UTC"),
        id="ingest",
        name="Daily ingestion",
        misfire_grace_time=300,
    )

    # 9:15 AM ET = pre-market sanity check before orders fire
    scheduler.add_job(
        run_premarket,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=15, timezone="America/New_York"),
        id="premarket",
        name="Pre-market validator",
        misfire_grace_time=120,
    )

    # 9:40 AM ET = raise stops on profitable overnight positions before new orders fire
    scheduler.add_job(
        run_stop_update,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=40, timezone="America/New_York"),
        id="stop_update",
        name="Trailing stop update",
        misfire_grace_time=120,
    )

    # 9:45 AM ET = after first 15-min candle confirms direction
    scheduler.add_job(
        run_orders,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=45, timezone="America/New_York"),
        id="orders",
        name="Morning auto-orders",
        misfire_grace_time=300,
    )

    # Every 30 min during market hours: :15 and :45 of each hour, 10 AM–3 PM ET
    # Fires at 10:15, 10:45, 11:15, 11:45, 12:15, 12:45, 13:15, 13:45, 14:15, 14:45
    scheduler.add_job(
        run_intraday_monitor,
        CronTrigger(day_of_week="mon-fri", hour="10-14", minute="15,45", timezone="America/New_York"),
        id="intraday",
        name="Intraday RSI monitor",
        misfire_grace_time=120,
    )

    # 3:30 PM ET = 30 min before close — end-of-day signal check (daily bars)
    scheduler.add_job(
        run_monitor,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone="America/New_York"),
        id="monitor",
        name="Exit monitor",
        misfire_grace_time=300,
    )

    # 22:00 UTC = after ingest has time to finish
    scheduler.add_job(
        run_notify,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=0, timezone="UTC"),
        id="notify",
        name="Telegram notification",
        misfire_grace_time=300,
    )

    log.info("Scheduler started. Jobs: premarket@9:15ET, stops@9:40ET, orders@9:45ET, intraday@every30min(10-15ET), monitor@15:30ET, ingest@21:30UTC, notify@22:00UTC (Mon–Fri)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
