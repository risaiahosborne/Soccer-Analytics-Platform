# Learning Log

## Current Status

| Module                        | Status               | Notes                                                      |
|-------------------------------|----------------------|------------------------------------------------------------|
| football_data_org ingestor    | ✅ Working            | 3 seasons (2023-2026), free tier blocks pre-2023           |
| football_data_csv ingestor    | ✅ Working            | Odds + shots proxy xG, TEAM_NAME_MAP added                 |
| transfermarkt ingestor        | ✅ Working            | 677 squad valuations, Man Utd override added               |
| open_meteo ingestor           | ⚠️ Blocked            | Needs static stadium coords — no venue_lat/lon in DB       |
| the_odds_api ingestor         | ⚠️ Limited            | Live/upcoming only — use CSV for historical odds           |
| understat xG ingestor         | ❌ Broken             | Data now loaded dynamically, can't scrape page             |
| feature engineering           | ✅ Working            | 153 features, 73.3% non-null across 3 seasons              |
| model training                | ✅ Working            | LogisticRegression best; LightGBM needs more data          |
| strategy / Kelly backtester   | ✅ Working            | Over_under showing positive signal; 1x2 losing             |
| Streamlit dashboard           | ⬜ Not started        |                                                            |

**DB state (current):**
- matches: 1,140 (3 seasons: 2023/24, 2024/25, 2025/26)
- xg_records: 2,204 (shots proxy from CSV — HS/AS/HST/AST)
- odds_snapshots: 22,772
- squad_valuations: 677
- weather_records: 1

---

## Common Commands

Always cd first — DB path is relative:
```bash
cd /workspaces/Soccer-App-1/ss_env/SoccerSystem
```

Install dependencies from repo root:
```bash
python3 -m pip install -r requirements.txt
```

Run with correct Python and path prefix:
```bash
PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 ...
```

Row counts:
```bash
PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 - <<'PY'
from data_storage.duckdb_store import DuckDBStore
store = DuckDBStore('data/soccer_smoketest.duckdb')
for t in ['matches','teams','xg_records','odds_snapshots','squad_valuations','weather_records','player_ratings']:
    try:
        n = store.conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'{t}: {n}')
    except Exception as e:
        print(f'{t}: ERROR — {e}')
store.close()
PY
```

Re-ingest all data (run after Codespace restart):
```bash
cd /workspaces/Soccer-App-1/ss_env/SoccerSystem
export FOOTBALL_DATA_API_KEY=your_key_here
PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 - <<'PY'
import os, logging
logging.basicConfig(level=logging.INFO)
from data_storage.duckdb_store import DuckDBStore
from entity_resolution.id_mapper import EntityResolver
from data_ingestion.api_sources.football_data_org import FootballDataOrgIngestor
from data_ingestion.csv_sources.football_data_csv import FootballDataCsvIngestor

store = DuckDBStore('data/soccer_smoketest.duckdb')
resolver = EntityResolver(store)

for season in [2023, 2024, 2025]:
    print(f'\n=== {season}/{season+1} ===')
    ingestor = FootballDataOrgIngestor(api_token=os.environ['FOOTBALL_DATA_API_KEY'], resolver=resolver)
    print(ingestor.run(store, competition_codes=['PL'], season=str(season), status='FINISHED'))
    csv = FootballDataCsvIngestor(store, resolver, league='EPL', season_start_year=season)
    print(csv.run())

for t in ['matches','xg_records','odds_snapshots']:
    n = store.conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {n}')
store.close()
PY
```

Run model trainer:
```bash
PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 -m model_training.trainer 2>/dev/null
```

---

## What to Tell the AI Next Time

- **[AI NOTE]** Use `store.conn` not `store.con`
- **[AI NOTE]** Use `python3` not `python` — no python shim in this env
- **[AI NOTE]** Always `cd /workspaces/Soccer-App-1/ss_env/SoccerSystem` first — DB path is relative
- **[AI NOTE]** Base class is `Ingestor` not `BaseIngestor`
- **[AI NOTE]** Do NOT attempt `pip install understatapi` — won't build on this env
- **[AI NOTE]** football-data.org free tier only allows current season (2025); seasons 2020-2022 return 403
- **[AI NOTE]** The Odds API only serves live/upcoming odds — use football_data_csv for historical
- **[AI NOTE]** `requirements.txt` is at repo root, not inside `ss_env/SoccerSystem`
- **[AI NOTE]** `competition` not `league` column on matches table
- **[AI NOTE]** Matches table uses `home_team_canonical_id` / `away_team_canonical_id`
- **[AI NOTE]** football-data.co.uk uses short team names — TEAM_NAME_MAP in football_data_csv.py handles mapping
- **[AI NOTE]** DROP_COLS in trainer.py includes `kickoff_hour`, `is_weekend`, `matchday` to prevent leakage
- **[AI NOTE]** `pip install` flag is `--break-system-packages` on some Pythons but not all — try without flag if it fails

---

## Issue Log

### 2026-07-04

#### [FIX] DuckDB connection access errors
- **[CAUSE]** Code used `store.con`; project now uses `DuckDBStore.conn`
- **[FIX]** Added backward-compatible `con` property; updated feature engineering to use `store.conn`

