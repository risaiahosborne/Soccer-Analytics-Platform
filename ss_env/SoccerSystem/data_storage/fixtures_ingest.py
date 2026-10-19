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
        odds_home: float,
        odds_draw: float,
        odds_away: float,
        odds_btts_yes: float,
        odds_btts_no: float,
        odds_over25: float,
        odds_under25: float,
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
                odds_home,
                odds_draw,
                odds_away,
                odds_btts_yes,
                odds_btts_no,
                odds_over25,
                odds_under25
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                fixture_id,
                competition,
                season,
                kickoff_utc,
                home_team_id,
                away_team_id,
                odds_home,
                odds_draw,
                odds_away,
                odds_btts_yes,
                odds_btts_no,
                odds_over25,
                odds_under25,
            ],
        )
    def get_upcoming_fixtures(self):
        return self.con.execute(
            """
            SELECT *
            FROM future_fixtures
            ORDER BY kickoff_utc
            """
        ).fetchdf()
