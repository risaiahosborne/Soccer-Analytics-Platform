"""
football-data.co.uk historical match + odds ingestor.

Downloads a season CSV from football-data.co.uk and normalizes:
    • Match records (fixture metadata + final score)
    • Pre‑match and closing odds (Bet365 + market averages)
    • Basic shot-based proxy xG records (HS/HST/AS/AST)

This ingestor now inserts full Match rows into the normalized `matches`
table. Canonical match IDs are created and managed exclusively by the
EntityResolver (via id_mappings), ensuring cross‑source consistency.
Ingestors DO NOT write canonical_id fields directly.

CSV URL pattern:
    https://www.football-data.co.uk/mmz4281/{season}/{league}.csv

    season:  "2526" for 2025/26, "2425" for 2024/25, etc.
    league:  "E0" = EPL, "E1" = Championship, "SP1" = La Liga, etc.

Key columns used:
    HomeTeam, AwayTeam, Date, Time
    FTHG, FTAG (final score)
    HS, HST, AS, AST (shot-based proxy xG inputs)

Odds columns:
    --- opening odds ---
    B365H, B365D, B365A          Bet365 1X2
    B365>2.5, B365<2.5           Bet365 over/under 2.5
    AvgH, AvgD, AvgA             Market average 1X2
    Avg>2.5, Avg<2.5             Market average over/under 2.5

    --- closing odds ---
    B365CH, B365CD, B365CA       Bet365 closing 1X2
    B365C>2.5, B365C<2.5         Bet365 closing over/under 2.5
    AvgCH, AvgCD, AvgCA          Market average closing 1X2
    AvgC>2.5, AvgC<2.5           Market average closing over/under 2.5

Match identity:
    Each fixture is assigned a stable `source_id` of the form:
        fdcsv_{home}_{away}_{YYYYMMDD}

    The EntityResolver maps this (source, source_id) to a canonical_id
    stored in `id_mappings`. The canonical_id is NOT written into the
    `matches` table; downstream joins use id_mappings instead.

Odds snapshots:
    Each CSV row produces up to 16 OddsSnapshot records:
        2 bookmakers × 4 markets × 2 timings (opening/closing)

XG proxy records:
    Two XGRecord rows per match (home + away), using shot counts only.

This module produces three normalized tables:
    • matches
    • odds_snapshots
    • xg_records
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import pandas as pd

from data_ingestion.base import Ingestor, IngestionResult
from data_normalization.schemas import OddsSnapshot, XGRecord, Match, Team
from data_storage.duckdb_store import DuckDBStore
from entity_resolution.id_mapper import EntityResolver, normalize_name

logger = logging.getLogger(__name__)

LEAGUE_CSV_CODES = {
    "EPL": "E0",
    "Championship": "E1",
    "La_liga": "SP1",
    "Bundesliga": "D1",
    "Serie_A": "I1",
    "Ligue_1": "F1",
}

BASE_URL = "https://www.football-data.co.uk/mmz4281"

TEAM_NAME_MAP = {
    "Man United": "Manchester United FC",
    "Man City": "Manchester City FC",
    "Newcastle": "Newcastle United FC",
    "Spurs": "Tottenham Hotspur FC",
    "Tottenham": "Tottenham Hotspur FC",
    "Wolves": "Wolverhampton Wanderers FC",
    "West Ham": "West Ham United FC",
    "Nott'm Forest": "Nottingham Forest FC",
    "Brighton": "Brighton & Hove Albion FC",
    "Leicester": "Leicester City FC",
    "Leeds": "Leeds United FC",
    "Aston Villa": "Aston Villa FC",
    "Arsenal": "Arsenal FC",
    "Chelsea": "Chelsea FC",
    "Liverpool": "Liverpool FC",
    "Everton": "Everton FC",
    "Fulham": "Fulham FC",
    "Brentford": "Brentford FC",
    "Burnley": "Burnley FC",
    "Sunderland": "Sunderland AFC",
    "Bournemouth": "AFC Bournemouth",
    "Crystal Palace": "Crystal Palace FC",
    "Southampton": "Southampton FC",
    "Ipswich": "Ipswich Town FC",
    "Sheffield United": "Sheffield United FC",
    "Luton": "Luton Town FC",
}

def make_short_name(full_name: str) -> str:
    return (
        full_name.replace(" FC", "")
                 .replace("AFC", "")
                 .replace("United", "Utd")
                 .strip()
    )

def make_team_code(full_name: str) -> str:
    return normalize_name(full_name)[:3].upper()

def _season_code(year: int) -> str:
    return f"{str(year)[2:]}{str(year+1)[2:]}"


class FootballDataCsvIngestor(Ingestor):
    source_name = "football-data-csv"

    def __init__(self, store: DuckDBStore, resolver: EntityResolver, league: str, season_start_year: int):
        self.store = store
        self.resolver = resolver
        if league not in LEAGUE_CSV_CODES:
            raise ValueError(f"Unknown league '{league}'")
        self.league = league
        self.season_start_year = season_start_year
        self.csv_code = LEAGUE_CSV_CODES[league]
        self.season_code = _season_code(season_start_year)

    # ----------------------------------------------------------
    # FETCH
    # ----------------------------------------------------------
    def fetch(self) -> pd.DataFrame:
        url = f"{BASE_URL}/{self.season_code}/{self.csv_code}.csv"
        logger.info("Downloading %s", url)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.content.decode("latin-1")))
        logger.info("Downloaded %d rows, %d columns", len(df), len(df.columns))
        return df

    # ----------------------------------------------------------
    # VALIDATE
    # ----------------------------------------------------------
    def validate_raw(self, raw: pd.DataFrame) -> bool:
        required = {"HomeTeam", "AwayTeam", "Date"}
        missing = required - set(raw.columns)
        if missing:
            logger.error("CSV missing required columns: %s", missing)
            return False
        return True

    # ----------------------------------------------------------
    # TRANSFORM
    # ----------------------------------------------------------
    def transform(self, raw: pd.DataFrame):
        snapshots = []
        xg_records = []
        out = []  # NEW: includes Team + Match models

        for _, row in raw.iterrows():
            kickoff_utc = _parse_kickoff(row)
            if kickoff_utc is None:
                continue

            home_name = TEAM_NAME_MAP.get(str(row.get("HomeTeam", "")).strip(), None)
            away_name = TEAM_NAME_MAP.get(str(row.get("AwayTeam", "")).strip(), None)
            if not home_name or not away_name:
                continue

            # ----------------------------------------------------------
            # Resolve teams
            # ----------------------------------------------------------
            home_res = self.resolver.resolve_team(
                source=self.source_name,
                source_id=f"fdcsv_{normalize_name(home_name)}",
                name=home_name,
                country="England",
                league="Premier League",
            )
            away_res = self.resolver.resolve_team(
                source=self.source_name,
                source_id=f"fdcsv_{normalize_name(away_name)}",
                name=away_name,
                country="England",
                league="Premier League",
            )

            # ----------------------------------------------------------
            # NEW: Enriched Team metadata (Option A)
            # ----------------------------------------------------------
            home_team = Team(
                canonical_id=home_res.canonical_id,
                display_name=home_name,
                short_name=make_short_name(home_name),
                normalized_name=normalize_name(home_name),
                country="England",
                league="Premier League",
                team_code=make_team_code(home_name),
                stadium=None,
                city=None,
            )

            away_team = Team(
                canonical_id=away_res.canonical_id,
                display_name=away_name,
                short_name=make_short_name(away_name),
                normalized_name=normalize_name(away_name),
                country="England",
                league="Premier League",
                team_code=make_team_code(away_name),
                stadium=None,
                city=None,
            )

            out.append(home_team)
            out.append(away_team)

            # ----------------------------------------------------------
            # Match creation
            # ----------------------------------------------------------
            match_source_id = (
                f"fdcsv_{normalize_name(home_name)}_"
                f"{normalize_name(away_name)}_"
                f"{kickoff_utc.strftime('%Y%m%d')}"
            )

            match_canonical_id = self.resolver.resolve_match(
                source=self.source_name,
                source_id=match_source_id,
            )

            match = Match(
                canonical_id=match_canonical_id,
                source=self.source_name,
                source_id=match_source_id,
                competition="Premier League",
                season=f"{self.season_start_year}/{self.season_start_year+1}",
                kickoff_utc=kickoff_utc,
                status="finished",
                home_team_source_id=f"fdcsv_{normalize_name(home_name)}",
                away_team_source_id=f"fdcsv_{normalize_name(away_name)}",
                home_team_canonical_id=home_res.canonical_id,
                away_team_canonical_id=away_res.canonical_id,
                home_score=_safe_int(row.get("FTHG")),
                away_score=_safe_int(row.get("FTAG")),
                venue_name=None,
                referee=None,
                matchday=_safe_int(row.get("MW")),
                ingested_at=datetime.utcnow(),
            )

            out.append(match)

            # ----------------------------------------------------------
            # Odds snapshots
            # ----------------------------------------------------------
            snapshots.extend(_build_snapshots(row, match_canonical_id, kickoff_utc))

            # ----------------------------------------------------------
            # XG proxy records
            # ----------------------------------------------------------
            xg_records.append(XGRecord(
                source=self.source_name,
                match_canonical_id=match_canonical_id,
                team_canonical_id=home_res.canonical_id,
                xg=None, npxg=None, xga=None,
                shots=_safe_int(row.get("HS")),
                shots_on_target=_safe_int(row.get("HST")),
                possession_pct=None,
            ))

            xg_records.append(XGRecord(
                source=self.source_name,
                match_canonical_id=match_canonical_id,
                team_canonical_id=away_res.canonical_id,
                xg=None, npxg=None, xga=None,
                shots=_safe_int(row.get("AS")),
                shots_on_target=_safe_int(row.get("AST")),
                possession_pct=None,
            ))

        return out, snapshots, xg_records

    # ----------------------------------------------------------
    # RUN
    # ----------------------------------------------------------
    def run(self) -> IngestionResult:
        try:
            raw = self.fetch()
        except Exception as exc:
            return IngestionResult(self.source_name, 0, 0, [f"fetch() failed: {exc}"])

        if not self.validate_raw(raw):
            return IngestionResult(self.source_name, 0, 0, ["validate_raw() failed"])

        try:
            teams_and_matches, snapshots, xg_records = self.transform(raw)
        except Exception as exc:
            return IngestionResult(self.source_name, 0, 0, [f"transform() failed: {exc}"])

        stored = 0
        errors = []

        try:
            stored += self.store.upsert_models(teams_and_matches)
            stored += self.store.upsert_models(snapshots)
            stored += self.store.upsert_models(xg_records)
        except Exception as exc:
            errors.append(f"store() failed: {exc}")

        return IngestionResult(
            source=self.source_name,
            records_fetched=len(raw),
            records_stored=stored,
            errors=errors,
        )


# ----------------------------------------------------------
# HELPERS
# ----------------------------------------------------------
def _build_snapshots(row: pd.Series, match_canonical_id: str, kickoff_utc: datetime) -> list[OddsSnapshot]:
    snaps = []
    opening_time = kickoff_utc - timedelta(days=3)
    closing_time = kickoff_utc - timedelta(hours=1)

    # 1X2 markets
    for bookmaker, h_col, d_col, a_col, ch_col, cd_col, ca_col in [
        ("bet365",  "B365H",  "B365D",  "B365A",  "B365CH",  "B365CD",  "B365CA"),
        ("average", "AvgH",   "AvgD",   "AvgA",   "AvgCH",   "AvgCD",   "AvgCA"),
    ]:
        selections = [
            ("home", _safe_float(row.get(h_col)),  opening_time, True,  False),
            ("draw", _safe_float(row.get(d_col)),  opening_time, True,  False),
            ("away", _safe_float(row.get(a_col)),  opening_time, True,  False),
            ("home", _safe_float(row.get(ch_col)), closing_time, False, True),
            ("draw", _safe_float(row.get(cd_col)), closing_time, False, True),
            ("away", _safe_float(row.get(ca_col)), closing_time, False, True),
        ]
        snaps.extend(_extract_market_snapshots(match_canonical_id, bookmaker, "1x2", selections))

    # Over/Under 2.5 markets
    for bookmaker, o_col, u_col, co_col, cu_col in [
        ("bet365",  "B365>2.5", "B365<2.5", "B365C>2.5", "B365C<2.5"),
        ("average", "Avg>2.5",  "Avg<2.5",  "AvgC>2.5",  "AvgC<2.5"),
    ]:
        selections = [
            ("over_2.5",  _safe_float(row.get(o_col)),  opening_time, True,  False),
            ("under_2.5", _safe_float(row.get(u_col)),  opening_time, True,  False),
            ("over_2.5",  _safe_float(row.get(co_col)), closing_time, False, True),
            ("under_2.5", _safe_float(row.get(cu_col)), closing_time, False, True),
        ]
        snaps.extend(_extract_market_snapshots(match_canonical_id, bookmaker, "over_under", selections))

    return snaps


def _extract_market_snapshots(
    match_canonical_id: str,
    bookmaker: str,
    market: str,
    selections: list[tuple[str, float, datetime, bool, bool]],
) -> list[OddsSnapshot]:
    out = []
    for sel_name, price, captured_at, is_open, is_close in selections:
        if price is None or price <= 1.0:
            continue
        out.append(OddsSnapshot(
            source="football-data-csv",
            match_canonical_id=match_canonical_id,
            bookmaker=bookmaker,
            market=market,
            selection=sel_name,
            price_decimal=price,
            captured_at=captured_at,
            is_opening=is_open,
            is_closing=is_close,
        ))
    return out


def _parse_kickoff(row: pd.Series) -> datetime | None:
    date_str = str(row.get("Date", "")).strip()
    time_str = str(row.get("Time", "00:00")).strip()
    if not date_str:
        return None
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%y %H:%M")
    except ValueError:
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M")
        except ValueError:
            return None
    return dt.replace(tzinfo=timezone.utc)


def _safe_int(value: Any) -> int | None:
    try:
        f = float(value)
        return None if pd.isna(f) else int(f)
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        f = float(value)
        return f if not pd.isna(f) else None
    except Exception:
        return None

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    store = DuckDBStore("data/soccer_smoketest.duckdb")
    resolver = EntityResolver(store)

    ingestor = FootballDataCsvIngestor(
        store=store,
        resolver=resolver,
        league="EPL",
        season_start_year=2025,
    )

    result = ingestor.run()
    print(result)
    print("Total odds snapshots:", store.table_row_count("odds_snapshots"))
    print("Total xG records:", store.table_row_count("xg_records"))
    print("Total matches:", store.table_row_count("matches"))

    store.close()
