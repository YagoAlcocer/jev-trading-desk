"""brier1 r2: pure outcome logic (no network, no LLM). 24 h horizon.
Window rule (fix 4): only COMPLETE hourly candles fully inside [t0, t0+24h] are used, i.e. candle start e with
  e >= t0 and e + 3600 <= t0 + 24h. The partial hour containing t0 (may hold pre-decision prices) and the partial
  hour ending after t0+24h (may hold post-horizon prices) are trimmed, so the window covers 23-24 full hours.
  The event uses each candle's OHLC HIGH (intrabar max) vs 1.15*p0; p0 is price_usd logged in the decision record.
Dead/illiquid (fix 3): if GeckoTerminal answered HTTP 200 for a valid pool, an hour with no candle = no trades =
  no rise in that hour, so the window resolves on the candles present (0 candles -> max rise not reached)."""
import datetime as dt, math
H24 = dt.timedelta(hours=24); HOUR = 3600
DEFS = {
 "Q2": ("Q2_no_rise15_24h_v1", "questions.md",
        "yes=1 if max(OHLC high of complete hourly candles inside [t0,t0+24h]) < 1.15*p0"),
 "Q1": ("Q1_top10_drop2pp_24h_proxy_v0", "proxy_pending_approval",
        "yes=1 if top10_pct(t+24h,+-60min) - top10_pct(t,[-30min,+5min]) <= -2.0 pp (holder_snapshots)"),
 "Q4": ("Q4_rise15_24h_proxy_v1", "proxy_pending_approval", "p=(score-1)/9; yes=1 if max high >= 1.15*p0 (same window)"),
 "Q4b": ("Q4b_rise15_24h_proxy_v1", "proxy_pending_approval", "p=(score-1)/9; yes=1 if max high >= 1.15*p0 (same window)"),
 "Q3": ("Q3_needs_label_v0", "proxy_pending_approval", "not resolvable from prices -> always unresolved"),
}
def parse(ts): return dt.datetime.fromisoformat(ts).astimezone(dt.timezone.utc)

def p_from_answer(q, a):
    """Validated P(yes) in [0,1] or None (fix 6)."""
    try:
        if q in ("Q4", "Q4b"):
            if not isinstance(a, (list, tuple)) or not a: return None
            s = float(a[0])
            if not (math.isfinite(s) and 1 <= s <= 10): return None
            return (s - 1) / 9
        if isinstance(a, bool) or not isinstance(a, (int, float)): return None
        p = float(a)
        return p if math.isfinite(p) and 0.0 <= p <= 1.0 else None
    except (TypeError, ValueError): return None

def window(candles, t0):
    a, b = t0.timestamp(), (t0 + H24).timestamp()
    return [(e, h) for e, h in candles if e >= a and e + HOUR <= b]

def price_outcome(q, p0, fetch, t0, now):
    """fetch: None (not fetched) or dict(http=int, pool_valid=bool, candles=[(epoch_start, high)]).
    Returns (status, reason, outcome, died, evidence)."""
    if now < t0 + H24: return "unresolved", "pending_horizon", None, 0, {}
    if not p0 or p0 <= 0: return "unresolved", "no_p0", None, 0, {}
    if fetch is None: return "unresolved", "not_fetched", None, 0, {}
    if fetch.get("http") != 200 or not fetch.get("pool_valid"):
        return "unresolved", "fetch_failed", None, 0, {"http": fetch.get("http")}
    w = window(fetch.get("candles") or [], t0)
    mx = max((h for _, h in w), default=None)
    last = max((e for e, _ in w), default=None)
    died = int(last is None or last + HOUR < (t0 + H24).timestamp() - 3 * HOUR)
    ev = {"source": "geckoterminal_ohlcv_hour_high", "n_complete_candles": len(w), "max_high": mx, "p0": p0,
          "ratio": (mx / p0) if mx else None, "last_candle": last, "missing_hours_as_no_trade": 24 - len(w)}
    hit = mx is not None and mx >= 1.15 * p0
    return "resolved", None, (int(not hit) if q == "Q2" else int(hit)), died, ev

def holder_outcome(snaps, t0, now):
    end = t0 + H24
    if now < end + dt.timedelta(hours=1): return "unresolved", "pending_horizon", None, 0, {}
    b = [(e, v) for e, v in snaps if v is not None and t0.timestamp() - 1800 <= e <= t0.timestamp() + 300]
    a = [(e, v) for e, v in snaps if v is not None and abs(e - end.timestamp()) <= 3600]
    if not b or not a: return "unresolved", "no_holder_snapshot", None, 0, {"n_base": len(b), "n_after": len(a)}
    vb = min(b, key=lambda x: abs(x[0] - t0.timestamp()))[1]; va = min(a, key=lambda x: abs(x[0] - end.timestamp()))[1]
    return "resolved", None, int(va - vb <= -2.0), 0, {"source": "paper.db holder_snapshots", "top10_t0": vb, "top10_t24": va}
