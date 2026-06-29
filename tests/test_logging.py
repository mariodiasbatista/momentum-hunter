"""
Tests for the logging system:
  - notifier/log_config.py    — level persistence
  - notifier/telegram_handler.py — TelegramHandler emit + setup
  - notifier/bot_listener.py  — /loglevel and /setlevel commands
  - notifier/schedule_display.py — ✅/⬜ logic, duration, watchlist
  - scanner/stock_scanner.py  — approved/blocked log
  - scanner/crypto_scanner.py — approved/blocked log
"""
import json
import logging
from datetime import datetime, time as dtime
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ── log_config ──────────────────────────────────────────────────────────────

class TestLogConfig:
    def test_default_level_is_2_when_no_file(self, tmp_path):
        cfg_file = tmp_path / "log_config.json"
        with patch("notifier.log_config._FILE", cfg_file):
            from notifier.log_config import get_telegram_level
            assert get_telegram_level() == 2

    def test_set_and_get_roundtrip(self, tmp_path):
        cfg_file = tmp_path / "log_config.json"
        with patch("notifier.log_config._FILE", cfg_file):
            from notifier.log_config import get_telegram_level, set_telegram_level
            set_telegram_level(1)
            assert get_telegram_level() == 1

    def test_level_clamped_to_0(self, tmp_path):
        cfg_file = tmp_path / "log_config.json"
        with patch("notifier.log_config._FILE", cfg_file):
            from notifier.log_config import get_telegram_level, set_telegram_level
            set_telegram_level(-5)
            assert get_telegram_level() == 0

    def test_level_clamped_to_3(self, tmp_path):
        cfg_file = tmp_path / "log_config.json"
        with patch("notifier.log_config._FILE", cfg_file):
            from notifier.log_config import get_telegram_level, set_telegram_level
            set_telegram_level(99)
            assert get_telegram_level() == 3

    def test_returns_default_on_corrupt_file(self, tmp_path):
        cfg_file = tmp_path / "log_config.json"
        cfg_file.write_text("not json")
        with patch("notifier.log_config._FILE", cfg_file):
            from notifier.log_config import get_telegram_level
            assert get_telegram_level() == 2


# ── TelegramHandler ──────────────────────────────────────────────────────────

class TestTelegramHandler:
    def _make_record(self, level, msg="test message"):
        record = logging.LogRecord(
            name="test", level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )
        return record

    def test_level_0_never_sends(self):
        with patch("notifier.log_config.get_telegram_level", return_value=0), \
             patch("notifier.telegram_handler._send_safe") as mock_send:
            from notifier.telegram_handler import TelegramHandler
            h = TelegramHandler()
            h.emit(self._make_record(logging.INFO))
            mock_send.assert_not_called()

    def test_level_2_blocks_debug(self):
        with patch("notifier.log_config.get_telegram_level", return_value=2), \
             patch("notifier.telegram_handler._send_safe") as mock_send:
            from notifier.telegram_handler import TelegramHandler
            h = TelegramHandler()
            h.emit(self._make_record(logging.DEBUG))
            mock_send.assert_not_called()

    def test_level_2_allows_info(self):
        with patch("notifier.log_config.get_telegram_level", return_value=2), \
             patch("notifier.telegram_handler.threading") as mock_threading:
            from notifier.telegram_handler import TelegramHandler
            h = TelegramHandler()
            h.emit(self._make_record(logging.INFO, "hello info"))
            mock_threading.Thread.assert_called_once()

    def test_level_3_blocks_info(self):
        with patch("notifier.log_config.get_telegram_level", return_value=3), \
             patch("notifier.telegram_handler.threading") as mock_threading:
            from notifier.telegram_handler import TelegramHandler
            h = TelegramHandler()
            h.emit(self._make_record(logging.INFO))
            mock_threading.Thread.assert_not_called()

    def test_level_3_allows_error(self):
        with patch("notifier.log_config.get_telegram_level", return_value=3), \
             patch("notifier.telegram_handler.threading") as mock_threading:
            from notifier.telegram_handler import TelegramHandler
            h = TelegramHandler()
            h.emit(self._make_record(logging.ERROR, "something broke"))
            mock_threading.Thread.assert_called_once()

    def test_notifier_telegram_logs_are_not_forwarded(self):
        with patch("notifier.log_config.get_telegram_level", return_value=2), \
             patch("notifier.telegram_handler.threading") as mock_threading:
            from notifier.telegram_handler import TelegramHandler
            h = TelegramHandler()
            record = logging.LogRecord(
                name="notifier.telegram", level=logging.WARNING,
                pathname="", lineno=0, msg="400 error loop", args=(), exc_info=None,
            )
            h.emit(record)
            mock_threading.Thread.assert_not_called()

    def test_setup_does_not_add_duplicate_handlers(self):
        from notifier.telegram_handler import TelegramHandler, setup_telegram_logging
        root = logging.getLogger()
        before = [h for h in root.handlers if isinstance(h, TelegramHandler)]
        setup_telegram_logging()
        setup_telegram_logging()
        after = [h for h in root.handlers if isinstance(h, TelegramHandler)]
        assert len(after) == len(before) + 1 if not before else len(after) == 1


