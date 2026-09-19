"""Crypto entry and exit execution — separate from the equity order path.

Two reasons this cannot share `trader/order_placer.py`:

  1. Alpaca does not support bracket, OCO or OTO order classes for crypto — only
     market, limit and stop-limit, with GTC or IOC. The equity path's entire
     execution model is a bracket order, so there is nothing to reuse.

     The take-profit is therefore carried as a *standalone* resting GTC limit
     sell, placed as soon as the entry fills. Measured on 120 days of hourly bars,
     polling the target every 4 hours misses 18.6% of the moves that touch it —
     the price spikes through and round-trips between two checks — and tightening
     the loop to 1h still misses 13.5%. Since the MFE backtest that chose these
     targets assumed a touch is a fill, only a resting order makes live behaviour
     match the backtest.

     The stop cannot rest alongside it: without OCO a second sell order would
     reserve the same quantity twice. It stays on the cycle poll, so a stop exits
     roughly 0.5pt worse than intended at the median. Anything that sells from
     the poll must cancel the resting target first, or the quantity is locked.
  2. Crypto is fractionable, so positions are sized by notional dollars. The
     equity path rounds to whole shares with a `max(1, …)` floor, which for a
     $250 slot would buy one whole BTC.

Capital is ring-fenced: crypto counts only crypto positions against
CRYPTO_MAX_CONCURRENT, and the equity path excludes crypto from its own cap, so
neither can crowd the other out.

Exits follow TP-new-ST H=7, backtested 2026-09-17. The ladder is ordered so a
loss resolves before any upside: stop, take-profit, RSI exhaustion, max hold.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import config
from crypto import fees
from crypto.exit_levels import adaptive_tp
from trader._utils import log_api_error

log = logging.getLogger("crypto.trader")

_STATE_FILE = Path(__file__).parent.parent / "data" / "crypto_positions.json"

# Crypto keeps its own realised-trade ledger. It cannot share data/trades.json:
# that file is written by trader/trade_recorder.py, which resolves entry prices
# from the equity orders_placed.json and stores qty as an int.
_TRADES_FILE = Path(__file__).parent.parent / "data" / "crypto_trades.json"

# A full crypto scan takes ~12s, far too slow to run inside an interactive
# /summary. The 4-hourly cycle writes its result here instead.
_WATCHLIST_FILE = Path(__file__).parent.parent / "data" / "crypto_watchlist.json"


# ── Position state ───────────────────────────────────────────────────────────
# Alpaca owns qty and average entry price. This file owns what the broker does
# not track for us: when we entered and what target we derived at entry.

def _load_state() -> dict:
    try:
        return json.loads(_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _record_entry(symbol: str, entry_price: float, stop_price: float,
                  tp_pct: float, tp_kind: str, fee_rate: float) -> None:
    state = _load_state()
    state[symbol] = {
        "entry_date": date.today().isoformat(),
        "entry_price": entry_price,
        "stop_price": stop_price,
        "tp_pct": tp_pct,
        "tp_price": entry_price * (1 + tp_pct),
        "tp_kind": tp_kind,
        # Measured on this buy, so the exit prices its fee off the tier the account
        # was actually on rather than a constant that may have gone stale.
        "fee_rate": fee_rate,
    }
    _save_state(state)


def _forget(symbol: str) -> None:
    state = _load_state()
    state.pop(symbol, None)
    _save_state(state)


def load_trades(today_only: bool = False) -> list[dict]:
    """Realised crypto trades, all-time or just today's."""
    try:
        data = json.loads(_TRADES_FILE.read_text())
    except Exception:
        return []
    if today_only:
        return data.get(date.today().isoformat(), [])
    return [t for day in data.values() for t in day]


def load_watchlist() -> dict:
    """Last cycle's scan result: {"computed_at": iso, "candidates": [...]}."""
    try:
        return json.loads(_WATCHLIST_FILE.read_text())
    except Exception:
        return {}


def _save_watchlist(candidates: list[dict], eligible: list[dict]) -> None:
    eligible_symbols = {c["symbol"] for c in eligible}
    _WATCHLIST_FILE.write_text(json.dumps({
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "candidates": [{
            "symbol": c["symbol"],
            "score": c["score"],
            "adx": round(c["momentum"]["adx"], 1),
            "rsi": round(c["momentum"]["rsi"], 1),
            "rs_return": c["relative_strength"]["rs_return"],
            "eligible": c["symbol"] in eligible_symbols,
        } for c in candidates],
    }, indent=2))


