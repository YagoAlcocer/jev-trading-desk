"""brier1 r2 scorer: code only. python3 -B score.py data/staging.db out.json
fix 2: climatology for row i uses only outcomes with resolve_ts < decision_ts(i) (same question version + view).
fix 3: brier_adverse = Brier over resolved rows + non-pending unresolved rows imputed adversely (o = 0 if p >= 0.5 else 1).
fix 6: p must be in [0,1]; nothing is published (brier/bins/CI = null) when N < MIN_PUBLISH_N; 95% cluster bootstrap CI by mint."""
import datetime as dt, json, math, random, sqlite3, sys
MIN_PUBLISH_N = 30; BOOT = 2000; SEED = 20261030
NON_IMPUTABLE = {"pending_horizon"}   # not yet knowable -> never imputed

def _check(rows):
    for p, o in rows:
        if not (isinstance(p, (int, float)) and math.isfinite(p) and 0 <= p <= 1): raise ValueError(f"p out of [0,1]: {p}")
        if o not in (0, 1): raise ValueError(f"outcome not 0/1: {o}")
def brier(rows):
    _check(rows); return sum((p - o) ** 2 for p, o in rows) / len(rows) if rows else None
def bins(rows, k=None):
    n = len(rows)
    if not n: return []
    k = k or (10 if n >= 100 else 5 if n >= 30 else 2 if n >= 10 else 1)
    out = []
    for i in range(k):
        lo, hi = i / k, (i + 1) / k
        b = [(p, o) for p, o in rows if lo <= p < hi or (i == k - 1 and p == 1.0)]
        out.append(dict(lo=round(lo, 3), hi=round(hi, 3), n=len(b),
                        mean_p=round(sum(p for p, _ in b) / len(b), 4) if b else None,
                        obs_rate=round(sum(o for _, o in b) / len(b), 4) if b else None))
    return out
def bootstrap_ci(rows_with_mint, B=BOOT, seed=SEED):
    """rows: (mint, p, o). Resample mints with replacement (answers per mint are correlated)."""
    by = {}
    for m, p, o in rows_with_mint: by.setdefault(m, []).append((p, o))
    keys = sorted(by)
    if len(keys) < 2: return None
    rng = random.Random(seed); stats = []
    for _ in range(B):
        s = [x for k in (rng.choice(keys) for _ in keys) for x in by[k]]
        stats.append(sum((p - o) ** 2 for p, o in s) / len(s))
    stats.sort(); return [round(stats[int(0.025 * B)], 4), round(stats[int(0.975 * B) - 1], 4)]
def prior_climatology(rows):
    """rows: dicts with decision_ts, resolve_ts, p_jev, outcome. Returns (pairs model, pairs clim) on rows that have >=1 prior resolved outcome."""
    t = lambda s: dt.datetime.fromisoformat(s)
    m, c = [], []
    for r in rows:
        prior = [x["outcome"] for x in rows if t(x["resolve_ts"]) < t(r["decision_ts"])]
        if prior: m.append((r["p_jev"], r["outcome"])); c.append((sum(prior) / len(prior), r["outcome"]))
    return m, c

def view(g, f):
    res = [r for r in g if f(r) and r["status"] == "resolved" and r["p_jev"] is not None]
    s = [(r["p_jev"], r["outcome"]) for r in res]; n = len(s)
    imp = [r for r in g if f(r) and r["status"] == "unresolved" and r["p_jev"] is not None and r["unresolved_reason"] not in NON_IMPUTABLE]
    adv = s + [(r["p_jev"], 0 if r["p_jev"] >= 0.5 else 1) for r in imp]
    mc, cc = prior_climatology(res)
    pub = n >= MIN_PUBLISH_N
    return dict(n=n, publishable=pub, min_publish_n=MIN_PUBLISH_N, n_mints=len({r["mint"] for r in res}),
                brier=round(brier(s), 4) if pub else None,
                brier_ci95=bootstrap_ci([(r["mint"], r["p_jev"], r["outcome"]) for r in res]) if pub else None,
                base_rate=round(sum(o for _, o in s) / n, 4) if pub else None,
                n_clim=len(cc), brier_model_on_clim_rows=round(brier(mc), 4) if pub and cc else None,
                brier_climatology_prior_only=round(brier(cc), 4) if pub and cc else None,
                n_adverse_imputed=len(imp), brier_adverse=round(brier(adv), 4) if len(adv) >= MIN_PUBLISH_N else None,
                reliability=bins(s) if pub else [])

