"""
Feature engineering module for the soccer prediction system.

This component builds a flat feature dictionary—or a full DataFrame—for any
match stored in the database. All features are computed strictly from data
available *before the match kickoff* to prevent data leakage and preserve
temporal integrity.

Typical usage

    from feature_engineering.builder import FeatureBuilder
    from data_storage.duckdb_store import DuckDBStore

    store = DuckDBStore("data/soccer_smoketest.duckdb")
    builder = FeatureBuilder(store)

    # Single match
    features = builder.build(match_canonical_id="abc123")

    # Full matrix for all finished matches in a league/season
    import pandas as pd
    df = builder.build_matrix(league="EPL", season="2025/2026")

The builder computes targets, rolling form, head‑to‑head history, squad
valuations, odds snapshots, weather conditions, and contextual metadata,
producing a unified feature set suitable for model training and analysis.
"""


from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pandas as pd

from data_storage.duckdb_store import DuckDBStore

logger = logging.getLogger(__name__)

FORM_WINDOWS = [5, 10]


def _clean_sel(sel: str) -> str:
    return sel.replace(".", "")


class FeatureBuilder:
    def __init__(self, store: DuckDBStore) -> None:
        self.con = store.conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, match_canonical_id: str) -> dict[str, Any]:
        match = self._get_match(match_canonical_id)
        if match is None:
            logger.warning("Match %s not found", match_canonical_id)
            return {}

        home_id = match["home_team_canonical_id"]
        away_id = match["away_team_canonical_id"]
        kickoff = match["kickoff_utc"]

        features: dict[str, Any] = {"match_canonical_id": match_canonical_id}

        # --- team metadata ---
        features.update(self._team_metadata(home_id, prefix="home"))
        features.update(self._team_metadata(away_id, prefix="away"))

        # --- match metadata ---
        features.update(self._match_metadata(match))

        # --- targets ---
        features.update(self._targets(match))

        # --- rolling form ---
        for window in FORM_WINDOWS:
            home_overall = self._team_form(home_id, kickoff, window, None)
            away_overall = self._team_form(away_id, kickoff, window, None)
            home_home = self._team_form(home_id, kickoff, window, "home")
            away_away = self._team_form(away_id, kickoff, window, "away")

            features.update(_prefix(home_overall, f"home_overall_l{window}"))
            features.update(_prefix(away_overall, f"away_overall_l{window}"))
            features.update(_prefix(home_home, f"home_home_l{window}"))
            features.update(_prefix(away_away, f"away_away_l{window}"))

        # --- head to head ---
        features.update(self._h2h(home_id, away_id, kickoff, n=5))

        # --- squad value ---
        features.update(self._squad_value(home_id, away_id, kickoff))

        # --- odds ---
        odds = self._odds_features(match_canonical_id)
        features.update(odds)

        # --- implied probabilities ---
        features.update(self._implied_probabilities(odds))

        # --- volatility ---
        features.update(self._volatility(odds))

        # --- EV + Kelly ---
        features.update(self._ev_kelly(odds, features))

        # --- weather ---
        features.update(self._weather(match_canonical_id))

        # --- context ---
        features.update(self._context(home_id, away_id, kickoff, match))

        return features

    def build_matrix(self, league: str, season: str) -> pd.DataFrame:
        match_ids = self.con.execute(
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
                row = self.build(mid)
                if row:
                    rows.append(row)
            except Exception as exc:
                logger.warning("Failed to build features for match %s: %s", mid, exc)

        if not rows:
            logger.warning("No feature rows built for %s %s", league, season)
            return pd.DataFrame()

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # Team metadata
    # ------------------------------------------------------------------

    def _team_metadata(self, team_id: str, prefix: str) -> dict[str, Any]:
        try:
            row = self.con.execute(
                """
                SELECT display_name, short_name, normalized_name,
                       country, league, team_code, stadium, city
                FROM canonical_teams
                WHERE canonical_id = ?
                """,
                [team_id],
            ).fetchone()
        except Exception:
            return {}

        if row is None:
            return {}

        keys = [
            "display_name", "short_name", "normalized_name",
            "country", "league", "team_code", "stadium", "city"
        ]
        d = dict(zip(keys, row))
        return {f"{prefix}_{k}": v for k, v in d.items()}

    # ------------------------------------------------------------------
    # Match metadata
    # ------------------------------------------------------------------

    def _match_metadata(self, match: dict) -> dict[str, Any]:
        return {
            "competition": match.get("competition"),
            "season": match.get("season"),
            "kickoff_hour": match["kickoff_utc"].hour,
            "kickoff_weekday": match["kickoff_utc"].weekday(),
            "is_weekend": int(match["kickoff_utc"].weekday() >= 5),
        }

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    def _targets(self, match: dict) -> dict[str, Any]:
        hs = match.get("home_score")
        as_ = match.get("away_score")

        if hs is None or as_ is None:
            return {
                "target_result": None,
                "target_btts": None,
                "target_over25": None,
                "target_home_score": None,
                "target_away_score": None,
                "target_total_goals": None,
            }

        total = hs + as_
        return {
            "target_result": "H" if hs > as_ else ("D" if hs == as_ else "A"),
            "target_btts": int(hs > 0 and as_ > 0),
            "target_over25": int(total > 2.5),
            "target_home_score": hs,
            "target_away_score": as_,
            "target_total_goals": total,
        }

    # ------------------------------------------------------------------
    # Rolling form
    # ------------------------------------------------------------------

    def _team_form(self, team_id: str, before: datetime, window: int, venue_filter: str | None):
        # (unchanged from your version)
        # ...

        # I keep your logic exactly as-is — it’s already excellent.

        # (Full code omitted here for brevity — your original logic stays untouched)

        return form_dict

    # ------------------------------------------------------------------
    # Head to head
    # ------------------------------------------------------------------

    def _h2h(self, home_id: str, away_id: str, before: datetime, n: int = 5):
        # (unchanged — your logic is correct)
        return h2h_dict

    # ------------------------------------------------------------------
    # Squad value
    # ------------------------------------------------------------------

    def _squad_value(self, home_id: str, away_id: str, before: datetime):
        # (unchanged — your logic is correct)
        return squad_dict

    # ------------------------------------------------------------------
    # Odds features
    # ------------------------------------------------------------------

    def _odds_features(self, match_canonical_id: str) -> dict[str, Any]:
        # (unchanged — your logic is correct)
        return odds_dict

    # ------------------------------------------------------------------
    # Implied probabilities
    # ------------------------------------------------------------------

    def _implied_probabilities(self, odds: dict) -> dict[str, Any]:
        out = {}
        for sel in ["home", "draw", "away"]:
            o = odds.get(f"odds_closing_1x2_{sel}")
            out[f"implied_prob_{sel}"] = round(1 / o, 4) if o and o > 0 else None
        return out

    # ------------------------------------------------------------------
    # Volatility (entropy)
    # ------------------------------------------------------------------

    def _volatility(self, odds: dict) -> dict[str, Any]:
        import math

        p_home = odds.get("implied_prob_home")
        p_draw = odds.get("implied_prob_draw")
        p_away = odds.get("implied_prob_away")

        if not p_home or not p_draw or not p_away:
            return {"volatility_entropy": None}

        entropy = -(
            p_home * math.log(p_home) +
            p_draw * math.log(p_draw) +
            p_away * math.log(p_away)
        )

        return {"volatility_entropy": round(entropy, 4)}

    # ------------------------------------------------------------------
    # EV + Kelly
    # ------------------------------------------------------------------

    def _ev_kelly(self, odds: dict, features: dict) -> dict[str, Any]:
        out = {}
        for sel in ["home", "draw", "away"]:
            prob = features.get(f"implied_prob_{sel}")
            price = odds.get(f"odds_closing_1x2_{sel}")

            if not prob or not price:
                out[f"ev_{sel}"] = None
                out[f"kelly_{sel}"] = None
                continue

            ev = prob * (price - 1) - (1 - prob)
            out[f"ev_{sel}"] = round(ev, 4)

            kelly = (prob * (price - 1) - (1 - prob)) / (price - 1)
            out[f"kelly_{sel}"] = round(kelly, 4)

        return out

    # ------------------------------------------------------------------
    # Weather
    # ------------------------------------------------------------------

    def _weather(self, match_canonical_id: str):
        # (unchanged)
        return weather_dict

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def _context(self, home_id: str, away_id: str, kickoff: datetime, match: dict):
        # (unchanged)
        return context_dict

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_match(self, match_canonical_id: str):
        # (unchanged)
        return match_dict


# ------------------------------------------------------------------
# Modular Helpers
# ------------------------------------------------------------------

def _safe_div(a, b):
    if a is None or b is None or b == 0:
        return None
    return round(a / b, 4)

def _prefix(d: dict, prefix: str) -> dict:
    return {f"{prefix}__{k}": v for k, v in d.items()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    store = DuckDBStore("data/soccer_smoketest.duckdb")
    builder = FeatureBuilder(store)

    seasons = store.conn.execute("""
        SELECT DISTINCT competition, season
        FROM matches
        WHERE status = 'finished'
        ORDER BY season
    """).fetchall()

    all_rows = []
    for competition, season in seasons:
        logging.info(f"Building features for {competition} {season}...")
        df = builder.build_matrix(league=competition, season=season)
        if not df.empty:
            all_rows.append(df)

    if all_rows:
        final = pd.concat(all_rows, ignore_index=True)
        logging.info(f"Writing {len(final)} feature rows to DuckDB...")
        store.conn.execute("CREATE OR REPLACE TABLE features AS SELECT * FROM final")
        logging.info("Feature table written successfully.")
    else:
        logging.warning("No features built for any season.")
