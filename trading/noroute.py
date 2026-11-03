"""noroute1 (PROPOSAL, staged, NOT installed; pending 069's approval). Valuation + lifecycle of positions with no executable sell quote.
Pure layer over the collector: the collector calls `step()` once per open/sin_ruta position per monitor pass. Paper only.

Cases when there is NO real quote (Jupiter / direct pool quote):
 A) pool ALIVE per DexScreener (liquidity >= ALIVE_MIN_LIQ_USD and volume_h1 >= ALIVE_MIN_VOL_H1_USD and priceUsd present):
    mark = priceUsd * qty, mark_source='dexscreener_fallback'. The stop is EVALUATED on that mark, but it can never FILL there:
    a triggered stop becomes stop_pending and closes at the first real quote (slippage + fees), even if worse.
 B) dead / drained / no fallback price: mark = $0 (-100%), mark_source='conservative_zero'.
Lifecycle (hours configurable, pending 069):
 - no route for >= SIN_RUTA_AFTER_H (2 h): section 'sin_ruta'. Still rechecked every cycle; cost still counts in the 6%-per-mint and
   portfolio exposure caps. PnL: case B = full loss; case A = DexScreener mark (display).
 - no route for >= WRITE_OFF_AFTER_H (24 h): total loss recorded (write_off), removed from caps; row never deleted, still monitored.
 - route returns: open/sin_ruta -> back to normal (stop_pending closes now); written off -> proceeds recorded as SEPARATE income
   entry (recovery_income) + log + notice.
"""
import json, time, datetime as dt
import thresholds as T   # r3 (7): caps come from the live thresholds module, read at call time (no hardcoded copies)
VERSION = "noroute1-r3"
TRANSIENT_FAILURES = ("rate_limited", "timeout", "server_error", "quote_error")   # r3 (11): never start/advance the no-route clock
def caps():
    """r3 (7): MAX_EXPOSURE_PCT (fraction), per-mint = MAX_TICKET_PCT_BANK % of equity (pre-FACTOR_NO_X base, as live sizing),
    MAX_OPEN_POSITIONS, MAX_ENTRIES_PER_MINT, all from thresholds."""
    return dict(exposure=float(T.MAX_EXPOSURE_PCT), mint=float(T.MAX_TICKET_PCT_BANK) / 100, open=T.MAX_OPEN_POSITIONS,
                entries_per_mint=T.MAX_ENTRIES_PER_MINT, factor_no_x=float(T.FACTOR_NO_X))
PNL_A_OFFICIAL = "sin dato ejecutable"   # r2: case A official strategy PnL; DexScreener mid is reference only
ALIVE_MIN_LIQ_USD = 10_000.0
ALIVE_MIN_VOL_H1_USD = 100.0
SIN_RUTA_AFTER_H = 2.0      # configurable (Chefus/auditor)
WRITE_OFF_AFTER_H = 24.0    # configurable, PENDING 069's decision
LABEL_B = "-100% · SIN RUTA (valuación conservadora)"
LABEL_A = "SIN RUTA · marca DexScreener (no ejecutable)"
SCHEMA = """CREATE TABLE IF NOT EXISTS noroute_state(position_id INTEGER PRIMARY KEY, section TEXT DEFAULT 'open', since_ts REAL,
  mark_source TEXT, last_ds_price REAL, last_ds_ts REAL, stop_pending INTEGER DEFAULT 0, stop_trigger_ts REAL, written_off_ts REAL,
  written_off_cost REAL, route_returned_ts REAL, recovered_usd REAL);"""

def ensure(c): c.executescript(SCHEMA)

def alive(ds):
    ds = ds or {}
    liq = (ds.get("liquidity") or {}).get("usd"); vol = (ds.get("volume") or {}).get("h1"); px = ds.get("priceUsd")
    try: return liq is not None and float(liq) >= ALIVE_MIN_LIQ_USD and vol is not None and float(vol) >= ALIVE_MIN_VOL_H1_USD and px is not None and float(px) > 0
    except (TypeError, ValueError): return False

def _row(c, pid):
    r = c.execute("SELECT section,since_ts,mark_source,last_ds_price,last_ds_ts,stop_pending,stop_trigger_ts,written_off_ts,written_off_cost,"
                  "route_returned_ts,recovered_usd FROM noroute_state WHERE position_id=?", (pid,)).fetchone()
    k = ("section", "since_ts", "mark_source", "last_ds_price", "last_ds_ts", "stop_pending", "stop_trigger_ts", "written_off_ts",
         "written_off_cost", "route_returned_ts", "recovered_usd")
    return dict(zip(k, r)) if r else None

