import datetime as dt, io, json, os, sqlite3, sys, tempfile, unittest, urllib.error
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import outcomes_lib as L, score as S, thresholds_log as T
U = dt.timezone.utc; T0 = dt.datetime(2026, 10, 30, 3, 0, tzinfo=U); LATER = T0 + dt.timedelta(hours=30)
T0h = dt.datetime(2026, 10, 30, 3, 30, tzinfo=U)  # mid-hour decision
def hourly(highs, start):
    s = int(start.replace(minute=0, second=0, microsecond=0).timestamp()); return [(s + 3600 * i, h) for i, h in enumerate(highs)]
CLOCK = [LATER]   # stands in for the real wall clock in backfill.wall_clock
def ok(c): return {"http": 200, "pool_valid": True, "candles": c}

# ---------------- prior r1 tests (kept; adapted to r2 API) ----------------
class Outcomes(unittest.TestCase):
    def test_pending(self): self.assertEqual(L.price_outcome("Q2", 1, ok([]), T0, T0 + dt.timedelta(hours=23))[1], "pending_horizon")
    def test_q2_no_rise(self): self.assertEqual(L.price_outcome("Q2", 1.0, ok(hourly([1.0] * 26, T0)), T0, LATER)[:3], ("resolved", None, 1))
    def test_q2_rise(self):
        c = ok(hourly([1.0] * 5 + [1.2] + [1.0] * 20, T0)); self.assertEqual(L.price_outcome("Q2", 1.0, c, T0, LATER)[2], 0)
        self.assertEqual(L.price_outcome("Q4b", 1.0, c, T0, LATER)[2], 1)
    def test_no_lookback_bucket(self):
        self.assertEqual(L.price_outcome("Q2", 1.0, ok(hourly([5.0] + [1.0] * 25, T0h)), T0h, LATER)[2], 1)
    def test_no_lookahead(self):
        self.assertEqual(L.price_outcome("Q2", 1.0, ok(hourly([1.0] * 24 + [9.0] * 3, T0)), T0, LATER)[2], 1)
    def test_dead_token_with_prior_spike_resolves(self):
        self.assertEqual(L.price_outcome("Q2", 1.0, ok(hourly([1.0, 1.0, 2.0], T0)), T0, LATER)[:3], ("resolved", None, 0))
    def test_no_data(self): self.assertEqual(L.price_outcome("Q2", 1.0, None, T0, LATER)[1], "not_fetched")
    def test_q4_map(self):
        self.assertEqual(L.p_from_answer("Q4b", [1, .9]), 0.0); self.assertEqual(L.p_from_answer("Q4", [10, .1]), 1.0)
        self.assertAlmostEqual(L.p_from_answer("Q4b", [7, .5]), 6 / 9); self.assertIsNone(L.p_from_answer("Q1", None))
    def test_holder(self):
        e = T0.timestamp(); self.assertEqual(L.holder_outcome([(e - 60, 30.0), (e + 86400 + 100, 27.5)], T0, LATER)[2], 1)
        self.assertEqual(L.holder_outcome([(e - 60, 30.0)], T0, LATER)[1], "no_holder_snapshot")
        self.assertEqual(L.holder_outcome([(e + 900, 30.0), (e + 86400, 20.0)], T0, LATER)[1], "no_holder_snapshot")
class Scoring(unittest.TestCase):
    def test_brier(self):
        self.assertAlmostEqual(S.brier([(1, 1), (0, 0)]), 0); self.assertAlmostEqual(S.brier([(.7, 0), (.2, 1)]), (.49 + .64) / 2)
    def test_bins(self):
        b = S.bins([(i / 100, i % 2) for i in range(100)]); self.assertEqual(len(b), 10); self.assertEqual(sum(x["n"] for x in b), 100)
        self.assertEqual(len(S.bins([(.5, 1)] * 12)), 2); self.assertEqual(S.bins([(1.0, 1)])[0]["n"], 1)

def mk_env(recs, snaps=()):
    d = tempfile.mkdtemp(); os.environ["BRIER1_DATA"] = d
    p = sqlite3.connect(os.path.join(d, "paper_copy.db"))
    p.execute("create table scans(ts,pool,mint,name,dex,liquidity,volume_h24,txns_h1,passed,state)")
    p.execute("create table holder_snapshots(ts_epoch,ts,mint,holders_count,holders_source,top10_pct,top_holder_pct)")
    p.execute("insert into scans values(?,?,?,?,?,?,?,?,?,?)", ("2026-10-30T02:00:00+00:00", "POOL", "M1", "", "", 0, 0, 0, 1, "{}"))
    p.executemany("insert into holder_snapshots values(?,?,?,?,?,?,?)", snaps); p.commit(); p.close()
    open(os.path.join(d, "decisions_copy.jsonl"), "w").write("\n".join(json.dumps(r) for r in recs) + "\n")
    log = os.path.join(d, "live_log.jsonl"); open(log, "w").write(json.dumps({"kind": "scan_mode", "ts": "2026-10-29T00:00:00+00:00"}) + "\n")
    import backfill as B; B.wall_clock = lambda: CLOCK[0]; return d, B, log
