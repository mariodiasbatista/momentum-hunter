"""Runtime feature flags, toggled from Telegram with /setfeature.

Flags live in a JSON file rather than config.py so they can be flipped without a
restart — the scheduler reads the flag at the top of each job run.
"""
import json
from pathlib import Path

_FILE = Path(__file__).parent.parent / "data" / "feature_flags.json"

DEFAULTS = {"crypto": False}

DESCRIPTIONS = {
    "crypto": "Crypto entries — every 4h, 24/7, separate budget (exits always run)",
}


def _load() -> dict:
    try:
        data = json.loads(_FILE.read_text())
        return {**DEFAULTS, **data} if isinstance(data, dict) else dict(DEFAULTS)
    except Exception:
        return dict(DEFAULTS)


def is_enabled(name: str) -> bool:
    return bool(_load().get(name, False))


def set_flag(name: str, enabled: bool) -> None:
    if name not in DEFAULTS:
        raise ValueError(f"Unknown feature flag: {name}")
    flags = _load()
    flags[name] = bool(enabled)
    _FILE.write_text(json.dumps(flags))


def all_flags() -> dict:
    return _load()


def status() -> list[tuple[str, bool, str]]:
    """Every known flag as (name, enabled, description), ordered by name."""
    flags = _load()
    return [(name, bool(flags.get(name, False)), DESCRIPTIONS.get(name, ""))
            for name in sorted(DEFAULTS)]


def parse_arg(arg: str) -> tuple[str, bool] | None:
    """Parse `crypto_on` / `crypto_off` into (name, enabled), or None if invalid."""
    arg = arg.strip().lower()
    for suffix, enabled in (("_on", True), ("_off", False)):
        if arg.endswith(suffix):
            name = arg[: -len(suffix)]
            if name in DEFAULTS:
                return name, enabled
    return None
