# btc-race-24h-20261117-0603 entry: a cbBTC/USDC minute-bar trading bot.
#
# Strategy (v2): mean-reversion dip buying against the slow EMA (240 bars),
# structured to survive execution costs and parameter drift:
#   - Entry: when the close stretches 0.575% below the slow EMA, buy 70% of
#     equity, SPLIT across two bars (half now, half next bar while the price
#     is still below the mean). Each fill faces a fresh pool, so splitting
#     roughly halves the price impact of the entry; it was the single biggest
#     cost saver found on the dev window.
#   - Ladder: if the dip deepens a further 0.4% below the entry reference,
#     add 30% of equity once, averaging into the stretch instead of stopping
#     out of it.
#   - Exit in two halves: at the mean cross, bank half; the rest rides while
#     the price holds above the mean and is sold when it falls back below.
#     Banking only half at the cross was worth more than any entry tweak —
#     full exits at the mean leave the overshoot on the table, while waiting
#     for a fixed overshoot gives the whole reversion back.
#   - Risk bounds unchanged in spirit: 0.8% hard stop below the entry
#     reference, 0.6% trailing stop below the peak close, 360-bar max hold,
#     30-bar cooldown after a full exit, no entries in the last 20 bars.
# Chosen by a robustness score (worst of full window, both halves, and a
# +-25% parameter-neighbourhood), not by peak dev PnL: dev +$10.33 with a
# positive worst case across all of those, vs +$3.67 and a negative worst
# case for the single-shot v1. Long only, at most one position.
# Pure function of the bars seen so far: no clock, no environment, no
# randomness, so the two graded executions agree byte for byte.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

P = {
    "mode": "dip",
    "fixed_frac": 0.70,    # equity fraction per entry (before the split)
    "dip_pct": 0.00575,    # stretch below the slow EMA that arms a buy
    "dip_exit_pct": 0.0,   # mean-cross exit reference offset
    "stop_pct": 0.008,     # hard stop below the entry reference
    "trail_pct": 0.006,    # trailing stop below the peak close
    "cooldown": 30,        # bars after a full exit before a new entry
    "ema_slow": 240,
    "atr_n": 30,
    "max_hold": 360,
    "max_slippage_bps": 500,
    "end_cutoff": 20,
    "window_bars": 1440,
    "vol_ref_bps": 55.0,
    # --- structure knobs (0 = off) ---
    "ema_mid": 90,          # tier B reference EMA (unused while dip_b_pct = 0)
    "dip_b_pct": 0.0,       # tier B stretch vs ema_mid (off)
    "frac_b": 0.45,         # tier B equity fraction
    "depth_mult": 0.0,      # size scaling with dip depth (off: hurt on dev)
    "frac_cap": 0.97,
    "slope_n": 0,           # EMA-slope entry filter lookback (off: hurt on dev)
    "slope_max": 0.002,
    "vol_k": 0.0,           # vol-adaptive dip threshold (off: hurt on dev)
    "trail_arm": 0.0,       # trailing stop arming level (off: no effect on dev)
    "ladder_add_pct": 0.004,  # add-on buy stretch below the entry reference
    "ladder_frac": 0.3,       # add-on equity fraction
    "split_share": 0.5,       # share of the entry deferred to the next bar
    "add_after": 0,           # time-ladder (off)
    "add_after_frac": 0.0,
    "ladder2_mult": 0.0,      # second ladder level (off: never triggered)
    "ladder2_frac": 0.0,
    "exit_split": 0.5,        # share banked at the mean cross; rest rides
}

_hist = {"closes": [], "highs": [], "lows": [], "ema_m": None, "ema_s": None,
         "atr": None, "prev_close": None, "holding": False,
         "peak": 0, "held_bars": 0, "entry_ref": 0, "cool": 0,
         "ema_s_hist": [], "kind": "A", "pend_kind": "A", "pend_frac": 0.0,
         "added": False, "split_pending": 0.0, "added_t": False,
         "added2": False, "partial": False}


def _ema(prev, x, n):
    k = 2.0 / (n + 1)
    return x if prev is None else prev + k * (x - prev)


def _update_indicators(close, high, low):
    h = _hist
    h["closes"].append(close)
    h["highs"].append(high)
    h["lows"].append(low)
    h["ema_m"] = _ema(h["ema_m"], close, P["ema_mid"])
    h["ema_s"] = _ema(h["ema_s"], close, P["ema_slow"])
    h["ema_s_hist"].append(h["ema_s"])
    if h["prev_close"] is None:
        tr = high - low
    else:
        tr = max(high - low, abs(high - h["prev_close"]), abs(low - h["prev_close"]))
    h["atr"] = float(tr) if h["atr"] is None else h["atr"] + (tr - h["atr"]) / P["atr_n"]
    h["prev_close"] = close


def _notional(state, frac):
    return min(int(state.equity * frac), state.cash, state.weight_room(SYMBOL))


