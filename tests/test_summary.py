"""Coverage for the /summary split: stocks and crypto reported separately."""
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from notifier import summary


# ── Formatting ───────────────────────────────────────────────────────────────

def test_share_quantities_stay_whole():
    assert summary._fmt_qty(23.0, crypto=False) == "23sh"


def test_fractional_crypto_quantity_is_not_truncated_to_zero():
    """0.0033 BTC is a real position — `0sh` would read as nothing held."""
    assert summary._fmt_qty(0.00329, crypto=True) == "0.00329"
    assert summary._fmt_qty(1.5, crypto=True) == "1.5"


def test_sub_dollar_coins_get_more_precision_than_cents():
    assert summary._fmt_price(0.123456, crypto=True) == "$0.123456"
    assert summary._fmt_price(80_758.7, crypto=True) == "$80,758.70"
    # equities stay at cents even below a dollar
    assert summary._fmt_price(0.99, crypto=False) == "$0.99"


@pytest.mark.parametrize("reason,expected", [
    ("take_profit", "TP"),
    ("take profit", "TP"),
    ("stop_loss", "SL"),
    ("stop hit", "SL"),
    ("gap-down stop", "SL"),
    ("max hold", "HOLD"),
    ("RSI>65", "RSI"),
    ("15-min RSI=85.2 > 65 (overbought)", "RSI"),
    ("something else", "EXIT"),
])
def test_exit_labels_cover_both_paths_naming(reason, expected):
    assert summary._exit_label(reason) == expected


# ── Section rendering ────────────────────────────────────────────────────────

def _position(symbol, qty, entry, current, pnl, pnl_pct):
    return SimpleNamespace(
        symbol=symbol, qty=str(qty), avg_entry_price=str(entry),
        current_price=str(current), unrealized_pl=str(pnl),
        unrealized_plpc=str(pnl_pct / 100),
        unrealized_intraday_pl=str(pnl), unrealized_intraday_plpc=str(pnl_pct / 100),
    )


def _section(**kwargs):
    args = dict(title="📈 *Stocks*", positions=[], trades_today=[], buys_today=[],
                watchlist=[], levels={}, crypto=False)
    args.update(kwargs)
    return "\n".join(summary._asset_section(**args))


def test_empty_section_says_so():
    assert "No open positions." in _section()


def test_each_position_is_its_own_block():
    """Three-line position blocks run together as a wall of text without this."""
    out = _section(positions=[_position("AAA", 1, 100, 101, 1.0, 1.0),
                              _position("BBB", 1, 100, 101, 1.0, 1.0)])
    assert "(`+1.0%`)\n\n`BBB`" in out


def test_exit_flags_and_activity_stats_are_separated_groups():
    """The footer is three distinct ideas — nearest exits, today, then the index."""
    out = _section(positions=[_position("AAA", 1, 100, 100, 0.0, 0.0),
                              _position("BBB", 1, 100, 103, 3.0, 3.0)],
                   levels={"AAA": {"stop_price": 99.0, "take_price": 101.0},
                           "BBB": {"stop_price": 98.0, "take_price": 120.0}})
    assert "\n\n  Closest to exit:" in out
    assert "to stop\n\n  Buys today:" in out
    assert "0L)`\n\n  🔄 `AAA`" in out
    # label and each flag on their own line, not run together on one
    assert "  Closest to exit:\n    🎯 `AAA`" in out
    assert "to target\n    ⚠️ `BBB`" in out


def test_section_counts_open_positions_in_the_header():
    out = _section(positions=[_position("AAPL", 3, 100, 101, 3, 1)])
    assert "📈 *Stocks* — 1 open" in out


def test_win_rate_counts_only_closed_trades():
    trades = [{"symbol": "A", "pnl": 5.0, "reason": "take_profit"},
              {"symbol": "B", "pnl": -2.0, "reason": "stop_loss"},
              {"symbol": "C", "pnl": 0.0, "reason": "max hold"}]   # flat: neither
    out = _section(trades_today=trades)
    assert "Win rate:      `50%  (1W / 1L)`" in out
    assert "Realized P&L:  🟢 `+$3.00`" in out


def test_stop_and_target_levels_are_shown_when_known():
    out = _section(
        positions=[_position("BTCUSD", 0.003, 80_000, 81_000, 3.0, 1.25)],
        levels={"BTC/USD": {"stop_price": 76_000.0, "tp_price": 82_400.0}},
        crypto=True,
    )
    assert "Stop `$76,000.00`" in out
    assert "Target `$82,400.00`" in out


def test_equity_target_is_read_from_take_price():
    """The equity path records `take_price`, the crypto path `tp_price`."""
    out = _section(
        positions=[_position("AAPL", 3, 100, 101, 3.0, 1.0)],
        levels={"AAPL": {"stop_price": 95.0, "take_price": 110.0}},
    )
    assert "Target `$110.00`" in out


