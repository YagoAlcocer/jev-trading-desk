-- brier1 r2 staging schema (data/staging.db only; rebuilt by build()). Never applied to live paper.db.
CREATE TABLE IF NOT EXISTS outcomes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_hash TEXT NOT NULL, decision_seq INTEGER,
  mint TEXT NOT NULL, pool TEXT, symbol TEXT,
  question_id TEXT NOT NULL, question_version TEXT NOT NULL,
  outcome_def TEXT NOT NULL, def_status TEXT NOT NULL,      -- 'questions.md' | 'proxy_pending_approval'
  p_jev REAL CHECK(p_jev IS NULL OR (p_jev >= 0 AND p_jev <= 1)),
  raw_answer TEXT, jev_model TEXT, thresholds_sha TEXT,
  decision_ts TEXT NOT NULL, resolve_ts TEXT NOT NULL,       -- UTC ISO; resolve_ts = decision_ts + 24h
  p0_usd REAL,
  is_trade INTEGER NOT NULL,                                -- 1 = Jev 'enter' record, 0 = rejection/none
  decision_action TEXT, decision_rule TEXT,
  first_for_mint INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('resolved','unresolved')),
  unresolved_reason TEXT,   -- pending_horizon|no_pool|no_p0|invalid_p|fetch_failed|not_fetched|no_holder_snapshot|not_price_resolvable|fetch_aborted
  died_suspected INTEGER NOT NULL DEFAULT 0,
  outcome INTEGER CHECK(outcome IS NULL OR outcome IN (0,1)),
  outcome_evidence TEXT, resolved_at TEXT,
  UNIQUE(decision_hash, question_id)
);
-- Jev decisions with no forecast at all (never enter the Brier; listed for the audit trail)
CREATE TABLE IF NOT EXISTS unanswered_decisions(
  decision_hash TEXT PRIMARY KEY, decision_seq INTEGER, ts TEXT, mint TEXT, action TEXT, rule TEXT, why TEXT);