def rec(h, ts, **k):
    r = dict(kind="decision", strategy="jev", action="none", rule="x", mint="M1", hash=h, seq=0, ts=ts.isoformat(), state_shown={"price_usd": "1.0"}); r.update(k); return r
class FakeG:
    def __init__(s, res): s.res = res; s.n = 0
    def ohlcv(s, pool, t0): s.n += 1; return s.res(t0) if callable(s.res) else s.res

class EndToEnd(unittest.TestCase):
    def test_backfill_and_score(self):
        recs = [rec("h1", T0, answers={"Q1": .3, "Q2": .6, "Q4b": [7, .5]}),
                rec("h2", T0, action="enter", pool="POOL", jev={"answers": {"Q2": .2}}, state={"price_usd": "1.0"}),
                rec("h3", T0, action="reject", rule="edge", pool="POOL", state={"price_usd": "1.0", "edge_inputs": {"P_exhausted": .4}}),
                rec("h4", T0, action="reject", rule="position_limit", state={"price_usd": "1.0"})]
        d, B, log = mk_env(recs)
        st, meta = B.build(LATER, fetch=True, gecko=FakeG(lambda t0: ok(hourly([1.0] * 26, t0))), live_log=log)
        rows = sqlite3.connect(st).execute("select question_id,status,outcome,is_trade,first_for_mint from outcomes order by id").fetchall()
        self.assertIn(("Q2", "resolved", 1, 0, 1), rows); self.assertIn(("Q2", "resolved", 1, 1, 0), rows); self.assertEqual(len(rows), 5)
        q2 = S.score(st)["questions"]["Q2|Q2_v0"]; self.assertEqual(q2["all_answers"]["n"], 3); self.assertEqual(q2["first_for_mint"]["n"], 1)
        st2, _ = B.build(T0 + dt.timedelta(hours=2), fetch=True, gecko=FakeG(ok([])), live_log=log)
        self.assertEqual(sqlite3.connect(st2).execute("select count(*) from outcomes where status='resolved'").fetchone()[0], 0)

# ---------------- fix 1: append-only threshold log ----------------
class Thresholds(unittest.TestCase):
    def setUp(s):
        s.d = tempfile.mkdtemp(); s.db = T.open_db(os.path.join(s.d, "threshold_changes.db")); s.ev = os.path.join(s.d, "threshold_events.jsonl")
        f = os.path.join(s.d, "thresholds.py"); open(f, "w").write("X=1\n"); s.f = f
        s.kw = dict(sha_before="a", thresholds_path_after=f, metric="brier_Q2_first_for_mint", metric_fixed_ts="2026-11-01T00:00:00+00:00",
                    window_start="2026-11-02T00:00:00+00:00", window_end="2026-11-09T00:00:00+00:00", n_trades=5, approved_by="069", notes="r2 test")
    def test_verdicts_and_sha(s):
        s.assertEqual(T.log_change(s.db, s.ev, n_scored=40, metric_before=.25, metric_after=.20, **s.kw), "keep")
        s.assertEqual(T.log_change(s.db, s.ev, n_scored=10, metric_before=.25, metric_after=.20, **s.kw), "discard")
        s.assertEqual(s.db.execute("select thresholds_sha_after from threshold_changes").fetchone()[0], T.sha256_file(s.f))
        kw = dict(s.kw, metric_fixed_ts="2026-11-03T00:00:00+00:00")
        with s.assertRaises(ValueError): T.log_change(s.db, s.ev, n_scored=40, metric_before=.25, metric_after=.2, **kw)
    def test_no_update_no_delete(s):
        T.log_change(s.db, s.ev, n_scored=40, metric_before=.25, metric_after=.2, **s.kw)
        with s.assertRaises(sqlite3.DatabaseError): s.db.execute("update threshold_changes set verdict='discard'")
        with s.assertRaises(sqlite3.DatabaseError): s.db.execute("delete from threshold_changes")
        s.assertEqual(s.db.execute("select count(*) from threshold_changes").fetchone()[0], 1)
    def test_approved_by_notes_required(s):
        for k in ("approved_by", "notes"):
            with s.assertRaises(ValueError): T.log_change(s.db, s.ev, n_scored=40, metric_before=.25, metric_after=.2, **dict(s.kw, **{k: " "}))
        with s.assertRaises(sqlite3.IntegrityError):
            s.db.execute("insert into threshold_changes(ts,thresholds_sha_before,thresholds_sha_after,metric,metric_fixed_ts,window_start,window_end,n_trades,n_scored,verdict,approved_by,notes,event_hash) values('t','a','b','m','1','2','3',0,0,'keep',NULL,'x','h')")
    def test_event_emitted_and_chained(s):
        T.log_change(s.db, s.ev, n_scored=40, metric_before=.25, metric_after=.2, **s.kw)
        T.log_change(s.db, s.ev, n_scored=40, metric_before=.25, metric_after=.3, **s.kw)
        evs = [json.loads(l) for l in open(s.ev)]; s.assertEqual(len(evs), 2); s.assertEqual(evs[1]["prev_hash"], evs[0]["hash"])
        s.assertEqual([r[0] for r in s.db.execute("select event_hash from threshold_changes order by id")], [e["hash"] for e in evs])
    def test_build_never_deletes_threshold_db(s):
        d, B, log = mk_env([rec("h1", T0, answers={"Q2": .5})])
        db = T.open_db(os.path.join(d, "threshold_changes.db")); T.log_change(db, os.path.join(d, "threshold_events.jsonl"), n_scored=40, metric_before=.25, metric_after=.2, **s.kw); db.close()
        B.build(LATER, fetch=False); B.build(LATER, fetch=False)
        s.assertEqual(sqlite3.connect(os.path.join(d, "threshold_changes.db")).execute("select count(*) from threshold_changes").fetchone()[0], 1)

