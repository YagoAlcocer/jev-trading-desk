# brier1 r2 — fixes for auditor BLOCK (evidence /tmp/aud20). Staging only; nothing applied live.
Run: python3 -B backfill.py --snapshot; python3 -B backfill.py [--fetch]; python3 -B score.py data/staging.db out.json; python3 -B test_brier.py

1. threshold_changes: own DB data/threshold_changes.db (thresholds_schema.sql). Triggers block UPDATE/DELETE (RAISE ABORT).
   approved_by, notes NOT NULL + non-blank (CHECK and code). Each change appends a hash-chained event to
   data/threshold_events.jsonl; the row stores event_hash. build() writes only staging.db (tmp + os.replace).
2. Climatology: for each resolved row, base rate = mean outcome of rows of the same question/view whose resolve_ts < its decision_ts.
   Rows without priors are excluded; brier_model_on_clim_rows is reported on the same subset for a fair comparison.
3. Dead/illiquid: HTTP 200 + ohlcv_list present (valid pool) -> missing hours = no trades = no rise; resolves (died_suspected flags it).
   Non-200 / invalid pool / network error -> unresolved fetch_failed. brier_adverse imputes every non-pending unresolved row
   against Jev (o = 0 if p >= 0.5 else 1) and is reported beside brier.
4. Window: only complete hourly candles with start >= t0 and start+1h <= t0+24h. The partial hour holding t0 and the hour past
   t0+24h are trimmed (23 candles for a mid-hour decision, 24 on the hour). Event uses each candle's OHLC HIGH (intrabar max,
   so one wick >= 1.15*p0 counts as a rise; GeckoTerminal highs can include outlier prints). p0 = price_usd logged at decision.
5. --fetch: >= 15 s between requests (<= 4/min); first 429 aborts all remaining fetches (rows -> fetch_aborted), Retry-After
   (seconds or HTTP date; unknown -> 600 s; min 60 s) saved to data/gecko_not_before.json and honored by later runs; refuses to
   fetch if the live log (read-only tail, last 2 MB) shows gecko_backoff within 30 min or still active. Exit code 3 on abort.
6. p validated in [0,1] (Q4/Q4b score in [1,10]; bool/str/NaN rejected) -> unresolved invalid_p; DB CHECK too; scorer raises on
   bad p. brier/CI/base_rate/bins null with publishable=false when N < 30. 95% CI = cluster bootstrap by mint (2000 draws, fixed seed).
   Unanswered decisions (table unanswered_decisions): position_limit and cycle_entry_limit are rejected in collector.py's scan loop
   before Jev is called; jev_unavailable records with no answers are Jev errors (NO TRADE). No forecast -> not in Brier, not imputed.
   jev_unavailable records that DID log partial answers (e.g. Q1/Q2 before a Q4 failure) are kept as forecasts.
Changed from r1 on purpose: r1 test "dead token -> unresolved incomplete_window" now resolves (fix 3); "no_price_data" -> "not_fetched".

## r3 (auditor BLOCK on atomicity, evidence /tmp/aud21)
1. log_change: under the lock, INSERT + COMMIT the row first, then append + fsync the event. A crash in between leaves only
   "row_without_event" (tests: in-process SystemExit and a forked process killed with os._exit after the commit).
   log_change refuses (before any commit) if the event log has a corrupt line.
2. verify(db, events) (CLI: python3 -B thresholds_log.py <db> <events>; exit 1 on problems), read-only. Reports:
   truncated_last_line, corrupt_line, bad_hash, chain_break, duplicate_event, duplicate_row_event_hash, row_without_event,
   event_without_row, row_event_mismatch. On a copy of /tmp/aud21/tl it reports n_rows=1, n_events=2, event_without_row (line 2),
   plus truncated/corrupt line 3.
3. fcntl.LOCK_EX on <events>.lock covers reading prev_hash, the commit and the append (6 processes x 5 writes test: 30 rows,
   30 events, 30 distinct prev_hash, verify ok).
