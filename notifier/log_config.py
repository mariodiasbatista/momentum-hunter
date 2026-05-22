import json
from pathlib import Path

_FILE = Path(__file__).parent.parent / "data" / "log_config.json"

LEVEL_LABELS = {0: "Off", 1: "Debug", 2: "Info", 3: "Errors only"}


def get_telegram_level() -> int:
    try:
        return int(json.loads(_FILE.read_text()).get("telegram_level", 2))
    except Exception:
        return 2


def set_telegram_level(level: int) -> None:
    level = max(0, min(3, level))
    _FILE.write_text(json.dumps({"telegram_level": level}))