def _buy_fee(rec: dict) -> float:
    """Taker rate measured when this position was opened, or the configured default.

    Positions opened before the rate was measured per trade have no recorded value.
    """
    return float(rec.get("fee_rate") or config.CRYPTO_FEE_TAKER_PCT)


def _record_trade(trade: dict) -> None:
    try:
        data = json.loads(_TRADES_FILE.read_text()) if _TRADES_FILE.exists() else {}
    except Exception:
        data = {}
    data.setdefault(date.today().isoformat(), []).append(trade)
    _TRADES_FILE.write_text(json.dumps(data, indent=2))


# ── Alpaca helpers ───────────────────────────────────────────────────────────

def _client():
    from alpaca.trading.client import TradingClient
    paper = "paper-api" in config.ALPACA_BASE_URL
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_SECRET_KEY, paper=paper)


def slash_symbol(symbol: str) -> str:
    """Alpaca reports crypto positions as BTCUSD; orders and our files use BTC/USD."""
    bare = symbol.replace("/", "")
    quote = config.CRYPTO_QUOTE.lstrip("/")
    return f"{bare[:-len(quote)]}/{quote}" if bare.endswith(quote) else symbol


def open_crypto_positions(client) -> dict[str, object]:
    """Open crypto positions keyed by slash-form symbol (BTC/USD)."""
    from alpaca.trading.enums import AssetClass
    return {slash_symbol(p.symbol): p
            for p in client.get_all_positions()
            if p.asset_class == AssetClass.CRYPTO}


def has_open_positions() -> bool:
    """Cheap check used to decide whether a flag-off cycle still has work to do."""
    return bool(open_crypto_positions(_client()))


def latest_prices(pairs: list[str]) -> dict[str, float]:
    if not pairs:
        return {}
    from alpaca.data.requests import CryptoLatestTradeRequest
    from data.alpaca_client import _crypto
    try:
        trades = _crypto().get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=pairs))
        return {s: float(t.price) for s, t in trades.items()}
    except Exception as exc:
        log.warning("[crypto] Latest price fetch failed: %s", exc)
        return {}


# ── Resting take-profit orders ───────────────────────────────────────────────

