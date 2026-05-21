"""
Long-running scheduler service — keeps ingest.py and main.py running on a cron schedule.

Jobs:
  - ingest  : runs Mon–Fri at 21:30 UTC (30 min after US market close)
  - notify  : runs Mon–Fri at 22:00 UTC (after ingest completes)

Run as a service:
  .venv/bin/python scheduler.py
"""
import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def run_ingest() -> None:
    import ingest
    log.info("=== Starting daily ingestion ===")
    try:
        ingest.main()
        log.info("=== Ingestion complete ===")
    except Exception as exc:
        log.exception("Ingestion failed")
        from notifier.telegram import send_alert
        send_alert(f"Daily ingestion failed: `{exc}`\nCheck `journalctl -u momentum-hunter` for details.")


def run_notify() -> None:
    import subprocess, sys
    log.info("=== Sending Telegram notifications ===")
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
        log.info("=== Notifications sent ===")


def main() -> None:
    scheduler = BlockingScheduler(timezone="UTC")

    # 21:30 UTC = 30 min after US market close (4pm ET)
    scheduler.add_job(
        run_ingest,
        CronTrigger(day_of_week="mon-fri", hour=21, minute=30, timezone="UTC"),
        id="ingest",
        name="Daily ingestion",
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

    log.info("Scheduler started. Jobs: ingest @ 21:30 UTC, notify @ 22:00 UTC (Mon–Fri)")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
