# Learning Log

## Current status
- Core project setup is working from the repo root with the import helper in place.
- DuckDB access is functioning through the compatible store interface.
- Transfermarkt and API-Football ingestors have been updated to match the current architecture.
- Some live-data ingestors still depend on external credentials or network access.

This file is a compact troubleshooting record for recurring setup and runtime issues in the project.

## Common commands
- Install dependencies from the repo root:
  `python -m pip install -r requirements.txt`
- Run Python with project modules available from the repo root:
  `PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python ...`
- Inspect DuckDB tables:
  `PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python - <<'PY'`
  `from data_storage.duckdb_store import DuckDBStore`
  `store = DuckDBStore('data/soccer_smoketest.duckdb')`
  `print(store.con.execute('SHOW TABLES').fetchall())`
  `store.close()`
  `PY`

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

## What to tell the AI next time
- [AI NOTE] If it references `store.con` or `self.store.con`, remind it that the project uses `DuckDBStore.conn` and that `store.con` is now aliased for compatibility.
- [AI NOTE] If it hits `ModuleNotFoundError` for `data_storage` or `data_ingestion`, tell it to use the repo-root `sitecustomize.py` helper or add `ss_env/SoccerSystem` to `PYTHONPATH`.
- [AI NOTE] If it tries to install dependencies from inside `ss_env/SoccerSystem`, remind it that `requirements.txt` is at the repo root.
- [AI NOTE] If it edits ingestors, keep them compatible with the current `Ingestor` interface and `DuckDBStore` API.