def resting_tp_orders(client) -> dict[str, object]:
    """Open crypto limit SELLs, keyed by slash-form symbol — our resting targets."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import AssetClass, OrderSide, OrderType, QueryOrderStatus

    try:
        orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.OPEN, side=OrderSide.SELL, limit=200))
    except Exception as exc:
        log_api_error(log, "[crypto] Failed to list open sell orders", exc)
        return {}

    return {slash_symbol(o.symbol): o for o in orders
            if o.asset_class == AssetClass.CRYPTO and o.order_type == OrderType.LIMIT}


def _cancel_tp_order(client, symbol: str, resting: dict) -> bool:
    """Release the quantity a resting target is holding so a market sell can run."""
    order = resting.get(symbol)
    if order is None:
        return True
    try:
        client.cancel_order_by_id(order.id)
        log.info("[crypto] Cancelled resting target on %s to free qty", symbol)
        return True
    except Exception as exc:
        log_api_error(log, f"[crypto] Failed to cancel resting target on {symbol}", exc)
        return False


def ensure_tp_orders(client=None) -> list[str]:
    """Guarantee every open position has its target resting at the broker.

    Runs every cycle rather than only after entry, so a position also regains its
    target after a failed submission, a restart, or a manual cancellation.
    """
    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    client = client or _client()
    positions = open_crypto_positions(client)
    if not positions:
        return []

    resting = resting_tp_orders(client)
    state = _load_state()
    placed = []

    for symbol, pos in positions.items():
        if symbol in resting:
            continue
        tp_price = state.get(symbol, {}).get("tp_price")
        if not tp_price:
            log.warning("[crypto] %s has no recorded target — cannot rest one", symbol)
            continue
        # Anything already reserved by another order is not ours to sell.
        qty = float(pos.qty_available or 0)
        if qty <= 0:
            log.debug("[crypto] %s has no free qty to rest a target against", symbol)
            continue
        try:
            client.submit_order(LimitOrderRequest(
                symbol=symbol,
                qty=str(qty),
                side=OrderSide.SELL,
                limit_price=round(float(tp_price), 6),
                time_in_force=TimeInForce.GTC,
            ))
        except Exception as exc:
            log_api_error(log, f"[crypto] ❌ Failed to rest target on {symbol}", exc)
            continue
        log.info("[crypto] 🎯 Target resting on %s @ $%.6f (qty %s)", symbol, tp_price, qty)
        placed.append(symbol)

    return placed


def _booked_order_ids() -> set:
    return {t["order_id"] for t in load_trades() if t.get("order_id")}


def record_tp_fills(client=None) -> list[dict]:
    """Book targets the broker filled between cycles.

    A resting limit is filled by Alpaca, so by the time the cycle runs the position
    is simply gone. Without this the trade would never reach the ledger, /summary
    or Telegram — it would look like the position silently vanished.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import (AssetClass, OrderSide, OrderStatus, OrderType,
                                      QueryOrderStatus)

    client = client or _client()
    lookback = datetime.now(timezone.utc) - timedelta(days=config.CRYPTO_MAX_HOLD_DAYS + 1)
    try:
        orders = client.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, side=OrderSide.SELL,
            after=lookback, limit=200))
    except Exception as exc:
        log_api_error(log, "[crypto] Failed to fetch closed sell orders", exc)
        return []

    booked = _booked_order_ids()
    state = _load_state()
    recorded = []

    for order in orders:
        if order.asset_class != AssetClass.CRYPTO:
            continue
        if order.status != OrderStatus.FILLED or not order.filled_avg_price:
            continue
        if str(order.id) in booked:
            continue

        symbol = slash_symbol(order.symbol)
        rec = state.get(symbol)
        if not rec:
            # No entry record: either already booked and forgotten by a previous
            # cycle, or not ours. Either way there is no basis to compute P&L.
            continue

        entry = float(rec["entry_price"])
        exit_price = float(order.filled_avg_price)
        qty = float(order.filled_qty or 0)
        try:
            held = (date.today() - date.fromisoformat(rec["entry_date"])).days
        except Exception:
            held = 0

        # This sell rested on the book before it filled, so it is charged the maker
        # rate — roughly a third less than the taker rate the entry paid.
        buy_fee = _buy_fee(rec)
        sell_fee = (fees.maker_rate(buy_fee) if order.order_type == OrderType.LIMIT
                    else buy_fee)
        pnl, pnl_pct = fees.net_pnl(entry, exit_price, qty, buy_fee, sell_fee)
        trade = {
            "symbol": symbol, "reason": "take profit",
            "entry_price": entry, "exit_price": exit_price, "qty": qty,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "gross_pnl": round((exit_price - entry) * qty, 2),
            "held_days": held,
            "exited_at": str(order.filled_at),
            "order_id": str(order.id),
        }
        _record_trade(trade)
        _forget(symbol)
        log.info("[crypto] 🎯 Target filled %s @ $%.4f | %+.2f%% net | $%+.2f",
                 symbol, exit_price, pnl_pct, pnl)
        recorded.append({**trade, "price": exit_price, "entry": entry})

    return recorded


# ── Exits ────────────────────────────────────────────────────────────────────

