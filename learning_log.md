# Learning Log

## Current status
- Core project setup is working from the repo root with the import helper in place.
- DuckDB access is functioning through the compatible store interface.
- Transfermarkt and API-Football ingestors have been updated to match the current architecture.
- Some live-data ingestors still depend on external credentials or network access.

This file is a compact troubleshooting record for recurring setup and runtime issues in the project.

## Common commands
- Install dependencies from the repo root:
  `python3 -m pip install -r requirements.txt`
- Export the project import path before running ingestion scripts:
  `export PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem`
- Inspect DuckDB tables using the package-local database that receives ingested rows:
  `PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 - <<'PY'`
  `from data_storage.duckdb_store import DuckDBStore`
  `store = DuckDBStore('/workspaces/Soccer-App-1/ss_env/SoccerSystem/data/soccer_smoketest.duckdb')`
  `print(store.conn.execute('SHOW TABLES').fetchall())`
  `store.close()`
  `PY`

## Daily startup notes (verified 2026-07-05)
- Use `python3` rather than `python`; this environment does not expose a `python` shim.
- Export `PYTHONPATH` before any ingestion or inspection command.
- The DuckDB file that receives ingested rows is the package-local database at `ss_env/SoccerSystem/data/soccer_smoketest.duckdb`.
- The external `understatapi` package is not required here; the repo already contains its own scraper implementation.

## Project status update
- The project currently uses a DuckDB-backed storage layer with Pydantic models and an entity-resolution layer for teams, players, and matches.
- Ingestors are expected to follow the current `Ingestor` interface and to use `DuckDBStore.conn` for direct database access.
- The repo root now has a `sitecustomize.py` helper so Python can import project modules reliably when launched from the repository root.
- The Transfermarkt and API-Football ingestors were updated to match the current architecture and are now runnable with the correct import path and environment setup.

## 2026-07-04

### [FIX] DuckDB connection access errors
- [CAUSE] Code paths were using `store.con` or `self.store.con`, while the project now uses `DuckDBStore.conn`.
- [FIX] Added a backward-compatible `con` property on `DuckDBStore` that returns the active connection.
- [FIX] Updated feature-engineering code to use `store.conn` explicitly for clarity.
- [VERIFY] Confirmed the connection alias works by opening the database and running a simple query.

### [FIX] Python import path errors for project modules
- [CAUSE] Running Python from the repo root caused `ModuleNotFoundError` for packages such as `data_storage`, `data_ingestion`, and `entity_resolution`.
- [FIX] Added a repo-root helper file, `sitecustomize.py`, which injects the `ss_env/SoccerSystem` directory into `sys.path` automatically.
- [WORKAROUND] If needed, run from the repo root with:
  `PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem`

### [FIX] Requirements install command path issue
- [CAUSE] The requirements file lives at the repo root, not inside `ss_env/SoccerSystem`.
- [FIX] Install from the repo root with:
  `python -m pip install -r requirements.txt`
- [WORKAROUND] From inside `ss_env/SoccerSystem`, use:
  `python -m pip install -r ../requirements.txt`

### [FIX] Transfermarkt scraper import/runtime breakage
- [CAUSE] The scraper expected an older base class name, `BaseIngestor`, that no longer exists in the current codebase.
- [FIX] Updated the scraper to use the current `Ingestor` base class and compatible constructor pattern.
- [FIX] Added resolver compatibility for player resolution and improved network resilience with retries and longer timeouts.
- [VERIFY] Verified the scraper successfully completed a live run and stored valuation rows.

### [FIX] API-Football ingestor import/runtime breakage
- [CAUSE] The ingestor also referenced the older base-class interface and old store connection usage.
- [FIX] Updated it to use the current `Ingestor` API and `store.conn` for database access.
- [WORKAROUND] This module still requires the environment variable `API_FOOTBALL_KEY` to run.

## 2026-07-05

### [PROCESS] Push changes to GitHub
- [STEP] Confirm the repository state from the repo root:
  `git status --short`
  `git branch --show-current`
  `git remote -v`
- [STEP] If changes are pending, stage and commit them before pushing:
  `git add .`
  `git commit -m "<descriptive message>"`
- [STEP] Push the current branch to the configured GitHub remote:
  `git push origin main`
- [NOTE] In this case, the repo was already on `main`, had no uncommitted changes, and the push completed successfully to `origin`.

#### [FIXED] matches table column names verified and populated
- home_team_canonical_id and away_team_canonical_id confirmed present and populated.
- Root cause: schemas.py edit was not saved initially; also corrupted by another AI agent adding bogus `from math import Math`.
- Fixed by: removing bad import, confirming schema fields, re-running football_data_org ingestor.

### [BLOCKER] Weather ingestor needs static stadium coordinates
- [CAUSE] The `matches` table does not include `venue_lat` or `venue_lon`, so the weather ingestor cannot fetch coordinates from the DB.
- [NEED] Add a static EPL stadium coordinates lookup (by home team canonical ID or team name) in the weather ingestor and build weather requests from that data instead of querying match coordinates from the database.
- [STATUS] Other data is ready:
  - matches: 380 ✅
  - xg_records: 608 ✅
  - odds_snapshots: 2200 EPL rows ✅
  - squad_valuations: 693 ✅
  - weather_records: 0 ⬜ blocked
