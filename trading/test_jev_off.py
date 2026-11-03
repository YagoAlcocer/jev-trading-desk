"""Auditor test: Jev off -> 0 Jev entries; the simple baseline can still enter. Offline (mocked data/quotes)."""
import os
for _k in ("OPENROUTER_API_KEY", "JEV_API_KEY"): os.environ.pop(_k, None)   # Jev must be OFF inside this test (auditor cond. 4)
import sqlite3, tempfile, collector as C, hashlog
from judge import Judge

def run():
    tmp = tempfile.mkdtemp(); C.DB = os.path.join(tmp, "t.db"); hashlog.PATH = os.path.join(tmp, "log.jsonl")
    C.T.QUOTE_RETRY_DELAY_SEC = 0
    C.hard_filter = lambda rpc, pool, mint, name, d: ([], {"liquidity": 50_000, "volume_h24": 100_000, "symbol": "TST"})
    C.jup_quote = lambda i, o, a: {"inAmount": str(int(a)), "outAmount": str(int(a) * 1000), "priceImpactPct": "0.001"}
    C.spot = lambda s: 100.0
    C.cycle(judge=Judge(), fetch=lambda: ([("POOL1", "MINT1", "TST / SOL")], {}), rpc=object())
    c = sqlite3.connect(C.DB)
    jev = c.execute("SELECT COUNT(*) FROM positions WHERE strategy='jev'").fetchone()[0]
    simple = c.execute("SELECT COUNT(*) FROM positions WHERE strategy='simple'").fetchone()[0]
    rule = c.execute("SELECT rule FROM rejections WHERE strategy='jev'").fetchone()
    assert jev == 0, f"Jev off must yield 0 Jev entries, got {jev}"
    assert simple == 1, f"simple baseline should enter, got {simple}"
    assert rule and rule[0] == "jev_unavailable", rule
    assert hashlog.verify(hashlog.PATH)[0]
    print(f"PASS: jev entries={jev}, simple entries={simple}, jev rule={rule[0]}, hash chain ok")

if __name__ == "__main__": run()

def run_edge():
    """edge_est math check (risk_rules note: p>=0.51 needed at ~1% costs)."""
    e, p = C.edge_est(0.32, 0.35, 0.20, 0.15, 0.01)   # p_jev 0.68 -> p 0.515
    assert abs(p - 0.515) < 1e-9 and e > 0, (e, p)
    e2, _ = C.edge_est(0.6, 0.35, 0.20, 0.15, 0.01)
    assert e2 < 0.02
    print(f"PASS: edge_est p=0.515 -> {e:.4f}; P_exh 0.6 -> {e2:.4f} (no entry)")

def run_mask():
    import io, contextlib
    if not C._HK: print("SKIP mask test: no key"); return
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): C.print("err at", C.HELIUS)
    assert C._HK not in buf.getvalue() and C._HK not in C.mask(C.HELIUS); print("PASS: key masked")

if __name__ == "__main__": run_edge(); run_mask()
