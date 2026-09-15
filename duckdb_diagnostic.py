import duckdb
import os
import sys

DB_PATH = "ss_env/SoccerSystem/data/soccer_smoketest.duckdb"

def safe_connect(path):
    try:
        return duckdb.connect(path)
    except Exception as e:
        print("\n❌ DuckDB connection failed:")
        print(e)
        print("\n👉 If you see a lock error, close ALL Python REPLs:")
        print("   - Type exit() in any open python shell")
        print("   - Or press Ctrl+D")
        sys.exit(1)

def print_section(title):
    print("\n" + "="*80)
    print(title)
    print("="*80)

def main():
    print_section("DuckDB Diagnostic Script")
    print(f"Database path: {DB_PATH}")

    if not os.path.exists(DB_PATH):
        print("❌ ERROR: Database file does not exist.")
        sys.exit(1)

    con = safe_connect(DB_PATH)

    # 1. List tables
    print_section("Tables in database")
    print(con.execute("SHOW TABLES").fetchdf())

    # 2. Row counts
    print_section("Row counts")
    tables = con.execute("SHOW TABLES").fetchall()
    for (t,) in tables:
        count = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t:20} {count}")

    # 3. Sample rows
    print_section("Sample rows: matches")
    print(con.execute("SELECT * FROM matches LIMIT 5").fetchdf())

    print_section("Sample rows: odds_snapshots")
    print(con.execute("SELECT * FROM odds_snapshots LIMIT 5").fetchdf())

    print_section("Sample rows: xg_records")
    print(con.execute("SELECT * FROM xg_records LIMIT 5").fetchdf())

    print_section("Sample rows: canonical_teams")
    print(con.execute("SELECT * FROM canonical_teams LIMIT 5").fetchdf())

    print_section("Diagnostic complete")

if __name__ == "__main__":
    main()
