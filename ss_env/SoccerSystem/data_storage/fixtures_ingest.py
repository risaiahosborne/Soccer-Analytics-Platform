from datetime import datetime
from ss_env.SoccerSystem.data_storage.duckdb_store import DuckDBStore

class FutureFixturesIngestor:
    def __init__(self, store: DuckDBStore):
        self.con = store.conn

    def add_fixture(
        self,
        fixture_id: str,
        competition: str,
        season: str,
        kickoff_utc: datetime,
        home_team_id: str,
        away_team_id: str,
        venue: str | None = None,
        referee: str | None = None,
        round: int | None = None,
    ):
        self.con.execute(
            """
            INSERT OR REPLACE INTO future_fixtures (
                fixture_id,
                competition,
                season,
                kickoff_utc,
                home_team_canonical_id,
                away_team_canonical_id,
                venue,
                referee,
                round,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled')
            """,
            [
                fixture_id,
                competition,
                season,
                kickoff_utc,
                home_team_id,
                away_team_id,
                venue,
                referee,
                round,
            ],
        )

    def get_upcoming_fixtures(self):
        return self.con.execute(
            """
            SELECT *
            FROM future_fixtures
            WHERE status = 'scheduled'
            ORDER BY kickoff_utc
            """
        ).fetchdf()
