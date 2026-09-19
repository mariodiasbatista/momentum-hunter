import json
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest
from alpaca.trading.enums import OrderStatus

import config
from crypto import exit_levels, scanner, trader
from notifier import feature_flags


# ── Feature flag ─────────────────────────────────────────────────────────────

@pytest.fixture
def flags_file(tmp_path, monkeypatch):
    path = tmp_path / "feature_flags.json"
    monkeypatch.setattr(feature_flags, "_FILE", path)
    return path


def test_crypto_flag_defaults_off(flags_file):
    assert feature_flags.is_enabled("crypto") is False


def test_set_and_read_flag(flags_file):
    feature_flags.set_flag("crypto", True)
    assert feature_flags.is_enabled("crypto") is True
    feature_flags.set_flag("crypto", False)
    assert feature_flags.is_enabled("crypto") is False


def test_unknown_flag_rejected(flags_file):
    with pytest.raises(ValueError):
        feature_flags.set_flag("bogus", True)


def test_corrupt_flag_file_falls_back_to_off(flags_file):
    flags_file.write_text("{not json")
    assert feature_flags.is_enabled("crypto") is False


@pytest.mark.parametrize("arg,expected", [
    ("crypto_on", ("crypto", True)),
    ("crypto_off", ("crypto", False)),
    ("CRYPTO_ON", ("crypto", True)),
    ("crypto", None),
    ("bogus_on", None),
    ("", None),
])
def test_parse_arg(arg, expected):
    assert feature_flags.parse_arg(arg) == expected


def test_every_flag_has_a_description():
    """A flag with no description renders as a bare name in /feature."""
    assert set(feature_flags.DESCRIPTIONS) == set(feature_flags.DEFAULTS)


def test_status_reports_name_state_and_description(flags_file):
    assert feature_flags.status() == [
        ("crypto", False, feature_flags.DESCRIPTIONS["crypto"])
    ]
    feature_flags.set_flag("crypto", True)
    assert feature_flags.status() == [
        ("crypto", True, feature_flags.DESCRIPTIONS["crypto"])
    ]


def test_status_lists_known_flags_even_when_file_is_missing(flags_file):
    assert not flags_file.exists()
    assert [name for name, _, _ in feature_flags.status()] == sorted(feature_flags.DEFAULTS)


def test_feature_message_shows_status_and_the_toggle_that_flips_it(flags_file):
    from notifier.bot_listener import build_feature_message

    off = build_feature_message()
    assert "`crypto`" in off
    assert "OFF" in off
    assert feature_flags.DESCRIPTIONS["crypto"] in off
    # the suggested toggle must be the opposite of the current state
    assert "/setfeature=crypto_on" in off
    assert "/setfeature=crypto_off" not in off

    feature_flags.set_flag("crypto", True)
    on = build_feature_message()
    assert "ON" in on
    assert "/setfeature=crypto_off" in on


@pytest.mark.parametrize("cmd", ["/feature", "/features"])
def test_feature_command_replies_with_flag_status(monkeypatch, flags_file, cmd):
    from notifier import bot_listener
    sent = []
    monkeypatch.setattr(bot_listener, "_reply", lambda chat_id, text: sent.append(text))

    bot_listener._COMMANDS[cmd](1, [])

    assert len(sent) == 1
    assert "Feature Flags" in sent[0]
    assert "`crypto`" in sent[0]


def test_help_lists_the_feature_commands(monkeypatch):
    from notifier import bot_listener
    sent = []
    monkeypatch.setattr(bot_listener, "_reply", lambda chat_id, text: sent.append(text))

    bot_listener._COMMANDS["/help"](1, [])

    assert "/feature" in sent[0]
    assert "/setfeature" in sent[0]


def test_setfeature_equals_syntax_reaches_handler(monkeypatch, flags_file):
    """`/setfeature=crypto_on` arrives as one token — dispatch must split it."""
    from notifier import bot_listener
    sent = []
    monkeypatch.setattr(bot_listener, "_reply", lambda chat_id, text: sent.append(text))

    raw_text = "/setfeature=crypto_on"
    parts = raw_text.split("@")[0].split()
    cmd, args = parts[0], parts[1:]
    if "=" in cmd:
        cmd, value = cmd.split("=", 1)
        args = [value] + args

    assert cmd == "/setfeature"
    bot_listener._COMMANDS[cmd](1, args)
    assert feature_flags.is_enabled("crypto") is True
    assert "ON" in sent[0]