- [NEXT SESSION PRIORITY ORDER]
  1. Fix weather ingestor with static stadium coordinates.
  2. Run `FeatureBuilder.build_matrix(league="Premier League", season="2025/2026")` to confirm the full feature matrix builds cleanly.
  3. Begin model training.
- [ENVIRONMENT REMINDERS]
  - Use `store.conn`, not `store.con`
  - Use `python3` and `PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem`
  - DB path to inspect/write: `/workspaces/Soccer-App-1/ss_env/SoccerSystem/data/soccer_smoketest.duckdb`
  - Base class: `Ingestor`, not `BaseIngestor`
  - Skip `pip install understatapi`; the repo uses its built-in Understat scraper code.

## What to tell the AI next time
- [AI NOTE] If it references `store.con` or `self.store.con`, remind it that the project uses `DuckDBStore.conn` and that `store.con` is now aliased for compatibility.
- [AI NOTE] If it hits `ModuleNotFoundError` for `data_storage` or `data_ingestion`, tell it to use the repo-root `sitecustomize.py` helper or add `ss_env/SoccerSystem` to `PYTHONPATH`.
- [AI NOTE] If it tries to install dependencies from inside `ss_env/SoccerSystem`, remind it that `requirements.txt` is at the repo root.
- [AI NOTE] If it edits ingestors, keep them compatible with the current `Ingestor` interface and `DuckDBStore` API.

## 2026-07-06

### [IMPROVEMENT] Quality checks now cover missing canonical IDs
- [CHANGE] Added a `canonical_id_presence_check` for match rows so missing `home_team_canonical_id` / `away_team_canonical_id` values are surfaced explicitly.
- [CHANGE] Hardened null-rate checks to return clear failure details when a table or column is missing instead of crashing.
- [VERIFY] Ran `PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 -m pytest -q tests/test_quality_checks.py` and got `2 passed`.
- [VERIFY] Ran the quality runner against `data/soccer_smoketest.duckdb` and got `13/13 checks passed`.

Recommended order before model training:

Re-run odds ingestor targeting EPL
Re-run weather ingestor against all 380 matches
Investigate squad valuation linking
Re-run quality checks
Test build_matrix() end to end

## 2026-07-09  
  
### [REFACTOR] Deduplicated feature_engineering/builder.py  
- [CHANGE] Removed the dead `.replace('_', '_')` no-op in `_odds_features`; extracted a `_clean_sel(sel)` helper that only does `sel.replace('.', '')` (e.g. `over_2.5` -> `over_25`).  
- [CHANGE] Cleaned up `_team_form`: dropped the dead `venue_clause = ""` line and the throwaway `params` pre-initialization; `params` is now assigned exactly once per venue branch.  
- [VERIFY] Confirmed feature-key and value parity with the original: odds keys still produce `odds_closing_over_under_over_25` etc., which the backtester's `_extract_market_odds` mapping in `strategy_backtest/strategy.py` depends on.  
- [VERIFY] Confirmed `build_matrix` still unpacks `for (mid,) in match_ids` (full canonical IDs) and the odds line-movement loop still includes `("over_under", "over_25")`, so `odds_movement_over_under_over_25` is not dropped.  
- [NOTE] `builder.py` still uses `self.con = store.con`; the `con`/`conn` standardization was not applied here (not a regression — matches the compat alias on `DuckDBStore`).  
  
### [NEXT] Remaining refactor pass  
- [TODO] Apply the same duplication cleanup to `model_training/trainer.py` (shared multiclass Brier helper; dedupe prediction-row assembly).  
- [TODO] `strategy_backtest/strategy.py`: use `dataclasses.replace` in `adjust_for_simultaneous`.  
- [TODO] `data_ingestion/csv_sources/football_data_csv.py`: consolidate the two identical `_build_snapshots` market loops.  
- [VERIFY] Run `PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 -m pytest -q tests/test_quality_checks.py` after each change.

- [REFACTOR] Deduplicated model_training/trainer.py (multiclass Brier + prediction rows)
- [CHANGE] Extracted the duplicated multiclass Brier‑score computation into a new helper _multiclass_brier(y_true, proba).
- [CHANGE] Updated both evaluate_cv and evaluate_holdout to call the helper, preserving the binary branches exactly.
- [CHANGE] Consolidated the duplicated per‑match prediction‑row assembly into a new _prediction_rows(round_probs) helper.
- [DETAIL] The helper preserves the two different behaviors:
save_best_models: uses iterrows(), includes the match_canonical_id fallback, and does no rounding.
get_predictions_df: uses range(len(df)), only includes the ID when present, and rounds to 4 decimals.
- [VERIFY] Confirmed byte‑for‑byte parity for predictions.csv and metrics.
- [VERIFY] All tests pass:
PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 -m pytest -q tests/
- [NEXT] Remaining refactor targets
strategy_backtest/strategy.py: dedupe simultaneous‑match adjustment logic using dataclasses.replace.
data_ingestion/csv_sources/football_data_csv.py: merge the duplicated _build_snapshots loops for opening/closing odds.