def manage_exits(indicators: dict[str, dict]) -> list[dict]:
    """Close positions that hit a stop, target, RSI exhaustion or the hold limit.

    `indicators` maps symbol → {"rsi": float} from the daily scan.
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    client = _client()
    positions = open_crypto_positions(client)
    if not positions:
        return []

    state = _load_state()
    prices = latest_prices(list(positions))
    resting = resting_tp_orders(client)
    today = date.today()
    closed = []

    for symbol, pos in positions.items():
        price = prices.get(symbol)
        if price is None:
            log.warning("[crypto] No price for %s — leaving position open", symbol)
            continue

        rec = state.get(symbol, {})
        entry = float(rec.get("entry_price") or pos.avg_entry_price)
        stop = rec.get("stop_price", entry * (1 - config.CRYPTO_STOP_PCT))
        tp_price = rec.get("tp_price", entry * (1 + config.CRYPTO_TP_FLAT))
        gain = (price - entry) / entry
        rsi = indicators.get(symbol, {}).get("rsi")

        try:
            held = (today - date.fromisoformat(rec["entry_date"])).days
        except Exception:
            held = 0

        if price <= stop:
            reason = "stop hit"
        elif price >= tp_price:
            reason = "take profit"
        elif rsi is not None and rsi > config.CRYPTO_RSI_EXIT and gain >= 0:
            reason = f"RSI>{config.CRYPTO_RSI_EXIT}"
        elif held >= config.CRYPTO_MAX_HOLD_DAYS:
            reason = "max hold"
        else:
            continue

        # The resting target holds this quantity — the market sell is rejected
        # until it is released.
        if not _cancel_tp_order(client, symbol, resting):
            continue

        qty = str(pos.qty)
        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            ))
        except Exception as exc:
            log_api_error(log, f"[crypto] ❌ Failed to close {symbol}", exc)
            continue

        _forget(symbol)
        # Sold at market, so both sides of this round trip pay the taker rate.
        buy_fee = _buy_fee(rec)
        pnl, pnl_pct = fees.net_pnl(entry, price, float(pos.qty), buy_fee, buy_fee)
        log.info("[crypto] 🔻 Closed %s @ $%.4f | %s | %+.2f%% net | $%+.2f",
                 symbol, price, reason, pnl_pct, pnl)
        trade = {
            "symbol": symbol, "reason": reason,
            "entry_price": entry, "exit_price": price, "qty": float(pos.qty),
            "pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 2),
            "gross_pnl": round((price - entry) * float(pos.qty), 2),
            "held_days": held, "exited_at": datetime.now(timezone.utc).isoformat(),
            # Lets record_tp_fills recognise this sell as already booked.
            "order_id": str(getattr(order, "id", "")),
        }
        _record_trade(trade)
        closed.append({**trade, "price": price, "entry": entry})

    return closed


# ── Entries ──────────────────────────────────────────────────────────────────

def _await_fill(client, order_id, timeout: float = 20.0):
    """Wait briefly for a market buy to fill, or None if it does not.

    Crypto trades continuously so these fill in seconds. Waiting matters because
    the resting target is priced off the fill and cannot be placed until the
    quantity exists; on timeout the caller falls back to the pre-trade quote and
    the next cycle's ensure_tp_orders covers the position.
    """
    import time
    from alpaca.trading.enums import OrderStatus

    # Compare enum members, never str(): the SDK renders these as
    # "OrderStatus.FILLED", so a string comparison silently never matches.
    dead = {OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            order = client.get_order_by_id(order_id)
        except Exception as exc:
            log.warning("[crypto] Could not read order %s: %s", order_id, exc)
            return None
        if order.status == OrderStatus.FILLED and order.filled_avg_price:
            return order
        if order.status in dead:
            log.warning("[crypto] Entry order %s ended %s", order_id, order.status)
            return None
        time.sleep(1.0)
    log.warning("[crypto] Entry order %s did not fill within %.0fs", order_id, timeout)
    return None


def _measure_fee(client, symbol: str, filled) -> float:
    """Taker rate this buy paid, from the gap between the qty paid for and received.

    Alpaca deducts the crypto fee in the asset and reports it nowhere on the order,
    so this gap is the only reliable reading of it. Falls back to the configured
    rate when the fill or the fresh position cannot be read — the entry is already
    open by then and is not worth unwinding over a fee measurement.
    """
    default = config.CRYPTO_FEE_TAKER_PCT
    if filled is None:
        return default
    try:
        pos = open_crypto_positions(client).get(symbol)
        rate = fees.observed_taker_rate(float(filled.filled_qty or 0), float(pos.qty))
    except Exception as exc:
        log.warning("[crypto] Could not measure fee on %s: %s", symbol, exc)
        return default
    if rate is None:
        log.warning("[crypto] %s fee reading implausible — using %.4f%%",
                    symbol, default * 100)
        return default
    if abs(rate - default) > 1e-6:
        log.info("[crypto] %s charged %.4f%%, not the configured %.4f%% — "
                 "the volume tier may have changed", symbol, rate * 100, default * 100)
    return rate


def place_entries(candidates: list[dict], bars: dict) -> list[dict]:
    """Buy up to CRYPTO_ORDER_TOP_N candidates at CRYPTO_POSITION_SIZE_DOLLARS each."""
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    if not candidates:
        return []

    client = _client()
    positions = open_crypto_positions(client)
    n_open = len(positions)

    if n_open >= config.CRYPTO_MAX_CONCURRENT:
        log.info("[crypto] 🚫 Position cap reached — %d/%d open. No new orders.",
                 n_open, config.CRYPTO_MAX_CONCURRENT)
        return []

    prices = latest_prices([c["symbol"] for c in candidates])
    placed = []

    for c in candidates:
        symbol = c["symbol"]
        if len(placed) >= config.CRYPTO_ORDER_TOP_N:
            break
        if n_open + len(placed) >= config.CRYPTO_MAX_CONCURRENT:
            log.info("[crypto] Position cap reached mid-loop. Stopping.")
            break
        if symbol in positions:
            log.info("[crypto] Skip %s — already an open position", symbol)
            continue

        price = prices.get(symbol)
        if not price or price <= 0:
            log.warning("[crypto] Skip %s — no live price", symbol)
            continue

        atr = c["momentum"]["atr"]
        tp_pct, tp_kind = adaptive_tp(symbol, bars.get(symbol))

        try:
            order = client.submit_order(MarketOrderRequest(
                symbol=symbol,
                notional=config.CRYPTO_POSITION_SIZE_DOLLARS,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
            ))
        except Exception as exc:
            log_api_error(log, f"[crypto] ❌ Failed to buy {symbol}", exc)
            continue

        # Levels are derived from the actual fill, not the pre-trade quote — the
        # resting target is an absolute limit price and has to sit off the price
        # we truly paid.
        filled = _await_fill(client, order.id)
        entry_price = float(filled.filled_avg_price) if filled else price
        stop = min(entry_price - atr * 1.5, entry_price * (1 - config.CRYPTO_STOP_PCT))
        fee_rate = _measure_fee(client, symbol, filled)

        _record_entry(symbol, entry_price, stop, tp_pct, tp_kind, fee_rate)
        log.info("[crypto] ✅ %s $%d @ $%.4f | stop $%.4f | tp +%.1f%% (%s) | score %d",
                 symbol, config.CRYPTO_POSITION_SIZE_DOLLARS, entry_price, stop,
                 tp_pct * 100, tp_kind, c["score"])
        placed.append({
            "symbol": symbol, "price": entry_price, "stop_price": stop,
            "tp_pct": tp_pct, "tp_kind": tp_kind, "score": c["score"],
            "notional": config.CRYPTO_POSITION_SIZE_DOLLARS,
        })

    return placed


# ── Cycle ────────────────────────────────────────────────────────────────────

def run_cycle() -> dict:
    """One full crypto pass: exits first so freed slots are reusable immediately.

    The `crypto` flag gates entries only. Exits always run: crypto orders carry no
    broker-side stop (Alpaca has no crypto brackets), so this cycle is the only
    thing enforcing them. A flag that stopped exits too would strand open
    positions with no stop, no target and no max-hold until it was switched back on.
    """
    from notifier.feature_flags import is_enabled
    from crypto.scanner import scan, entry_filtered
    from data.db import load_all_bars

    entries_enabled = is_enabled("crypto")

    bars = load_all_bars("crypto")
    candidates = scan()
    indicators = {c["symbol"]: {"rsi": c["momentum"]["rsi"]} for c in candidates}

    # With entries off nothing is a buy candidate, so the watchlist is saved empty
    # rather than advertising names we will not act on.
    eligible = entry_filtered(candidates) if entries_enabled else []
    _save_watchlist(candidates, eligible)

    # Targets the broker filled between cycles must be booked before anything
    # else looks at positions or state.
    filled = record_tp_fills()
    closed = filled + manage_exits(indicators)
    placed = place_entries(eligible, bars)
    rested = ensure_tp_orders()

    log.info("[crypto] Cycle done — %d closed (%d by resting target), %d placed, "
             "%d target(s) rested, %d candidate(s)",
             len(closed), len(filled), len(placed), len(rested), len(candidates))
    return {"closed": closed, "placed": placed, "candidates": len(candidates)}


def send_cycle_summary(result: dict) -> None:
    from notifier.telegram import _send

    closed, placed = result["closed"], result["placed"]
    if not closed and not placed:
        log.debug("[crypto] Nothing to report this cycle")
        return

    lines = ["₿ *Crypto Cycle*\n"]
    for o in closed:
        icon = "🟢" if o["pnl"] >= 0 else "🔴"
        lines.append(
            f"{icon} Closed *{o['symbol']}* @ `${o['price']:,.4f}`\n"
            f"  {o['reason']} | `{o['pnl_pct']:+.2f}%` (`${o['pnl']:+.2f}`) | "
            f"{o['held_days']}d"
        )
    for o in placed:
        lines.append(
            f"📥 Bought *{o['symbol']}* `${o['notional']}` @ `${o['price']:,.4f}`\n"
            f"  stop `${o['stop_price']:,.4f}` | tp `+{o['tp_pct'] * 100:.1f}%` "
            f"({o['tp_kind']}) | score {o['score']}/8"
        )
    _send("\n".join(lines))