def _dispatch(raw_text: str) -> tuple[str, list]:
    """The command/argument split performed in run_forever()."""
    parts = raw_text.split("@")[0].split()
    cmd, args = parts[0], parts[1:]
    if "=" in cmd:
        cmd, value = cmd.split("=", 1)
        args = [value] + args
    return cmd, args


@pytest.mark.parametrize("raw_text,expected", [
    ("/setfeature=crypto_on", ("/setfeature", ["crypto_on"])),
    ("/setfeature crypto_on", ("/setfeature", ["crypto_on"])),
    ("/setfeature=crypto_on@momentumbot", ("/setfeature", ["crypto_on"])),
    ("/feature", ("/feature", [])),
    ("/setlevel 2", ("/setlevel", ["2"])),   # regression: plain commands still parse
])
def test_command_dispatch_split(raw_text, expected):
    assert _dispatch(raw_text) == expected


@pytest.mark.parametrize("args", [[], ["crypto"], ["bogus_on"]])
def test_setfeature_with_bad_argument_explains_usage_and_changes_nothing(
        monkeypatch, flags_file, args):
    from notifier import bot_listener
    sent = []
    monkeypatch.setattr(bot_listener, "_reply", lambda chat_id, text: sent.append(text))

    bot_listener._COMMANDS["/setfeature"](1, args)

    assert "Usage" in sent[0]
    assert "crypto_on" in sent[0]
    assert not flags_file.exists()


# ── Adaptive take-profit ─────────────────────────────────────────────────────

def test_mfe_distribution_is_sorted_pct_runs():
    closes = [100, 100, 100]
    highs = [100, 110, 105]
    assert exit_levels.mfe_distribution(closes, highs, 1) == pytest.approx([5.0, 10.0])


def test_reach_rate_counts_windows_at_or_above_level():
    assert exit_levels.reach_rate([1.0, 5.0, 9.0, 12.0], 8.0) == 50.0


def test_level_at_reach_probability_picks_the_quantile():
    dist = list(range(0, 100))
    # reached in 70% of windows → the 30th percentile of the sorted distribution
    assert exit_levels.level_at_reach_probability(dist, 0.7) == 30


def test_flat_target_kept_when_already_reachable():
    cfg = dict(config.CRYPTO_TP_CONFIG, min_windows=4)
    dist = [20.0] * 10          # 8% reached in 100% of windows
    assert exit_levels.resolve_take_profit(dist, 0.08, cfg) == 0.08


def test_target_adapts_down_when_flat_is_unreachable():
    cfg = dict(config.CRYPTO_TP_CONFIG, min_windows=4)
    dist = [1.0] * 9 + [2.0]    # 8% essentially never reached
    tp = exit_levels.resolve_take_profit(dist, 0.08, cfg)
    assert tp == cfg["tp_min"]  # clamped at the floor


def test_thin_history_returns_none():
    cfg = dict(config.CRYPTO_TP_CONFIG, min_windows=40)
    assert exit_levels.resolve_take_profit([5.0] * 10, 0.08, cfg) is None


def test_adaptive_tp_uses_only_pre_entry_bars():
    """A post-entry spike must not raise the target derived at entry."""
    dates = pd.date_range("2026-01-01", periods=200, freq="D")
    closes = [100.0] * 200
    highs = [101.0] * 100 + [500.0] * 100   # huge run, all after the entry date
    bars = pd.DataFrame({"close": closes, "high": highs}, index=dates)

    tp, kind = exit_levels.adaptive_tp("X/USD", bars, entry_date=dates[100])
    assert kind == "adaptive"
    assert tp == config.CRYPTO_TP_CONFIG["tp_min"]


def test_adaptive_tp_without_history_falls_back_to_flat():
    tp, kind = exit_levels.adaptive_tp("X/USD", None)
    assert (tp, kind) == (config.CRYPTO_TP_FLAT, "no-history")


# ── Universe filter ──────────────────────────────────────────────────────────

def _bars(close, volume, n=250):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": [close] * n, "volume": [volume] * n}, index=idx)


