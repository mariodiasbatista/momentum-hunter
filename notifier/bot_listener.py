"""
Telegram bot command listener — long-polling loop.

Commands:
  /schedule        — show today's job schedule and watchlist
  /loglevel        — show current Telegram log level
  /setlevel <N>    — set log level (0=off 1=debug 2=info 3=errors only)
"""
import logging
import time

import requests

import config

log = logging.getLogger(__name__)

_API = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
_COMMANDS = {}


def _register(cmd):
    def decorator(fn):
        _COMMANDS[cmd] = fn
        return fn
    return decorator


def _reply(chat_id: int, text: str) -> None:
    requests.post(
        f"{_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10,
    ).raise_for_status()


@_register("/schedule")
def _cmd_schedule(chat_id: int, args: list) -> None:
    from notifier.schedule_display import build_schedule_message
    _reply(chat_id, build_schedule_message())


@_register("/loglevel")
def _cmd_loglevel(chat_id: int, args: list) -> None:
    from notifier.log_config import get_telegram_level, LEVEL_LABELS
    level = get_telegram_level()
    label = LEVEL_LABELS.get(level, "Unknown")
    _reply(chat_id, f"📊 *Log Level:* {level} — {label}")


@_register("/summary")
def _cmd_summary(chat_id: int, args: list) -> None:
    from notifier.summary import build_summary
    _reply(chat_id, build_summary())


@_register("/help")
def _cmd_help(chat_id: int, args: list) -> None:
    _reply(chat_id, (
        "📊 *Momentum Hunter — Available Commands*\n\n"
        "/schedule — Today's jobs schedule\n"
        "/summary — Today's P&L summary on demand\n"
        "/loglevel — Show current Telegram log level\n"
        "/setlevel — Set log level (0=off 1=debug 2=info 3=errors only)\n"
        "/help — Show this message"
    ))


@_register("/setlevel")
def _cmd_setlevel(chat_id: int, args: list) -> None:
    from notifier.log_config import set_telegram_level, get_telegram_level, LEVEL_LABELS
    if not args or not args[0].isdigit():
        _reply(chat_id, "Usage: `/setlevel N`\n0=Off  1=Debug  2=Info  3=Errors only")
        return
    level = int(args[0])
    if level not in (0, 1, 2, 3):
        _reply(chat_id, "Level must be 0, 1, 2, or 3.\n0=Off  1=Debug  2=Info  3=Errors only")
        return
    set_telegram_level(level)
    label = LEVEL_LABELS[level]
    _reply(chat_id, f"✅ Log level set to *{level} — {label}*")
    log.info("Telegram log level changed to %d (%s)", level, label)


def run_forever() -> None:
    """Block forever, polling Telegram for commands."""
    offset = None
    log.info("Bot listener started — waiting for commands")

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset

            resp = requests.get(f"{_API}/getUpdates", params=params, timeout=35)
            resp.raise_for_status()
            updates = resp.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                raw_text = (msg.get("text") or "").strip()
                chat_id = msg.get("chat", {}).get("id")

                if not raw_text or not chat_id:
                    continue

                # Strip @botname suffix and split into cmd + args
                parts = raw_text.split("@")[0].split()
                cmd, args = parts[0], parts[1:]

                handler = _COMMANDS.get(cmd)
                if handler:
                    log.info("Command %s %s from chat %s", cmd, args, chat_id)
                    try:
                        handler(chat_id, args)
                    except Exception as exc:
                        log.warning("Command %s failed: %s", cmd, exc)

        except requests.exceptions.Timeout:
            pass  # normal long-poll timeout, loop again
        except Exception as exc:
            log.warning("Poll error: %s — retrying in 5s", exc)
            time.sleep(5)
