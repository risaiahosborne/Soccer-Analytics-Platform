"""
Upgraded Predictions Builder
----------------------------

This module takes model outputs (probabilities) + enriched features from
FeatureBuilder and produces a unified predictions record suitable for:

• Dashboard display
• Backtesting
• Model evaluation
• Calibration analysis
• Betting strategy evaluation

All clarity is upstream. No dashboard logic remains here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Any

import pandas as pd

from ss_env.SoccerSystem.data_storage.duckdb_store import DuckDBStore
from ss_env.SoccerSystem.feature_engineering.builder import FeatureBuilder

logger = logging.getLogger(__name__)


class PredictionsBuilder:
    def __init__(self, store: DuckDBStore, model) -> None:
        self.store = store
        self.model = model
        self.features = FeatureBuilder(store)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def predict_match(self, match_canonical_id: str) -> Dict[str, Any]:
        """
        Build features → run model → compute betting metrics → return dict.
        """

        # 1. Build enriched features
        feats = self.features.build(match_canonical_id)
        if not feats:
            logger.warning("No features for match %s", match_canonical_id)
            return {}

        # 2. Extract model inputs
        model_inputs = self._extract_model_inputs(feats)

        # 3. Run model inference
        probs = self.model.predict_proba(model_inputs)

        prob_home, prob_draw, prob_away = probs

        # 4. Build predictions record
        out = {
            "match_canonical_id": match_canonical_id,
            "generated_at": datetime.utcnow().isoformat(),

            # model probabilities
            "prob_home": prob_home,
            "prob_draw": prob_draw,
            "prob_away": prob_away,
        }

        # 5. Add enriched metadata
        out.update(self._team_metadata(feats))
        out.update(self._match_metadata(feats))

        # 6. Add implied probabilities (from odds)
        out.update(self._implied_probabilities(feats))

        # 7. Add volatility
        out.update(self._volatility(out))

        # 8. Add EV + Kelly
        out.update(self._ev_kelly(out))

        # 9. Add value flags
        out.update(self._value_flags(out))

        # 10. Add residuals (model vs implied)
        out.update(self._residuals(out))

        return out

    def predict_all(self, league: str, season: str) -> pd.DataFrame:
        """
        Predict all matches in a league/season.
        """

        match_ids = self.store.conn.execute(
            """
            SELECT canonical_id
            FROM matches
            WHERE competition = ?
              AND season = ?
              AND status = 'finished'
            ORDER BY kickoff_utc
            """,
            [league, season],
        ).fetchall()

        rows = []
        for (mid,) in match_ids:
            try:
                row = self.predict_match(mid)
                if row:
                    rows.append(row)
            except Exception as exc:
                logger.warning("Prediction failed for match %s: %s", mid, exc)

        return pd.DataFrame(rows)

    def write_predictions(self, df: pd.DataFrame) -> None:
        """
        Write predictions to DuckDB.
        """

        self.store.conn.execute("CREATE OR REPLACE TABLE predictions AS SELECT * FROM df")

        logger.info("Predictions table written successfully.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_model_inputs(self, feats: Dict[str, Any]):
        """
        Select only model input columns.
        """
        return {k: v for k, v in feats.items() if k.startswith("home_") or k.startswith("away_") or "form" in k}

    def _team_metadata(self, feats: Dict[str, Any]):
        return {
            "home_team": feats.get("home_display_name"),
            "away_team": feats.get("away_display_name"),
            "home_team_short": feats.get("home_short_name"),
            "away_team_short": feats.get("away_short_name"),
            "home_team_league": feats.get("home_league"),
            "away_team_league": feats.get("away_league"),
        }

    def _match_metadata(self, feats: Dict[str, Any]):
        return {
            "competition": feats.get("competition"),
            "season": feats.get("season"),
            "kickoff_hour": feats.get("kickoff_hour"),
            "kickoff_weekday": feats.get("kickoff_weekday"),
        }

    def _implied_probabilities(self, feats: Dict[str, Any]):
        return {
            "implied_home": feats.get("implied_prob_home"),
            "implied_draw": feats.get("implied_prob_draw"),
            "implied_away": feats.get("implied_prob_away"),
        }

    def _volatility(self, preds: Dict[str, Any]):
        p_home = preds.get("prob_home")
        p_draw = preds.get("prob_draw")
        p_away = preds.get("prob_away")

        if not p_home or not p_draw or not p_away:
            return {"volatility_entropy": None}

        entropy = -(
            p_home * math.log(p_home) +
            p_draw * math.log(p_draw) +
            p_away * math.log(p_away)
        )

        return {"volatility_entropy": round(entropy, 4)}

    def _ev_kelly(self, preds: Dict[str, Any]):
        out = {}
        for sel in ["home", "draw", "away"]:
            prob = preds.get(f"prob_{sel}")
            implied = preds.get(f"implied_{sel}")

            if not prob or not implied:
                out[f"ev_{sel}"] = None
                out[f"kelly_{sel}"] = None
                continue

            price = 1 / implied
            ev = prob * (price - 1) - (1 - prob)
            kelly = ev / (price - 1)

            out[f"ev_{sel}"] = round(ev, 4)
            out[f"kelly_{sel}"] = round(kelly, 4)

        return out

    def _value_flags(self, preds: Dict[str, Any]):
        out = {}
        for sel in ["home", "draw", "away"]:
            prob = preds.get(f"prob_{sel}")
            implied = preds.get(f"implied_{sel}")

            if not prob or not implied:
                out[f"value_{sel}"] = None
                continue

            out[f"value_{sel}"] = int(prob > implied)

        return out

    def _residuals(self, preds: Dict[str, Any]):
        out = {}
        for sel in ["home", "draw", "away"]:
            prob = preds.get(f"prob_{sel}")
            implied = preds.get(f"implied_{sel}")

            if not prob or not implied:
                out[f"residual_{sel}"] = None
                continue

            out[f"residual_{sel}"] = round(prob - implied, 4)

        return out


# ------------------------------------------------------------------
# __main__
# ------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    store = DuckDBStore("data/soccer_smoketest.duckdb")

    # Load your trained model here
    from my_model import MyModel
    model = MyModel.load("models/latest.pkl")

    builder = PredictionsBuilder(store, model)

    df = builder.predict_all(league="Premier League", season="2025/2026")

    builder.write_predictions(df)

    print("Predictions generated:", len(df))
