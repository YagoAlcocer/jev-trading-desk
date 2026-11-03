"""brier1 r2 backfill (read-only w.r.t. live). Usage:
  python3 -B backfill.py --snapshot              # copy live paper.db (sqlite backup API, ro) + decisions log into ./data
  python3 -B backfill.py [--fetch] [--now ISO]   # rebuild ./data/staging.db from the COPIES
--fetch (fix 5): <=4 req/min, aborts the whole fetch on the first 429 (records Retry-After, no retry),
refuses to start if the live collector logged gecko_backoff in the last BACKOFF_LOOKBACK_MIN minutes,
and only for rows whose 24 h horizon has passed. build() only ever replaces staging.db; it never touches
data/threshold_changes.db."""
import argparse, datetime as dt, json, os, shutil, sqlite3, sys, time, urllib.request, urllib.error
import outcomes_lib as L
HERE = os.path.dirname(os.path.abspath(__file__))
def data_dir(): return os.environ.get("BRIER1_DATA") or os.path.join(HERE, "data")
LIVE = "/workspace/trading"; LIVE_LOG = f"{LIVE}/decisions.log.jsonl"
MIN_INTERVAL_S = 15.0; BACKOFF_LOOKBACK_MIN = 30; TAIL_BYTES = 2_000_000

def wall_clock(): return dt.datetime.now(dt.timezone.utc)

def snapshot():
    d = data_dir(); os.makedirs(d, exist_ok=True)
    src = sqlite3.connect(f"file:{LIVE}/paper.db?mode=ro", uri=True); dst = sqlite3.connect(os.path.join(d, "paper_copy.db"))
    src.backup(dst); dst.close(); src.close()
    shutil.copyfile(LIVE_LOG, os.path.join(d, "decisions_copy.jsonl"))

def load_log(path):
    out = []
    for l in open(path):
        try: out.append(json.loads(l))
        except ValueError: pass
    return out

def collector_in_backoff(log_path, now, lookback_min=BACKOFF_LOOKBACK_MIN):
    """Read-only tail of the live log. True (with record) if a gecko_backoff is recent or still running."""
    if not os.path.exists(log_path): return True, {"why": "log missing"}
    with open(log_path, "rb") as f:
        f.seek(0, 2); n = f.tell(); f.seek(max(0, n - TAIL_BYTES)); lines = f.read().decode("utf-8", "replace").splitlines()
    for l in reversed(lines):
        try: r = json.loads(l)
        except ValueError: continue
        if r.get("kind") != "gecko_backoff": continue
        t = L.parse(r["ts"]); until = t + dt.timedelta(seconds=float(r.get("seconds") or 0))
        if now - t <= dt.timedelta(minutes=lookback_min) or until >= now: return True, r
        return False, r
    return False, None

class GeckoAbort(Exception):
    def __init__(s, retry_after): super().__init__(f"HTTP 429, Retry-After={retry_after}"); s.retry_after = retry_after

class CollectorBackoff(Exception):
    """Live collector entered gecko_backoff mid-run: stop fetching, do not write gecko_not_before."""

