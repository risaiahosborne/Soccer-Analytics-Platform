"""
football-data.co.uk historical odds ingestor.

Downloads the season CSV and maps pre-match and closing odds for
1X2 and Over/Under 2.5 to OddsSnapshot records.

CSV URL pattern:
    https://www.football-data.co.uk/mmz4281/{season}/{league}.csv

    season:  "2526" for 2025/26, "2425" for 2024/25, etc.
    league:  "E0" = EPL, "E1" = Championship, "SP1" = La Liga, etc.

Key columns used:
    HomeTeam, AwayTeam, Date, Time
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

Each CSV row produces up to 16 OddsSnapshot records (2 bookmakers ×
4 markets × 2 timings [opening/closing]).
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
import pandas as pd

from data_ingestion.base import Ingestor, IngestionResult
from data_normalization.schemas import OddsSnapshot
from data_storage.duckdb_store import DuckDBStore
from entity_resolution.id_mapper import EntityResolver

logger = logging.getLogger(__name__)

# League CSV file codes
LEAGUE_CSV_CODES: dict[str, str] = {
    "EPL":        "E0",
    "Championship": "E1",
    "La_liga":    "SP1",
    "Bundesliga": "D1",
    "Serie_A":    "I1",
    "Ligue_1":    "F1",
}

BASE_URL = "https://www.football-data.co.uk/mmz4281"


def _season_code(season_start_year: int) -> str:
    """2025 → '2526'"""
    y1 = str(season_start_year)[2:]
    y2 = str(season_start_year + 1)[2:]
    return f"{y1}{y2}"


class FootballDataCsvIngestor(Ingestor):
    """
    Ingests historical odds from football-data.co.uk CSV files.

    Usage:
        ingestor = FootballDataCsvIngestor(
            store=store,
            resolver=resolver,
            league="EPL",
            season_start_year=2025,
        )
        result = ingestor.run()
    """

    source_name = "football-data-csv"

    def __init__(
        self,
        store: DuckDBStore,
        resolver: EntityResolver,
        league: str,
        season_start_year: int,
    ) -> None:
        self.store = store
        self.resolver = resolver
        if league not in LEAGUE_CSV_CODES:
            raise ValueError(
                f"Unknown league '{league}'. "
                f"Valid options: {list(LEAGUE_CSV_CODES.keys())}"
            )
        self.league = league
        self.season_start_year = season_start_year
        self.csv_code = LEAGUE_CSV_CODES[league]
        self.season_code = _season_code(season_start_year)

    # ------------------------------------------------------------------
    # Ingestor interface
    # ------------------------------------------------------------------

    def fetch(self) -> pd.DataFrame:
        url = f"{BASE_URL}/{self.season_code}/{self.csv_code}.csv"
        logger.info("Downloading %s", url)
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        # football-data CSVs are latin-1 encoded
        df = pd.read_csv(io.StringIO(resp.content.decode("latin-1")))
        logger.info("Downloaded %d rows, %d columns", len(df), len(df.columns))
        return df

    def validate_raw(self, raw: pd.DataFrame) -> bool:
        if raw.empty:
            logger.error("CSV is empty")
            return False
        required = {"HomeTeam", "AwayTeam", "Date"}
        missing = required - set(raw.columns)
        if missing:
            logger.error("CSV missing required columns: %s", missing)
            return False
        return True

    def transform(self, raw: pd.DataFrame) -> list[OddsSnapshot]:
        snapshots: list[OddsSnapshot] = []

        for _, row in raw.iterrows():
            kickoff_utc = _parse_kickoff(row)
            if kickoff_utc is None:
                continue

            home_name = str(row.get("HomeTeam", "")).strip()
            away_name = str(row.get("AwayTeam", "")).strip()
            if not home_name or not away_name:
                continue

            # Resolve teams → canonical match
            home_res = self.resolver.resolve_team(
                source=self.source_name,
                source_id=f"fdcsv_{home_name.lower().replace(' ', '_')}",
                name=home_name,
            )
            away_res = self.resolver.resolve_team(
                source=self.source_name,
                source_id=f"fdcsv_{away_name.lower().replace(' ', '_')}",
                name=away_name,
            )

            home_canonical = home_res.canonical_id if home_res else None
            away_canonical = away_res.canonical_id if away_res else None

            match_canonical_id = self._resolve_match(
                home_canonical, away_canonical, kickoff_utc
            )
            if match_canonical_id is None:
                logger.debug(
                    "No match found for %s vs %s on %s",
                    home_name, away_name, kickoff_utc.date()
                )
                continue

            # Build snapshots for each bookmaker × market × timing
            snapshots.extend(
                _build_snapshots(row, match_canonical_id, kickoff_utc)
            )

        return snapshots

    def run(self) -> IngestionResult:
        errors: list[str] = []

        try:
            raw = self.fetch()
        except Exception as exc:
            msg = f"fetch() failed: {exc}"
            logger.error(msg)
            return IngestionResult(
                source=self.source_name,
                records_fetched=0,
                records_stored=0,
                errors=[msg],
            )

        if not self.validate_raw(raw):
            return IngestionResult(
                source=self.source_name,
                records_fetched=0,
                records_stored=0,
                errors=["validate_raw() failed"],
            )

        try:
            snapshots = self.transform(raw)
        except Exception as exc:
            msg = f"transform() failed: {exc}"
            logger.error(msg)
            return IngestionResult(
                source=self.source_name,
                records_fetched=0,
                records_stored=0,
                errors=[msg],
            )

        records_stored = 0
        if snapshots:
            try:
                self.store.upsert_models(snapshots)
                records_stored = len(snapshots)
            except Exception as exc:
                msg = f"upsert_models() failed: {exc}"
                logger.error(msg)
                errors.append(msg)

        return IngestionResult(
            source=self.source_name,
            records_fetched=len(snapshots),
            records_stored=records_stored,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_match(
        self,
        home_canonical: str | None,
        away_canonical: str | None,
        kickoff_utc: datetime,
    ) -> str | None:
        if not home_canonical or not away_canonical:
            return None

        window_start = kickoff_utc - timedelta(hours=20)
        window_end = kickoff_utc + timedelta(hours=20)

        result = self.store.conn.execute(
            """
            SELECT canonical_id FROM matches
            WHERE home_team_canonical_id = ?
              AND away_team_canonical_id = ?
              AND kickoff_utc BETWEEN ? AND ?
            LIMIT 1
            """,
            [
                home_canonical,
                away_canonical,
                window_start.isoformat(),
                window_end.isoformat(),
            ],
        ).fetchone()

        return result[0] if result else None


# ------------------------------------------------------------------
# Snapshot builders
# ------------------------------------------------------------------

def _build_snapshots(
    row: pd.Series,
    match_canonical_id: str,
    kickoff_utc: datetime,
) -> list[OddsSnapshot]:
    """Build OddsSnapshot records from one CSV row."""
    snaps: list[OddsSnapshot] = []

    # Opening: captured ~3 days before kickoff (approximation)
    opening_time = kickoff_utc - timedelta(days=3)
    # Closing: captured 1 hour before kickoff
    closing_time = kickoff_utc - timedelta(hours=1)

    # --- 1X2 markets ---
    for bookmaker, h_col, d_col, a_col, ch_col, cd_col, ca_col in [
        ("bet365",  "B365H",  "B365D",  "B365A",  "B365CH",  "B365CD",  "B365CA"),
        ("average", "AvgH",   "AvgD",   "AvgA",   "AvgCH",   "AvgCD",   "AvgCA"),
    ]:
        for col, sel, timing, captured_at, is_open, is_close in [
            (h_col,  "home",  "opening", opening_time, True,  False),
            (d_col,  "draw",  "opening", opening_time, True,  False),
            (a_col,  "away",  "opening", opening_time, True,  False),
            (ch_col, "home",  "closing", closing_time, False, True),
            (cd_col, "draw",  "closing", closing_time, False, True),
            (ca_col, "away",  "closing", closing_time, False, True),
        ]:
            price = _safe_float(row.get(col))
            if price is None or price <= 1.0:
                continue
            snaps.append(OddsSnapshot(
                source="football-data-csv",
                match_canonical_id=match_canonical_id,
                bookmaker=bookmaker,
                market="1x2",
                selection=sel,
                price_decimal=price,
                captured_at=captured_at,
                is_opening=is_open,
                is_closing=is_close,
            ))

    # --- Over/Under 2.5 markets ---
    for bookmaker, o_col, u_col, co_col, cu_col in [
        ("bet365",  "B365>2.5", "B365<2.5", "B365C>2.5", "B365C<2.5"),
        ("average", "Avg>2.5",  "Avg<2.5",  "AvgC>2.5",  "AvgC<2.5"),
    ]:
        for col, sel, captured_at, is_open, is_close in [
            (o_col,  "over_2.5",  opening_time, True,  False),
            (u_col,  "under_2.5", opening_time, True,  False),
            (co_col, "over_2.5",  closing_time, False, True),
            (cu_col, "under_2.5", closing_time, False, True),
        ]:
            price = _safe_float(row.get(col))
            if price is None or price <= 1.0:
                continue
            snaps.append(OddsSnapshot(
                source="football-data-csv",
                match_canonical_id=match_canonical_id,
                bookmaker=bookmaker,
                market="over_under",
                selection=sel,
                price_decimal=price,
                captured_at=captured_at,
                is_opening=is_open,
                is_closing=is_close,
            ))

    return snaps


def _parse_kickoff(row: pd.Series) -> datetime | None:
    date_str = str(row.get("Date", "")).strip()
    time_str = str(row.get("Time", "00:00")).strip()
    if not date_str or date_str == "nan":
        return None
    try:
        # football-data uses DD/MM/YY format
        dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%y %H:%M")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return f if not pd.isna(f) else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    import logging
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from data_storage.duckdb_store import DuckDBStore
    from entity_resolution.id_mapper import EntityResolver

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
    store.close()