"""
Model training module for soccer match prediction.

Trains and compares multiple models for three targets:
    - match result: H / D / A  (multiclass)
    - btts:         1 / 0      (binary)
    - over25:       1 / 0      (binary)

Models compared:
    - LightGBM      (handles nulls natively, fast, well-calibrated)
    - XGBoost       (strong baseline, industry standard)
    - Logistic Regression (linear baseline, fully explainable)

Evaluation:
    - 5-fold time-series cross validation (model comparison)
    - Final hold-out test on last 20% of season (honest estimate)

Outputs:
    - Comparison table printed to console
    - Best model per target saved to model_training/saved_models/
    - Probability predictions saved to model_training/predictions.csv

Usage:
    cd ss_env/SoccerSystem
    PYTHONPATH=/workspaces/Soccer-App-1/ss_env/SoccerSystem python3 -m model_training.trainer
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    brier_score_loss,
    log_loss,
)
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
    "result": {
        "col": "target_result",
        "type": "multiclass",
        "classes": ["H", "D", "A"],
    },
    "btts": {
        "col": "target_btts",
        "type": "binary",
        "classes": [0, 1],
    },
    "over25": {
        "col": "target_over25",
        "type": "binary",
        "classes": [0, 1],
    },
}

# Columns to drop before training (non-feature columns)
DROP_COLS = [
    "match_canonical_id",
    "target_result", "target_btts", "target_over25",
    "target_over15", "target_over35",
    "target_home_score", "target_away_score", "target_total_goals",
    "kickoff_hour",
    "is_weekend",
    "matchday",
]

N_CV_SPLITS = 5
TEST_SIZE = 0.20
SAVED_MODELS_DIR = Path("model_training/saved_models")
PREDICTIONS_PATH = Path("model_training/predictions.csv")


# ------------------------------------------------------------------
# Feature preparation
# ------------------------------------------------------------------

def prepare_features(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Clean and prepare feature matrix for training.
    Returns (X, encoders) where encoders holds any fitted LabelEncoders.
    """
    drop = [c for c in DROP_COLS if c in df.columns]
    X = df.drop(columns=drop).copy()

    encoders = {}

    # Encode any remaining string columns
    for col in X.select_dtypes(include="object").columns:
        le = LabelEncoder()
        X[col] = le.fit_transform(X[col].astype(str))
        encoders[col] = le

    # Convert booleans to int
    for col in X.select_dtypes(include="bool").columns:
        X[col] = X[col].astype(int)

    return X, encoders


def get_target(df: pd.DataFrame, target_name: str) -> pd.Series:
    cfg = TARGETS[target_name]
    y = df[cfg["col"]].copy()
    if cfg["type"] == "multiclass":
        # Encode H=0, D=1, A=2 consistently
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


# ------------------------------------------------------------------
# Evaluation
# ------------------------------------------------------------------

def evaluate_cv(
    model,
    X: pd.DataFrame,
    y: pd.Series,
    target_type: str,
    n_splits: int = N_CV_SPLITS,
) -> dict[str, float]:
    """
    Time-series cross validation. Returns mean metrics across folds.
    """
    tscv = TimeSeriesSplit(n_splits=n_splits)
    logloss_scores, brier_scores, acc_scores = [], [], []

    for train_idx, val_idx in tscv.split(X):
        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

        # Skip folds with missing classes
        if len(y_train.unique()) < 2:
            continue

        try:
            m = _clone_model(model, target_type)
            m.fit(X_train, y_train)
            proba = m.predict_proba(X_val)
            preds = m.predict(X_val)

            logloss_scores.append(log_loss(y_val, proba))
            acc_scores.append(accuracy_score(y_val, preds))

            if target_type == "binary":
                brier_scores.append(brier_score_loss(y_val, proba[:, 1]))
            else:
                # Average Brier score across classes for multiclass
                brier_avg = np.mean([
                    brier_score_loss((y_val == i).astype(int), proba[:, i])
                    for i in range(proba.shape[1])
                ])
                brier_scores.append(brier_avg)

        except Exception as exc:
            logger.warning("CV fold failed: %s", exc)
            continue

    return {
        "cv_logloss": np.mean(logloss_scores) if logloss_scores else None,
        "cv_brier": np.mean(brier_scores) if brier_scores else None,
        "cv_accuracy": np.mean(acc_scores) if acc_scores else None,
        "cv_folds": len(logloss_scores),
    }