def test_gap_to_exit_is_measured_from_the_live_price():
    """8.91% to target, not 10% — the position has already moved 1 of the 10."""
    out = _section(
        positions=[_position("AAPL", 3, 100, 101, 3.0, 1.0)],
        levels={"AAPL": {"stop_price": 95.0, "take_price": 110.0}},
    )
    assert "Target `$110.00` (`+8.9%`)" in out
    assert "Stop `$95.00` (`-5.9%`)" in out


def test_flags_the_position_closest_to_each_exit():
    positions = [_position("NEAR_TP", 1, 100, 109, 9.0, 9.0),
                 _position("NEAR_SL", 1, 100, 96, -4.0, -4.0),
                 _position("MIDDLE", 1, 100, 102, 2.0, 2.0)]
    levels = {
        "NEAR_TP": {"stop_price": 80.0,  "take_price": 110.0},   # +0.9% to target
        "NEAR_SL": {"stop_price": 95.0,  "take_price": 130.0},   # -1.0% to stop
        "MIDDLE":  {"stop_price": 90.0,  "take_price": 120.0},
    }
    out = _section(positions=positions, levels=levels)
    assert "🎯 `NEAR_TP` `+0.9%` to target" in out
    assert "⚠️ `NEAR_SL` `-1.0%` to stop" in out
    # the markers land on the right position lines, not just the footer
    assert "Target `$110.00` (`+0.9%`) 🎯" in out
    assert "Stop `$95.00` (`-1.0%`) ⚠️" in out
    assert "Target `$120.00` (`+17.6%`)\n" in out + "\n"


def test_the_two_exit_flags_never_land_on_the_same_symbol():
    """A tight position is nearest on both counts; naming it twice would hide
    whatever is genuinely closest to stopping out."""
    positions = [_position("TIGHT", 1, 100, 100, 0.0, 0.0),
                 _position("RUNNER_UP", 1, 100, 103, 3.0, 3.0)]
    levels = {
        "TIGHT":     {"stop_price": 99.0, "take_price": 101.0},   # nearest to both
        "RUNNER_UP": {"stop_price": 98.0, "take_price": 120.0},   # -4.9% to stop
    }
    out = _section(positions=positions, levels=levels)
    assert "🎯 `TIGHT` `+1.0%` to target" in out
    assert "⚠️ `RUNNER_UP` `-4.9%` to stop" in out
    assert "`TIGHT` `-1.0%` to stop" not in out


def test_a_lone_position_is_flagged_for_its_target_only():
    """With no runner-up the stop flag is dropped rather than reusing the name —
    one symbol cannot stand for two different warnings."""
    out = _section(positions=[_position("ONLY", 1, 100, 100, 0.0, 0.0)],
                   levels={"ONLY": {"stop_price": 95.0, "take_price": 110.0}})
    assert "🎯 `ONLY` `+10.0%` to target" in out
    assert "to stop" not in out


def test_closest_to_exit_is_scoped_to_one_asset_class():
    """Stocks and crypto each get their own pair of flags — _asset_section only
    ever sees one class, so the crypto winner cannot mask the equity one."""
    positions = [_position("BTCUSD", 0.003, 80_000, 81_000, 3.0, 1.25),
                 _position("ETHUSD", 0.5, 4_000, 3_900, -50.0, -2.5)]
    levels = {"BTC/USD": {"stop_price": 76_000.0, "tp_price": 82_400.0},
              "ETH/USD": {"stop_price": 3_850.0, "tp_price": 4_400.0}}
    out = _section(positions=positions, levels=levels, crypto=True)
    assert "🎯 `BTC/USD`" in out
    assert "⚠️ `ETH/USD`" in out


def test_position_past_its_target_still_counts_as_closest():
    """A resting target that has not filled yet is the nearest exit there is."""
    out = _section(
        positions=[_position("SPIKE", 1, 100, 115, 15.0, 15.0),
                   _position("OTHER", 1, 100, 101, 1.0, 1.0)],
        levels={"SPIKE": {"take_price": 110.0}, "OTHER": {"take_price": 111.0}},
    )
    assert "🎯 `SPIKE`" in out


def test_no_exit_flags_without_recorded_levels():
    out = _section(positions=[_position("AAPL", 3, 100, 101, 3.0, 1.0)])
    assert "Closest to exit" not in out
    assert "🎯" not in out


