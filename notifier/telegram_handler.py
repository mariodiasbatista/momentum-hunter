import logging
import threading

_LEVEL_MAP = {1: logging.DEBUG, 2: logging.INFO, 3: logging.ERROR}
_ICONS = {
    logging.DEBUG:    "🔍",
    logging.INFO:     "📋",
    logging.WARNING:  "⚠️",
    logging.ERROR:    "🚨",
    logging.CRITICAL: "🆘",
}


def _send_safe(text: str) -> None:
    try:
        from notifier.telegram import _send
        _send(text)
    except Exception:
        pass


class TelegramHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        from notifier.log_config import get_telegram_level
        tg_level = get_telegram_level()
        if tg_level == 0:
            return
        min_level = _LEVEL_MAP.get(tg_level, logging.INFO)
        if record.levelno < min_level:
            return
        try:
            icon = _ICONS.get(record.levelno, "📋")
            text = f"{icon} `[{record.levelname}]` {self.format(record)}"
            threading.Thread(target=_send_safe, args=(text,), daemon=True).start()
        except Exception:
            pass


def setup_telegram_logging() -> None:
    handler = TelegramHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
    root = logging.getLogger()
    # Avoid adding duplicate handlers on repeated calls
    if not any(isinstance(h, TelegramHandler) for h in root.handlers):
        root.addHandler(handler)
