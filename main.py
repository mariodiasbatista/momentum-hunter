import argparse
import sys

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


def main():
    args = parse_args()
    markets = ["stocks", "crypto"] if args.market == "all" else [args.market]

    for market in markets:
        if market == "stocks":
            from scanner.stock_scanner import run_stock_scan
            print("Running stock scan...")
            candidates = run_stock_scan(min_score=args.min_score)
        else:
            from scanner.crypto_scanner import run_crypto_scan
            print("Running crypto scan...")
            candidates = run_crypto_scan(min_score=args.min_score)

        top = candidates[: args.top]
        label = market.capitalize()
        print(f"Found {len(candidates)} candidate(s), sending top {len(top)} to Telegram...")
        send_results(top, market_label=label)
        print(f"Telegram notification sent for {label}.")


if __name__ == "__main__":
    main()