#### [FIX] Python import path errors
- **[CAUSE]** Running from repo root caused `ModuleNotFoundError`
- **[FIX]** Added `sitecustomize.py` at repo root; use `PYTHONPATH=...` prefix

#### [FIX] Transfermarkt scraper breakage
- **[CAUSE]** Referenced old `BaseIngestor` class
- **[FIX]** Updated to `Ingestor` base class, added retries and timeouts
- **[VERIFY]** 677 valuations stored across 20 EPL teams

#### [FIX] API-Football ingestor breakage
- **[CAUSE]** Old base class + `store.con` usage
- **[FIX]** Updated to current `Ingestor` API and `store.conn`
- **[KNOWN LIMITATION]** Free tier blocks EPL 2025 for fixtures/injuries/player stats

---

### 2026-07-05

#### [FIXED] matches table missing canonical team ID columns
- **[CAUSE]** DDL didn't include `home_team_canonical_id`/`away_team_canonical_id`; schemas.py corrupted by another AI agent adding `from math import Math`
- **[FIX]** Added `_run_migrations()` method to DuckDBStore, fixed schemas.py, re-ran football_data_org ingestor
- **[VERIFY]** All 380 matches have canonical IDs populated

#### [BLOCKER] Weather ingestor needs static stadium coordinates
- **[CAUSE]** `matches` table has no `venue_lat`/`venue_lon` columns
- **[TODO]** Add static EPL stadium coords lookup dict to open_meteo ingestor keyed by team name

---

### 2026-07-06

#### [IMPROVEMENT] Quality checks hardened
- Added `canonical_id_presence_check` for matches
- Hardened null-rate checks against missing tables/columns
- **[VERIFY]** 13/13 checks passing

---

### 2026-07-09

#### [REFACTOR] builder.py deduplicated
- Removed dead `.replace('_', '_')` no-op in `_odds_features`
- Extracted `_clean_sel(sel)` helper
- Cleaned up `_team_form` dead code
- **[VERIFY]** Feature key/value parity confirmed; odds keys unchanged

#### [REFACTOR] trainer.py deduplicated
- Extracted `_multiclass_brier(y_true, proba)` helper
- Extracted `_prediction_rows(round_probs)` helper
- **[VERIFY]** Byte-for-byte parity on predictions.csv and metrics; all tests pass

---

### 2026-07-10–11 (model training + backtesting sessions)

#### [FIX] Model trainer sort key caused data leakage
- **[CAUSE]** `sort_values("kickoff_hour")` used time-of-day as sort proxy — `kickoff_hour` became the single most important feature (importance=1258)
- **[FIX]** Changed to `sort_values("matchday")`; added `kickoff_hour`, `is_weekend`, `matchday` to `DROP_COLS`
- **[VERIFY]** LightGBM now wins all 3 targets with correct time ordering

#### [FIX] Understat xG ingestor broken permanently
- **[CAUSE]** Understat moved team data to dynamically loaded JS bundle — not in page HTML, `getLeagueData` endpoint returns 404, `understatapi` package won't build on this env
- **[WORKAROUND]** Using shots (`HS`, `AS`, `HST`, `AST`) from football-data.co.uk CSV as xG proxy via `XGRecord`
- **[RESULT]** 760 xG proxy records per season (380 matches × 2 teams) after team name fix

#### [FIX] football_data_csv team name mismatches
- **[CAUSE]** CSV uses short names ("Man United", "Wolves", "Nott'm Forest") — fuzzy matcher couldn't bridge gap to full canonical names
- **[FIX]** Added `TEAM_NAME_MAP` dict in `football_data_csv.py` mapping short → full names before resolver call
- **[VERIFY]** Went from 220 → 760 xG records matched (110 → 380 matches linked per season)

#### [FIX] football_data_org free tier blocks historical seasons
- **[CAUSE]** API returns 403 for seasons before 2023
- **[WORKAROUND]** football-data.co.uk CSV has full match results — can build Match records from CSV for pre-2023 seasons if needed
- **[CURRENT STATE]** 3 seasons ingested (2023/24, 2024/25, 2025/26) = 1,140 matches

#### [FINDING] Model performance on 3 seasons
- LogisticRegression beats LightGBM (too little data for tree methods to shine)
- Result CV accuracy: 38.8% (vs 33% random baseline)
- Over25 test log-loss: 0.87 — best signal of the three targets
- Log-loss improving as more seasons added — worth adding more data

#### [FINDING] Backtest results — calibrated LogisticRegression + quarter Kelly
- **Over_under**: small positive signal (+£15 on 3 bets, 28.8% ROI)
- **1x2**: losing badly (-£554 on 100 bets, avg odds 3.93) — model overconfident on underdogs/draws
- **[ACTION]** Restrict live betting to over_under market only until 1x2 calibration improves
- **[ROOT CAUSE]** Only 3 seasons of data — need 5+ seasons for reliable 1x2 signal

---

## Next Session Priorities

1. **Run proper cross-season backtest** — train on 2023/24 + 2024/25, test on 2025/26 only (true out-of-sample). Script was written but session ended before output was captured.
2. **Add more historical seasons** — football-data.co.uk CSV goes back to 1990s; build Match records from CSV for 2020/21, 2021/22, 2022/23 to get 6 seasons total
3. **Fix weather ingestor** — add static stadium coords, low priority
4. **Streamlit dashboard** — final phase