"""
notifier/summary.py — daily portfolio summary for /summary Telegram command.

Pulls from:
  - Alpaca TradingClient  → account balances, open positions
  - data/trades.json      → today's and all-time realized trades
  - data/orders_placed.json → stop prices for open positions
  - data/db.py            → watchlist candidates (top N from last scan)
"""
import logging
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config

log = logging.getLogger("summary")
ET = ZoneInfo("America/New_York")

_TRADES_FILE = Path(__file__).parent.parent / "data" / "trades.json"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_client():
    from alpaca.trading.client import TradingClient
    paper = "paper-api" in config.ALPACA_BASE_URL
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)


def _pnl_icon(value: float) -> str:
    return "🟢" if value >= 0 else "🔴"


def _fmt_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _load_trades(today_only: bool = False) -> list[dict]:
    import json
    try:
        data = json.loads(_TRADES_FILE.read_text()) if _TRADES_FILE.exists() else {}
        if today_only:
            return data.get(date.today().isoformat(), [])
        return [t for day in data.values() for t in day]
    except Exception:
        return []


# ── Summary builder ──────────────────────────────────────────────────────────

def build_summary() -> str:
    client = _get_client()
    now_str = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")

    # ── Account ───────────────────────────────────────────────────────────────
    try:
        acct          = client.get_account()
        portfolio     = float(acct.portfolio_value)
        cash          = float(acct.cash)
        buying_power  = float(acct.buying_power)
        equity        = float(acct.equity)
        last_equity   = float(acct.last_equity)
        day_pnl       = equity - last_equity
    except Exception as exc:
        log.error("Failed to fetch account: %s", exc)
        return "❌ Could not fetch account data from Alpaca."

    all_trades   = _load_trades(today_only=False)
    today_trades = _load_trades(today_only=True)

    all_pnl    = sum(t.get("pnl") or 0 for t in all_trades)
    all_wins   = sum(1 for t in all_trades if (t.get("pnl") or 0) > 0)
    all_losses = sum(1 for t in all_trades if (t.get("pnl") or 0) < 0)
    all_pnl_pct = (all_pnl / max(portfolio - all_pnl, 1) * 100)

    # ── Open positions ────────────────────────────────────────────────────────
    try:
        positions = client.get_all_positions()
    except Exception:
        positions = []

    from trader.order_placer import load_orders_today
    orders_today = load_orders_today()

    # ── Today's realized activity ─────────────────────────────────────────────
    realized_pnl  = sum(t.get("pnl") or 0 for t in today_trades)
    wins_today    = sum(1 for t in today_trades if (t.get("pnl") or 0) > 0)
    losses_today  = sum(1 for t in today_trades if (t.get("pnl") or 0) < 0)
    total_closed  = wins_today + losses_today
    win_rate      = int(wins_today / total_closed * 100) if total_closed else 0
    buys_today    = list(orders_today.keys())
    sells_today   = [t["symbol"] for t in today_trades]

    # ── Build message ─────────────────────────────────────────────────────────
    lines = [f"📊 *Portfolio — {now_str}*\n"]

    # Account block
    lines += [
        "💼 *Account*",
        f"Portfolio:    `${portfolio:>12,.2f}`",
        f"Cash:         `${cash:>12,.2f}`",
        f"Buying Power: `${buying_power:>12,.2f}`",
        f"Day P&L:      {_pnl_icon(day_pnl)} `{_fmt_money(day_pnl)}`",
        f"Cumulative:   {_pnl_icon(all_pnl)} `{_fmt_money(all_pnl)} ({'+' if all_pnl >= 0 else ''}{all_pnl_pct:.2f}%)`"
        f"  `[{all_wins}W/{all_losses}L all-time]`",
    ]

    # Positions block
    lines.append(f"\n📈 *Positions ({len(positions)} open)*")
    if not positions:
        lines.append("  No open positions.")
    else:
        for pos in positions:
            sym         = pos.symbol
            qty         = int(float(pos.qty))
            entry       = float(pos.avg_entry_price)
            current     = float(pos.current_price)
            unpl        = float(pos.unrealized_pl)
            unpl_pct    = float(pos.unrealized_plpc) * 100
            intra_pl    = float(pos.unrealized_intraday_pl)
            intra_pct   = float(pos.unrealized_intraday_plpc) * 100
            stop        = orders_today.get(sym, {}).get("stop_price")
            stop_str    = f"  Stop `${stop:.2f}`" if stop else ""

            lines.append(
                f"`{sym}` {qty}sh @ `${entry:.2f}` → `${current:.2f}`\n"
                f"  {_pnl_icon(unpl)} Total `{_fmt_money(unpl)}` (`{unpl_pct:+.1f}%`)"
                f"  Today `{_fmt_money(intra_pl)}` (`{intra_pct:+.1f}%`){stop_str}"
            )

    # Today's activity block
    buys_str  = f"{len(buys_today)} — {', '.join(buys_today)}" if buys_today else "0 — none"
    sells_str = f"{len(sells_today)} — {', '.join(sells_today)}" if sells_today else "0 — none"
    lines += [
        "\n📋 *Today's Activity*",
        f"Positions open:  `{len(positions)}`",
        f"Buys today:      `{buys_str}`",
        f"Sells today:     `{sells_str}`",
        f"Realized P&L:    {_pnl_icon(realized_pnl)} `{_fmt_money(realized_pnl)}`",
        f"Win rate:        `{win_rate}%  ({wins_today}W / {losses_today}L)`",
    ]

    # Stocks detail block
    lines.append("\n📊 *Stocks*")
    shown = set()

    for t in today_trades:
        sym    = t["symbol"]
        shown.add(sym)
        reason = t["reason"]
        entry  = t.get("entry_price", 0.0)
        exit_p = t.get("exit_price", 0.0)
        pnl    = t.get("pnl") or 0
        icon   = "✅" if pnl >= 0 else "❌"
        label  = "TP" if reason == "take_profit" else "SL"
        lines.append(f"  {icon} `{sym}` {label} `${entry:.2f}` → `${exit_p:.2f}`  `{_fmt_money(pnl)}`")

    for pos in positions:
        sym = pos.symbol
        if sym in shown:
            continue
        shown.add(sym)
        lines.append(f"  🔄 `{sym}` — open position")

    from data.db import load_signals
    candidates = load_signals("us_equity", min_score=config.MIN_SCORE)[:config.AUTO_ORDER_TOP_N]
    for c in candidates:
        sym = c["symbol"]
        if sym not in shown:
            shown.add(sym)
            lines.append(f"  🔍 `{sym}` — on watchlist")

    return "\n".join(lines)