# ---------------- fix 2: prior-only climatology ----------------
class Climatology(unittest.TestCase):
    def test_prior_only(s):
        r = lambda d, o, p=.5: dict(decision_ts=d.isoformat(), resolve_ts=(d + L.H24).isoformat(), p_jev=p, outcome=o)
        rows = [r(T0, 1), r(T0 + dt.timedelta(hours=1), 0), r(T0 + dt.timedelta(hours=30), 0)]
        m, c = S.prior_climatology(rows)
        s.assertEqual(len(c), 1)                     # only the 3rd row has prior resolved outcomes
        s.assertEqual(c[0], (0.5, 0))                 # mean of rows 1-2 outcomes; row 3's own outcome not used
        rows.append(r(T0 + dt.timedelta(hours=10), 1))  # a later-resolving row must not leak into row 3 (resolve 34h > 30h)
        s.assertEqual(S.prior_climatology(rows)[1][0], (0.5, 0))

# ---------------- fix 3: dead/illiquid + adverse ----------------
class DeadTokens(unittest.TestCase):
    def test_gap_resolves_no_rise(s):
        st, why, o, died, ev = L.price_outcome("Q2", 1.0, ok(hourly([1.0, 0.5, 0.1], T0)), T0, LATER)
        s.assertEqual((st, o, died), ("resolved", 1, 1)); s.assertEqual(ev["missing_hours_as_no_trade"], 21)
        s.assertEqual(L.price_outcome("Q4b", 1.0, ok([]), T0, LATER)[:3], ("resolved", None, 0))
    def test_non200_or_invalid_pool_unresolved(s):
        s.assertEqual(L.price_outcome("Q2", 1.0, {"http": 404, "pool_valid": False, "candles": []}, T0, LATER)[1], "fetch_failed")
        s.assertEqual(L.price_outcome("Q2", 1.0, {"http": 200, "pool_valid": False, "candles": []}, T0, LATER)[1], "fetch_failed")
    def test_adverse_imputation(s):
        g = [dict(mint=f"m{i}", status="resolved", p_jev=.5, outcome=1, unresolved_reason=None, decision_ts=T0.isoformat(), resolve_ts=(T0 + L.H24).isoformat()) for i in range(30)]
        g += [dict(mint="x", status="unresolved", p_jev=.9, outcome=None, unresolved_reason="fetch_failed", decision_ts=T0.isoformat(), resolve_ts=T0.isoformat()),
              dict(mint="y", status="unresolved", p_jev=.1, outcome=None, unresolved_reason="pending_horizon", decision_ts=T0.isoformat(), resolve_ts=T0.isoformat())]
        v = S.view(g, lambda r: True)
        s.assertEqual(v["n_adverse_imputed"], 1); s.assertAlmostEqual(v["brier_adverse"], round((30 * .25 + .81) / 31, 4)); s.assertEqual(v["brier"], .25)

# ---------------- fix 4: complete candles only ----------------
class Window(unittest.TestCase):
    def test_edge_trim(s):
        c = hourly([1.0] * 26, T0h)  # starts 03:00, decision 03:30
        w = L.window(c, T0h); s.assertEqual(len(w), 23)
        s.assertTrue(all(e >= T0h.timestamp() and e + 3600 <= (T0h + L.H24).timestamp() for e, _ in w))
        s.assertEqual(len(L.window(hourly([1.0] * 26, T0), T0)), 24)  # on-the-hour decision: 24 full candles
    def test_last_partial_candle_excluded(s):  # candle 02:00 next day ends 03:00 = t0+24h -> included; 03:00 one excluded
        c = hourly([1.0] * 23 + [1.2] + [5.0], T0); s.assertEqual(L.price_outcome("Q2", 1.0, ok(c), T0, LATER)[2], 0)
        c = hourly([1.0] * 24 + [5.0], T0); s.assertEqual(L.price_outcome("Q2", 1.0, ok(c), T0, LATER)[2], 1)
    def test_uses_high(s):
        e = int(T0.timestamp()); s.assertEqual(L.price_outcome("Q2", 1.0, ok([(e, 1.16)]), T0, LATER)[2], 0)

# ---------------- fix 5: fetch discipline ----------------
class Resp(io.BytesIO):
    status = 200
    def __enter__(s): return s
    def __exit__(s, *a): return False