# ── bot_listener commands ────────────────────────────────────────────────────

class TestBotCommands:
    def test_loglevel_replies_with_current_level(self):
        with patch("notifier.log_config.get_telegram_level", return_value=2), \
             patch("notifier.bot_listener._reply") as mock_reply:
            from notifier.bot_listener import _cmd_loglevel
            _cmd_loglevel(chat_id=123, args=[])
            text = mock_reply.call_args[0][1]
            assert "2" in text
            assert "Info" in text

    def test_setlevel_valid_updates_level(self):
        with patch("notifier.log_config.set_telegram_level") as mock_set, \
             patch("notifier.log_config.get_telegram_level", return_value=1), \
             patch("notifier.bot_listener._reply") as mock_reply:
            from notifier.bot_listener import _cmd_setlevel
            _cmd_setlevel(chat_id=123, args=["1"])
            mock_set.assert_called_once_with(1)
            text = mock_reply.call_args[0][1]
            assert "✅" in text

    def test_setlevel_no_args_shows_usage(self):
        with patch("notifier.bot_listener._reply") as mock_reply:
            from notifier.bot_listener import _cmd_setlevel
            _cmd_setlevel(chat_id=123, args=[])
            text = mock_reply.call_args[0][1]
            assert "Usage" in text

    def test_setlevel_non_digit_shows_usage(self):
        with patch("notifier.bot_listener._reply") as mock_reply:
            from notifier.bot_listener import _cmd_setlevel
            _cmd_setlevel(chat_id=123, args=["abc"])
            text = mock_reply.call_args[0][1]
            assert "Usage" in text

    def test_setlevel_out_of_range_rejected(self):
        with patch("notifier.bot_listener._reply") as mock_reply:
            from notifier.bot_listener import _cmd_setlevel
            _cmd_setlevel(chat_id=123, args=["5"])
            text = mock_reply.call_args[0][1]
            assert "0, 1, 2, or 3" in text

    def test_help_command_lists_all_commands(self):
        with patch("notifier.bot_listener._reply") as mock_reply:
            from notifier.bot_listener import _cmd_help
            _cmd_help(chat_id=123, args=[])
            text = mock_reply.call_args[0][1]
            assert "/schedule" in text
            assert "/summary" in text
            assert "/loglevel" in text
            assert "/setlevel" in text
            assert "/help" in text

    def test_schedule_command_calls_build_message(self):
        with patch("notifier.schedule_display.build_schedule_message", return_value="📅 msg") as mock_build, \
             patch("notifier.bot_listener._reply") as mock_reply:
            from notifier.bot_listener import _cmd_schedule
            _cmd_schedule(chat_id=123, args=[])
            mock_build.assert_called_once()
            mock_reply.assert_called_once_with(123, "📅 msg")


# ── schedule_display ─────────────────────────────────────────────────────────

class TestScheduleDisplay:
    def _build(self, hour, minute, runs=None, signals=None):
        from zoneinfo import ZoneInfo
        fake_now = datetime(2026, 5, 22, hour, minute, 0, tzinfo=ZoneInfo("America/New_York"))
        runs_file_data = json.dumps(runs) if runs is not None else None
        with patch("notifier.schedule_display.datetime") as mock_dt, \
             patch("notifier.schedule_display._RUNS_FILE") as mock_file, \
             patch("data.db.load_signals", return_value=signals or []):
            mock_dt.now.return_value = fake_now
            mock_file.exists.return_value = runs_file_data is not None
            mock_file.read_text.return_value = runs_file_data or "{}"
            from notifier.schedule_display import build_schedule_message
            return build_schedule_message()

    def test_jobs_pending_before_schedule_time(self):
        text = self._build(hour=9, minute=0)
        assert "⬜" in text
        assert "✅" not in text

    def test_jobs_done_after_schedule_time(self):
        text = self._build(hour=23, minute=0)
        assert "✅" in text
        assert "⬜" not in text

    def test_intraday_shows_recurrent_icon_during_window(self):
        # 11:00 AM — intraday window is active (10:15–14:45)
        text = self._build(hour=11, minute=0)
        assert "🔄" in text

    def test_intraday_shows_done_after_window(self):
        # 3:00 PM — past 2:45 PM end of intraday window
        text = self._build(hour=15, minute=0)
        assert "🔄" not in text

    def test_first_job_done_rest_pending(self):
        # 9:20 AM ET — only premarket (9:15) done, everything else pending
        text = self._build(hour=9, minute=20)
        lines = [l for l in text.splitlines() if "✅" in l or "⬜" in l or "🔄" in l]
        assert lines[0].startswith("✅")
        assert all(l.startswith("⬜") for l in lines[1:])

    def test_duration_shown_when_job_ran(self):
        runs = {"ingest": {"duration_seconds": 728}}
        text = self._build(hour=23, minute=0, runs=runs)
        assert "12m 8s" in text

    def test_no_duration_when_job_not_in_runs(self):
        text = self._build(hour=23, minute=0, runs={})
        assert "12m" not in text

    def test_watchlist_shows_top_symbols(self):
        signals = [{"symbol": f"SYM{i}", "score": 8} for i in range(5)]
        text = self._build(hour=9, minute=0, signals=signals)
        assert "SYM0" in text
        assert "SYM4" in text

    def test_no_candidates_message_when_empty(self):
        text = self._build(hour=9, minute=0, signals=[])
        assert "No candidates" in text

    def test_date_shown_in_header(self):
        text = self._build(hour=9, minute=0)
        assert "2026-05-22" in text


