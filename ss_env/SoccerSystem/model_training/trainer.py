"""
Model training module for soccer match prediction.

Trains and compares multiple models for three targets:
    - match result: H / D / A  (multiclass)
    - btts:         1 / 0      (binary)
    - over25:       1 / 0      (binary)

Models compared:
    - LightGBM
    - XGBoost
    - Logistic Regression

Evaluation:
    - 5-fold time-series cross validation
    - Final hold-out test on last 20% of season

Outputs:
    - Best model per target saved to model_training/saved_models/
    - Predictions written to DuckDB via PredictionsBuilder
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.impute import SimpleImputer

import lightgbm as lgb

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

TARGETS = {
    "result": {"col": "target_result", "type": "multiclass", "classes": ["H", "D", "A"]},
    "btts": {"col": "target_btts", "type": "binary", "classes": [0, 1]},
    "over25": {"col": "target_over25", "type": "binary", "classes": [0, 1]},
}

DROP_COLS = [
    "match_canonical_id",
    "target_result", "target_btts", "target_over25",
    "target_over15", "target_over35",
    "target_home_score", "target_away_score", "target_total_goals",
    "kickoff_hour", "is_weekend", "matchday",
]

N_CV_SPLITS = 5
TEST_SIZE = 0.20
SAVED_MODELS_DIR = Path("/workspaces/Soccer-App-1/model_training/saved_models")

# ------------------------------------------------------------------
# Feature preparation
# ------------------------------------------------------------------

def prepare_features(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    drop = [c for c in DROP_COLS if c in df.columns]
    X = df.drop(columns=drop).copy()

    encoders = {}

    for col in X.select_dtypes(include="object").columns:
        le = LabelEncoder()
        X[col] = le.fit_transform(X[col].astype(str))
        encoders[col] = le

    for col in X.select_dtypes(include="bool").columns:
        X[col] = X[col].astype(int)

    return X, encoders


def get_target(df: pd.DataFrame, target_name: str) -> pd.Series:
    cfg = TARGETS[target_name]
    y = df[cfg["col"]].copy()
   
    if cfg["type"] == "multiclass":
        mapping = {"H": 0, "D": 1, "A": 2}
        y = y.map(mapping)
    return y

# ------------------------------------------------------------------
# Model definitions
# ------------------------------------------------------------------

def make_lgbm(target_type: str) -> lgb.LGBMClassifier:
    params = dict(
        n_estimators=300,
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=10,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        verbose=-1,
    )
    if target_type == "multiclass":
        params["objective"] = "multiclass"
        params["num_class"] = 3
    else:
        params["objective"] = "binary"
    return lgb.LGBMClassifier(**params)


def make_xgboost(target_type: str):
    try:
        from xgboost import XGBClassifier
        params = dict(
            n_estimators=300,
            learning_rate=0.05,
            max_depth=4,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=42,
            eval_metric="logloss",
            verbosity=0,
        )
        if target_type == "multiclass":
            params["objective"] = "multi:softprob"
            params["num_class"] = 3
        else:
            params["objective"] = "binary:logistic"
        return XGBClassifier(**params)
    except ImportError:
        return None


def make_logreg(target_type: str) -> Pipeline:
    return Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(
            max_iter=1000,
            random_state=42,
            multi_class="multinomial" if target_type == "multiclass" else "auto",
        )),
    ])


def get_models(target_type: str) -> dict[str, Any]:
    models = {
        "LightGBM": make_lgbm(target_type),
        "LogisticRegression": make_logreg(target_type),
    }
    xgb = make_xgboost(target_type)
    if xgb is not None:
        models["XGBoost"] = xgb
    return models


def _clone_model(model):
    import copy
    return copy.deepcopy(model)

# ------------------------------------------------------------------
# Evaluation
# ------------------------------------------------------------------

def evaluate_cv(model, X, y, target_type, n_splits=N_CV_SPLITS):
    tscv = TimeSeriesSplit(n_splits=n_splits)
    logloss_scores, brier_scores, acc_scores = [], [], []

    for train_idx, val_idx in tscv.split(X):
        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

        if len(y_train.unique()) < 2:
            continue

        try:
            m = _clone_model(model)
            m.fit(X_train, y_train)
            proba = m.predict_proba(X_val)
            preds = m.predict(X_val)

            logloss_scores.append(log_loss(y_val, proba))
            acc_scores.append(accuracy_score(y_val, preds))

            if target_type == "binary":
                brier_scores.append(brier_score_loss(y_val, proba[:, 1]))
            else:
                brier_scores.append(np.mean([
                    brier_score_loss((y_val == i).astype(int), proba[:, i])
                    for i in range(proba.shape[1])
                ]))

        except Exception as exc:
            logger.warning("CV fold failed: %s", exc)

    return {
        "cv_logloss": np.mean(logloss_scores) if logloss_scores else None,
        "cv_brier": np.mean(brier_scores) if brier_scores else None,
        "cv_accuracy": np.mean(acc_scores) if acc_scores else None,
    }


def evaluate_holdout(model, X_train, y_train, X_test, y_test, target_type):
    model.fit(X_train, y_train)
    proba = model.predict_proba(X_test)
    preds = model.predict(X_test)

    result = {
        "test_logloss": log_loss(y_test, proba),
        "test_accuracy": accuracy_score(y_test, preds),
    }

    if target_type == "binary":
        result["test_brier"] = brier_score_loss(y_test, proba[:, 1])
    else:
        result["test_brier"] = np.mean([
            brier_score_loss((y_test == i).astype(int), proba[:, i])
            for i in range(proba.shape[1])
        ])

    return result

# ------------------------------------------------------------------
# Trainer
# ------------------------------------------------------------------

class ModelTrainer:
    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df.dropna(subset=["target_result", "target_btts", "target_over25"])
        self.df = self.df.sort_values("matchday").reset_index(drop=True)

        self.X, self.encoders = prepare_features(self.df)

        split_idx = int(len(self.df) * (1 - TEST_SIZE))
        self.split_idx = split_idx

        self.X_train = self.X.iloc[:split_idx]
        self.X_test = self.X.iloc[split_idx:]

        logger.info("Train: %d | Test: %d", split_idx, len(self.df) - split_idx)

        self.results = {}
        self.best_models = {}

    def run(self):
        for target_name, cfg in TARGETS.items():
            print(f"\n=== TARGET: {target_name.upper()} ({cfg['type']}) ===")

            y = get_target(self.df, target_name)
            y_train = y.iloc[:self.split_idx]
            y_test = y.iloc[self.split_idx:]

            models = get_models(cfg["type"])
            rows = []

            for model_name, model in models.items():
                print(f"\nTraining {model_name}...")

                cv_metrics = evaluate_cv(model, self.X_train, y_train, cfg["type"])
                test_model = _clone_model(model)
                test_metrics = evaluate_holdout(
                    test_model, self.X_train, y_train, self.X_test, y_test, cfg["type"]
                )

                rows.append({"model": model_name, **cv_metrics, **test_metrics})

            df_results = pd.DataFrame(rows).set_index("model")
            self.results[target_name] = df_results

            valid = df_results.dropna(subset=["cv_logloss"])
            best_name = valid["cv_logloss"].idxmin()

            best_model = _clone_model(models[best_name])
            best_model.fit(self.X_train, y_train)

            # ✔ Save actual model (not dict)
            self.best_models[target_name] = best_model

        return self.results

    def save_best_models(self):
        SAVED_MODELS_DIR.mkdir(parents=True, exist_ok=True)

        for target_name, model in self.best_models.items():
            path = SAVED_MODELS_DIR / f"{target_name}_model.pkl"
            joblib.dump(model, path)
            logger.info("Saved %s model → %s", target_name, path)

# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    from ss_env.SoccerSystem.data_storage.duckdb_store import DuckDBStore
    from ss_env.SoccerSystem.feature_engineering.builder import FeatureBuilder

    store = DuckDBStore("ss_env/SoccerSystem/data/soccer_smoketest.duckdb")
    builder = FeatureBuilder(store)

    print("Building feature matrix...")
    df = builder.build_matrix("Premier League", "2025/2026")
    print(f"Matrix: {len(df)} rows × {len(df.columns)} columns")

    trainer = ModelTrainer(df)
    trainer.run()
    trainer.save_best_models()

    print("Training complete.")
    store.close()