Notes: collector_in_backoff is re-checked before EACH GeckoTerminal request (CollectorBackoff stops fetching without writing
gecko_not_before.json); the startup and per-request checks and the Retry-After gate use the real wall clock, never --now.
Repair of a row_without_event is NOT automated (append-only): it needs a human decision; verify only reports it.

## r3.1 (auditor APPROVED r3 with conditions, evidence /tmp/aud22)
1. Chain head anchor: thresholds_log.anchor(db, events) -> {head_hash, n_events, n_rows, at}, only from a clean verify.
   score.py <staging> <out.json> <threshold_changes.db> <threshold_events.jsonl> writes it under threshold_log.anchor and,
   if out.json already held an anchor, verifies against it first (prev_anchor). On failure the old anchor is kept, never moved.
   verify(..., anchor=a) / CLI 3rd arg (anchor JSON or score JSON) reports chain_truncated, rows_truncated,
   anchor_head_missing (tail rewritten), anchor_position_mismatch (reordered), anchor_invalid. This closes the adv.py case
   "delete last row AND last event" that is internally consistent and passes a plain verify.
   The anchor only protects if it is kept somewhere the attacker/bug cannot also rewrite (score JSON published to the
   dashboard / the auditor report); an anchor stored only next to the log can be truncated with it.
2. Fetch guard: collector_in_backoff is checked before the 15 s wait AND again right after it, immediately before the
   request (a backoff that starts during our wait stops the request).
3. Operating rules (mandatory):
   - Orphaned rows (row_without_event, the only possible crash state) are NOT auto-repaired. The log is append-only; a
     human decides (069 + auditor) and records the decision as a NEW threshold change with notes; nothing is edited.
   - verify (with the last published anchor) is MANDATORY before any keep/discard or threshold decision that reads this
     log. A decision taken on a log that fails verify is invalid.
   - The fcntl lock and the fsync ordering only hold on a LOCAL filesystem on this box (not NFS/SMB/sync folders/
     multi-host). Do not move data/threshold_changes.db or threshold_events.jsonl to shared storage.

## r3.2 (auditor BLOCKED r3.1: anchor in score.json is not external; evidence /tmp/aud23)
1. data/anchors.jsonl (beside threshold_events.jsonl): own append-only, hash-chained file. Each clean anchor() appends
   {head_hash, n_events, n_rows, ts, prev_hash, hash} under fcntl.LOCK_EX (anchors.jsonl.lock) with flush+fsync.
   Previous score JSONs are NEVER used as a prior anchor any more. score JSON carries threshold_log.anchors_head_hash
   (= last anchor record hash), ready to quote in the daily report.
2. verify() by default checks the threshold log against EVERY anchor in anchors.jsonl, verifies anchors.jsonl's own chain
   (anchors_truncated_last_line / anchors_corrupt_line / anchors_bad_hash / anchors_chain_break), returns anchor_missing if
   there is no anchor, and with expected_anchors_head (CLI --expect-anchors-head) returns anchors_head_not_found if a head
   quoted outside the box is not in the file. use_anchors=False = internal-consistency diagnostics only.
3. score.py refuses (exit 2, verify_ok false, no new anchor appended) when anchors.jsonl has any line and verify fails,
   when anchors.jsonl is damaged, or when anchors.jsonl is missing/empty while the threshold log has rows or events.
   Bootstrap (first anchor) only on an empty threshold log with no anchors.
   Auditor sequence (truncate last row + event, delete score.json, regenerate) now FAILS with chain_truncated + rows_truncated.
LIMIT (must read): anchors.jsonl lives on the SAME box as the log. Someone who can rewrite both can truncate the log AND
anchors.jsonl back to an older consistent state (or delete all three and bootstrap again); on-box verify cannot see that.
Full protection requires quoting anchors_head_hash OUTSIDE the box (daily report to 069 and/or the auditor's report) and
running verify with --expect-anchors-head <last quoted head> before any threshold decision.