def test_universe_keeps_usd_majors_and_drops_the_rest():
    bars = {
        "BTC/USD":  _bars(76_000, 3),        # $228k/day — the major the unit-volume filter loses
        "BTC/USDT": _bars(76_000, 3),        # same asset, different quote
        "USDC/USD": _bars(1.0, 5_000_000),   # stablecoin
        "TINY/USD": _bars(0.01, 1_000),      # $10/day
    }
    assert scanner.eligible_pairs(bars) == ["BTC/USD"]


# ── Position sizing / exit ladder ────────────────────────────────────────────

class _FakePosition:
    def __init__(self, symbol, qty, entry, qty_available=None):
        from alpaca.trading.enums import AssetClass
        self.symbol = symbol
        self.qty = qty
        # Alpaca reports 0 here once a resting sell reserves the position.
        self.qty_available = qty if qty_available is None else qty_available
        self.avg_entry_price = entry
        self.asset_class = AssetClass.CRYPTO


class _FakeClient:
    def __init__(self, positions, open_orders=None, closed_orders=None):
        self._positions = positions
        self._open_orders = list(open_orders or [])
        self._closed_orders = list(closed_orders or [])
        self.orders = []
        self.cancelled = []

    def get_all_positions(self):
        return self._positions

    def submit_order(self, req):
        self.orders.append(req)
        return SimpleNamespace(id=f"fake-order-{len(self.orders)}")

    def get_orders(self, filter=None):
        from alpaca.trading.enums import QueryOrderStatus
        if filter is not None and filter.status == QueryOrderStatus.CLOSED:
            return self._closed_orders
        return self._open_orders

    def cancel_order_by_id(self, order_id):
        self.cancelled.append(order_id)
        self._open_orders = [o for o in self._open_orders if o.id != order_id]

    def get_order_by_id(self, order_id):
        raise RuntimeError("not used in these tests")


def _fake_order(symbol="BTCUSD", order_id="tp-1", order_type=None,
                status=None, filled_avg_price=None, filled_qty="2.5"):
    """Uses the real SDK enums — str(OrderStatus.FILLED) is 'OrderStatus.FILLED',
    so a fake built from plain strings would not catch comparison bugs."""
    from alpaca.trading.enums import AssetClass, OrderStatus, OrderType
    return SimpleNamespace(
        id=order_id, symbol=symbol,
        order_type=order_type or OrderType.LIMIT,
        status=status or OrderStatus.FILLED,
        asset_class=AssetClass.CRYPTO, filled_avg_price=filled_avg_price,
        filled_qty=filled_qty, filled_at="2026-09-18T12:00:00+00:00")


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "crypto_positions.json"
    monkeypatch.setattr(trader, "_STATE_FILE", path)
    return path


def _arm(state_file, symbol="BTC/USD", entry=100.0, stop=95.0, tp_price=108.0, days_ago=0):
    state_file.write_text(json.dumps({symbol: {
        "entry_date": (date.today() - timedelta(days=days_ago)).isoformat(),
        "entry_price": entry, "stop_price": stop,
        "tp_pct": (tp_price - entry) / entry, "tp_price": tp_price, "tp_kind": "adaptive",
    }}))


@pytest.mark.parametrize("price,rsi,days_ago,expected", [
    (94.0,  40, 0, "stop hit"),
    (109.0, 40, 0, "take profit"),
    (103.0, 80, 0, "RSI>65"),
    (103.0, 40, 7, "max hold"),
    (103.0, 40, 0, None),      # nothing triggered — position stays open
])
def test_exit_ladder(monkeypatch, state_file, price, rsi, days_ago, expected):
    _arm(state_file, days_ago=days_ago)
    client = _FakeClient([_FakePosition("BTCUSD", "2.5", 100.0)])
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": price})

    closed = trader.manage_exits({"BTC/USD": {"rsi": rsi}})

    if expected is None:
        assert closed == []
        assert client.orders == []
    else:
        assert [c["reason"] for c in closed] == [expected]
        assert len(client.orders) == 1
        assert client.orders[0].symbol == "BTC/USD"
        assert json.loads(state_file.read_text()) == {}


