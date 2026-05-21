import pandas as pd

import config


def compute_volume(df: pd.DataFrame) -> dict:
    vol = df["volume"]
    avg_vol = vol.rolling(config.VOLUME_PERIOD).mean()
    last_vol = float(vol.iloc[-1])
    last_avg = float(avg_vol.iloc[-1])
    ratio = last_vol / last_avg if last_avg > 0 else 0.0

    return {
        "volume": last_vol,
        "avg_volume": last_avg,
        "volume_ratio": ratio,
        "volume_above_avg": bool(ratio >= config.VOLUME_MULTIPLIER),
        "volume_drying_up": bool(ratio < 0.8),
    }
