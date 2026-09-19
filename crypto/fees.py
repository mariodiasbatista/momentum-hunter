"""Alpaca's crypto fee, measured rather than assumed.

Alpaca charges the fee and exposes it almost nowhere: orders carry no fee field,
`accrued_fees` stays at zero, and the CFEE activity type posts only sporadically
(the 2026-09-19 LINK round trip never produced one). The single reliable
observation is a buy's quantity gap — you pay for `filled_qty` but the position
receives `filled_qty × (1 - taker)`, exact to nine decimal places.

Two consequences shape this module:

  1. Every entry is a market buy, so that gap is always the *taker* rate, and the
     taker rate identifies which published volume tier the account is on. Reading
     it per trade means a tier change is picked up automatically instead of
     silently over-charging against a hardcoded 0.25%.
  2. The two sides are not symmetric. A take-profit rests on the book as a limit
     and fills as a *maker*, a third cheaper than the taker rate; only stops, RSI
     exits and max-hold exits sell at market and pay taker. The maker half cannot
     be observed the same way — a sell's fee comes out of USD proceeds, which
     equity fills also move — so it is read off the tier the buy revealed.
"""

# Published 30-day volume tiers as (taker, maker), highest fee first.
_TIERS = [
    (0.0025, 0.0015),   # $0 – 100k
    (0.0022, 0.0012),   # 100k – 500k
    (0.0020, 0.0010),   # 500k – 1M
    (0.0018, 0.0008),   # 1M – 10M
    (0.0015, 0.0005),   # 10M – 25M
    (0.0013, 0.0002),   # 25M – 50M
    (0.0012, 0.0002),   # 50M – 100M
    (0.0010, 0.0000),   # 100M+
]

# Deliberately far above the published schedule rather than pegged to it: a ceiling
# of 0.25% would reject the true reading if Alpaca ever raised its fees, leaving the
# stale constant in place — the exact failure this measurement exists to prevent.
# What it does rule out is a gap that is not a fee at all (a partial fill, or a
# position that already held units of the asset), which runs to whole percent.
_MAX_PLAUSIBLE_TAKER = 0.01


def observed_taker_rate(paid_qty: float, received_qty: float) -> float | None:
    """Fee rate a market buy was charged, or None if the quantities cannot show one."""
    if paid_qty <= 0 or received_qty <= 0:
        return None
    rate = 1 - received_qty / paid_qty
    return rate if 0 <= rate <= _MAX_PLAUSIBLE_TAKER else None


def maker_rate(taker: float) -> float:
    """Maker half of whichever published tier this taker rate belongs to."""
    return min(_TIERS, key=lambda tier: abs(tier[0] - taker))[1]


def net_pnl(entry_price: float, exit_price: float, qty: float,
            buy_fee: float, sell_fee: float) -> tuple[float, float]:
    """P&L after fees on both sides. Returns (dollars, percent).

    The buy fee is charged in the asset — you pay for `filled_qty` but hold less —
    so the cost basis of the units actually held is `entry / (1 - buy_fee)`. The
    sell fee comes out of the proceeds, so they realise at `exit × (1 - sell_fee)`.

    Gross P&L overstates a round trip by roughly 0.4% of notional, which is ~13% of
    the gain at the 3% target floor most majors clamp to. It also compounds with
    churn, so a ledger that ignores it cannot answer whether re-entering a symbol
    is profitable.
    """
    basis    = entry_price / (1 - buy_fee)
    proceeds = exit_price * (1 - sell_fee)
    return (proceeds - basis) * qty, (proceeds - basis) / basis * 100