def test_losing_position_does_not_rsi_exit(monkeypatch, state_file):
    """RSI exhaustion only closes at breakeven or better — never crystallises a loss."""
    _arm(state_file)
    client = _FakeClient([_FakePosition("BTCUSD", "2.5", 100.0)])
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 97.0})

    assert trader.manage_exits({"BTC/USD": {"rsi": 90}}) == []


# ── Resting take-profit orders ───────────────────────────────────────────────

@pytest.fixture
def trades_file(tmp_path, monkeypatch):
    path = tmp_path / "crypto_trades.json"
    monkeypatch.setattr(trader, "_TRADES_FILE", path)
    return path


def test_target_rests_at_broker_as_gtc_limit_sell(monkeypatch, state_file):
    """The whole point: the target sits at Alpaca, not in a 4-hourly poll."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    _arm(state_file, tp_price=108.0)
    client = _FakeClient([_FakePosition("BTCUSD", "2.5", 100.0)])

    rested = trader.ensure_tp_orders(client)

    assert rested == ["BTC/USD"]
    order = client.orders[0]
    assert order.symbol == "BTC/USD"
    assert order.side == OrderSide.SELL
    assert order.time_in_force == TimeInForce.GTC
    assert float(order.limit_price) == 108.0


def test_target_is_not_duplicated_when_one_already_rests(monkeypatch, state_file):
    _arm(state_file)
    client = _FakeClient([_FakePosition("BTCUSD", "2.5", 100.0)],
                         open_orders=[_fake_order(status=OrderStatus.NEW)])

    assert trader.ensure_tp_orders(client) == []
    assert client.orders == []


def test_no_target_rested_without_a_recorded_level(state_file):
    """Never guess a target — an unknown level means no resting order at all."""
    state_file.write_text(json.dumps({}))
    client = _FakeClient([_FakePosition("BTCUSD", "2.5", 100.0)])

    assert trader.ensure_tp_orders(client) == []
    assert client.orders == []


def test_poll_exit_cancels_resting_target_before_market_sell(monkeypatch, state_file):
    """Without the cancel the resting order still reserves the qty and the sell fails."""
    _arm(state_file)
    resting = _fake_order(order_id="tp-9", status=OrderStatus.NEW)
    client = _FakeClient([_FakePosition("BTCUSD", "2.5", 100.0)], open_orders=[resting])
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 94.0})

    closed = trader.manage_exits({"BTC/USD": {"rsi": 40}})

    assert [c["reason"] for c in closed] == ["stop hit"]
    assert client.cancelled == ["tp-9"]


def test_position_stays_open_if_resting_target_cannot_be_cancelled(monkeypatch, state_file):
    """A failed cancel must not leave us believing we sold — the qty is still locked."""
    _arm(state_file)
    client = _FakeClient([_FakePosition("BTCUSD", "2.5", 100.0)],
                         open_orders=[_fake_order(order_id="tp-9", status=OrderStatus.NEW)])
    monkeypatch.setattr(client, "cancel_order_by_id",
                        lambda order_id: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 94.0})

    assert trader.manage_exits({"BTC/USD": {"rsi": 40}}) == []
    assert client.orders == []
    assert json.loads(state_file.read_text())          # state kept — still holding


def test_broker_filled_target_is_booked_to_the_ledger(state_file, trades_file):
    """A resting fill happens without us — if we don't book it the trade vanishes."""
    _arm(state_file, entry=100.0)
    client = _FakeClient([], closed_orders=[
        _fake_order(order_id="tp-7", filled_avg_price="108.0", filled_qty="2.5")])

    recorded = trader.record_tp_fills(client)

    assert [t["reason"] for t in recorded] == ["take profit"]
    # +8% gross, less 0.25% taker in and 0.15% maker out on the rested target
    assert recorded[0]["gross_pnl"] == pytest.approx(20.0)
    assert recorded[0]["pnl"] == pytest.approx(18.97, abs=0.01)
    assert recorded[0]["pnl_pct"] == pytest.approx(7.57, abs=0.01)
    assert trader.load_trades()[0]["symbol"] == "BTC/USD"
    assert json.loads(state_file.read_text()) == {}     # position forgotten


