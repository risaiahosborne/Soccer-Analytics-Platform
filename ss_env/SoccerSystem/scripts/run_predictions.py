import logging

from ss_env.SoccerSystem.data_storage.duckdb_store import DuckDBStore
from ss_env.SoccerSystem.model_training.predictor import PredictionEngine

logging.basicConfig(level=logging.INFO)

store = DuckDBStore("ss_env/SoccerSystem/data/soccer_smoketest.duckdb")

engine = PredictionEngine(
    store=store,
    result_model_path="/workspaces/Soccer-App-1/model_training/saved_models/result_model.pkl",
    btts_model_path="/workspaces/Soccer-App-1/model_training/saved_models/btts_model.pkl",
    over25_model_path="/workspaces/Soccer-App-1/model_training/saved_models/over25_model.pkl",
)

predictions_df = engine.run()

cols_to_show = [
    "match_canonical_id",
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
]

print("\n=== Prediction Summary ===")
print(predictions_df[cols_to_show].head())

print("\nPredictions written to DuckDB.")
