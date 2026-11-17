# btc-race-24h-20261117-0603 entry: a cbBTC/USDC minute-bar trading bot.
#
# Shipped strategy (P["mode"] = "dip"): mean-reversion dip buying.
#   - The slow EMA (240 bars) is the reference mean. When the close stretches
#     0.6% below it, a buy is armed and taken at 90% of equity: in this market
#     the sharp multi-hour zigzags revert to the mean far more reliably than
#     breakouts follow through, and each round trip must clear ~25 bps of fees,
#     slippage and impact, so the bot trades rarely and only on real stretches.
#   - Exit when the close reverts to the slow EMA; a 0.8% hard stop below the
#     entry reference and a 0.6% trailing stop bound a dip that keeps falling,
#     and a 30-bar cooldown after each exit keeps it from churning.
#   - No entries in the last 20 bars: a fill there is flattened at once and
#     only pays the round-trip cost.
# The other modes (donchian / ema / mr / hybrid / mom) are the alternatives the
# strategy was tuned against on the kit's dev window; they stay in the file as
# the record of that comparison. Long only, at most one position.
# Pure function of the bars seen so far: no clock, no environment, no randomness,
# so the two graded executions agree byte for byte.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

P = {
    "mode": "dip",
    "fixed_frac": 0.9,     # >0: use this equity fraction instead of vol scaling
    "mom_h": 240,          # momentum lookback (bars) for mom mode
    "mom_band": 0.002,     # momentum hysteresis band (fraction of price)
    "dip_pct": 0.006,      # dip mode: stretch below the slow EMA that arms a buy
    "dip_exit_pct": 0.0,   # dip mode: exit at slow EMA * (1 + this)
    "stop_pct": 0.008,     # dip mode: hard stop below the entry reference
    "trail_pct": 0.006,    # dip mode: trailing stop below the peak close
    "dip_trigger": 0,      # dip mode: 1 = wait for a 1-bar reversal, 0 = buy at once
    "cooldown": 30,        # bars after an exit before a new entry (dip mode)
    "entry_n": 120,        # Donchian entry lookback (bars)
    "exit_n": 60,          # Donchian exit lookback (bars)
    "ema_fast": 15,
    "ema_slow": 240,
    "ema_trend": 240,      # regime EMA on closes
    "atr_n": 30,           # ATR lookback (true range, Wilder smoothing)
    "trail_atr": 4.0,      # trailing stop distance, in ATRs
    "stop_atr": 3.0,       # hard stop below entry, in ATRs (mr mode)
    "mr_k": 1.0,           # mr entry stretch below slow EMA, in ATRs
    "max_hold": 360,       # bars; exit a stale position
    "base_frac": 0.85,     # position size at reference volatility
    "vol_ref_bps": 55.0,   # ATR (bps of price) at which base_frac is used
    "min_frac": 0.30,
    "max_frac": 0.95,
    "max_slippage_bps": 500,
    "end_cutoff": 20,      # no new entries this many bars before the window ends
    "window_bars": 1440,
}

_hist = {"closes": [], "highs": [], "lows": [], "ema_f": None, "ema_s": None,
         "ema_t": None, "atr": None, "prev_close": None, "holding": False,
         "peak": 0, "held_bars": 0, "entry_ref": 0, "prev_f": None, "prev_s": None,
         "armed": False, "cool": 0}


def _ema(prev, x, n):
    k = 2.0 / (n + 1)
    return x if prev is None else prev + k * (x - prev)


def _update_indicators(close, high, low):
    h = _hist
    h["prev_f"] = h["ema_f"]
    h["prev_s"] = h["ema_s"]
    h["closes"].append(close)
    h["highs"].append(high)
    h["lows"].append(low)
    h["ema_f"] = _ema(h["ema_f"], close, P["ema_fast"])
    h["ema_s"] = _ema(h["ema_s"], close, P["ema_slow"])
    h["ema_t"] = _ema(h["ema_t"], close, P["ema_trend"])
    if h["prev_close"] is None:
        tr = high - low
    else:
        tr = max(high - low, abs(high - h["prev_close"]), abs(low - h["prev_close"]))
    h["atr"] = float(tr) if h["atr"] is None else h["atr"] + (tr - h["atr"]) / P["atr_n"]
    h["prev_close"] = close


def _size(state):
    h = _hist
    if P.get("fixed_frac", 0) > 0:
        frac = P["fixed_frac"]
    else:
        atr_bps = (h["atr"] / h["prev_close"] * 10000.0) if h["atr"] and h["prev_close"] else P["vol_ref_bps"]
        frac = P["base_frac"] * P["vol_ref_bps"] / max(atr_bps, 1.0)
        frac = max(P["min_frac"], min(P["max_frac"], frac))
    notional = int(state.equity * frac)
    return min(notional, state.cash, state.weight_room(SYMBOL))