class Fetch(unittest.TestCase):
    def mk(s, script):
        s.t = [1000.0]; s.slept = []
        def sleep(x): s.slept.append(x); s.t[0] += x
        def opener(req, timeout=None):
            act = script.pop(0)
            if isinstance(act, Exception): raise act
            return Resp(json.dumps(act).encode())
        import backfill as B
        return B, B.Gecko(tempfile.mkdtemp(), opener=opener, clock=lambda: s.t[0], sleep=sleep)
    def test_rate_4_per_min(s):
        body = {"data": {"attributes": {"ohlcv_list": []}}}
        B, g = s.mk([body] * 5)
        for i in range(5): g.ohlcv(f"P{i}", T0)
        gaps = [b - a for a, b in zip(g.calls, g.calls[1:])]; s.assertTrue(all(x >= 15.0 for x in gaps))
        s.assertLessEqual(sum(1 for c in g.calls if c - g.calls[0] < 60), 4)
    def test_abort_on_first_429_and_retry_after(s):
        e = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "120"}, None)
        recs = [rec(f"h{i}", T0, pool="POOL", answers={"Q2": .5}) for i in range(3)]
        d, B, log = mk_env(recs); CLOCK[0] = LATER
        B2, g = s.mk([e, {"data": {"attributes": {"ohlcv_list": []}}}])
        st, meta = B.build(LATER, fetch=True, gecko=g, live_log=log)
        s.assertEqual(len(g.calls), 1); s.assertEqual(meta["fetch_aborted"]["retry_after"], "120")
        s.assertEqual({r[0] for r in sqlite3.connect(st).execute("select unresolved_reason from outcomes")}, {"fetch_aborted"})
        nb = B.retry_not_before(d); s.assertEqual(nb, LATER + dt.timedelta(seconds=120))
        fg = FakeG(ok([])); CLOCK[0] = LATER + dt.timedelta(seconds=60); B.build(LATER, fetch=True, gecko=fg, live_log=log); s.assertEqual(fg.n, 0)
        CLOCK[0] = LATER + dt.timedelta(seconds=121); B.build(LATER, fetch=True, gecko=fg, live_log=log); s.assertGreater(fg.n, 0); CLOCK[0] = LATER
    def test_skip_when_collector_in_backoff(s):
        d, B, log = mk_env([rec("h1", T0, pool="POOL", answers={"Q2": .5})])
        with open(log, "a") as f: f.write(json.dumps({"kind": "gecko_backoff", "seconds": 15.0, "ts": (LATER - dt.timedelta(minutes=5)).isoformat()}) + "\n")
        CLOCK[0] = LATER; fg = FakeG(ok([])); st, meta = B.build(LATER, fetch=True, gecko=fg, live_log=log)
        s.assertEqual(fg.n, 0); s.assertTrue(meta["backoff_check"]["busy"])
        s.assertFalse(B.collector_in_backoff(log, LATER + dt.timedelta(hours=2))[0])

# ---------------- fix 6: p validation, N<30, bootstrap, unanswered ----------------
class Fix6(unittest.TestCase):
    def test_p_validation(s):
        for q, a in (("Q1", 1.2), ("Q1", -0.1), ("Q2", float("nan")), ("Q2", True), ("Q2", "0.5"), ("Q4b", [11, .5]), ("Q4", [0, .5]), ("Q4b", [])):
            s.assertIsNone(L.p_from_answer(q, a), (q, a))
        with s.assertRaises(ValueError): S.brier([(1.5, 1)])
        d, B, log = mk_env([rec("h1", T0, answers={"Q2": 1.7})])
        st, _ = B.build(LATER, fetch=False); s.assertEqual(sqlite3.connect(st).execute("select unresolved_reason, p_jev from outcomes").fetchone(), ("invalid_p", None))
    def test_suppress_below_30(s):
        mk = lambda n: [dict(mint=f"m{i}", status="resolved", p_jev=.3, outcome=i % 2, unresolved_reason=None, decision_ts=T0.isoformat(), resolve_ts=T0.isoformat()) for i in range(n)]
        v = S.view(mk(29), lambda r: True); s.assertFalse(v["publishable"]); s.assertIsNone(v["brier"]); s.assertEqual(v["reliability"], []); s.assertIsNone(v["brier_ci95"])
        v = S.view(mk(30), lambda r: True); s.assertTrue(v["publishable"]); s.assertIsNotNone(v["brier"])
    def test_bootstrap(s):
        rows = [(f"m{i % 10}", .3, i % 2) for i in range(60)]
        ci = S.bootstrap_ci(rows); b = S.brier([(p, o) for _, p, o in rows])
        s.assertEqual(ci, S.bootstrap_ci(rows)); s.assertLessEqual(ci[0], b); s.assertGreaterEqual(ci[1], b)
        s.assertIsNone(S.bootstrap_ci([("m", .3, 1)] * 5))
    def test_unanswered_documented(s):
        recs = [rec("a", T0, action="reject", rule="position_limit"), rec("b", T0, action="none", rule="jev_unavailable"),
                rec("c", T0, action="reject", rule="cycle_entry_limit"), rec("d", T0, answers={"Q2": .4})]
        d, B, log = mk_env(recs); st, _ = B.build(LATER, fetch=False)
        u = S.score(st)["unanswered_decisions"]; s.assertEqual(u["total"], 3)
        s.assertIn("BEFORE Jev", next(x["why"] for x in u["by_rule"] if x["rule"] == "position_limit"))

# ================= r3 =================
import multiprocessing as MP
def _kw(f): return dict(sha_before="a", thresholds_path_after=f, metric="brier_Q2_first_for_mint", metric_fixed_ts="2026-11-01T00:00:00+00:00",
                         window_start="2026-11-02T00:00:00+00:00", window_end="2026-11-09T00:00:00+00:00", n_trades=5, approved_by="069",
                         notes="r3 test", n_scored=40, metric_before=.25, metric_after=.2)