def test_filled_target_is_booked_only_once(state_file, trades_file):
    """record_tp_fills re-reads a 8-day window every cycle — it must dedupe."""
    _arm(state_file, entry=100.0)
    order = _fake_order(order_id="tp-7", filled_avg_price="108.0")
    client = _FakeClient([], closed_orders=[order])

    assert len(trader.record_tp_fills(client)) == 1
    _arm(state_file, entry=100.0)                       # position record restored
    assert trader.record_tp_fills(client) == []
    assert len(trader.load_trades()) == 1


def test_poll_sell_is_not_rebooked_by_record_tp_fills(monkeypatch, state_file, trades_file):
    """manage_exits and record_tp_fills both see the same sell — only one may book it."""
    _arm(state_file)
    client = _FakeClient([_FakePosition("BTCUSD", "2.5", 100.0)])
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 94.0})
    trader.manage_exits({"BTC/USD": {"rsi": 40}})

    booked_id = trader.load_trades()[0]["order_id"]
    _arm(state_file)
    client._closed_orders = [_fake_order(order_id=booked_id, filled_avg_price="94.0")]

    assert trader.record_tp_fills(client) == []
    assert len(trader.load_trades()) == 1


def test_entry_is_notional_so_crypto_is_never_whole_unit_sized(monkeypatch, state_file):
    client = _FakeClient([])
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 76_000.0})

    candidate = {"symbol": "BTC/USD", "score": 7, "momentum": {"atr": 1_000.0}}
    placed = trader.place_entries([candidate], bars={})

    assert len(placed) == 1
    assert client.orders[0].notional == config.CRYPTO_POSITION_SIZE_DOLLARS
    assert getattr(client.orders[0], "qty", None) is None


def test_entries_respect_the_crypto_position_cap(monkeypatch, state_file):
    full = [_FakePosition(f"C{i}USD", "1", 1.0) for i in range(config.CRYPTO_MAX_CONCURRENT)]
    client = _FakeClient(full)
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 76_000.0})

    candidate = {"symbol": "BTC/USD", "score": 7, "momentum": {"atr": 1_000.0}}
    assert trader.place_entries([candidate], bars={}) == []
    assert client.orders == []


def test_entry_stop_is_at_least_the_floor_percentage(monkeypatch, state_file):
    """A tiny ATR must not produce a stop closer than CRYPTO_STOP_PCT."""
    client = _FakeClient([])
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 100.0})

    candidate = {"symbol": "BTC/USD", "score": 7, "momentum": {"atr": 0.01}}
    placed = trader.place_entries([candidate], bars={})
    assert placed[0]["stop_price"] == pytest.approx(100.0 * (1 - config.CRYPTO_STOP_PCT))


# ── Separation from the equity path ──────────────────────────────────────────

def test_equity_jobs_ignore_crypto_positions():
    from alpaca.trading.enums import AssetClass
    from trader._utils import equity_positions

    equity = SimpleNamespace(symbol="AAPL", asset_class=AssetClass.US_EQUITY)
    crypto = SimpleNamespace(symbol="BTCUSD", asset_class=AssetClass.CRYPTO)
    client = _FakeClient([equity, crypto])

    assert [p.symbol for p in equity_positions(client)] == ["AAPL"]


def _stub_cycle(monkeypatch, held: bool):
    """Patch the scheduler's crypto entry points; returns the run_cycle call log."""
    import scheduler  # noqa: F401 — imported for the side effect of being importable
    called = []
    monkeypatch.setattr("crypto.trader.has_open_positions", lambda: held)
    monkeypatch.setattr("crypto.trader.run_cycle", lambda: called.append(1) or
                        {"closed": [], "placed": [], "candidates": 0})
    monkeypatch.setattr("crypto.trader.send_cycle_summary", lambda result: None)
    return called


def test_crypto_cycle_is_skipped_when_flag_off_and_nothing_held(monkeypatch, flags_file):
    import scheduler
    called = _stub_cycle(monkeypatch, held=False)
    scheduler.run_crypto()
    assert called == []


def test_crypto_cycle_still_runs_when_flag_off_but_position_held(monkeypatch, flags_file):
    """An open crypto position has no broker-side stop — the cycle is its only exit."""
    import scheduler
    called = _stub_cycle(monkeypatch, held=True)
    scheduler.run_crypto()
    assert called == [1]