def on_batch(state, batch):
    bar = state.latest.get(SYMBOL)
    if bar is None:
        return None
    close = int(bar["c"])
    high = int(bar["h"])
    low = int(bar["l"])
    h = _hist
    closes = h["closes"]
    highs = h["highs"]
    lows = h["lows"]
    n = len(closes)  # bars before this one

    held = state.positions.get(SYMBOL)
    holding = held is not None and held.qty > 0
    if holding and not h["holding"]:
        h["peak"] = close
        h["held_bars"] = 0
        h["entry_ref"] = close
    h["holding"] = holding
    if holding:
        h["held_bars"] += 1
        if close > h["peak"]:
            h["peak"] = close

    orders = []
    mode = P["mode"]
    if state.warming_up:
        _update_indicators(close, high, low)
        return None

    if holding:
        exit_now = False
        if mode == "donchian":
            if n >= P["exit_n"] and close < min(lows[-P["exit_n"]:]):
                exit_now = True
        elif mode == "ema":
            if (h["prev_f"] is not None and h["prev_s"] is not None
                    and h["ema_f"] is not None and h["ema_s"] is not None
                    and h["prev_f"] >= h["prev_s"] and h["ema_f"] < h["ema_s"]):
                exit_now = True
        elif mode == "mr":
            if h["ema_f"] is not None and close >= h["ema_f"]:
                exit_now = True
            if h["atr"] and close < h["entry_ref"] - P["stop_atr"] * h["atr"]:
                exit_now = True
        elif mode == "hybrid":
            if h["ema_s"] is not None and close < h["ema_s"]:
                exit_now = True
        elif mode == "mom":
            if n >= P["mom_h"] and close / closes[-P["mom_h"]] - 1.0 < -P["mom_band"]:
                exit_now = True
        elif mode == "dip":
            if h["ema_s"] is not None and close >= h["ema_s"] * (1.0 + P["dip_exit_pct"]):
                exit_now = True
            if close < h["entry_ref"] * (1.0 - P["stop_pct"]):
                exit_now = True
            if close < h["peak"] * (1.0 - P["trail_pct"]):
                exit_now = True
        if mode != "mr" and mode != "dip" and h["atr"] and close < h["peak"] - P["trail_atr"] * h["atr"]:
            exit_now = True
        if h["held_bars"] > P["max_hold"]:
            exit_now = True
        if exit_now:
            h["cool"] = P["cooldown"]
            h["armed"] = False
            orders.append({"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                           "max_slippage_bps": P["max_slippage_bps"]})
    else:
        bars_left = P["window_bars"] - state.seq
        enter = False
        if bars_left > P["end_cutoff"]:
            if mode == "donchian":
                if (n >= P["entry_n"] and h["ema_t"] is not None and close > h["ema_t"]
                        and close > max(highs[-P["entry_n"]:])):
                    enter = True
            elif mode == "ema":
                if (h["prev_f"] is not None and h["prev_s"] is not None
                        and h["ema_f"] is not None and h["ema_s"] is not None
                        and h["ema_t"] is not None
                        and h["prev_f"] <= h["prev_s"] and h["ema_f"] > h["ema_s"]
                        and close > h["ema_t"]):
                    enter = True
            elif mode == "mr":
                if (h["ema_s"] is not None and h["atr"]
                        and close < h["ema_s"] - P["mr_k"] * h["atr"]):
                    enter = True
            elif mode == "hybrid":
                if (h["ema_t"] is not None and h["ema_f"] is not None
                        and h["prev_close"] is not None
                        and close > h["ema_t"] and close > h["ema_f"]
                        and h["prev_close"] <= h["ema_f"]):
                    enter = True
            elif mode == "mom":
                if (n >= P["mom_h"]
                        and close / closes[-P["mom_h"]] - 1.0 > P["mom_band"]):
                    enter = True
            elif mode == "dip":
                if h["cool"] > 0:
                    h["cool"] -= 1
                if h["ema_s"] is not None:
                    if close < h["ema_s"] * (1.0 - P["dip_pct"]):
                        h["armed"] = True
                    elif close >= h["ema_s"]:
                        h["armed"] = False
                    if h["armed"] and h["cool"] <= 0:
                        if not P["dip_trigger"]:
                            enter = True
                        elif n >= 1 and close > highs[-1]:
                            enter = True
                    if enter:
                        h["armed"] = False
        if enter:
            notional = _size(state)
            if notional >= 1_000_000:
                orders.append({"side": "buy", "symbol": SYMBOL,
                               "notional_quote_units": notional,
                               "max_slippage_bps": P["max_slippage_bps"]})
    _update_indicators(close, high, low)
    return orders if orders else None


if __name__ == "__main__":
    makefun_bars.run(on_batch)