def on_batch(state, batch):
    bar = state.latest.get(SYMBOL)
    if bar is None:
        return None
    close = int(bar["c"])
    high = int(bar["h"])
    low = int(bar["l"])
    h = _hist

    held = state.positions.get(SYMBOL)
    holding = held is not None and held.qty > 0
    if holding and not h["holding"]:
        h["peak"] = close
        h["held_bars"] = 0
        h["entry_ref"] = close
        h["kind"] = h["pend_kind"]
        h["added"] = False
    if not holding:
        h["added"] = False
        h["added_t"] = False
        h["added2"] = False
        h["partial"] = False
        h["split_pending"] = 0.0
    h["holding"] = holding
    if holding:
        h["held_bars"] += 1
        if close > h["peak"]:
            h["peak"] = close

    orders = []
    if state.warming_up:
        _update_indicators(close, high, low)
        return None

    if holding:
        ref = h["ema_s"] if h["kind"] == "A" else h["ema_m"]
        risk_exit = False
        if close < h["entry_ref"] * (1.0 - P["stop_pct"]):
            risk_exit = True
        if P["trail_arm"] <= 0 or h["peak"] >= h["entry_ref"] * (1.0 + P["trail_arm"]):
            if close < h["peak"] * (1.0 - P["trail_pct"]):
                risk_exit = True
        if h["held_bars"] > P["max_hold"]:
            risk_exit = True
        mean_cross = ref is not None and close >= ref * (1.0 + P["dip_exit_pct"])
        fell_back = h["partial"] and ref is not None and close < ref
        if risk_exit or fell_back:
            h["cool"] = P["cooldown"]
            orders.append({"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                           "max_slippage_bps": P["max_slippage_bps"]})
        elif mean_cross:
            qty_part = int(held.qty * P["exit_split"]) if P["exit_split"] > 0 else 0
            if qty_part > 0 and not h["partial"]:
                h["partial"] = True
                orders.append({"side": "sell", "symbol": SYMBOL, "qty": qty_part,
                               "max_slippage_bps": P["max_slippage_bps"]})
            else:
                h["cool"] = P["cooldown"]
                orders.append({"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                               "max_slippage_bps": P["max_slippage_bps"]})
        else:
            # deferred second tranche of a split entry: complete it while the
            # price is still below the reference mean
            if h["split_pending"] > 0 and h["held_bars"] >= 1 \
                    and ref is not None and close < ref:
                notional = _notional(state, h["split_pending"])
                if notional >= 1_000_000:
                    orders.append({"side": "buy", "symbol": SYMBOL,
                                   "notional_quote_units": notional,
                                   "max_slippage_bps": P["max_slippage_bps"]})
                h["split_pending"] = 0.0
            # time ladder: average in if the dip persists
            if P["add_after"] > 0 and not h["added_t"] \
                    and h["held_bars"] >= P["add_after"] \
                    and ref is not None and close < ref * (1.0 - P["dip_pct"] * 0.5):
                notional = _notional(state, P["add_after_frac"])
                if notional >= 1_000_000:
                    h["added_t"] = True
                    orders.append({"side": "buy", "symbol": SYMBOL,
                                   "notional_quote_units": notional,
                                   "max_slippage_bps": P["max_slippage_bps"]})
            if P["ladder_add_pct"] > 0 and not h["added"] \
                    and close < h["entry_ref"] * (1.0 - P["ladder_add_pct"]):
                notional = _notional(state, P["ladder_frac"])
                if notional >= 1_000_000:
                    h["added"] = True
                    orders.append({"side": "buy", "symbol": SYMBOL,
                                   "notional_quote_units": notional,
                                   "max_slippage_bps": P["max_slippage_bps"]})
            if P["ladder2_mult"] > 0 and h["added"] and not h["added2"] \
                    and close < h["entry_ref"] * (1.0 - P["ladder_add_pct"] * P["ladder2_mult"]):
                notional = _notional(state, P["ladder2_frac"])
                if notional >= 1_000_000:
                    h["added2"] = True
                    orders.append({"side": "buy", "symbol": SYMBOL,
                                   "notional_quote_units": notional,
                                   "max_slippage_bps": P["max_slippage_bps"]})
    else:
        bars_left = P["window_bars"] - state.seq
        enter = False
        if bars_left > P["end_cutoff"]:
            if h["cool"] > 0:
                h["cool"] -= 1
            elif h["ema_s"] is not None:
                # slope filter: skip knife-catching in a strong downtrend
                slope_ok = True
                if P["slope_n"] > 0 and len(h["ema_s_hist"]) > P["slope_n"]:
                    slope = h["ema_s"] / h["ema_s_hist"][-1 - P["slope_n"]] - 1.0
                    if slope < -P["slope_max"]:
                        slope_ok = False
                eff_dip = P["dip_pct"]
                if P["vol_k"] > 0 and h["atr"]:
                    atr_bps = h["atr"] / close * 10000.0
                    factor = 1.0 + P["vol_k"] * (atr_bps / P["vol_ref_bps"] - 1.0)
                    factor = max(0.7, min(1.6, factor))
                    eff_dip = P["dip_pct"] * factor
                depth_s = (h["ema_s"] - close) / h["ema_s"]
                if slope_ok and depth_s >= eff_dip:
                    frac = P["fixed_frac"]
                    if P["depth_mult"] > 0:
                        frac = P["fixed_frac"] * (
                            1.0 + P["depth_mult"] * (depth_s / P["dip_pct"] - 1.0))
                        frac = max(0.2, min(P["frac_cap"], frac))
                    h["pend_kind"] = "A"
                    h["pend_frac"] = frac
                    enter = True
                elif P["dip_b_pct"] > 0 and h["ema_m"] is not None:
                    depth_m = (h["ema_m"] - close) / h["ema_m"]
                    if depth_m >= P["dip_b_pct"]:
                        h["pend_kind"] = "B"
                        h["pend_frac"] = P["frac_b"]
                        enter = True
        if enter:
            frac_now = h["pend_frac"] * (1.0 - P["split_share"])
            h["split_pending"] = h["pend_frac"] * P["split_share"]
            notional = _notional(state, frac_now)
            if notional >= 1_000_000:
                orders.append({"side": "buy", "symbol": SYMBOL,
                               "notional_quote_units": notional,
                               "max_slippage_bps": P["max_slippage_bps"]})
            else:
                h["split_pending"] = 0.0
    _update_indicators(close, high, low)
    return orders if orders else None


if __name__ == "__main__":
    makefun_bars.run(on_batch)
