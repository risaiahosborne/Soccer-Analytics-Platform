from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import duckdb
import joblib

from ss_env.SoccerSystem.data_storage.duckdb_store import DuckDBStore
from ss_env.SoccerSystem.feature_engineering.builder import FeatureBuilder
from ss_env.SoccerSystem.model_training.trainer import prepare_features, DROP_COLS

logger = logging.getLogger(__name__)


class PredictionEngine:
    def __init__(
        self,
        store: DuckDBStore,
        result_model_path: str,
        btts_model_path: str | None = None,
        over25_model_path: str | None = None,
    ) -> None:
        self.store = store
        self.con: duckdb.DuckDBPyConnection = store.conn
        self.builder = FeatureBuilder(store)

        # load models
        self.result_model = joblib.load(result_model_path)
        self.btts_model = joblib.load(btts_model_path) if btts_model_path else None
        self.over25_model = joblib.load(over25_model_path) if over25_model_path else None

    def _get_upcoming_fixtures(self) -> pd.DataFrame:
        return self.con.execute(
            """
            SELECT *
            FROM future_fixtures
            WHERE status = 'scheduled'
            ORDER BY kickoff_utc
            """
        ).fetchdf()

    def _build_features_for_fixture(self, row: pd.Series) -> dict[str, Any]:
        feats = self.builder.build_for_fixture(
            home_team_id=row["home_team_canonical_id"],
            away_team_id=row["away_team_canonical_id"],
            kickoff=row["kickoff_utc"],
            competition=row["competition"],
            season=row["season"],
            match_canonical_id=row["fixture_id"],
        )

        feats["home_team_canonical_id"] = row["home_team_canonical_id"]
        feats["away_team_canonical_id"] = row["away_team_canonical_id"]
        feats["kickoff_utc"] = row["kickoff_utc"]   # <-- ADD THIS

        return feats

    def run(self) -> pd.DataFrame:
        fixtures = self._get_upcoming_fixtures()
        if fixtures.empty:
            logger.warning("No upcoming fixtures found.")
            return pd.DataFrame()

        feature_rows: list[dict[str, Any]] = []
        for _, fx in fixtures.iterrows():
            try:
                feats = self._build_features_for_fixture(fx)
                if feats:
                    feature_rows.append(feats)
            except Exception as exc:
                logger.warning(
                    "Failed to build features for fixture %s: %s",
                    fx["fixture_id"],
                    exc,
                )

        if not feature_rows:
            logger.warning("No feature rows built for upcoming fixtures.")
            return pd.DataFrame()

        features_df = pd.DataFrame(feature_rows)
       
        # --- ENRICH WITH TEAM NAMES ---
        teams_df = self.con.execute("SELECT canonical_id, name FROM teams").fetchdf()

        # Home team merge
        features_df = features_df.merge(
            teams_df.rename(columns={"canonical_id": "home_team_canonical_id", "name": "home_team_name"}),
            on="home_team_canonical_id",
            how="left"
        )

        # Away team merge
        features_df = features_df.merge(
            teams_df.rename(columns={"canonical_id": "away_team_canonical_id", "name": "away_team_name"}),
            on="away_team_canonical_id",
            how="left"
        )

        # --- ALIGN FEATURES WITH TRAINING ---
        # Drop enrichment columns before model input
        extra_drop = [
            "home_team_canonical_id",
            "away_team_canonical_id",
            "home_team_name",
            "away_team_name",
            "kickoff_utc"
        ]

        drop = [c for c in DROP_COLS if c in features_df.columns] + [
            c for c in extra_drop if c in features_df.columns
        ]

        X = features_df.drop(columns=drop).copy()
        X, _ = prepare_features(X)

        # ---------- RESULT MODEL (H/D/A) ----------
        proba_result = self.result_model.predict_proba(X)
        classes_result = list(self.result_model.classes_)  # [0, 1, 2]

        prob_home, prob_draw, prob_away = [], [], []

        for row_probs in proba_result:
            d = dict(zip(classes_result, row_probs))
            prob_home.append(d.get(2))  # Home
            prob_draw.append(d.get(1))  # Draw
            prob_away.append(d.get(0))  # Away

        features_df["model_prob_home"] = prob_home
        features_df["model_prob_draw"] = prob_draw
        features_df["model_prob_away"] = prob_away

        # --- EV & KELLY FOR RESULT (home/draw/away) ---
        ev_home, kelly_home = [], []
        ev_draw, kelly_draw = [], []
        ev_away, kelly_away = [], []

        for ph, p_draw, pa, oh, od, oa in zip(
            features_df["model_prob_home"],
            features_df["model_prob_draw"],
            features_df["model_prob_away"],
            features_df.get("odds_home", [None] * len(features_df)),
            features_df.get("odds_draw", [None] * len(features_df)),
            features_df.get("odds_away", [None] * len(features_df)),
        ):
            eh, kh = self._ev_and_kelly(ph, oh)
            ed, kd = self._ev_and_kelly(p_draw, od)
            ea, ka = self._ev_and_kelly(pa, oa)

            ev_home.append(eh); kelly_home.append(kh)
            ev_draw.append(ed); kelly_draw.append(kd)
            ev_away.append(ea); kelly_away.append(ka)

        features_df["ev_home"] = ev_home
        features_df["kelly_home"] = kelly_home
        features_df["ev_draw"] = ev_draw
        features_df["kelly_draw"] = kelly_draw
        features_df["ev_away"] = ev_away
        features_df["kelly_away"] = kelly_away

        # ---------- BTTS MODEL ----------
        if self.btts_model is not None:
            proba_btts = self.btts_model.predict_proba(X)
            classes_btts = list(self.btts_model.classes_)  # e.g. [0, 1] or ["No", "Yes"]

            prob_btts_yes: list[float | None] = []
            prob_btts_no: list[float | None] = []

            for row_probs in proba_btts:
                d = dict(zip(classes_btts, row_probs))
                # support both numeric and string labels
                prob_btts_yes.append(d.get(1) if 1 in d else d.get("Yes"))
                prob_btts_no.append(d.get(0) if 0 in d else d.get("No"))

            features_df["model_prob_btts_yes"] = prob_btts_yes
            features_df["model_prob_btts_no"] = prob_btts_no

            # --- EV & KELLY FOR BTTS (Yes/No) ---
            ev_btts_yes, kelly_btts_yes = [], []
            ev_btts_no, kelly_btts_no = [], []

            for py, pn, oy, on in zip(
                features_df["model_prob_btts_yes"],
                features_df["model_prob_btts_no"],
                features_df.get("odds_btts_yes", [None] * len(features_df)),
                features_df.get("odds_btts_no", [None] * len(features_df)),
            ):
                ey, ky = self._ev_and_kelly(py, oy)
                en, kn = self._ev_and_kelly(pn, on)

                ev_btts_yes.append(ey); kelly_btts_yes.append(ky)
                ev_btts_no.append(en); kelly_btts_no.append(kn)

            features_df["ev_btts_yes"] = ev_btts_yes
            features_df["kelly_btts_yes"] = kelly_btts_yes
            features_df["ev_btts_no"] = ev_btts_no
            features_df["kelly_btts_no"] = kelly_btts_no

        # ---------- OVER 2.5 MODEL ----------
        if self.over25_model is not None:
            proba_over25 = self.over25_model.predict_proba(X)
            classes_over25 = list(self.over25_model.classes_)  # e.g. [0, 1] or ["Under", "Over"]

            prob_over25: list[float | None] = []
            prob_under25: list[float | None] = []

            for row_probs in proba_over25:
                d = dict(zip(classes_over25, row_probs))
                prob_over25.append(d.get(1) if 1 in d else d.get("Over"))
                prob_under25.append(d.get(0) if 0 in d else d.get("Under"))

            features_df["model_prob_over25"] = prob_over25
            features_df["model_prob_under25"] = prob_under25

            # --- EV & KELLY FOR OVER/UNDER 2.5 ---
            ev_over25, kelly_over25 = [], []
            ev_under25, kelly_under25 = [], []

            for po, pu, oo, ou in zip(
                features_df["model_prob_over25"],
                features_df["model_prob_under25"],
                features_df.get("odds_over25", [None] * len(features_df)),
                features_df.get("odds_under25", [None] * len(features_df)),
            ):
                eo, ko = self._ev_and_kelly(po, oo)
                eu, ku = self._ev_and_kelly(pu, ou)

                ev_over25.append(eo); kelly_over25.append(ko)
                ev_under25.append(eu); kelly_under25.append(ku)

            features_df["ev_over25"] = ev_over25
            features_df["kelly_over25"] = kelly_over25
            features_df["ev_under25"] = ev_under25
            features_df["kelly_under25"] = kelly_under25
        
        # Drop old predictions table so schema matches new features_df
        self.con.execute("DROP TABLE IF EXISTS predictions")

        # Create new predictions table with the exact schema of features_df
        self.con.execute(
            "CREATE TABLE predictions AS SELECT * FROM features_df LIMIT 0"
        )

        # Insert enriched predictions
        self.con.execute("INSERT INTO predictions SELECT * FROM features_df")

        return features_df

    @staticmethod
    def _ev_and_kelly(prob: float | None, odds: float | None) -> tuple[float | None, float | None]:
        if prob is None or odds is None or odds <= 1.0:
            return None, None
        edge = prob * (odds - 1) - (1 - prob)
        ev = edge
        kelly = edge / (odds - 1)
        if kelly < 0:
            kelly = 0.0
        return ev, kelly