def test_watchlist_omits_symbols_already_held_or_closed():
    out = _section(
        positions=[_position("AAPL", 1, 1, 1, 0, 0)],
        trades_today=[{"symbol": "MSFT", "pnl": 1.0, "reason": "take_profit"}],
        watchlist=["AAPL", "MSFT", "NVDA"],
    )
    assert "🔍 `NVDA` — on watchlist" in out
    assert "🔍 `AAPL`" not in out
    assert "🔍 `MSFT`" not in out
    assert "🔄 `AAPL` — open position" in out


def test_crypto_positions_render_with_slash_symbols():
    """Alpaca reports BTCUSD; every other surface in the system says BTC/USD."""
    out = _section(positions=[_position("BTCUSD", 0.0031, 80_000, 81_000, 3.1, 1.25)],
                   crypto=True)
    assert "`BTC/USD` 0.0031 @ `$80,000.00` → `$81,000.00`" in out


# ── Crypto section wiring ────────────────────────────────────────────────────

@pytest.fixture
def crypto_files(tmp_path, monkeypatch):
    from crypto import trader
    from notifier import feature_flags
    files = SimpleNamespace(
        state=tmp_path / "crypto_positions.json",
        trades=tmp_path / "crypto_trades.json",
        watchlist=tmp_path / "crypto_watchlist.json",
        flags=tmp_path / "feature_flags.json",
    )
    monkeypatch.setattr(trader, "_STATE_FILE", files.state)
    monkeypatch.setattr(trader, "_TRADES_FILE", files.trades)
    monkeypatch.setattr(trader, "_WATCHLIST_FILE", files.watchlist)
    monkeypatch.setattr(feature_flags, "_FILE", files.flags)
    monkeypatch.setattr(trader, "_client", lambda: SimpleNamespace(get_all_positions=lambda: []))
    return files


def test_crypto_section_collapses_to_one_line_when_off_and_idle(crypto_files):
    out = "\n".join(summary._crypto_section())
    assert out == "\n₿ *Crypto* — ⚪️ off (`/setfeature=crypto_on`)"


def test_crypto_section_expands_once_the_flag_is_on(crypto_files):
    from notifier.feature_flags import set_flag
    set_flag("crypto", True)
    out = "\n".join(summary._crypto_section())
    assert "₿ *Crypto* — 0 open" in out
    assert "Realized P&L" in out


def test_crypto_section_still_reports_while_flag_is_off_if_positions_remain(crypto_files, monkeypatch):
    """Turning the flag off must not hide open crypto risk."""
    from crypto import trader
    pos = _position("BTCUSD", 0.003, 80_000, 81_000, 3.0, 1.25)
    monkeypatch.setattr(trader, "_client",
                        lambda: SimpleNamespace(get_all_positions=lambda: [pos]))
    monkeypatch.setattr(trader, "open_crypto_positions", lambda client: {"BTC/USD": pos})

    out = "\n".join(summary._crypto_section())
    assert "winding down" in out
    assert "`BTC/USD`" in out


def test_crypto_section_reads_its_own_ledger_and_watchlist(crypto_files):
    from notifier.feature_flags import set_flag
    set_flag("crypto", True)
    today = date.today().isoformat()
    crypto_files.trades.write_text(json.dumps({today: [
        {"symbol": "ETH/USD", "entry_price": 2_500.0, "exit_price": 2_600.0,
         "qty": 0.1, "reason": "take profit", "pnl": 10.0, "pnl_pct": 4.0},
    ]}))
    crypto_files.state.write_text(json.dumps({
        "SOL/USD": {"entry_date": today, "entry_price": 100.0,
                    "stop_price": 95.0, "tp_price": 103.0},
    }))
    crypto_files.watchlist.write_text(json.dumps({
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "candidates": [{"symbol": "LTC/USD", "score": 7, "eligible": True},
                       {"symbol": "XRP/USD", "score": 6, "eligible": False}],
    }))

    out = "\n".join(summary._crypto_section())
    assert "✅ `ETH/USD` TP `$2,500.00` → `$2,600.00`  `+$10.00`" in out
    assert "Buys today:    `1 — SOL/USD`" in out
    assert "Sells today:   `1 — ETH/USD`" in out
    assert "🔍 `LTC/USD` — on watchlist" in out
    assert "XRP/USD" not in out          # scanned but not entry-eligible
    assert "last scan" in out


def test_crypto_watchlist_is_cached_not_rescanned(crypto_files, monkeypatch):
    """A live scan takes ~12s — /summary must never trigger one."""
    from crypto import scanner
    from notifier.feature_flags import set_flag
    set_flag("crypto", True)
    monkeypatch.setattr(scanner, "scan", lambda *a, **k: pytest.fail("scanned during /summary"))
    summary._crypto_section()