def _worker(dbp, ev, f, n, start):
    start.wait(); db = T.open_db(dbp)
    for _ in range(n): T.log_change(db, ev, **_kw(f))
class R3Base(unittest.TestCase):
    def setUp(s):
        s.d = tempfile.mkdtemp(); s.dbp = os.path.join(s.d, "tc.db"); s.ev = os.path.join(s.d, "ev.jsonl")
        s.f = os.path.join(s.d, "thresholds.py")
        with open(s.f, "w") as fh: fh.write("X=1\n")
        s.db = T.open_db(s.dbp)
        s.boot = T.anchor(s.dbp, s.ev)   # r3.2: bootstrap anchor on the empty log (verify now requires anchors)
    def add(s, n=1):
        for _ in range(n): T.log_change(s.db, s.ev, **_kw(s.f))
    def kinds(s): return sorted(p["kind"] for p in T.verify(s.dbp, s.ev)["problems"])
class R3Atomic(R3Base):
    def test_commit_first_then_event(s):
        s.add(); s.assertEqual(s.db.execute("select count(*) from threshold_changes").fetchone()[0], 1); s.assertTrue(T.verify(s.dbp, s.ev)["ok"])
    def test_crash_between_commit_and_append(s):
        s.add()
        with s.assertRaises(SystemExit): T.log_change(s.db, s.ev, _crash_after_commit=True, **_kw(s.f))
        r = T.verify(s.dbp, s.ev); s.assertEqual((r["n_rows"], r["n_events"]), (2, 1))
        s.assertEqual(s.kinds(), ["row_without_event"])    # the ONLY possible failure state
        with open(s.ev) as fh: s.assertEqual(len(fh.readlines()), 1)
    def test_crash_in_subprocess(s):  # real process death after commit (os._exit, no cleanup)
        def child():
            db = T.open_db(s.dbp); orig = T._append; T._append = lambda *a: os._exit(9); T.log_change(db, s.ev, **_kw(s.f))
        p = MP.get_context("fork").Process(target=child); p.start(); p.join(); s.assertEqual(p.exitcode, 9)
        s.assertEqual(s.kinds(), ["row_without_event"])
    def test_lock_released_after_crash(s):
        with s.assertRaises(SystemExit): T.log_change(s.db, s.ev, _crash_after_commit=True, **_kw(s.f))
        s.add(); s.assertEqual(T.verify(s.dbp, s.ev)["n_events"], 1)
class R3Verify(R3Base):
    def test_clean(s): s.add(3); r = T.verify(s.dbp, s.ev); s.assertTrue(r["ok"]); s.assertEqual((r["n_rows"], r["n_events"]), (3, 3))
    def test_event_without_row_1row_2events(s):  # auditor /tmp/aud21 reproduction
        s.add(1)
        with open(s.ev) as fh: e = json.loads(fh.readline())
        e2 = dict(e, prev_hash=e["hash"], ts="2026-10-30T20:47:22.507274+00:00"); e2["hash"] = T.ev_hash(e2)
        with open(s.ev, "a") as fh: fh.write(json.dumps(e2, sort_keys=True) + "\n")
        r = T.verify(s.dbp, s.ev); s.assertEqual((r["n_rows"], r["n_events"]), (1, 2)); s.assertEqual(s.kinds(), ["event_without_row"])
    def test_duplicate_event(s):
        s.add(1)
        with open(s.ev) as fh: l = fh.readline()
        with open(s.ev, "a") as fh: fh.write(l)
        s.assertIn("duplicate_event", s.kinds()); s.assertIn("chain_break", s.kinds())
    def test_truncated_line(s):
        s.add(2)
        with open(s.ev, "rb") as fh: raw = fh.read()
        with open(s.ev, "wb") as fh: fh.write(raw[:-40])
        k = s.kinds(); s.assertIn("truncated_last_line", k); s.assertIn("corrupt_line", k); s.assertIn("row_without_event", k)
    def test_corrupt_line_and_bad_hash(s):
        s.add(2)
        with open(s.ev) as fh: ls = fh.readlines()
        e = json.loads(ls[1]); e["verdict"] = "discard"; ls[1] = json.dumps(e, sort_keys=True) + "\n"; ls.insert(1, "{not json\n")
        with open(s.ev, "w") as fh: fh.writelines(ls)
        k = s.kinds(); s.assertIn("corrupt_line", k); s.assertIn("bad_hash", k); s.assertIn("row_event_mismatch", k)
    def test_chain_break(s):
        s.add(2)
        with open(s.ev) as fh: ls = fh.readlines()
        e = json.loads(ls[1]); e["prev_hash"] = "f" * 64; e["hash"] = T.ev_hash(e); ls[1] = json.dumps(e, sort_keys=True) + "\n"
        with open(s.ev, "w") as fh: fh.writelines(ls)
        s.assertIn("chain_break", s.kinds())
    def test_append_refuses_on_corrupt_log(s):
        s.add(1)
        with open(s.ev, "a") as fh: fh.write("garbage\n")
        with s.assertRaises(RuntimeError): s.add(1)
        s.assertEqual(s.db.execute("select count(*) from threshold_changes").fetchone()[0], 1)  # refused before commit
