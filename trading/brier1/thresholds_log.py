"""brier1 r3 append-only threshold-change log.
Order (fix 1): under an exclusive fcntl lock, INSERT+COMMIT the row first, then append the event. A crash between the
two steps can only leave a row with no event (verify() reports it as row_without_event); never an event with no row.
fix 3: the lock (<events>.lock, fcntl.LOCK_EX) covers reading prev_hash, the commit and the append, so the chain cannot fork.
fix 2: verify() recomputes the chain and cross-checks rows <-> events both ways."""
import datetime as dt, fcntl, hashlib, json, os, sqlite3
HERE = os.path.dirname(os.path.abspath(__file__)); ZERO = "0" * 64
def sha256_file(p):
    with open(p, "rb") as f: return hashlib.sha256(f.read()).hexdigest()
def open_db(path):
    db = sqlite3.connect(path, timeout=30)
    with open(os.path.join(HERE, "thresholds_schema.sql")) as f: db.executescript(f.read())
    return db
def ev_hash(ev): return hashlib.sha256(json.dumps({k: v for k, v in ev.items() if k != "hash"}, sort_keys=True).encode()).hexdigest()
def _last_hash(events):
    if not os.path.exists(events): return ZERO
    last = ZERO
    with open(events) as f:
        for l in f:
            if l.strip():
                try: last = json.loads(l)["hash"]
                except (ValueError, KeyError): raise RuntimeError("event log corrupt; run verify() before appending")
    return last
class _Lock:
    def __init__(s, events): s.p = events + ".lock"
    def __enter__(s): s.f = open(s.p, "a"); fcntl.flock(s.f, fcntl.LOCK_EX); return s
    def __exit__(s, *a): fcntl.flock(s.f, fcntl.LOCK_UN); s.f.close()
def _append(events_path, ev):
    with open(events_path, "a") as f:
        f.write(json.dumps(ev, sort_keys=True) + "\n"); f.flush(); os.fsync(f.fileno())
def log_change(db, events_path, *, sha_before, thresholds_path_after, metric, metric_fixed_ts, window_start, window_end,
               n_trades, n_scored, metric_before, metric_after, approved_by, notes, lower_is_better=True, min_n=30,
               question_id=None, param=None, old_value=None, new_value=None, _crash_after_commit=False):
    if not (approved_by and str(approved_by).strip()): raise ValueError("approved_by required")
    if not (notes and str(notes).strip()): raise ValueError("notes required")
    if not metric_fixed_ts < window_start: raise ValueError("metric must be fixed before the evaluation window")
    better = None not in (metric_after, metric_before) and ((metric_after < metric_before) if lower_is_better else (metric_after > metric_before))
    verdict = "keep" if (better and n_scored >= min_n) else "discard"
    with _Lock(events_path):
        row = dict(ts=dt.datetime.now(dt.timezone.utc).isoformat(), thresholds_sha_before=sha_before,
                   thresholds_sha_after=sha256_file(thresholds_path_after), question_id=question_id, param=param,
                   old_value=None if old_value is None else str(old_value), new_value=None if new_value is None else str(new_value),
                   metric=metric, metric_fixed_ts=metric_fixed_ts, window_start=window_start, window_end=window_end,
                   metric_before=metric_before, metric_after=metric_after, n_trades=n_trades, n_scored=n_scored,
                   verdict=verdict, approved_by=approved_by, notes=notes)
        ev = dict(kind="threshold_change", prev_hash=_last_hash(events_path), **row); ev["hash"] = ev_hash(ev)
        db.execute(f"insert into threshold_changes({','.join(row)},event_hash) values({','.join('?' * len(row))},?)", (*row.values(), ev["hash"]))
        db.commit()                                   # step 1: row durable
        if _crash_after_commit: raise SystemExit("simulated crash between commit and append")
        _append(events_path, ev)                      # step 2: event
    return verdict
ROW_FIELDS = ("ts","thresholds_sha_before","thresholds_sha_after","question_id","param","old_value","new_value","metric","metric_fixed_ts",
              "window_start","window_end","metric_before","metric_after","n_trades","n_scored","verdict","approved_by","notes")