def test_crypto_cycle_runs_when_flag_on(monkeypatch, flags_file):
    import scheduler
    feature_flags.set_flag("crypto", True)
    called = _stub_cycle(monkeypatch, held=False)
    scheduler.run_crypto()
    assert called == [1]


def test_run_cycle_manages_exits_but_places_no_entries_when_flag_off(monkeypatch, flags_file):
    """Option 1 semantics: the flag gates buying, never selling."""
    monkeypatch.setattr("data.db.load_all_bars", lambda market: {})
    monkeypatch.setattr("crypto.scanner.scan", lambda: [
        {"symbol": "BTC/USD", "score": 8, "momentum": {"rsi": 55.0, "adx": 30.0},
         "relative_strength": {"rs_return": 5.0}, "volume": {"volume_ratio": 1.2}},
    ])
    monkeypatch.setattr("crypto.scanner.entry_filtered",
                        lambda candidates: list(candidates))
    monkeypatch.setattr(trader, "_save_watchlist",
                        lambda candidates, eligible: saved.append(eligible))
    exits, entries = [], []
    monkeypatch.setattr(trader, "manage_exits", lambda ind: exits.append(ind) or [])
    monkeypatch.setattr(trader, "place_entries",
                        lambda cands, bars: entries.append(cands) or [])
    saved = []

    trader.run_cycle()

    assert exits, "exits must run with the flag off"
    assert entries == [[]], "no candidate may be offered for entry with the flag off"
    assert saved == [[]], "watchlist must not advertise names we will not buy"


# ── Fees ─────────────────────────────────────────────────────────────────────

def test_net_pnl_charges_a_fee_on_both_sides():
    """Taker in, taker out — a flat round trip at market loses money."""
    from crypto.fees import net_pnl

    pnl, pnl_pct = net_pnl(100.0, 100.0, 10.0, buy_fee=0.0025, sell_fee=0.0025)
    assert pnl < 0
    assert pnl_pct == pytest.approx(-0.4994, abs=1e-3)   # ~2 × 0.25%


def test_resting_target_is_cheaper_than_selling_at_market():
    """The whole point of the maker/taker split: a rested exit keeps 0.1% more."""
    from crypto.fees import net_pnl

    _, maker_pct = net_pnl(100.0, 103.0, 1.0, buy_fee=0.0025, sell_fee=0.0015)
    _, taker_pct = net_pnl(100.0, 103.0, 1.0, buy_fee=0.0025, sell_fee=0.0025)
    assert maker_pct > taker_pct
    assert maker_pct - taker_pct == pytest.approx(0.103, abs=0.005)


def test_net_pnl_matches_the_live_link_round_trip():
    """The 2026-09-19 LINK/USD trade: market buy, then a target that rested 9h."""
    from crypto.fees import net_pnl

    pnl, pnl_pct = net_pnl(12.239, 12.660068, 19.990620941,
                           buy_fee=0.0025, sell_fee=0.0015)
    gross = (12.660068 - 12.239) * 19.990620941
    assert gross == pytest.approx(8.42, abs=0.01)
    assert pnl == pytest.approx(7.42, abs=0.01)
    assert pnl_pct == pytest.approx(3.03, abs=0.01)


def test_fee_eats_an_eighth_of_the_target_floor():
    """Most majors clamp to the 3% tp_min. Documents why churn is not free."""
    from crypto.fees import net_pnl

    _, net_pct = net_pnl(100.0, 103.0, 1.0, buy_fee=0.0025, sell_fee=0.0015)
    assert (3.0 - net_pct) / 3.0 == pytest.approx(0.132, abs=0.01)


def test_taker_rate_is_read_from_the_quantity_gap():
    """The live LINK buy: paid for 20.0407 units, the position received 19.9906."""
    from crypto.fees import observed_taker_rate

    assert observed_taker_rate(20.040722748, 19.990620941) == pytest.approx(0.0025, abs=1e-7)


@pytest.mark.parametrize("paid,received", [
    (0.0, 1.0),        # no buy to measure
    (1.0, 0.0),        # no position to compare against
    (1.0, 0.9),        # 10% gap is not a fee — a partial fill or a pre-existing holding
    (1.0, 1.5),        # received more than paid for
    (1.0, -1.0),       # nonsense quantity
])
def test_implausible_quantity_pairs_yield_no_rate(paid, received):
    """A bad reading taken at face value would corrupt every P&L that followed."""
    from crypto.fees import observed_taker_rate

    assert observed_taker_rate(paid, received) is None


