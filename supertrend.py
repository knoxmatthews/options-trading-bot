"""
SuperTrend indicator, implemented from the verified canonical recurrence
(basic bands -> final bands -> supertrend line), cross-checked against
multiple independent reference implementations before writing this.

Convention (explicit, not sign-based, to avoid any ambiguity):
    is_uptrend[i] == True  -> SuperTrend line is BELOW price (bullish / buy)
    is_uptrend[i] == False -> SuperTrend line is ABOVE price (bearish / sell)

This matches the verified TradingView Pine convention (direction < 0 = uptrend),
though the sign isn't reused here on purpose — using an explicit boolean
instead of a float-equality check on the previous bands is more robust
than the textbook version, which compares floats for exact equality.
"""

import pandas as pd


def compute_supertrend(df: pd.DataFrame, atr_period: int = 10, multiplier: float = 1.75) -> pd.DataFrame:
    """
    df must have columns: 'high', 'low', 'close', sorted ascending by time.
    Returns a copy of df with added columns:
        atr, final_upperband, final_lowerband, supertrend, is_uptrend
    """
    if not {"high", "low", "close"}.issubset(df.columns):
        raise ValueError("df must have 'high', 'low', 'close' columns")

    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder's smoothing (matches Pine's ta.atr)
    atr = true_range.ewm(alpha=1 / atr_period, adjust=False, min_periods=atr_period).mean()

    hl2 = (high + low) / 2
    basic_upperband = hl2 + multiplier * atr
    basic_lowerband = hl2 - multiplier * atr

    n = len(df)
    final_upperband = basic_upperband.copy()
    final_lowerband = basic_lowerband.copy()
    supertrend = pd.Series(index=df.index, dtype="float64")
    is_uptrend = pd.Series(index=df.index, dtype="object")

    for i in range(n):
        if i == 0 or pd.isna(atr.iloc[i]) or pd.isna(atr.iloc[i - 1]):
            final_upperband.iloc[i] = basic_upperband.iloc[i]
            final_lowerband.iloc[i] = basic_lowerband.iloc[i]
            supertrend.iloc[i] = final_upperband.iloc[i]
            is_uptrend.iloc[i] = False
            continue

        # Final upperband only ratchets down, unless price closed above it
        if basic_upperband.iloc[i] < final_upperband.iloc[i - 1] or close.iloc[i - 1] > final_upperband.iloc[i - 1]:
            final_upperband.iloc[i] = basic_upperband.iloc[i]
        else:
            final_upperband.iloc[i] = final_upperband.iloc[i - 1]

        # Final lowerband only ratchets up, unless price closed below it
        if basic_lowerband.iloc[i] > final_lowerband.iloc[i - 1] or close.iloc[i - 1] < final_lowerband.iloc[i - 1]:
            final_lowerband.iloc[i] = basic_lowerband.iloc[i]
        else:
            final_lowerband.iloc[i] = final_lowerband.iloc[i - 1]

        prev_is_uptrend = is_uptrend.iloc[i - 1]

        if not prev_is_uptrend and close.iloc[i] <= final_upperband.iloc[i]:
            supertrend.iloc[i] = final_upperband.iloc[i]
            is_uptrend.iloc[i] = False
        elif not prev_is_uptrend and close.iloc[i] > final_upperband.iloc[i]:
            supertrend.iloc[i] = final_lowerband.iloc[i]
            is_uptrend.iloc[i] = True
        elif prev_is_uptrend and close.iloc[i] >= final_lowerband.iloc[i]:
            supertrend.iloc[i] = final_lowerband.iloc[i]
            is_uptrend.iloc[i] = True
        else:  # prev_is_uptrend and close.iloc[i] < final_lowerband.iloc[i]
            supertrend.iloc[i] = final_upperband.iloc[i]
            is_uptrend.iloc[i] = False

    out = df.copy()
    out["atr"] = atr
    out["final_upperband"] = final_upperband
    out["final_lowerband"] = final_lowerband
    out["supertrend"] = supertrend
    out["is_uptrend"] = is_uptrend.astype(bool)
    return out