class R3Concurrency(R3Base):
    def test_concurrent_processes_no_fork(s):
        ctx = MP.get_context("fork"); start = ctx.Event()
        ps = [ctx.Process(target=_worker, args=(s.dbp, s.ev, s.f, 5, start)) for _ in range(6)]
        for p in ps: p.start()
        start.set()
        for p in ps: p.join(60); s.assertEqual(p.exitcode, 0)
        r = T.verify(s.dbp, s.ev); s.assertTrue(r["ok"], r["problems"]); s.assertEqual((r["n_rows"], r["n_events"]), (30, 30))
        with open(s.ev) as fh: prevs = [json.loads(l)["prev_hash"] for l in fh]
        s.assertEqual(len(set(prevs)), 30)   # every prev_hash used once -> no fork
class R3Backoff(Fetch):
    def test_backoff_checked_before_each_request(s):
        recs = [rec(f"h{i}", T0 + dt.timedelta(minutes=i), pool=f"P{i}", answers={"Q2": .5}) for i in range(3)]
        d, B, log = mk_env(recs); CLOCK[0] = LATER
        body = {"data": {"attributes": {"ohlcv_list": []}}}; script = [body, body, body]
        B2, g = s.mk(script)
        orig = g.opener
        def opener(req, timeout=None):   # collector hits a 429 right after our first request
            with open(log, "a") as fh: fh.write(json.dumps({"kind": "gecko_backoff", "seconds": 15.0, "ts": CLOCK[0].isoformat()}) + "\n")
            return orig(req, timeout)
        g.opener = opener; g.guard = None
        st, meta = B.build(LATER, fetch=True, gecko=g, live_log=log)
        s.assertEqual(len(g.calls), 1); s.assertIn("collector_backoff_ts", meta["fetch_aborted"])
        s.assertFalse(os.path.exists(os.path.join(d, "gecko_not_before.json")))
    def test_backoff_uses_real_clock_not_now(s):
        d, B, log = mk_env([rec("h1", T0, pool="POOL", answers={"Q2": .5})])
        real = LATER + dt.timedelta(days=3); CLOCK[0] = real
        with open(log, "a") as fh: fh.write(json.dumps({"kind": "gecko_backoff", "seconds": 15.0, "ts": (real - dt.timedelta(minutes=2)).isoformat()}) + "\n")
        fg = FakeG(ok([])); st, meta = B.build(LATER, fetch=True, gecko=fg, live_log=log)   # --now = LATER (3 days earlier)
        s.assertTrue(meta["backoff_check"]["busy"]); s.assertEqual(fg.n, 0)
        CLOCK[0] = real + dt.timedelta(hours=1); fg2 = FakeG(ok([]))
        st, meta = B.build(real + dt.timedelta(hours=1), fetch=True, gecko=fg2, live_log=log); s.assertEqual(fg2.n, 1); CLOCK[0] = LATER

# ================= r3.1 =================
import subprocess
class R31Anchor(R3Base):
    def test_anchor_clean(s):
        s.add(3); a = T.anchor(s.dbp, s.ev); s.assertEqual(a["n_events"], 3)
        s.assertTrue(T.verify(s.dbp, s.ev, anchor=a)["ok"]); s.add(1); s.assertTrue(T.verify(s.dbp, s.ev, anchor=a)["ok"])  # growth is fine
    def test_delete_last_row_and_last_event_detected(s):  # auditor /tmp/aud22 adv.py last case: internally consistent
        s.add(3); a = T.anchor(s.dbp, s.ev)
        db = sqlite3.connect(s.dbp); db.execute("drop trigger threshold_changes_no_delete"); db.execute("delete from threshold_changes where id=3"); db.commit()
        with open(s.ev) as fh: ls = fh.readlines()
        with open(s.ev, "w") as fh: fh.writelines(ls[:-1])
        s.assertTrue(T.verify(s.dbp, s.ev, use_anchors=False)["ok"])      # internal consistency alone: undetectable
        s.assertIn("chain_truncated", [p["kind"] for p in T.verify(s.dbp, s.ev)["problems"]])  # r3.2 default: anchors.jsonl catches it
        k = sorted(p["kind"] for p in T.verify(s.dbp, s.ev, anchor=a)["problems"])
        s.assertEqual(k, ["chain_truncated", "rows_truncated"])
    def test_tail_replaced_detected(s):
        s.add(2); a = T.anchor(s.dbp, s.ev)
        db = sqlite3.connect(s.dbp); db.execute("drop trigger threshold_changes_no_delete"); db.execute("delete from threshold_changes where id=2"); db.commit()
        with open(s.ev) as fh: ls = fh.readlines()
        with open(s.ev, "w") as fh: fh.writelines(ls[:-1])
        T.log_change(s.db, s.ev, **dict(_kw(s.f), notes="forged"))           # rewrite the tail with a different event
        s.assertIn("anchor_head_missing", [p["kind"] for p in T.verify(s.dbp, s.ev, anchor=a)["problems"]])
    def test_reorder_detected(s):
        s.add(3); a = T.anchor(s.dbp, s.ev)
        with open(s.ev) as fh: ls = fh.readlines()
        with open(s.ev, "w") as fh: fh.writelines([ls[0], ls[2], ls[1]])
        k = [p["kind"] for p in T.verify(s.dbp, s.ev, anchor=a)["problems"]]; s.assertIn("anchor_position_mismatch", k); s.assertIn("chain_break", k)
    def test_empty_anchor_and_refuse_dirty(s):
        a = T.anchor(s.dbp, s.ev); s.assertEqual((a["n_events"], a["head_hash"]), (0, T.ZERO)); s.assertTrue(T.verify(s.dbp, s.ev, anchor=a)["ok"])
        s.add(1)
        with open(s.ev, "a") as fh: fh.write("junk\n")
        with s.assertRaises(RuntimeError): T.anchor(s.dbp, s.ev)
    def test_score_json_carries_and_checks_anchor(s):   # r3.2 semantics: prior anchor comes from anchors.jsonl
        rc, j = run_score(s, 2); s.assertEqual(rc, 0); tl = j["threshold_log"]
        s.assertTrue(tl["verify_ok"]); s.assertEqual(tl["anchor"]["n_events"], 2); s.assertEqual(tl["anchors_head_hash"], T.anchors_head(T.anchors_default(s.ev)))
        truncate_last(s); rc, j = run_score(s, 0); s.assertEqual(rc, 2); s.assertFalse(j["threshold_log"]["verify_ok"])
        r = subprocess.run([sys.executable, "-B", os.path.join(HERE, "thresholds_log.py"), s.dbp, s.ev], capture_output=True)
        s.assertEqual(r.returncode, 1)
