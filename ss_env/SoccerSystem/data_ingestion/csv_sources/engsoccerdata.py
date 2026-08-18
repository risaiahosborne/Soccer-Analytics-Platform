from __future__ import annotations

import io
import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from data_ingestion.base import Ingestor, IngestionResult
from data_normalization.schemas import Match, MatchStatus, Team
from data_storage.duckdb_store import DuckDBStore
from entity_resolution.id_mapper import EntityResolver, normalize_name

logger = logging.getLogger(__name__)

CSV_URL = "https://raw.githubusercontent.com/jalapic/engsoccerdata/master/data-raw/england.csv"

# Map jalapic team names → canonical full names
TEAM_NAME_MAP: dict[str, str] = {
    "Manchester United":        "Manchester United FC",
    "Manchester City":          "Manchester City FC",
    "Arsenal":                  "Arsenal FC",
    "Chelsea":                  "Chelsea FC",
    "Liverpool":                "Liverpool FC",
    "Tottenham Hotspur":        "Tottenham Hotspur FC",
    "Everton":                  "Everton FC",
    "Leicester City":           "Leicester City FC",
    "West Ham United":          "West Ham United FC",
    "Aston Villa":              "Aston Villa FC",
    "Leeds United":             "Leeds United FC",
    "Newcastle United":         "Newcastle United FC",
    "Wolverhampton Wanderers":  "Wolverhampton Wanderers FC",
    "Crystal Palace":           "Crystal Palace FC",
    "Southampton":              "Southampton FC",
    "Brighton & Hove Albion":   "Brighton & Hove Albion FC",
    "Burnley":                  "Burnley FC",
    "Fulham":                   "Fulham FC",
    "Brentford":                "Brentford FC",
    "Nottingham Forest":        "Nottingham Forest FC",
    "Bournemouth":              "AFC Bournemouth",
    "West Bromwich Albion":     "West Bromwich Albion FC",
    "Sheffield United":         "Sheffield United FC",
    "Norwich City":             "Norwich City FC",
    "Watford":                  "Watford FC",
    "Luton Town":               "Luton Town FC",
    "Ipswich Town":             "Ipswich Town FC",
    "Sunderland":               "Sunderland AFC",
}

STATUS_MAP = {
    "H": MatchStatus.FINISHED,
    "D": MatchStatus.FINISHED,
    "A": MatchStatus.FINISHED,
}

def make_short_name(full_name: str) -> str:
    """Generate a readable short name."""
    return (
        full_name.replace(" FC", "")
                 .replace("AFC", "")
                 .replace("United", "Utd")
                 .strip()
    )

def make_team_code(full_name: str) -> str:
    """Generate a simple 3-letter team code."""
    return normalize_name(full_name)[:3].upper()