def _sold_usd(ret):
    """r3 (9): proceeds come ONLY from sell()'s return: dict(usd, fee_usd) (collector) or a number; falsy = not sold."""
    if not ret: return None
    if isinstance(ret, dict): return float(ret["usd"]) - float(ret.get("fee_usd") or 0)
    return float(ret)

def step(c, pos, quote, ds, stop_frac, log, now=None, sell=None, failure="no_route"):
    """One monitor step for one position.
    pos: dict(id, mint, qty_left_raw, decimals, cost_usd). quote: None or dict(usd, impact, fee_usd) = REAL executable sell quote.
    ds: DexScreener pair dict (fallback price only). stop_frac: e.g. 0.15 (stop at -15% vs cost). log(kind, **fields): hash-chain append.
    sell(pos, quote, reason) -> closes at the real quote (collector's paper sell, slippage+fees); returns dict(usd, fee_usd) or falsy.
    failure (r3 (11)): diag kind of the failed quote when quote is None (collector passes mark_valued()['fail_kind']). Only 'no_route'
    starts/advances the no-route clock; rate_limited/timeout/server_error/quote_error leave the state untouched. Returns a dict."""
    now = now or time.time(); ensure(c); pid = pos["id"]; st = _row(c, pid)
    cost = float(pos["cost_usd"]); qty = pos["qty_left_raw"] / 10 ** int(pos.get("decimals") or 0)
    if quote is not None:   # a real executable quote exists
        if st and st["section"] == "written_off":
            if st["recovered_usd"] is not None or st["route_returned_ts"] is not None:   # r2: recovery happens ONCE
                return dict(section="written_off", route_returned=True, mark_usd=0.0, mark_source="recovered", counts_in_caps=False,
                            pnl_usd=float(st["recovered_usd"] or 0) - float(st["written_off_cost"] or cost), recovered_usd=st["recovered_usd"], already_recovered=True)
            # r3 (8)+(9): sell() FIRST, inside a savepoint; recovered_usd = sell()'s own proceeds; written only if the sell booked.
            # If sell() fails (or none is wired) nothing is written, so the next real quote retries; if the UPDATE fails the sell rolls back.
            c.execute("SAVEPOINT noroute_recovery")
            try:
                usd = _sold_usd(sell(pos, quote, "recovery after write-off")) if sell else None
                if usd is None:
                    c.execute("RELEASE SAVEPOINT noroute_recovery")
                    log("recovery_sell_failed", position_id=pid, mint=pos["mint"], note="route returned after write-off but sell() did not book; retry next real quote")
                    return dict(section="written_off", route_returned=False, mark_usd=0.0, mark_source="written_off", counts_in_caps=False,
                                pnl_usd=-float(st["written_off_cost"] or cost), recovered_usd=None, already_recovered=False, recovery_failed=True)
                c.execute("UPDATE noroute_state SET route_returned_ts=?, recovered_usd=? WHERE position_id=?", (now, usd, pid))
                c.execute("RELEASE SAVEPOINT noroute_recovery")
            except BaseException:
                c.execute("ROLLBACK TO SAVEPOINT noroute_recovery"); c.execute("RELEASE SAVEPOINT noroute_recovery"); raise
            log("recovery_notice", position_id=pid, mint=pos["mint"], usd=round(usd, 6), booked_via="sell",
                note="route returned after write-off; proceeds booked once by sell() (amount = sell() return); notice only", notify=["069"])
            return dict(section="written_off", route_returned=True, mark_usd=usd, mark_source="quote", counts_in_caps=False,
                        pnl_usd=usd - float(st["written_off_cost"] or cost), recovered_usd=usd, already_recovered=False)
        pending = bool(st and st["stop_pending"]); filled = None
        if pending and sell:   # r3: sell first; stop_pending is cleared only if the sell booked (amount from sell()'s return)
            filled = _sold_usd(sell(pos, quote, "stop (pending, first real quote)"))
            if filled is not None: log("stop_pending_filled", position_id=pid, usd=round(filled, 6), impact=quote.get("impact"), note="closed at first real quote")
        keep = 1 if (pending and filled is None) else 0
        if st: c.execute("UPDATE noroute_state SET section='open', since_ts=NULL, mark_source='quote', stop_pending=?, route_returned_ts=? WHERE position_id=?", (keep, now, pid))
        pending = pending and filled is not None
        return dict(section="open", route_returned=bool(st and st["since_ts"]), mark_usd=float(quote["usd"]), mark_source="quote",
                    counts_in_caps=True, pnl_usd=float(quote["usd"]) - cost, closed_pending_stop=pending)
    # ---- no real quote ----
    if failure in TRANSIENT_FAILURES:   # r3 (11): not a no-route; keep state, no clock, no section change, no stop_pending
        sec = st["section"] if st else "open"
        return dict(section=sec, mark_usd=None, mark_source="quote_error", failure=failure, no_route_clock=False,
                    counts_in_caps=sec != "written_off", pnl_usd=None, route_returned=False, stop_pending=bool(st and st["stop_pending"]))
    if failure != "no_route": raise ValueError(f"unknown quote failure kind {failure!r}")
    if st is None:
        c.execute("INSERT INTO noroute_state(position_id, section, since_ts) VALUES(?, 'open', ?)", (pid, now)); st = _row(c, pid)
    if st["since_ts"] is None: c.execute("UPDATE noroute_state SET since_ts=? WHERE position_id=?", (now, pid)); st["since_ts"] = now
    hours = (now - st["since_ts"]) / 3600
    if st["section"] == "written_off":
        return dict(section="written_off", hours_no_route=round(hours, 2), mark_usd=0.0, mark_source="written_off", counts_in_caps=False,
                    pnl_usd=-float(st["written_off_cost"] or cost), last_ds_price=st["last_ds_price"], route_returned=False, label=LABEL_B)
    if alive(ds):
        px = float(ds["priceUsd"]); mark, src = px * qty, "dexscreener_fallback"
        c.execute("UPDATE noroute_state SET last_ds_price=?, last_ds_ts=? WHERE position_id=?", (px, now, pid)); st["last_ds_price"] = px
        if mark <= cost * (1 - stop_frac) and not st["stop_pending"]:
            c.execute("UPDATE noroute_state SET stop_pending=1, stop_trigger_ts=? WHERE position_id=?", (now, pid)); st["stop_pending"] = 1
            log("stop_pending", position_id=pid, mark_usd=round(mark, 6), mark_source=src, note="stop triggered on fallback mark; NOT filled; retry every cycle")
        pnl, label = None, LABEL_A   # r2: official PnL = PNL_A_OFFICIAL; mark is reference only
    else:
        mark, src, pnl, label = 0.0, "conservative_zero", -cost, LABEL_B
    section = st["section"]
    if hours >= WRITE_OFF_AFTER_H:
        c.execute("UPDATE noroute_state SET section='written_off', written_off_ts=?, written_off_cost=?, mark_source=? WHERE position_id=?", (now, cost, src, pid))
        log("write_off", position_id=pid, mint=pos["mint"], cost_usd=cost, hours_no_route=round(hours, 2), note="total loss; removed from caps; row kept, still monitored")
        return dict(section="written_off", hours_no_route=round(hours, 2), mark_usd=0.0, mark_source=src, counts_in_caps=False, pnl_usd=-cost,
                    last_ds_price=st["last_ds_price"], route_returned=False, label=LABEL_B, stop_pending=bool(st["stop_pending"]))
    if hours >= SIN_RUTA_AFTER_H and section != "sin_ruta":
        section = "sin_ruta"; log("sin_ruta", position_id=pid, mint=pos["mint"], hours_no_route=round(hours, 2), mark_source=src)
    c.execute("UPDATE noroute_state SET section=?, mark_source=? WHERE position_id=?", (section, src, pid))
    if src == "dexscreener_fallback":   # r2 case A: no official mark/PnL; DexScreener mid exported as reference only
        return dict(section=section, hours_no_route=round(hours, 2), mark_usd=None, mark_source=src, counts_in_caps=True,
                    pnl_usd=None, pnl_official=PNL_A_OFFICIAL, reference_mark_usd=mark, reference_pnl_usd=mark - cost,
                    last_ds_price=st["last_ds_price"], route_returned=False, label=label, stop_pending=bool(st["stop_pending"]))
    return dict(section=section, hours_no_route=round(hours, 2), mark_usd=mark, mark_source=src, counts_in_caps=True,
                pnl_usd=-cost, last_ds_price=st["last_ds_price"], route_returned=False, label=label, stop_pending=bool(st["stop_pending"]))

