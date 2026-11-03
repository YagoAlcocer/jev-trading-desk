"""Append-only, hash-chained decision log (risk_rules.md section 7). One JSON object per line.
Each record: seq, ts, prev_hash, hash = sha256(prev_hash + canonical JSON body). Outcomes are NEW records
referencing the decision's id; nothing is ever edited."""
import json, hashlib, os, fcntl, datetime as dt
PATH = "/workspace/trading/decisions.log.jsonl"

def _h(prev, body): return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()

def _mask(s):
    k = os.environ.get("HELIUS_API_KEY")
    return s.replace(k, "***") if k else s

class HashlogCorrupt(RuntimeError):
    """Last line of the log is invalid; record quarantined, HASHLOG_CORRUPT alert written. Must stop the loop."""

def _paths():
    d = os.path.dirname(PATH)
    return PATH + ".lock", os.path.join(d, "decisions.quarantine.jsonl"), os.path.join(d, "HASHLOG_CORRUPT")

def _last_line():
    """combo1 r2: (last line as str or None, seq for the next record) without re-reading the whole file: seek to EOF and
    read backwards in 64 KiB blocks until the previous newline. A trailing newline is ignored; a partial last line (no
    newline) is returned as the last line, exactly like the old splitlines()[-1], so a torn write still hits the corrupt path.
    seq = last record's seq + 1 (verify() requires seq == line index, so this equals the old len(lines))."""
    if not os.path.exists(PATH): return None, 0
    with open(PATH, "rb") as f:
        end = f.seek(0, 2)
        if end == 0: return None, 0
        f.seek(end - 1); stop = end - 1 if f.read(1) == b"\n" else end
        pos, buf = stop, b""
        while pos > 0 and b"\n" not in buf:
            n = min(65536, pos); pos -= n; f.seek(pos); buf = f.read(n) + buf
        buf = buf[:stop - pos]
    raw = buf[buf.rfind(b"\n") + 1:].decode(errors="replace")
    try: s = json.loads(raw).get("seq")
    except (ValueError, AttributeError): s = None
    return raw, (s + 1 if isinstance(s, int) and not isinstance(s, bool) else None)

def append(kind, **fields):
    lock, quar, alert = _paths()
    with open(lock, "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            last_line, seq = _last_line()   # combo1 r2: O(1) tail read instead of re-reading the whole file
            body = json.loads(_mask(json.dumps({"seq": seq, "ts": dt.datetime.now(dt.timezone.utc).isoformat(), "kind": kind, **fields}, default=str)))
            prev, reason = "0" * 64, None
            if last_line is not None:
                try:
                    last = json.loads(last_line); prev = last["hash"]
                    if not (isinstance(prev, str) and len(prev) == 64): reason = "last line hash malformed"
                    elif seq is None: reason = "last line seq missing/invalid"
                except (ValueError, KeyError, TypeError) as e: reason = f"last line invalid: {type(e).__name__}: {e}"
            if reason:
                ts = dt.datetime.now(dt.timezone.utc).isoformat()
                with open(quar, "a") as q:
                    q.write(json.dumps({"quarantined_ts": ts, "reason": reason, "line_no": seq - 1 if seq else None, "corrupt_line_raw": last_line, "record": body}, sort_keys=True, default=str) + "\n"); q.flush(); os.fsync(q.fileno())
                with open(alert, "w") as a:
                    a.write(f"{ts}\n{reason}\nline {seq} of {PATH}; pending record saved to {quar}\n"); a.flush(); os.fsync(a.fileno())
                raise HashlogCorrupt(f"hashlog corrupt at line {seq}: {reason}; record quarantined, alert {alert}")
            rec = {**body, "prev_hash": prev, "hash": _h(prev, body)}
            fd = os.open(PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                data = (json.dumps(rec, sort_keys=True, default=str) + "\n").encode()
                if os.write(fd, data) != len(data): raise OSError("short write to hashlog")
                os.fsync(fd)
            finally: os.close(fd)
            return rec["hash"]
        finally: fcntl.flock(lk, fcntl.LOCK_UN)

def correction(ref_seq, **fields):
    """Fixes are NEW records; existing lines are never rewritten or deleted."""
    return append("correction", ref_seq=int(ref_seq), **fields)

def verify(path=None):
    """week1d v2: a final line WITHOUT trailing newline is an append in progress (race with a reader) -> ignored, not a
    break. Any invalid/mismatched COMPLETE line (anywhere, incl. the last newline-terminated one) -> (False, line_no)."""
    path = path or PATH
    prev = "0" * 64
    if not os.path.exists(path): return True, 0
    data = open(path, "rb").read()
    lines = data.split(b"\n")
    lines = lines[:-1]   # last element is b"" (complete file) or a partial line being written -> ignored
    for i, l in enumerate(lines):
        try:
            r = json.loads(l); h = r.pop("hash"); p = r.pop("prev_hash")
        except (ValueError, KeyError, TypeError, AttributeError): return False, i
        if p != prev or _h(prev, r) != h or r.get("seq") != i: return False, i
        prev = h
    return True, len(lines)