class Gecko:
    def __init__(s, cache, opener=None, clock=time.time, sleep=time.sleep, guard=None):
        s.cache = cache; s.guard = guard; s.last = None; s.opener = opener or urllib.request.urlopen; s.clock = clock; s.sleep = sleep
        s.calls = []; s.not_before = 0.0; os.makedirs(cache, exist_ok=True)
    def ohlcv(s, pool, t0):
        end = int((t0 + L.H24).timestamp())
        f = os.path.join(s.cache, f"{pool}_{end}.json")
        if os.path.exists(f): return json.load(open(f))
        if s.guard: s.guard()          # r3: check collector backoff before waiting (raises CollectorBackoff)
        now = s.clock()
        wait = max(0.0, (s.last + MIN_INTERVAL_S - now) if s.last is not None else 0.0, s.not_before - now)
        if wait: s.sleep(wait)
        if s.guard: s.guard()          # r3.1: re-check AFTER the 15 s wait, right before the request
        s.last = s.clock(); s.calls.append(s.last)
        url = (f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/hour"
               f"?aggregate=1&limit=30&currency=usd&before_timestamp={end + L.HOUR}")
        try:
            with s.opener(urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "brier1-r2"}), timeout=20) as r:
                j = json.load(r); code = getattr(r, "status", 200)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = e.headers.get("Retry-After") if e.headers else None
                raise GeckoAbort(ra)
            return {"http": e.code, "pool_valid": False, "candles": []}
        except Exception as e:
            return {"http": None, "pool_valid": False, "candles": [], "error": type(e).__name__}
        lst = (((j or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list")
        res = {"http": code, "pool_valid": isinstance(lst, list),
               "candles": [(int(x[0]), float(x[2])) for x in (lst or [])]}
        if code == 200 and res["pool_valid"] and s.clock() > end + L.HOUR: json.dump(res, open(f, "w"))
        return res

def _ra_seconds(ra):
    try: return max(0.0, float(ra))
    except (TypeError, ValueError): pass
    try:
        import email.utils; return max(0.0, (email.utils.parsedate_to_datetime(ra) - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except Exception: return 600.0   # unknown -> 10 min
def save_not_before(d, now, ra):
    nb = now + dt.timedelta(seconds=max(_ra_seconds(ra), 60.0))
    json.dump({"not_before": nb.isoformat(), "retry_after": ra}, open(os.path.join(d, "gecko_not_before.json"), "w"))
def retry_not_before(d):
    f = os.path.join(d, "gecko_not_before.json")
    return L.parse(json.load(open(f))["not_before"]) if os.path.exists(f) else None

def answers_of(r):
    a = r.get("answers") if "answers" in r else (r.get("jev") or {}).get("answers")
    if not a and r.get("rule") == "edge":   # post-Jev edge rejects log only P_exhausted (= Q2 answer)
        pe = ((r.get("state") or {}).get("edge_inputs") or {}).get("P_exhausted")
        a = {"Q2": pe} if pe is not None else None
    return a

UNANSWERED_WHY = {
 "position_limit": "rejected by can_open() BEFORE Jev is asked (collector.py scan loop); no forecast exists",
 "cycle_entry_limit": "rejected by the per-cycle entry cap BEFORE Jev is asked; no forecast exists",
 "jev_unavailable": "Jev error/timeout with no answer logged (NO TRADE per questions.md); no forecast exists",
}

def build(now, fetch, gecko=None, live_log=LIVE_LOG):
    d = data_dir(); recs = load_log(os.path.join(d, "decisions_copy.jsonl"))
    pdb = sqlite3.connect(f"file:{os.path.join(d, 'paper_copy.db')}?mode=ro", uri=True)
    st = os.path.join(d, "staging.db"); tmp = st + ".tmp"
    if os.path.exists(tmp): os.remove(tmp)
    db = sqlite3.connect(tmp); db.executescript(open(os.path.join(HERE, "schema.sql")).read())
    meta = {"fetch": bool(fetch), "fetch_aborted": None, "backoff_check": None}
    g = None
    if fetch:
        real_now = wall_clock()   # r3: backoff check uses the real clock, never --now
        busy, rec = collector_in_backoff(live_log, real_now)
        meta["backoff_check"] = {"busy": busy, "record_ts": (rec or {}).get("ts")}
        nb = retry_not_before(d)
        if nb and real_now < nb: busy = True; meta["backoff_check"]["retry_after_until"] = nb.isoformat()
        if busy: print("fetch skipped: collector gecko_backoff recent/active", file=sys.stderr)
        else:
            def guard():
                b, rr = collector_in_backoff(live_log, wall_clock())
                if b: raise CollectorBackoff((rr or {}).get("ts"))
            g = gecko or Gecko(os.path.join(d, "ohlcv_cache"), guard=guard)
            if isinstance(g, Gecko) and g.guard is None: g.guard = guard
    seen = set()
    for r in recs:
        if r.get("kind") != "decision" or r.get("strategy") != "jev": continue
        ans = answers_of(r)
        if not ans:
            db.execute("insert or ignore into unanswered_decisions values(?,?,?,?,?,?,?)", (r["hash"], r.get("seq"), r["ts"], r.get("mint"),
                       r.get("action"), r.get("rule"), UNANSWERED_WHY.get(r.get("rule"), "no answers in record")))
            continue
        t0 = L.parse(r["ts"]); stt = r.get("state") or r.get("state_shown") or {}
        pool = r.get("pool") or next(iter(pdb.execute("select pool from scans where mint=? and ts<=? order by ts desc limit 1", (r["mint"], r["ts"]))), (None,))[0]
        try: p0 = float(stt["price_usd"]) if stt.get("price_usd") else None
        except (TypeError, ValueError): p0 = None
        jm = r.get("jev_model") or (r.get("jev") or {}).get("jev_model")
        for q, a in ans.items():
            qv = (r.get("q4_version") or (r.get("jev") or {}).get("q4_version") or "Q4b_organic_traction_v2p1") if q == "Q4b" else f"{q}_v0"
            od, dstat, _ = L.DEFS.get(q, (f"{q}_undefined", "proxy_pending_approval", ""))
            p = L.p_from_answer(q, a)
            first = int((r["mint"], qv) not in seen); seen.add((r["mint"], qv))
            if p is None: res = ("unresolved", "invalid_p", None, 0, {"raw": a})
            elif q == "Q1":
                res = L.holder_outcome(list(pdb.execute("select ts_epoch, top10_pct from holder_snapshots where mint=?", (r["mint"],))), t0, now)
            elif q in ("Q2", "Q4", "Q4b"):
                if not pool: res = ("unresolved", "no_pool", None, 0, {}) if now >= t0 + L.H24 else ("unresolved", "pending_horizon", None, 0, {})
                else:
                    f = None
                    if g and now >= t0 + L.H24 and p0 and not meta["fetch_aborted"]:
                        try: f = g.ohlcv(pool, t0)
                        except CollectorBackoff as e:
                            meta["fetch_aborted"] = {"at": r["ts"], "collector_backoff_ts": str(e)}; g = None
                        except GeckoAbort as e:
                            meta["fetch_aborted"] = {"at": r["ts"], "retry_after": e.retry_after}; g = None
                            save_not_before(d, wall_clock(), e.retry_after)
                    res = L.price_outcome(q, p0, f, t0, now)
                    if res[1] == "not_fetched" and meta["fetch_aborted"]: res = ("unresolved", "fetch_aborted") + res[2:]
            else: res = ("unresolved", "not_price_resolvable", None, 0, {})
            status, why, out, died, ev = res
            db.execute("""insert or ignore into outcomes(decision_hash,decision_seq,mint,pool,symbol,question_id,question_version,outcome_def,def_status,
              p_jev,raw_answer,jev_model,thresholds_sha,decision_ts,resolve_ts,p0_usd,is_trade,decision_action,decision_rule,first_for_mint,
              status,unresolved_reason,died_suspected,outcome,outcome_evidence,resolved_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (r["hash"], r.get("seq"), r["mint"], pool, stt.get("symbol"), q, qv, od, dstat, p, json.dumps(a), jm,
               r.get("thresholds_sha"), r["ts"], (t0 + L.H24).isoformat(), p0, int(r.get("action") == "enter"), r.get("action"), r.get("rule"), first,
               status, why, died, out, json.dumps(ev), now.isoformat() if status == "resolved" else None))
    db.execute("create table build_meta(k primary key, v)"); db.executemany("insert into build_meta values(?,?)", [(k, json.dumps(v)) for k, v in meta.items()])
    db.commit(); db.close(); os.replace(tmp, st)
    return st, meta

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--snapshot", action="store_true"); ap.add_argument("--fetch", action="store_true"); ap.add_argument("--now")
    a = ap.parse_args()
    if a.snapshot: snapshot(); sys.exit(0)
    now = L.parse(a.now) if a.now else dt.datetime.now(dt.timezone.utc)
    st, meta = build(now, a.fetch); print(st, json.dumps(meta))
    if meta.get("fetch_aborted"): sys.exit(3)