def cycle_summary(results, log):
    """Logs the no-route counts once per monitor pass; returns the dashboard block."""
    nr = [r for r in results if r["mark_source"] != "quote"]
    s = dict(no_route=len([r for r in nr if r["section"] != "written_off"]), sin_ruta=len([r for r in nr if r["section"] == "sin_ruta"]),
             written_off=len([r for r in nr if r["section"] == "written_off"]), stop_pending=len([r for r in nr if r.get("stop_pending")]))
    log("noroute_count", **s); return s

def dashboard_rows(c, now=None):
    """jev/simple-agnostic export for dashboard_data: every sin_ruta / written_off / no-route position (never deleted)."""
    now = now or time.time(); ensure(c); out = []
    for pid, sec, since, src, px, pend, wo, rr, rec in c.execute(
            "SELECT position_id,section,since_ts,mark_source,last_ds_price,stop_pending,written_off_ts,route_returned_ts,recovered_usd FROM noroute_state "
            "WHERE since_ts IS NOT NULL OR section!='open' OR route_returned_ts IS NOT NULL"):
        out.append(dict(position_id=pid, section=sec, hours_no_route=round((now - since) / 3600, 2) if since else None, mark_source=src,
                        last_ds_price=px, stop_pending=bool(pend), written_off=wo is not None, route_returned=rr is not None, recovered_usd=rec,
                        flag="SIN_RUTA", label=LABEL_B if src in ("conservative_zero", "written_off") or wo else LABEL_A))
    return out