class R31Guard(Fetch):
    def test_guard_rechecked_after_wait(s):
        body = {"data": {"attributes": {"ohlcv_list": []}}}
        B, g = s.mk([body, body]); seq = []
        state = {"busy": False}
        def guard():
            seq.append(("guard", s.t[0]))
            if state["busy"]: raise B.CollectorBackoff("x")
        g.guard = guard; orig_sleep = g.sleep
        def sleep(x): orig_sleep(x); seq.append(("sleep", s.t[0])); state["busy"] = True   # collector backs off DURING our wait
        g.sleep = sleep
        g.ohlcv("P0", T0)
        with s.assertRaises(B.CollectorBackoff): g.ohlcv("P1", T0)
        s.assertEqual(len(g.calls), 1)                                   # second request never sent
        s.assertEqual([k for k, _ in seq], ["guard", "guard", "guard", "sleep", "guard"])
    def test_guard_called_twice_per_request(s):
        body = {"data": {"attributes": {"ohlcv_list": []}}}
        B, g = s.mk([body]); n = []; g.guard = lambda: n.append(1); g.ohlcv("P0", T0); s.assertEqual(len(n), 2)

# ================= r3.2 =================
HERE = os.path.dirname(os.path.abspath(__file__))
def run_score(s, n_add, extra=()):
    if n_add: s.add(n_add)
    if not hasattr(s, "sd"):
        s.sd = tempfile.mkdtemp(); st = sqlite3.connect(os.path.join(s.sd, "st.db"))
        with open(os.path.join(HERE, "schema.sql")) as fh: st.executescript(fh.read())
        st.close()
    out = os.path.join(s.sd, "score.json")
    r = subprocess.run([sys.executable, "-B", os.path.join(HERE, "score.py"), os.path.join(s.sd, "st.db"), out, s.dbp, s.ev, *extra], capture_output=True)
    with open(out) as fh: return r.returncode, json.load(fh)
def truncate_last(s):
    db = sqlite3.connect(s.dbp); db.execute("drop trigger if exists threshold_changes_no_delete")
    db.execute("delete from threshold_changes where id=(select max(id) from threshold_changes)"); db.commit(); db.close()
    with open(s.ev) as fh: ls = fh.readlines()
    with open(s.ev, "w") as fh: fh.writelines(ls[:-1])
def _anchor_worker(dbp, ev, n, start):
    start.wait()
    for _ in range(n): T.anchor(dbp, ev)
