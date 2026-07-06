from pathlib import Path

from data_storage.duckdb_store import DuckDBStore
from quality_checks.checks import canonical_id_presence_check, null_rate_check


def test_null_rate_check_returns_failed_for_missing_table(tmp_path: Path) -> None:
    store = DuckDBStore(tmp_path / "quality_checks.duckdb")

    result = null_rate_check(store, "missing_table", "name")

    assert result.passed is False
    assert "does not exist" in result.details


def test_canonical_id_presence_check_flags_missing_match_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "quality_checks.duckdb"
    store = DuckDBStore(db_path)
    store.conn.execute("DROP TABLE IF EXISTS matches")
    store.conn.execute(
        "CREATE TABLE matches (source VARCHAR, source_id VARCHAR, home_team_canonical_id VARCHAR, away_team_canonical_id VARCHAR)"
    )
    store.conn.execute(
        "INSERT INTO matches VALUES ('source-a', '1', NULL, 'away-id')"
    )

    result = canonical_id_presence_check(store, "matches")

    assert result.passed is False
    assert result.rows_failed == 1
    assert result.rows_checked == 1
    store.close()