# ---- r2: limits. A sin_ruta (or open no-route) leg is still HELD: it counts toward pyramiding/per-mint legs and the 40 open cap.
def held_legs(c, positions, mint=None):
    """positions: collector rows (id, mint, cost_usd, ...) with status open. Every leg counts toward pyramiding / open-count limits,
    including sections 'open' (no route yet) and 'sin_ruta'. written_off legs also still block pyramiding (tokens still held) but
    are excluded from cost caps (counts_in_caps=False)."""
    ensure(c); return [p for p in positions if mint is None or p["mint"] == mint]

def section_of(c, pid):
    r = c.execute("SELECT section FROM noroute_state WHERE position_id=?", (pid,)).fetchone(); return r[0] if r else "open"

def can_add(c, positions, mint, ticket_usd, equity_usd, strategy, max_entries_per_mint=None, bank_amount_usd=None):
    """None if a new leg (entry or pyramid add-on) on `mint` passes; else the reason. r3 (7): caps from thresholds (caps()).
    positions MUST be this strategy's own open positions (collector: open_positions(c, strategy)); any other row -> ValueError.
    Exposure: open cost + final ticket <= MAX_EXPOSURE_PCT x equity (as live). Per mint: mint open cost + bank_amount_usd (the ticket
    BEFORE FACTOR_NO_X, size_breakdown()['bank_amount']; default ticket_usd / FACTOR_NO_X) <= MAX_TICKET_PCT_BANK% x equity (as live).
    (6) unchanged from r2: written_off legs still occupy open/leg slots; excluded from cost caps only."""
    ensure(c); k = caps(); mx = k["open"]; epm = k["entries_per_mint"] if max_entries_per_mint is None else max_entries_per_mint
    other = [p for p in positions if p.get("strategy") != strategy]
    if other: raise ValueError(f"can_add: positions of another strategy passed ({other[0].get('strategy')!r} != {strategy!r})")
    legs = held_legs(c, positions); max_entries_per_mint = epm
    if bank_amount_usd is None: bank_amount_usd = ticket_usd / k["factor_no_x"]
    if mx is not None and len(legs) >= mx: return f"max {mx} concurrent positions"
    mine = [p for p in legs if p["mint"] == mint]
    if len(mine) >= max_entries_per_mint:
        return f"already holding this token ({len(mine)} legs incl. sin_ruta, max {max_entries_per_mint})"
    if any(section_of(c, p["id"]) in ("sin_ruta", "written_off") for p in mine): return "pyramid blocked: a leg of this mint has no sell route"
    in_caps = [p for p in legs if section_of(c, p["id"]) != "written_off"]
    if sum(float(p["cost_usd"]) for p in in_caps) + ticket_usd > equity_usd * k["exposure"] + 1e-9: return f"exposure cap {k['exposure']:.0%}"
    if sum(float(p["cost_usd"]) for p in in_caps if p["mint"] == mint) + bank_amount_usd > equity_usd * k["mint"] + 1e-9: return f"per-mint cap {k['mint']:.0%}"
    return None