def anchors_default(events_path): return os.path.join(os.path.dirname(os.path.abspath(events_path)), "anchors.jsonl")
def read_anchors(anchors_path):
    """r3.2: parse + verify the anchors file's own hash chain. Returns (records, problems, exists)."""
    if not os.path.exists(anchors_path): return [], [], False
    P = []; recs = []
    with open(anchors_path, "rb") as f: raw = f.read()
    lines = raw.split(b"\n")
    if raw and not raw.endswith(b"\n"): P.append(dict(kind="anchors_truncated_last_line", line=len(lines)))
    prev = ZERO
    for i, l in enumerate(lines, 1):
        if not l.strip(): continue
        try: e = json.loads(l)
        except ValueError: P.append(dict(kind="anchors_corrupt_line", line=i)); continue
        if not isinstance(e, dict) or not {"hash", "prev_hash", "head_hash", "n_events", "n_rows", "ts"} <= set(e):
            P.append(dict(kind="anchors_corrupt_line", line=i)); continue
        if ev_hash(e) != e["hash"]: P.append(dict(kind="anchors_bad_hash", line=i))
        if e["prev_hash"] != prev: P.append(dict(kind="anchors_chain_break", line=i))
        prev = e["hash"]; recs.append(e)
    return recs, P, True
def anchors_head(anchors_path):
    recs, P, _ = read_anchors(anchors_path); return recs[-1]["hash"] if recs and not P else None
def anchor(db_path, events_path, anchors_path=None):
    """r3.2: verify against ALL prior anchors (or require an empty log when there are none), then append a hash-chained
    anchor {head_hash, n_events, n_rows, ts, prev_hash, hash} to anchors.jsonl under an fcntl lock with fsync."""
    ap = anchors_path or anchors_default(events_path)
    with _Lock(ap):
        recs, AP, exists = read_anchors(ap)
        if recs or AP: r = verify(db_path, events_path, anchors_path=ap)
        else:
            r = verify(db_path, events_path, use_anchors=False)
            if r["n_events"] or r["n_rows"]:
                r["ok"] = False; r["problems"].append(dict(kind="anchor_missing", why="no anchors but the threshold log is not empty"))
        if not r["ok"]: raise RuntimeError(f"refusing to anchor a log that fails verify: {r['problems'][:3]}")
        rec = dict(head_hash=r["head_hash"], n_events=r["n_events"], n_rows=r["n_rows"],
                   ts=dt.datetime.now(dt.timezone.utc).isoformat(), prev_hash=recs[-1]["hash"] if recs else ZERO)
        rec["hash"] = ev_hash(rec); _append(ap, rec)
    return rec