def test_measured_taker_rate_identifies_the_tier_for_the_maker_half():
    """Only the taker side is observable, so the maker rate rides on the tier it names."""
    from crypto.fees import maker_rate

    assert maker_rate(0.0025) == 0.0015      # tier 1, where the account sits
    assert maker_rate(0.0018) == 0.0008      # $1M–10M
    assert maker_rate(0.0010) == 0.0000      # $100M+
    # A measurement lands a hair off the published figure; it must still pick a tier.
    assert maker_rate(0.002499) == 0.0015


def _entry_client(monkeypatch, paid: str, received: str):
    """A client whose buy fills for `paid` units but delivers `received` to the position."""
    from alpaca.trading.enums import OrderStatus

    client = _FakeClient([])

    def submit(req):
        client.orders.append(req)
        client._positions = [_FakePosition("BTCUSD", received, 100.0)]
        return SimpleNamespace(id="buy-1")

    monkeypatch.setattr(client, "submit_order", submit)
    monkeypatch.setattr(client, "get_order_by_id", lambda order_id: SimpleNamespace(
        id=order_id, status=OrderStatus.FILLED, filled_avg_price="100.0", filled_qty=paid))
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 100.0})
    return client


def test_entry_records_the_fee_rate_the_buy_actually_paid(monkeypatch, state_file):
    """Alpaca never reports the fee, so the qty gap is the only reading of the tier.

    Uses a rate *above* the published schedule: a measurement clamped to today's
    top tier would reinstate the stale-constant problem it exists to solve.
    """
    _entry_client(monkeypatch, paid="2.5", received="2.4925")   # 0.30%

    trader.place_entries([{"symbol": "BTC/USD", "score": 7,
                           "momentum": {"atr": 1.0}}], bars={})

    recorded = json.loads(state_file.read_text())["BTC/USD"]["fee_rate"]
    assert recorded == pytest.approx(0.0030, abs=1e-7)


def test_unreadable_fill_falls_back_to_the_configured_rate(monkeypatch, state_file):
    """A fee measurement is not worth failing an entry that is already open."""
    client = _FakeClient([])
    monkeypatch.setattr(trader, "_client", lambda: client)
    monkeypatch.setattr(trader, "latest_prices", lambda pairs: {"BTC/USD": 100.0})

    trader.place_entries([{"symbol": "BTC/USD", "score": 7,
                           "momentum": {"atr": 1.0}}], bars={})

    recorded = json.loads(state_file.read_text())["BTC/USD"]["fee_rate"]
    assert recorded == config.CRYPTO_FEE_TAKER_PCT


def test_exit_prices_its_fee_off_the_rate_measured_at_entry(monkeypatch, state_file, trades_file):
    """A tier change must reach the ledger without anyone editing config."""
    _arm(state_file, entry=100.0)
    state = json.loads(state_file.read_text())
    state["BTC/USD"]["fee_rate"] = 0.0018          # tier 4: maker 0.08%
    state_file.write_text(json.dumps(state))

    client = _FakeClient([], closed_orders=[
        _fake_order(order_id="tp-7", filled_avg_price="108.0", filled_qty="2.5")])
    recorded = trader.record_tp_fills(client)

    from crypto.fees import net_pnl
    expected, _ = net_pnl(100.0, 108.0, 2.5, buy_fee=0.0018, sell_fee=0.0008)
    assert recorded[0]["pnl"] == pytest.approx(round(expected, 2))
    assert recorded[0]["pnl"] > 18.97              # cheaper tier than the default


def test_ledger_keeps_gross_alongside_net(trades_file):
    """Net is what /summary reports; gross stays so the fee drag is auditable."""
    from crypto import trader

    trader._record_trade({"symbol": "X/USD", "pnl": 7.17, "gross_pnl": 8.42})
    booked = trader.load_trades(today_only=True)
    assert booked[0]["pnl"] == 7.17
    assert booked[0]["gross_pnl"] == 8.42
