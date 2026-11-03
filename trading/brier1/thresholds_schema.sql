-- brier1 r2: append-only threshold-change log. Lives in its OWN db (data/threshold_changes.db); build() never touches it.
CREATE TABLE IF NOT EXISTS threshold_changes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  thresholds_sha_before TEXT NOT NULL, thresholds_sha_after TEXT NOT NULL,
  question_id TEXT, param TEXT, old_value TEXT, new_value TEXT,
  metric TEXT NOT NULL, metric_fixed_ts TEXT NOT NULL,
  window_start TEXT NOT NULL, window_end TEXT NOT NULL,
  metric_before REAL, metric_after REAL,
  n_trades INTEGER NOT NULL, n_scored INTEGER NOT NULL,
  verdict TEXT NOT NULL CHECK(verdict IN ('keep','discard')),
  approved_by TEXT NOT NULL CHECK(length(trim(approved_by)) > 0),
  notes TEXT NOT NULL CHECK(length(trim(notes)) > 0),
  event_hash TEXT NOT NULL,
  CHECK(metric_fixed_ts < window_start)
);
CREATE TRIGGER IF NOT EXISTS threshold_changes_no_update BEFORE UPDATE ON threshold_changes
  BEGIN SELECT RAISE(ABORT, 'threshold_changes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS threshold_changes_no_delete BEFORE DELETE ON threshold_changes
  BEGIN SELECT RAISE(ABORT, 'threshold_changes is append-only'); END;
