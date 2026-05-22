import argparse
import logging
import time
from datetime import date

import config  # noqa: F401 — triggers .env load and env var validation early
from notifier.telegram import send_results
from data.db import signals_last_computed_date

log = logging.getLogger("main")


def parse_args():
    parser = argparse.ArgumentParser(description="Momentum Hunter — scan for top momentum candidates")
    parser.add_argument(
        "--market",
        choices=["stocks", "crypto", "all"],
        default="stocks",
        help="Market to scan (default: stocks)",
    )
    parser.add_argument("--top", type=int, default=10, help="Number of top results to send (default: 10)")
    parser.add_argument(
        "--min-score",
        type=int,
        default=config.MIN_SCORE,
        help=f"Minimum signal score out of 8 (default: {config.MIN_SCORE})",
    )
    return parser.parse_args()


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s" if m else f"{s}s"


def main():
    from notifier.telegram_handler import setup_telegram_logging
    setup_telegram_logging()
    args = parse_args()
    markets = ["stocks", "crypto"] if args.market == "all" else [args.market]
    total_start = time.time()

    for market in markets:
        t0 = time.time()
        if market == "stocks":
            from scanner.stock_scanner import run_stock_scan
            print("Running stock scan...")
            candidates = run_stock_scan(min_score=args.min_score)
        else:
            from scanner.crypto_scanner import run_crypto_scan
            print("Running crypto scan...")
            candidates = run_crypto_scan(min_score=args.min_score)

        scan_time = time.time() - t0
        top = candidates[: args.top]
        label = market.capitalize()

        asset_class = "us_equity" if market == "stocks" else "crypto"
        last_date = signals_last_computed_date(asset_class)
        stale_warning = None
        if last_date and last_date < date.today().isoformat():
            stale_warning = f"Signals from {last_date} — today's ingestion may not have run yet."

        print(f"Found {len(candidates)} candidate(s) in {_fmt(scan_time)}, sending top {len(top)} to Telegram...")
        send_results(top, market_label=label, stale_warning=stale_warning)
        print(f"Telegram notification sent for {label}.")

    print(f"\nTotal run time: {_fmt(time.time() - total_start)}")


if __name__ == "__main__":
    main()