def test_stale_watchlist_timestamp_is_still_shown(crypto_files):
    """A stale scan is reported with its time rather than silently presented as current."""
    from notifier.feature_flags import set_flag
    set_flag("crypto", True)
    old = datetime.now(timezone.utc) - timedelta(hours=9)
    crypto_files.watchlist.write_text(json.dumps(
        {"computed_at": old.isoformat(), "candidates": []}))

    out = "\n".join(summary._crypto_section())
    assert f"last scan {old.astimezone(summary.ET).strftime('%H:%M ET')}" in out


def test_crypto_section_survives_a_broker_outage(crypto_files, monkeypatch):
    from crypto import trader
    from notifier.feature_flags import set_flag
    set_flag("crypto", True)
    monkeypatch.setattr(trader, "open_crypto_positions",
                        lambda client: (_ for _ in ()).throw(RuntimeError("api down")))

    out = "\n".join(summary._crypto_section())
    assert "₿ *Crypto* — 0 open" in out


# ── Separation invariants ────────────────────────────────────────────────────

def test_stocks_block_excludes_crypto_positions(monkeypatch, crypto_files):
    from alpaca.trading.enums import AssetClass

    equity = _position("AAPL", 3, 100, 101, 3.0, 1.0)
    equity.asset_class = AssetClass.US_EQUITY
    btc = _position("BTCUSD", 0.003, 80_000, 81_000, 3.0, 1.25)
    btc.asset_class = AssetClass.CRYPTO

    client = SimpleNamespace(
        get_all_positions=lambda: [equity, btc],
        get_account=lambda: SimpleNamespace(
            portfolio_value="1000", cash="500", buying_power="2000",
            equity="1000", last_equity="990"),
    )
    monkeypatch.setattr(summary, "_get_client", lambda: client)
    monkeypatch.setattr(summary, "_load_trades", lambda today_only=False: [])
    monkeypatch.setattr(summary, "_equity_watchlist", lambda: [])
    monkeypatch.setattr("trader.order_placer.load_orders_today", lambda: {})

    out = summary.build_summary()
    stocks_block = out.split("₿ *Crypto*")[0]

    assert "📈 *Stocks* — 1 open" in stocks_block
    assert "AAPL" in stocks_block
    assert "BTC" not in stocks_block


def test_equity_recorder_ignores_crypto_sells(monkeypatch):
    """Crypto fills booked here would get a zero entry price and a zero int qty."""
    from alpaca.trading.enums import AssetClass
    from trader import trade_recorder

    crypto_sell = SimpleNamespace(
        symbol="BTC/USD", asset_class=AssetClass.CRYPTO, status="filled",
        filled_avg_price="81000", qty="0.003", order_type="market",
        filled_at="2026-09-18T12:00:00Z",
    )
    monkeypatch.setattr(trade_recorder, "_get_client",
                        lambda: SimpleNamespace(get_orders=lambda filter: [crypto_sell]))
    monkeypatch.setattr(trade_recorder, "_recorded_today", lambda: set())
    monkeypatch.setattr(trade_recorder, "_save_trade",
                        lambda trade: pytest.fail("crypto sell written to the equity ledger"))

    assert trade_recorder.scan_for_fills() == []


def test_crypto_ledger_round_trips(crypto_files):
    from crypto import trader
    assert trader.load_trades(today_only=True) == []

    trader._record_trade({"symbol": "BTC/USD", "pnl": 12.5, "reason": "take profit"})

    assert trader.load_trades(today_only=True) == [
        {"symbol": "BTC/USD", "pnl": 12.5, "reason": "take profit"}
    ]
    assert trader.load_trades() == trader.load_trades(today_only=True)


def test_closing_a_crypto_position_books_it_to_the_crypto_ledger(crypto_files, monkeypatch):
    from crypto import trader
    from alpaca.trading.enums import AssetClass

    pos = _position("BTCUSD", 0.01, 100.0, 109.0, 0.09, 9.0)
    pos.asset_class = AssetClass.CRYPTO
    orders = []
    client = SimpleNamespace(get_all_positions=lambda: [pos],
                             submit_order=lambda req: orders.append(req))
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 109.0})
    crypto_files.state.write_text(json.dumps({"BTC/USD": {
        "entry_date": date.today().isoformat(), "entry_price": 100.0,
        "stop_price": 95.0, "tp_price": 108.0,
    }}))

    closed = trader.manage_exits({})

    assert [c["reason"] for c in closed] == ["take profit"]
    booked = trader.load_trades(today_only=True)
    assert booked[0]["symbol"] == "BTC/USD"
    assert booked[0]["qty"] == 0.01          # fractional, not rounded away
    # $0.09 gross, but the 0.25%-a-side fee takes a cent of it
    assert booked[0]["gross_pnl"] == pytest.approx(0.09)
    assert booked[0]["pnl"] == pytest.approx(0.08)