class EngSoccerDataIngestor(Ingestor):
    """
    Ingests historical EPL match results from jalapic/engsoccerdata.
    Stores Match records AND enriched Team metadata (Option A).
    """

    source_name = "engsoccerdata"

    def __init__(
        self,
        store: DuckDBStore,
        resolver: EntityResolver,
        seasons: list[int],
    ) -> None:
        self.store = store
        self.resolver = resolver
        self.seasons = seasons

    # ------------------------------------------------------------
    # FETCH
    # ------------------------------------------------------------
    def fetch(self) -> pd.DataFrame:
        logger.info("Downloading engsoccerdata from GitHub...")
        resp = requests.get(CSV_URL, timeout=60)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text), low_memory=False)

        df = df[(df["tier"] == 1) & (df["Season"].isin(self.seasons))].copy()
        logger.info("Filtered to %d matches for seasons %s", len(df), self.seasons)
        return df

    # ------------------------------------------------------------
    # VALIDATE
    # ------------------------------------------------------------
    def validate_raw(self, raw: pd.DataFrame) -> bool:
        required = {"Date", "Season", "home", "visitor", "hgoal", "vgoal", "result"}
        missing = required - set(raw.columns)
        if missing:
            logger.error("Missing columns: %s", missing)
            return False
        return True

    # ------------------------------------------------------------
    # TRANSFORM
    # ------------------------------------------------------------
    def transform(self, raw: pd.DataFrame) -> list[Any]:
        out: list[Any] = []

        for _, row in raw.iterrows():
            # Parse date
            try:
                date_str = str(row["Date"]).strip()
                kickoff = datetime.strptime(date_str, "%Y-%m-%d").replace(
                    hour=15, minute=0, tzinfo=timezone.utc
                )
            except ValueError:
                logger.debug("Could not parse date: %s", row.get("Date"))
                continue

            season_year = int(row["Season"])
            season_str = f"{season_year}/{season_year + 1}"

            # Resolve team names
            home_raw = str(row["home"]).strip()
            away_raw = str(row["visitor"]).strip()

            home_name = TEAM_NAME_MAP.get(home_raw, home_raw)
            away_name = TEAM_NAME_MAP.get(away_raw, away_raw)

            # Resolve canonical team IDs
            home_res = self.resolver.resolve_team(
                source=self.source_name,
                source_id=f"engs_{normalize_name(home_name)}",
                name=home_name,
                country="England",
                league="Premier League",
            )
            away_res = self.resolver.resolve_team(
                source=self.source_name,
                source_id=f"engs_{normalize_name(away_name)}",
                name=away_name,
                country="England",
                league="Premier League",
            )

            # ------------------------------------------------------------
            # NEW: Enriched Team metadata (Option A)
            # ------------------------------------------------------------
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

            # ------------------------------------------------------------
            # Match creation
            # ------------------------------------------------------------
            source_id = f"{home_raw}_{away_raw}_{date_str}".replace(" ", "_")

            match_canonical_id = self.resolver.resolve_match(
                source=self.source_name,
                source_id=source_id,
            )

            home_score = int(row["hgoal"]) if pd.notna(row["hgoal"]) else None
            away_score = int(row["vgoal"]) if pd.notna(row["vgoal"]) else None

            match = Match(
                canonical_id=match_canonical_id,
                source=self.source_name,
                source_id=source_id,
                competition="Premier League",
                season=season_str,
                kickoff_utc=kickoff,
                status=MatchStatus.FINISHED,
                home_team_source_id=f"engs_{normalize_name(home_name)}",
                away_team_source_id=f"engs_{normalize_name(away_name)}",
                home_team_canonical_id=home_res.canonical_id,
                away_team_canonical_id=away_res.canonical_id,
                home_score=home_score,
                away_score=away_score,
                matchday=None,
            )

            out.append(match)

        return out

    # ------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------
    def run(self) -> IngestionResult:
        try:
            raw = self.fetch()
        except Exception as exc:
            msg = f"fetch() failed: {exc}"
            logger.error(msg)
            return IngestionResult(self.source_name, 0, 0, [msg])

        if not self.validate_raw(raw):
            return IngestionResult(self.source_name, 0, 0, ["validate_raw() failed"])

        try:
            models = self.transform(raw)
        except Exception as exc:
            msg = f"transform() failed: {exc}"
            logger.error(msg)
            return IngestionResult(self.source_name, 0, 0, [msg])

        stored = 0
        errors: list[str] = []
        try:
            stored = self.store.upsert_models(models)
        except Exception as exc:
            msg = f"store() failed: {exc}"
            logger.error(msg)
            errors.append(msg)

        return IngestionResult(
            source=self.source_name,
            records_fetched=len(raw),
            records_stored=stored,
            errors=errors,
        )

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import logging
    logging.basicConfig(level=logging.INFO)

    from data_storage.duckdb_store import DuckDBStore
    from entity_resolution.id_mapper import EntityResolver

    store = DuckDBStore("data/soccer_smoketest.duckdb")
    resolver = EntityResolver(store)
    ingestor = EngSoccerDataIngestor(store, resolver, seasons=[2020, 2021])
    result = ingestor.run()
    print(result)
    rows = store.conn.execute(
        "SELECT season, COUNT(*) FROM matches GROUP BY season ORDER BY season"
    ).fetchall()
    for r in rows:
        print(f"  {r[0]}: {r[1]} matches")
    store.close()