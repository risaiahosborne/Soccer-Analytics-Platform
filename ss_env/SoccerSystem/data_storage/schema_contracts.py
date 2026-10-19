DDL_STATEMENTS = """ 
CREATE TABLE IF NOT EXISTS future_fixtures (
    fixture_id VARCHAR,
    competition VARCHAR,
    season VARCHAR,
    kickoff_utc TIMESTAMP,
    home_team_canonical_id VARCHAR,
    away_team_canonical_id VARCHAR,
    odds_home DOUBLE,
    odds_draw DOUBLE,
    odds_away DOUBLE,
    odds_btts_yes DOUBLE,
    odds_btts_no DOUBLE,
    odds_over25 DOUBLE,
    odds_under25 DOUBLE,
    PRIMARY KEY (fixture_id)
);

""""

FUTURE_FIXTURES_COLUMNS = [
    "fixture_id",
    "competition",
    "season",
    "kickoff_utc",
    "home_team_canonical_id",
    "away_team_canonical_id",
    "odds_home",
    "odds_draw",
    "odds_away",
    "odds_btts_yes",
    "odds_btts_no",
    "odds_over25",
    "odds_under25",
],

PREDICTIONS_COLUMNS = [
    "fixture_id",
    "home_team_name",
    "away_team_name",
    "competition",
    "kickoff_utc",

    "model_prob_home",
    "model_prob_draw",
    "model_prob_away",
    "model_prob_btts_yes",
    "model_prob_btts_no",
    "model_prob_over25",
    "model_prob_under25",

    "ev_home",
    "ev_draw",
    "ev_away",
    "ev_btts_yes",
    "ev_btts_no",
    "ev_over25",
    "ev_under25",
],

def validate_future_fixtures(df):
    if list(df.columns) != FUTURE_FIXTURES_COLUMNS:
        raise ValueError("future_fixtures schema mismatch")
        
def validate_predictions(df):
    if list(df.columns) != PREDICTIONS_COLUMNS:
        raise ValueError("predictions schema mismatch")


