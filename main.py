import argparse
import time

import config  # noqa: F401 — triggers .env load and env var validation early
from notifier.telegram import send_results


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
        print(f"Found {len(candidates)} candidate(s) in {_fmt(scan_time)}, sending top {len(top)} to Telegram...")
        send_results(top, market_label=label)
        print(f"Telegram notification sent for {label}.")

    print(f"\nTotal run time: {_fmt(time.time() - total_start)}")


if __name__ == "__main__":
    main()
