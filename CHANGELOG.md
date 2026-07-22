## [1.2.3] - 2026-07-20
### Added -- Snag 19: recent phantom rows in the Archie Brief
- The Archie Brief now lists the **last 5 phantom rows** (newest first) directly under
  the STAY OUT QUALITY summary, so Archie sees overnight phantom activity inline without
  a separate PHANTOM-page screenshot. Columns: Date/Time (UTC), Direction, Confidence,
  1hr Move, Verdict. PENDING rows shown as PENDING; empty -> "No phantom data yet".
  Display only -- reads the same stay_out_quality decisions; no logic/threshold change.
- Also folds in the previously-unstaged Guinevere Part-2 tweak to `_news_line()`
  (skip headlines with |score| < 1; empty message -> "No significant headlines in
  current period") -- brings this repo's archie_brief.py up to the desk standard.

## [1.2.2] - 2026-07-19
### Added -- dedicated PHANTOM page (desk rollout, template CryptoTrader v1.7.3)
- New **PHANTOM &rarr;** header button opens page 3: "PHANTOM TRADES -- Stay Out Quality"
  with a summary (Quality %% / Correct / Wrong / Neutral / Net Saved / Net Missed) and a
  clean **last-20** table (newest first): Date/Time UTC | Direction | Entry Price |
  Confidence | 1hr Move | colour-coded Verdict. Back to Dashboard + Trading nav.
- The right-panel Stay Out Quality card is now a **compact** clickable summary that opens
  the full page. Standardised to the last 20 rows (was 10). Display only -- reads the same
  get_stay_out_quality() data; no threshold/logic/recording change.
- Direction column retained for desk consistency (US is LONG_ONLY; older SHORT rows in history still display).

## [1.2.1] - 2026-07-18
### Changed -- phantom verdict threshold (System 5 Review desk-wide, Rec 1 pattern)
- **`VERDICT_THRESHOLD` 10 -> 8** (index pts, 1hr window). Re-scored 45 rows: CORRECT
  9/WRONG 8/NEUTRAL 28 -> 13/9/23 (48.9%% classified, 5 changed). Data-only (logs/, gitignored).
  No trading-rule change; no backtest required. Verdict threshold mis-scaling was assessed
  desk-wide on 18 Jul; see the OilTrader v1.1.18 fix that established this pattern.

## [1.2.0] - 2026-07-18
### Changed -- USHybrid System 4 Review: LONG_ONLY, bull-regime aware
Backtest-provisional; 4-week review. Nick sign-off confirmed. Fixes the structural
issue from the review: bullish signals were correct but execution delivered
counter-trend shorts while blocking profitable bull-market longs.

- **LONG_ONLY enforced (Change 3):** direction is forced LONG every tick (SHORT
  signals suppressed with a log); the ENTER_SHORT execution branch is hard-blocked.
  A genuine bear daily -> Lancelot blocks the LONG -> STAY_OUT (never flips to SHORT).
- **Regime-aware confidence gate (Change 1):** NEW gate in main (no confidence gate
  existed before -- execution acted directly on Arthur's decision). A LONG needs Arthur
  confidence >= 50 in a bull regime (55 in bear). Regime read from Gaius
  market_context.json (`sp500_above_200ma`) via new `regime_us.py`, 30-min cache.
- **Softened 1h SSL for LONG dips (Change 2):** `check_ssl_agreement` for LONG now
  passes if 1h SSL BULL, OR 1h SSL BEAR with 1h RSI > 45 (pullback dip, not reversal).
  The 5m momentum + candle checks are now direction-aware (keyed off the proposed
  direction, not the 1h SSL) so a dip is evaluated as a LONG throughout. NOTE: the
  separate `check_1h_rsi_confirms` LONG floor stays 52 (backtest-validated v1.0.6,
  not changed) -- so the effective dip floor is RSI 52 (shallow pullbacks).
- **Risk (Change 4):** `TAKE_PROFIT_POINTS` 200 -> **45** (200 never hit; ~46pt =
  90th-pct move; 1.5:1 R:R). `CAPITAL_SPREAD_POINTS` 0.4 -> **0.6** (real Capital.com
  spread confirmed 18 Jul). Stop 30 unchanged.
- **Profit ladder (Change 5):** recalibrated to **£8->£6, £16->£13, £24->£20**.
- **Arthur prompt (Change 6):** rewritten LONG_ONLY / bull-aware -- philosophy, bull
  market awareness, session awareness, FOMC awareness (next 2026-07-30 19:00 UTC),
  confidence calibration (act on 50+), LONG_ONLY discipline, point convention (30/45),
  profit ladder. Live regime + confidence floor injected per tick; `get_trading_decision`
  gained `regime` + `min_confidence`.

## [1.1.7] - 2026-07-16
### Fixed
- Snag 9: confidence bar could display 50 when the real Morgan score was 0. The
  dashboard read `perf.confidence_score || 50`, and JS treats 0 as falsy, so a
  legitimate 0 was replaced by the 50 fallback. Changed to
  `(perf.confidence_score != null ? perf.confidence_score : 50)` -- 0 now shows as
  0; 50 is used only when the value is genuinely absent. In practice only GasTrader
  showed the wrong value (the only system with a 0 score, from a 5-loss streak); the
  latent bug was in all 6 dashboards. RoundTable was already correct.

## [1.1.6] - 2026-07-16
### Added
- Job 1 (Gaius Commission 001, Priority 1): indicator snapshot at signal time in
  phantom_trades.csv. 17 columns APPENDED to the right of the existing 14-col schema
  (existing positions unchanged): ssl_daily/1hr/5min, rsi_daily/1hr/5min,
  tmo_1hr/5min, macd_1hr/5min, chande_mo_1hr/5min, money_flow_1hr/5min, morgan_score,
  session, guinevere_score. Captured from values Merlin already fetched for Arthur
  (no new data fetch) via phantom_tracker.build_snapshot() -> record_decision(indicators=).
  The snapshot build is wrapped in its own try/except so a failure can never stop a
  phantom row being written. phantom_tracker now migrates an older 14-col file in place
  on first use (old rows keep positions; new columns blank). Chronicle & Gaius read by
  column name and are unaffected. (guinevere_score currently blank pending a safe cached
  source -- column reserved.)

# USHybrid AI Changelog
## [1.1.5] - 2026-07-14
### Fixed
- Morgan confidence (perf.confidence_score) now included in the lightweight always-running
  dashboard push (_push_dashboard_live), so /api/state exposes it in ALL market states --
  including after the 21:00 UTC close. Previously perf was only pushed on full candle ticks
  (skipped when the market is closed), so RoundTable / Gaius / Chronicle showed null
  confidence out of hours. Matches CryptoTrader (performance in every push).

## [1.1.4] - 2026-07-13
### Fixed
- Bug C (desk-wide): "Locked P&L" now only shows once the trailing stop trails to break-even (genuine secured profit); until then "---" instead of an if-stopped loss figure.

## [1.1.3] - 2026-07-12
### Fixed
- Log timestamps now emitted in UTC (logging.Formatter.converter = time.gmtime; datefmt suffixed " UTC") across main, watchdog and dashboard. Previously local/BST, causing a +1h mismatch vs the UTC CSV artefacts (phantom_trades.csv etc.).
### Added
- ALBION STANDING RULE comment blocks baked into the logging setup and the log/analysis modules (phantom_tracker.py, performance_us.py, dashboard stay-out reader): all timestamps are UTC, never BST/local.

## [1.1.2] - 2026-07-11
### Added
- Silent launcher (pythonw -- no console windows); output to logs/console.log with daily rotation (7 days kept)
- Launcher now starts the dashboard + watchdog silently (was cmd windows)

## [1.1.1] - 2026-07-11
### Added
- Morgan confidence persistence: every confidence change is now also appended to logs/morgan_confidence.csv (timestamp, confidence, level HIGH>=65/LOW<=35/MEDIUM, reason) via save_confidence(); set_confidence(value, reason='update') writes both the JSON store and the CSV history. load_confidence() returns the last recorded value. On startup main_ushybrid.py restores Morgan's confidence from the CSV (set_confidence(saved, reason='restore')), falling back to baseline 50 when no history exists -- so confidence is restored on restart.

## [1.1.0] - 2026-07-11
### Added
- Morgan individual phantom feedback: persistent confidence store (logs/morgan_confidence.json, default 50, clamp 0-100) with get_confidence()/set_confidence(); apply_phantom_verdict_feedback() (NEUTRAL=0; CORRECT/WRONG scaled by |pnl_1hr|/50 clamped 0.5-2.0); process_new_phantom_verdicts() daemon poller (MorganPhantomPoller, 300s) consuming phantom_tracker.get_unprocessed_verdicts()/mark_processed(). Reported confidence now folds in the phantom delta (score + (get_confidence() - 50)) without double-counting get_stay_out_adjustment(). Started from main after phantom resolve/watchdog hook.
### Audit
- Arthur prompt (agent_brain_us.py) audited for hardcoded win-rate/historical/backtest % figures: CLEAN. No performance claims present; all percentages are live indicator thresholds (RSI 55/45), Morgan-driven confidence bands, or session/instrument constants. No reset required.

## [1.0.9] - 2026-07-11
### Added
- 7 flat status fields merged into /api/state (and any /api/status consumer): lancelot_status, lancelot_fails, lancelot_fail_reasons, arthur_decision, arthur_confidence, arthur_consulted, locked_pnl. Derived from panel_mode / pre_checks / decision / open position via compute_status_fields(), fully try/except-guarded so /api/state never 500s.
### Fixed
- Open Position panel Entry/Stop/Target rows now use a compact two-column layout (fixed ~120px label, value immediately after) instead of the wide label-left/value-hard-right spacing.

## [1.0.8] - 2026-07-10
### Fixed
- Staggered Capital.com API startup delay (30s + jitter) to prevent 429 rate limits on shared demo account (Z6CJSM)

## [1.0.7] - 2026-07-09
### Added
- phantom_tracker.start_watchdog() — continuous daemon thread that runs resolve_stale_pending() every 15 min, so stale PENDING rows resolve dynamically without a restart. Idempotent (single thread per process). Started in main after startup resolution.

## [1.0.6] - 2026-07-09
### Added
- backtest_us.py — S&P 500 (^GSPC) backtest engine; replays 5m bars through the live Lancelot entry gate, runs BASELINE vs RELAXED 1h RSI veto side by side, writes logs/backtest_us_results.txt and logs/backtest_us_trades.csv
### Changed
- 1h RSI veto relaxed 55→52 (LONG) / 45→48 (SHORT) in pre_checks_us.check_1h_rsi_confirms (Fix 3, backtest-validated: RELAXED 50.0% win rate, +1 trade, lower drawdown vs baseline)

## [1.0.5] - 2026-07-09
### Fixed
- Morgan quality score now excludes NEUTRAL decisions from the denominator (only CORRECT/WRONG judged)
- Morgan penalty minimum raised from 5 to 8 judged decisions before firing
### Notes
- 1-hour RSI veto relaxation (Fix 3) NOT applied: no backtest script exists for USHybrid to validate it. Deferred for Nick's decision.

## [1.0.2] - 2026-07-08
### Fixed
- STAY OUT QUALITY panel now ignores PENDING rows in the quality score (matches Morgan's get_summary)
### Changed
- README rewritten with Albion Trading Desk branding and team roster

## [1.0.1] - 2026-07-08
### Added
- phantom_tracker.py — STAY OUT decision recorder
- Morgan STAY OUT quality integration
- Main loop hook for STAY OUT recording

## v1.0.0 -- 7 Jul 2026
### Added
- Initial build (20 files, 5,700 lines)
- S&P 500 spread betting via Capital.com
- Epic: US500 | Stop: 30pts | Stake: £0.67/pt
- US session management (14:30-21:00 UTC)
- 15-min US open exclusion (14:30-14:45)
- Arthurian team, port 5044, navy theme #000080