# ── scanner logging ──────────────────────────────────────────────────────────

def _signal(symbol, score, asset_class="us_equity"):
    return {
        "symbol": symbol, "asset_class": asset_class, "score": score,
        "criteria": {k: True for k in [
            "above_sma50", "above_sma200", "ema9_above_ema21",
            "rsi_in_range", "macd_bullish", "adx_strong",
            "volume_above_avg", "outperforming_spy",
        ]},
        "trend": {"last_close": 10.0, "sma50": 9.0, "sma200": 8.0, "ema9": 10.1, "ema21": 9.9},
        "momentum": {
            "rsi": 60.0, "rsi_overbought": False, "macd_above_signal": True,
            "macd_histogram_positive": True, "macd_histogram_shrinking": False,
            "adx": 30.0, "adx_falling": False, "atr": 1.0,
        },
        "volume": {"volume": 1_000_000.0, "avg_volume": 800_000.0, "volume_ratio": 1.25, "volume_drying_up": False},
        "relative_strength": {"rs_return": 5.0, "spy_return": 2.0, "outperforming_spy": True},
        "exit": {"exit_mode": "trailing_stop", "warning_count": 0, "warnings": [], "trailing_stop_atr_range": (1.5, 3.0)},
        "market": "stocks",
        "days_in_scan": 1,
    }


class TestScannerLogging:
    def test_stock_scanner_logs_approved_and_blocked(self, caplog):
        all_sigs = [_signal(f"S{i}", score=8 if i < 3 else 4) for i in range(10)]
        with patch("scanner.stock_scanner.signals_computed_today", return_value=True), \
             patch("scanner.stock_scanner.load_signals", return_value=all_sigs), \
             patch("scanner.stock_scanner.signal_persistence", return_value={}):
            with caplog.at_level(logging.INFO, logger="scanner.stocks"):
                from scanner.stock_scanner import run_stock_scan
                result = run_stock_scan(min_score=6)
            assert any("Approved" in r.message for r in caplog.records)
            assert any("Blocked" in r.message for r in caplog.records)

    def test_stock_scanner_approved_count_correct(self, caplog):
        all_sigs = [_signal(f"S{i}", score=8 if i < 4 else 3) for i in range(10)]
        with patch("scanner.stock_scanner.signals_computed_today", return_value=True), \
             patch("scanner.stock_scanner.load_signals", return_value=all_sigs), \
             patch("scanner.stock_scanner.signal_persistence", return_value={}):
            with caplog.at_level(logging.INFO, logger="scanner.stocks"):
                from scanner.stock_scanner import run_stock_scan
                result = run_stock_scan(min_score=6)
            log_msg = next(r.message for r in caplog.records if "Approved" in r.message)
            assert "Approved: 4" in log_msg
            assert "Blocked: 6" in log_msg

    def test_crypto_scanner_logs_approved_and_blocked(self, caplog):
        all_sigs = [_signal(f"C{i}", score=7 if i < 2 else 2, asset_class="crypto") for i in range(8)]
        with patch("scanner.crypto_scanner.signals_computed_today", return_value=True), \
             patch("scanner.crypto_scanner.load_signals", return_value=all_sigs), \
             patch("scanner.crypto_scanner.signal_persistence", return_value={}):
            with caplog.at_level(logging.INFO, logger="scanner.crypto"):
                from scanner.crypto_scanner import run_crypto_scan
                result = run_crypto_scan(min_score=6)
            assert any("Approved" in r.message for r in caplog.records)

    def test_crypto_scanner_blocked_count_correct(self, caplog):
        all_sigs = [_signal(f"C{i}", score=2, asset_class="crypto") for i in range(5)]
        with patch("scanner.crypto_scanner.signals_computed_today", return_value=True), \
             patch("scanner.crypto_scanner.load_signals", return_value=all_sigs), \
             patch("scanner.crypto_scanner.signal_persistence", return_value={}):
            with caplog.at_level(logging.INFO, logger="scanner.crypto"):
                from scanner.crypto_scanner import run_crypto_scan
                result = run_crypto_scan(min_score=6)
            log_msg = next(r.message for r in caplog.records if "Approved" in r.message)
            assert "Approved: 0" in log_msg
            assert "Blocked: 5" in log_msg