def score(db_path):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True); db.row_factory = sqlite3.Row
    R = [dict(r) for r in db.execute("select * from outcomes")]
    un = [dict(r) for r in db.execute("select rule, why, count(*) n from unanswered_decisions group by rule, why order by n desc")]
    res = {}
    for qv in sorted({(r["question_id"], r["question_version"], r["outcome_def"], r["def_status"]) for r in R}):
        g = [r for r in R if (r["question_id"], r["question_version"]) == qv[:2]]
        ur = {}
        for r in g:
            if r["status"] != "resolved": ur[r["unresolved_reason"]] = ur.get(r["unresolved_reason"], 0) + 1
        q = dict(question_id=qv[0], question_version=qv[1], outcome_def=qv[2], def_status=qv[3], n_answers=len(g),
                 n_mints=len({r["mint"] for r in g}), unresolved_by_reason=ur, died_suspected=sum(r["died_suspected"] for r in g))
        for name, f in (("first_for_mint", lambda r: r["first_for_mint"]), ("all_answers", lambda r: True),
                        ("trades", lambda r: r["is_trade"]), ("rejections", lambda r: not r["is_trade"])):
            q[name] = view(g, f)
        res[f"{qv[0]}|{qv[1]}"] = q
    return dict(generated_at=dt.datetime.now(dt.timezone.utc).isoformat(), primary_view="first_for_mint",
                note="Brier over resolved rows only; brier_adverse adds non-pending unresolved rows imputed against Jev. Nothing published when N<30.",
                unanswered_decisions=dict(total=sum(x["n"] for x in un), by_rule=un), questions=res)

def dashboard_field(s):
    """Proposed dashboard_data.py field: out['calibration'] = dashboard_field(score(...)); null metrics until N>=30."""
    out = {}
    for k, v in s["questions"].items():
        f = v["first_for_mint"]
        out[k] = dict(n=f["n"], publishable=f["publishable"], brier=f["brier"], brier_ci95=f["brier_ci95"], base_rate=f["base_rate"],
                      brier_climatology_prior_only=f["brier_climatology_prior_only"], brier_adverse=f["brier_adverse"],
                      n_pending=v["unresolved_by_reason"].get("pending_horizon", 0), n_unresolved=sum(v["unresolved_by_reason"].values()),
                      def_status=v["def_status"], bins=[[b["mean_p"], b["obs_rate"], b["n"]] for b in f["reliability"]])
    return out

def threshold_log_section(db_path, events_path, anchors_path=None, expected_anchors_head=None):
    """r3.2: prior anchors come ONLY from the append-only anchors.jsonl (never from a previous score JSON).
    Refuses (verify_ok False, no new anchor) if: the log fails verify against any anchor; anchors.jsonl is damaged;
    anchors.jsonl has lines but none usable; or anchors.jsonl is missing/empty while the threshold log is not empty.
    Bootstrap (first anchor) only when there are no anchors AND the threshold log has no rows and no events."""
    import thresholds_log as TL
    ap = anchors_path or TL.anchors_default(events_path)
    recs, AP, exists = TL.read_anchors(ap)
    raw_lines = 0
    if exists:
        with open(ap, "rb") as f: raw_lines = sum(1 for l in f.read().split(b"\n") if l.strip())
    if raw_lines or recs or AP:
        r = TL.verify(db_path, events_path, anchors_path=ap, expected_anchors_head=expected_anchors_head); mode = "verified_against_anchors"
    else:
        r = TL.verify(db_path, events_path, use_anchors=False); mode = "bootstrap"
        if r["n_events"] or r["n_rows"]:
            r["ok"] = False; r["problems"].append(dict(kind="anchor_missing", why="anchors.jsonl missing/empty but threshold log has events/rows"))
        if expected_anchors_head:
            r["ok"] = False; r["problems"].append(dict(kind="anchors_head_not_found", expected=expected_anchors_head))
    out = dict(verify_ok=r["ok"], mode=mode, problems=r["problems"], n_rows=r["n_rows"], n_events=r["n_events"],
               prior_anchors=len(recs), prior_anchors_head=recs[-1]["hash"] if recs else None, anchors_path=ap)
    if r["ok"]:
        rec = TL.anchor(db_path, events_path, anchors_path=ap)
        out.update(anchor=rec, anchors_head_hash=rec["hash"])   # quote anchors_head_hash in the daily report
    else:
        out.update(anchor=None, anchors_head_hash=None)
    return out
if __name__ == "__main__":
    # python3 -B score.py <staging.db> <out.json> [<threshold_changes.db> <threshold_events.jsonl> [<anchors.jsonl> [<expected_anchors_head>]]]
    # exit 2 = threshold log refused (score JSON still written, with verify_ok false and no new anchor)
    s = score(sys.argv[1]); s["dashboard_calibration_proposal"] = dashboard_field(s); rc = 0
    if len(sys.argv) > 4:
        s["threshold_log"] = threshold_log_section(sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) > 5 else None,
                                                   sys.argv[6] if len(sys.argv) > 6 else None)
        rc = 0 if s["threshold_log"]["verify_ok"] else 2
    t = json.dumps(s, indent=1)
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w") as f: f.write(t)
    else: print(t)
    sys.exit(rc)