def evaluate_holdout(
    model,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    X_test: pd.DataFrame,
    y_test: pd.Series,
    target_type: str,
) -> dict[str, float]:
    """Final hold-out test evaluation."""
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


def _clone_model(model, target_type: str):
    """Return a fresh unfitted copy of the model."""
    import copy
    return copy.deepcopy(model)


# ------------------------------------------------------------------
# Main trainer
# ------------------------------------------------------------------

class ModelTrainer:
    """
    Trains and compares models for all prediction targets.

    Usage:
        trainer = ModelTrainer(df)
        results = trainer.run()
        trainer.save_best_models()
    """

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df.dropna(subset=["target_result", "target_btts", "target_over25"])
        self.df = self.df.sort_values("matchday").reset_index(drop=True)
        self.X, self.encoders = prepare_features(self.df)
        self.results: dict[str, pd.DataFrame] = {}
        self.best_models: dict[str, Any] = {}
        self.predictions: dict[str, np.ndarray] = {}

        # Time-based train/test split
        split_idx = int(len(self.df) * (1 - TEST_SIZE))
        self.X_train = self.X.iloc[:split_idx]
        self.X_test = self.X.iloc[split_idx:]
        self.split_idx = split_idx

        logger.info(
            "Train: %d matches | Test: %d matches",
            split_idx, len(self.df) - split_idx
        )

    def run(self) -> dict[str, pd.DataFrame]:
        """Train and evaluate all models for all targets."""
        for target_name, cfg in TARGETS.items():
            print(f"\n{'='*60}")
            print(f"TARGET: {target_name.upper()} ({cfg['type']})")
            print(f"{'='*60}")

            y = get_target(self.df, target_name)
            y_train = y.iloc[:self.split_idx]
            y_test = y.iloc[self.split_idx:]

            models = get_models(cfg["type"])
            rows = []

            for model_name, model in models.items():
                print(f"\n  Training {model_name}...")
                try:
                    # Cross validation
                    cv_metrics = evaluate_cv(
                        model, self.X_train, y_train, cfg["type"]
                    )

                    # Hold-out test
                    test_model = _clone_model(model, cfg["type"])
                    test_metrics = evaluate_holdout(
                        test_model,
                        self.X_train, y_train,
                        self.X_test, y_test,
                        cfg["type"],
                    )

                    row = {"model": model_name, **cv_metrics, **test_metrics}
                    rows.append(row)

                    print(f"    CV  log-loss={cv_metrics['cv_logloss']:.4f}  "
                          f"accuracy={cv_metrics['cv_accuracy']:.3f}  "
                          f"brier={cv_metrics['cv_brier']:.4f}")
                    print(f"    Test log-loss={test_metrics['test_logloss']:.4f}  "
                          f"accuracy={test_metrics['test_accuracy']:.3f}  "
                          f"brier={test_metrics['test_brier']:.4f}")

                except Exception as exc:
                    logger.warning("  %s failed: %s", model_name, exc)
                    rows.append({"model": model_name, "error": str(exc)})

            results_df = pd.DataFrame(rows).set_index("model")
            self.results[target_name] = results_df

            # Pick best model by CV log-loss
            valid = results_df.dropna(subset=["cv_logloss"])
            if not valid.empty:
                best_name = valid["cv_logloss"].idxmin()
                print(f"\n  Best model: {best_name}")
                best_model = _clone_model(models[best_name], cfg["type"])
                best_model.fit(self.X_train, y_train)
                self.best_models[target_name] = {
                    "name": best_name,
                    "model": best_model,
                    "target_type": cfg["type"],
                    "classes": cfg["classes"],
                }
                # Store full-data predictions for backtesting
                full_model = _clone_model(models[best_name], cfg["type"])
                full_model.fit(self.X, y)
                self.predictions[target_name] = full_model.predict_proba(self.X)

        self._print_summary()
        return self.results

    def _print_summary(self) -> None:
        print(f"\n{'='*60}")
        print("SUMMARY — Best model per target (by CV log-loss)")
        print(f"{'='*60}")
        for target_name, info in self.best_models.items():
            df = self.results[target_name]
            best = df.loc[info["name"]]
            print(f"\n  {target_name.upper()}: {info['name']}")
            print(f"    CV  log-loss={best.get('cv_logloss', 'N/A'):.4f}  "
                  f"accuracy={best.get('cv_accuracy', 'N/A'):.3f}")
            print(f"    Test log-loss={best.get('test_logloss', 'N/A'):.4f}  "
                  f"accuracy={best.get('test_accuracy', 'N/A'):.3f}")

    def save_best_models(self) -> None:
        """Save best models and predictions to disk."""
        SAVED_MODELS_DIR.mkdir(parents=True, exist_ok=True)

        for target_name, info in self.best_models.items():
            path = SAVED_MODELS_DIR / f"{target_name}_model.pkl"
            joblib.dump(info, path)
            logger.info("Saved %s model to %s", target_name, path)

        # Save probability predictions
        pred_rows = []
        for i, row in self.df.iterrows():
            r = {"match_canonical_id": self.df.iloc[i].get("match_canonical_id", i)}
            for target_name, proba in self.predictions.items():
                cfg = TARGETS[target_name]
                if cfg["type"] == "multiclass":
                    r["prob_home"] = proba[i, 0]
                    r["prob_draw"] = proba[i, 1]
                    r["prob_away"] = proba[i, 2]
                else:
                    r[f"prob_{target_name}"] = proba[i, 1]
            pred_rows.append(r)

        pred_df = pd.DataFrame(pred_rows)
        PREDICTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        pred_df.to_csv(PREDICTIONS_PATH, index=False)
        print(f"\nPredictions saved to {PREDICTIONS_PATH}")
        print(pred_df.head())

    def get_predictions_df(self) -> pd.DataFrame:
        """Return predictions merged with match canonical IDs."""
        rows = []
        for i in range(len(self.df)):
            r = {}
            if "match_canonical_id" in self.df.columns:
                r["match_canonical_id"] = self.df.iloc[i]["match_canonical_id"]
            for target_name, proba in self.predictions.items():
                cfg = TARGETS[target_name]
                if cfg["type"] == "multiclass":
                    r["prob_home"] = round(proba[i, 0], 4)
                    r["prob_draw"] = round(proba[i, 1], 4)
                    r["prob_away"] = round(proba[i, 2], 4)
                else:
                    r[f"prob_{target_name}"] = round(proba[i, 1], 4)
            rows.append(r)
        return pd.DataFrame(rows)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    logging.basicConfig(level=logging.INFO)

    from data_storage.duckdb_store import DuckDBStore
    from feature_engineering.builder import FeatureBuilder

    store = DuckDBStore("ss_env/SoccerSystem/data/soccer_smoketest.duckdb")
    builder = FeatureBuilder(store)

    print("Building feature matrix...")
    df = builder.build_matrix(league="Premier League", season="2025/2026")
    print(f"Matrix: {len(df)} rows x {len(df.columns)} columns")
    print(f"Non-null: {df.notna().mean().mean():.1%}")

    trainer = ModelTrainer(df)
    results = trainer.run()
    trainer.save_best_models()
    store.close()