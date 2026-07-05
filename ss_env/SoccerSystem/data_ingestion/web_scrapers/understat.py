"""
Understat xG ingestor.

Calls the undocumented but stable Understat league data endpoint:
    https://understat.com/getLeagueData/{league}/{season_start_year}

Response structure:
    {
      "<team_id>": {
        "id": "89",
        "title": "Manchester United",
        "history": [
          {
            "h_a": "h",           # "h" = home, "a" = away
            "xG": 1.37653,
            "xGA": 1.3304,
            "npxG": 1.37653,
            "npxGA": 1.3304,
            "ppda": {"att": 166, "def": 21},
            "ppda_allowed": {"att": 259, "def": 23},
            "date": "2025-08-17 15:30:00",
            "deep": 7,
            "deep_allowed": 3,
            "scored": 0,
            "missed": 1,
            "result": "l",
            "wins": 0,
            "draws": 0,
            "loses": 1,
            "pts": 0,
            "npxGD": 0.04613,
            "xpts": 1.3298
          },
          ...
        ]
      },
      ...
    }

Each history entry is one match for that team.  The same match appears
twice in the full payload (once for each side), so we emit two XGRecord
rows per match — one per team — which is exactly what the schema intends.

Linking to canonical matches:
    We look up the canonical match by team name + date (±12 h window) using
    the same approach as the_odds_api.py.  Matches for the requested
    league/season must already be ingested via football_data_org.py before
    running this ingestor.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any


from data_ingestion.base import Ingestor, IngestionResult
from data_normalization.schemas import XGRecord
from data_storage.duckdb_store import DuckDBStore
from entity_resolution.id_mapper import EntityResolver

logger = logging.getLogger(__name__)

# Understat league slug → football-data.org / canonical name
LEAGUE_SLUGS: dict[str, str] = {
    "EPL": "EPL",
    "La_liga": "La_liga",
    "Bundesliga": "Bundesliga",
    "Serie_A": "Serie_A",
    "Ligue_1": "Ligue_1",
    "RFPL": "RFPL",
}

# Be polite — Understat is a free community resource


class UnderstatIngestor(Ingestor):
    """
    Fetches team-level xG histories from Understat for a given
    league and season start year.

    Usage:
        ingestor = UnderstatIngestor(
            store=store,
            resolver=resolver,
            league="EPL",
            season_start_year=2025,   # for the 2025/26 season
        )
        result = ingestor.run()
    """

    source_name = "understat"

    def __init__(
        self,
        store: DuckDBStore,
        resolver: EntityResolver,
        league: str,
        season_start_year: int,
    ) -> None:
        self.store = store
        self.resolver = resolver
        if league not in LEAGUE_SLUGS:
            raise ValueError(
                f"Unknown league '{league}'. "
                f"Valid options: {list(LEAGUE_SLUGS.keys())}"
            )
        self.league = league
        self.season_start_year = season_start_year

    # ------------------------------------------------------------------
    # BaseIngestor interface
    # ------------------------------------------------------------------

    def fetch(self) -> dict[str, Any]:
        logger.info("Fetching %s %d from Understat via understatapi", self.league, self.season_start_year)
        from understatapi import UnderstatClient
        with UnderstatClient() as understat:
            data = understat.league(league=self.league).get_team_data(
                season=str(self.season_start_year)
            )
        if not isinstance(data, dict):
            raise ValueError(
                f"Unexpected response type from Understat: {type(data)}"
            )
        return data

    def transform(self, raw: dict[str, Any]) -> list[XGRecord]:
        records: list[XGRecord] = []

        for team_id_str, team_data in raw.items():
            team_name: str = team_data.get("title", "")
            history: list[dict] = team_data.get("history", [])

            if not team_name:
                logger.warning("Team entry %s missing 'title', skipping", team_id_str)
                continue

            # Resolve team → canonical_id
            team_resolution = self.resolver.resolve_team(
                source=self.source_name,
                source_id=team_id_str,
                name=team_name,
            )
            if team_resolution is None:
                logger.warning(
                    "Could not resolve team '%s' (id=%s), skipping its history",
                    team_name,
                    team_id_str,
                )
                continue

            team_canonical_id = team_resolution.canonical_id

            for entry in history:
                match_canonical_id = self._resolve_match(
                    team_canonical_id=team_canonical_id,
                    team_name=team_name,
                    entry=entry,
                )
                if match_canonical_id is None:
                    continue

                record = XGRecord(
                    source=self.source_name,
                    match_canonical_id=match_canonical_id,
                    team_canonical_id=team_canonical_id,
                    xg=_safe_float(entry.get("xG")),
                    npxg=_safe_float(entry.get("npxG")),
                    xga=_safe_float(entry.get("xGA")),
                    # Understat history entries don't include shot counts
                    # at the team-history level; leave as None.
                    shots=None,
                    shots_on_target=None,
                    possession_pct=None,
                )
                records.append(record)

        return records

    def validate_raw(self, raw: dict[str, Any]) -> bool:
        if not raw:
            logger.error("Understat returned empty payload")
            return False
        # Spot-check: every value should have 'title' and 'history'
        sample = next(iter(raw.values()))
        if "title" not in sample or "history" not in sample:
            logger.error(
                "Unexpected Understat payload shape; keys found: %s",
                list(sample.keys()),
            )
            return False
        return True

    def run(self) -> IngestionResult:
        errors: list[str] = []
        records_fetched = 0
        records_stored = 0

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
            xg_records = self.transform(raw)
            records_fetched = len(xg_records)
        except Exception as exc:
            msg = f"transform() failed: {exc}"
            logger.error(msg)
            return IngestionResult(
                source=self.source_name,
                records_fetched=0,
                records_stored=0,
                errors=[msg],
            )

        if xg_records:
            try:
                self.store.upsert_models(xg_records)
                records_stored = len(xg_records)
            except Exception as exc:
                msg = f"upsert_models() failed: {exc}"
                logger.error(msg)
                errors.append(msg)

        return IngestionResult(
            source=self.source_name,
            records_fetched=records_fetched,
            records_stored=records_stored,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_match(
        self,
        team_canonical_id: str,
        team_name: str,
        entry: dict,
    ) -> str | None:
        """
        Find the canonical match ID for a history entry.

        Strategy: query DuckDB for a match involving this team within
        a ±12-hour window of the Understat kickoff timestamp.
        """
        date_str: str = entry.get("date", "")
        if not date_str:
            logger.warning(
                "History entry for '%s' missing 'date', skipping", team_name
            )
            return None

        try:
            kickoff_naive = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
            # Understat dates are in UK local time (Europe/London).
            # For simplicity treat as UTC — the ±12 h window absorbs the offset.
            kickoff_utc = kickoff_naive.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(
                "Could not parse date '%s' for team '%s', skipping",
                date_str,
                team_name,
            )
            return None

        window_start = kickoff_utc - timedelta(hours=12)
        window_end = kickoff_utc + timedelta(hours=12)

        con = self.store.conn
        result = con.execute(
            """
            SELECT canonical_id
            FROM matches
            WHERE
                (home_team_canonical_id = ? OR away_team_canonical_id = ?)
                AND kickoff_utc BETWEEN ? AND ?
            LIMIT 1
            """,
            [
                team_canonical_id,
                team_canonical_id,
                window_start.isoformat(),
                window_end.isoformat(),
            ],
        ).fetchone()

        if result is None:
            logger.warning(
                "No canonical match found for team '%s' (canonical_id=%s) "
                "around %s — ingest matches first.",
                team_name,
                team_canonical_id,
                date_str,
            )
            return None

        return result[0]


# ------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------

def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None