def verify(db_path, events_path, anchor=None, anchors_path=None, use_anchors=True, expected_anchors_head=None):
    """Returns dict(ok, n_rows, n_events, head_hash, anchors_head, problems=[...]). Read-only.
    r3.2 default: checks against EVERY anchor in anchors.jsonl (beside the events file), verifies that file's own chain,
    and reports anchor_missing if there is none. use_anchors=False = internal consistency only (diagnostics/tests).
    expected_anchors_head: a head quoted OUTSIDE the box (daily/auditor report); must be present in anchors.jsonl.
    anchor (r3.1): a previously published dict(head_hash, n_events, n_rows); detects truncation of the END of the chain
    (incl. deleting the last row AND the last event together, which is internally consistent)."""
    P = []; evs = []
    if os.path.exists(events_path):
        with open(events_path, "rb") as f: raw = f.read()
        lines = raw.split(b"\n")
        if raw and not raw.endswith(b"\n"): P.append(dict(kind="truncated_last_line", line=len(lines)))
        for i, l in enumerate(lines, 1):
            if not l.strip(): continue
            try: e = json.loads(l)
            except ValueError: P.append(dict(kind="corrupt_line", line=i)); continue
            if not isinstance(e, dict) or "hash" not in e or "prev_hash" not in e: P.append(dict(kind="corrupt_line", line=i)); continue
            evs.append((i, e))
    prev = ZERO
    for i, e in evs:
        if ev_hash(e) != e["hash"]: P.append(dict(kind="bad_hash", line=i))
        if e["prev_hash"] != prev: P.append(dict(kind="chain_break", line=i, expected_prev=prev, got=e["prev_hash"]))
        prev = e["hash"]
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True); db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("select * from threshold_changes order by id")]; db.close()
    ecount = {}; rcount = {}
    for _, e in evs: ecount[e["hash"]] = ecount.get(e["hash"], 0) + 1
    for r in rows: rcount[r["event_hash"]] = rcount.get(r["event_hash"], 0) + 1
    byh = {e["hash"]: e for _, e in evs}
    for h, n in ecount.items():
        if n > 1: P.append(dict(kind="duplicate_event", event_hash=h, n=n))
    for h, n in rcount.items():
        if n > 1: P.append(dict(kind="duplicate_row_event_hash", event_hash=h, n=n))
    for r in rows:
        e = byh.get(r["event_hash"])
        if e is None: P.append(dict(kind="row_without_event", row_id=r["id"], event_hash=r["event_hash"]))
        elif any(e.get(k) != r[k] for k in ROW_FIELDS): P.append(dict(kind="row_event_mismatch", row_id=r["id"]))
    for i, e in evs:
        if e["hash"] not in rcount: P.append(dict(kind="event_without_row", line=i, event_hash=e["hash"]))
    head = evs[-1][1]["hash"] if evs else ZERO
    anchors = [anchor] if anchor else []; ahead = None
    if use_anchors and not anchor:
        ap = anchors_path or anchors_default(events_path)
        recs, AP, exists = read_anchors(ap); P.extend(AP); anchors = recs; ahead = recs[-1]["hash"] if recs else None
        if not recs: P.append(dict(kind="anchor_missing", anchors_path=ap, exists=exists))
        if expected_anchors_head and expected_anchors_head not in {x["hash"] for x in recs}:
            P.append(dict(kind="anchors_head_not_found", expected=expected_anchors_head))
        elif expected_anchors_head and recs and recs[-1]["hash"] != expected_anchors_head:
            pass   # anchors grew after the quote: fine
    for anchor in anchors:
        chain = [e["hash"] for _, e in evs]; k = int(anchor.get("n_events") or 0); h = anchor.get("head_hash")
        if k == 0:
            if h != ZERO: P.append(dict(kind="anchor_invalid", anchor=anchor))
        elif len(chain) < k: P.append(dict(kind="chain_truncated", anchor_n_events=k, n_events=len(chain)))
        elif chain[k - 1] != h:
            P.append(dict(kind="anchor_position_mismatch" if h in chain else "anchor_head_missing", anchor_n_events=k, anchor_head=h))
        if len(rows) < int(anchor.get("n_rows") or 0): P.append(dict(kind="rows_truncated", anchor_n_rows=anchor["n_rows"], n_rows=len(rows)))
    return dict(ok=not P, n_rows=len(rows), n_events=len(evs), head_hash=head, anchors_head=ahead, problems=P)
def load_anchor(path):
    """Accepts a bare anchor JSON or a score JSON carrying it under threshold_log.anchor."""
    with open(path) as f: j = json.load(f)
    if not isinstance(j, dict): return None
    return (j.get("threshold_log") or {}).get("anchor") if "threshold_log" in j else j
if __name__ == "__main__":
    import argparse   # python3 -B thresholds_log.py <db> <events> [--anchors P] [--expect-anchors-head H] [--anchor-json F]
    ap = argparse.ArgumentParser(); ap.add_argument("db"); ap.add_argument("events"); ap.add_argument("--anchors")
    ap.add_argument("--expect-anchors-head"); ap.add_argument("--anchor-json"); a = ap.parse_args()
    r = verify(a.db, a.events, anchor=load_anchor(a.anchor_json) if a.anchor_json else None, anchors_path=a.anchors,
               expected_anchors_head=a.expect_anchors_head)
    print(json.dumps(r, indent=1)); raise SystemExit(0 if r["ok"] else 1)