class R32Anchors(R3Base):
    def setUp(s): super().setUp(); s.ap = T.anchors_default(s.ev)
    def kinds_default(s, **k): return sorted(p["kind"] for p in T.verify(s.dbp, s.ev, **k)["problems"])
    def test_auditor_attack_truncate_delete_score_regenerate_FAILS(s):   # /tmp/aud23 exact sequence
        rc, j = run_score(s, 3); s.assertEqual(rc, 0); quoted = j["threshold_log"]["anchors_head_hash"]
        truncate_last(s); os.remove(os.path.join(s.sd, "score.json"))
        rc, j = run_score(s, 0); tl = j["threshold_log"]
        s.assertEqual(rc, 2); s.assertFalse(tl["verify_ok"]); s.assertIsNone(tl["anchor"])
        s.assertIn("chain_truncated", [p["kind"] for p in tl["problems"]]); s.assertIn("rows_truncated", [p["kind"] for p in tl["problems"]])
        s.assertEqual(T.anchors_head(s.ap), quoted)          # no new anchor was appended over the broken log
        rc, j = run_score(s, 0); s.assertEqual(rc, 2)          # stays failed on every rerun
    def test_anchors_jsonl_truncated_partial_line(s):
        run_score(s, 2)
        with open(s.ap, "rb") as fh: raw = fh.read()
        with open(s.ap, "wb") as fh: fh.write(raw[:-30])
        s.assertIn("anchors_truncated_last_line", s.kinds_default()); rc, j = run_score(s, 0); s.assertEqual(rc, 2)
        with s.assertRaises(RuntimeError): T.anchor(s.dbp, s.ev)
    def test_anchors_jsonl_truncated_whole_lines_needs_external_quote(s):
        rc, j = run_score(s, 2); quoted = j["threshold_log"]["anchors_head_hash"]
        with open(s.ap) as fh: ls = fh.readlines()
        with open(s.ap, "w") as fh: fh.writelines(ls[:1])    # back to the bootstrap anchor
        truncate_last(s); truncate_last(s)                     # and the log back to empty: internally consistent
        s.assertTrue(T.verify(s.dbp, s.ev)["ok"])              # on-box only: undetectable (documented)
        s.assertIn("anchors_head_not_found", s.kinds_default(expected_anchors_head=quoted))
        rc, j = run_score(s, 0, extra=(s.ap, quoted)); s.assertEqual(rc, 2)
        r = subprocess.run([sys.executable, "-B", os.path.join(HERE, "thresholds_log.py"), s.dbp, s.ev, "--expect-anchors-head", quoted], capture_output=True)
        s.assertEqual(r.returncode, 1)
    def test_anchors_jsonl_deleted_with_events(s):
        run_score(s, 2); os.remove(s.ap)
        s.assertEqual(s.kinds_default(), ["anchor_missing"])
        rc, j = run_score(s, 0); s.assertEqual(rc, 2); s.assertIn("anchor_missing", [p["kind"] for p in j["threshold_log"]["problems"]])
        with s.assertRaises(RuntimeError): T.anchor(s.dbp, s.ev)
        s.assertFalse(os.path.exists(s.ap) and os.path.getsize(s.ap))   # refusal wrote no anchor
    def test_anchors_jsonl_emptied_with_events(s):
        run_score(s, 1); open(s.ap, "w").close()
        rc, j = run_score(s, 0); s.assertEqual(rc, 2)
    def test_anchors_jsonl_garbage_line_refuses(s):
        run_score(s, 1)
        with open(s.ap, "w") as fh: fh.write("junk\n")
        rc, j = run_score(s, 0); s.assertEqual(rc, 2); s.assertIn("anchors_corrupt_line", [p["kind"] for p in j["threshold_log"]["problems"]])
    def test_anchors_chain_tamper(s):
        run_score(s, 1); run_score(s, 1)
        with open(s.ap) as fh: ls = fh.readlines()
        e = json.loads(ls[1]); e["n_events"] = 0; ls[1] = json.dumps(e) + "\n"
        with open(s.ap, "w") as fh: fh.writelines(ls)
        s.assertIn("anchors_bad_hash", s.kinds_default())
        e["hash"] = T.ev_hash(e); ls[1] = json.dumps(e) + "\n"
        with open(s.ap, "w") as fh: fh.writelines(ls)
        s.assertIn("anchors_chain_break", s.kinds_default())
    def test_verify_checks_every_anchor(s):   # truncating below an OLDER anchor is caught even if later anchors are removed
        run_score(s, 2); run_score(s, 2)
        with open(s.ap) as fh: ls = fh.readlines()
        with open(s.ap, "w") as fh: fh.writelines(ls[:2])    # keep bootstrap + anchor@2
        truncate_last(s); truncate_last(s); truncate_last(s)  # log now 1 event
        s.assertIn("chain_truncated", s.kinds_default())
    def test_default_verify_without_anchors_file(s):
        d = tempfile.mkdtemp(); dbp = os.path.join(d, "x.db"); T.open_db(dbp).close()
        s.assertEqual(sorted(p["kind"] for p in T.verify(dbp, os.path.join(d, "e.jsonl"))["problems"]), ["anchor_missing"])
        a = T.anchor(dbp, os.path.join(d, "e.jsonl")); s.assertEqual((a["n_events"], a["prev_hash"]), (0, T.ZERO))   # bootstrap on empty log
        s.assertTrue(T.verify(dbp, os.path.join(d, "e.jsonl"))["ok"])
    def test_anchors_append_fsync_and_concurrent_lock(s):
        s.add(2); ctx = MP.get_context("fork"); start = ctx.Event()
        ps = [ctx.Process(target=_anchor_worker, args=(s.dbp, s.ev, 4, start)) for _ in range(5)]
        for p in ps: p.start()
        start.set()
        for p in ps: p.join(60); s.assertEqual(p.exitcode, 0)
        recs, P, _ = T.read_anchors(s.ap); s.assertEqual(P, []); s.assertEqual(len(recs), 21)
        s.assertEqual(len({r["prev_hash"] for r in recs}), 21)
    def test_score_json_has_head_for_report(s):
        rc, j = run_score(s, 1); tl = j["threshold_log"]
        s.assertEqual(tl["anchors_head_hash"], tl["anchor"]["hash"]); s.assertEqual(tl["prior_anchors_head"], s.boot["hash"])

if __name__ == "__main__": unittest.main(verbosity=1)
