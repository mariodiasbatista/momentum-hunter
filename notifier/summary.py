"""
notifier/summary.py — daily portfolio summary for /summary Telegram command.

Stocks and crypto are reported in separate blocks running the same analysis.
They cannot share a data source: each path books its own trades, tracks its own
entry levels and sizes positions differently (whole shares vs fractional coins).

Pulls from:
  - Alpaca TradingClient       → account balances, open positions
  - data/trades.json           → equity realized trades
  - data/orders_placed.json    → equity stop prices
  - data/db.py                 → equity watchlist (top N from last scan)
  - data/crypto_trades.json    → crypto realized trades
  - data/crypto_positions.json → crypto entry, stop and target levels
  - data/crypto_watchlist.json → crypto watchlist (cached by the 4-hourly cycle)
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


def _fmt_qty(qty: float, crypto: bool) -> str:
    """Crypto holdings are fractional — 0.0033 BTC must not print as `0sh`."""
    if not crypto:
        return f"{int(qty)}sh"
    return f"{qty:,.6f}".rstrip("0").rstrip(".")


def _fmt_price(price: float, crypto: bool = False) -> str:
    """Sub-dollar coins need more precision than cents — DOGE is not `$0.12`."""
    if crypto and 0 < price < 1:
        return f"${price:,.6f}"
    return f"${price:,.2f}"


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

    today_trades = _load_trades(today_only=True)

    # The account block covers one broker account, so cumulative P&L spans both
    # paths even though everything below it is reported separately.
    from crypto.trader import load_trades as load_crypto_trades
    all_trades = _load_trades(today_only=False) + load_crypto_trades()

    all_pnl    = sum(t.get("pnl") or 0 for t in all_trades)
    all_wins   = sum(1 for t in all_trades if (t.get("pnl") or 0) > 0)
    all_losses = sum(1 for t in all_trades if (t.get("pnl") or 0) < 0)
    all_pnl_pct = (all_pnl / max(portfolio - all_pnl, 1) * 100)

    # ── Open positions ────────────────────────────────────────────────────────
    from trader._utils import equity_positions
    try:
        positions = equity_positions(client)
    except Exception:
        positions = []

    from trader.order_placer import load_orders_today
    orders_today = load_orders_today()
    buys_today = list(orders_today.keys())

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

    lines += _asset_section(
        "📈 *Stocks*", positions, today_trades, buys_today,
        _equity_watchlist(), levels=orders_today, crypto=False,
    )
    lines += _crypto_section()

    return "\n".join(lines)


def _equity_watchlist() -> list[str]:
    from data.db import load_signals
    candidates = load_signals("us_equity", min_score=config.MIN_SCORE)
    return [c["symbol"] for c in candidates[:config.AUTO_ORDER_TOP_N]]


def _crypto_section() -> list[str]:
    """Crypto's own block — same analysis, its own positions, ledger and watchlist.

    Everything here reads from the crypto path's files rather than the equity
    ones: entries, targets and realised trades are tracked separately because
    the two paths size, exit and book positions differently.
    """
    from notifier.feature_flags import is_enabled
    from crypto.trader import (_client, _load_state, load_trades, load_watchlist,
                               open_crypto_positions)

    try:
        positions = open_crypto_positions(_client())
    except Exception as exc:
        log.warning("Failed to fetch crypto positions: %s", exc)
        positions = {}

    trades_today = load_trades(today_only=True)
    state = _load_state()
    watchlist = load_watchlist()

    if not is_enabled("crypto") and not positions and not trades_today:
        return ["\n₿ *Crypto* — ⚪️ off (`/setfeature=crypto_on`)"]

    today = date.today().isoformat()
    buys_today = [s for s, rec in state.items() if rec.get("entry_date") == today]

    title = "₿ *Crypto*" if is_enabled("crypto") else "₿ *Crypto* _(flag off — winding down)_"
    lines = _asset_section(
        title, list(positions.values()), trades_today, buys_today,
        [c["symbol"] for c in watchlist.get("candidates", []) if c.get("eligible")],
        levels=state, crypto=True,
    )

    if watchlist.get("computed_at"):
        scanned = datetime.fromisoformat(watchlist["computed_at"]).astimezone(ET)
        lines.append(f"  _last scan {scanned.strftime('%H:%M ET')}_")
    return lines


def _asset_section(title: str, positions: list, trades_today: list[dict],
                   buys_today: list[str], watchlist: list[str],
                   levels: dict, crypto: bool) -> list[str]:
    """One asset class: open positions, today's activity, per-symbol detail."""
    realized = sum(t.get("pnl") or 0 for t in trades_today)
    wins     = sum(1 for t in trades_today if (t.get("pnl") or 0) > 0)
    losses   = sum(1 for t in trades_today if (t.get("pnl") or 0) < 0)
    closed   = wins + losses
    win_rate = int(wins / closed * 100) if closed else 0
    sells    = [t["symbol"] for t in trades_today]

    lines = [f"\n{title} — {len(positions)} open"]

    if not positions:
        lines.append("  No open positions.")
    for pos in positions:
        sym      = _symbol_of(pos, crypto)
        unpl     = float(pos.unrealized_pl)
        unpl_pct = float(pos.unrealized_plpc) * 100
        rec      = levels.get(sym, {})
        stop     = rec.get("stop_price")
        target   = rec.get("tp_price")

        marks = []
        if stop:
            marks.append(f"Stop `{_fmt_price(stop, crypto)}`")
        if target:
            marks.append(f"Target `{_fmt_price(target, crypto)}`")
        marks_str = ("  " + "  ".join(marks)) if marks else ""

        lines.append(
            f"`{sym}` {_fmt_qty(float(pos.qty), crypto)} @ "
            f"`{_fmt_price(float(pos.avg_entry_price), crypto)}` → `{_fmt_price(float(pos.current_price), crypto)}`\n"
            f"  {_pnl_icon(unpl)} Total `{_fmt_money(unpl)}` (`{unpl_pct:+.1f}%`)"
            f"  Today `{_fmt_money(float(pos.unrealized_intraday_pl))}` "
            f"(`{float(pos.unrealized_intraday_plpc) * 100:+.1f}%`){marks_str}"
        )

    lines += [
        f"  Buys today:    `{len(buys_today)} — {', '.join(buys_today) or 'none'}`",
        f"  Sells today:   `{len(sells)} — {', '.join(sells) or 'none'}`",
        f"  Realized P&L:  {_pnl_icon(realized)} `{_fmt_money(realized)}`",
        f"  Win rate:      `{win_rate}%  ({wins}W / {losses}L)`",
    ]

    shown = set()
    for t in trades_today:
        sym = t["symbol"]
        shown.add(sym)
        pnl = t.get("pnl") or 0
        lines.append(
            f"  {'✅' if pnl >= 0 else '❌'} `{sym}` {_exit_label(t['reason'])} "
            f"`{_fmt_price(t.get('entry_price', 0.0), crypto)}` → `{_fmt_price(t.get('exit_price', 0.0), crypto)}`"
            f"  `{_fmt_money(pnl)}`"
        )

    for pos in positions:
        sym = _symbol_of(pos, crypto)
        if sym not in shown:
            shown.add(sym)
            lines.append(f"  🔄 `{sym}` — open position")

    for sym in watchlist:
        if sym not in shown:
            shown.add(sym)
            lines.append(f"  🔍 `{sym}` — on watchlist")

    return lines


def _symbol_of(pos, crypto: bool) -> str:
    if not crypto:
        return pos.symbol
    from crypto.trader import slash_symbol
    return slash_symbol(pos.symbol)


def _exit_label(reason: str) -> str:
    """Compact tag for an exit. Both paths name their reasons differently."""
    known = {
        "take_profit": "TP", "take profit": "TP",
        "stop_loss": "SL", "stop hit": "SL", "gap-down stop": "SL",
        "max hold": "HOLD",
    }
    if reason in known:
        return known[reason]
    return "RSI" if "rsi" in reason.lower() else "EXIT"
