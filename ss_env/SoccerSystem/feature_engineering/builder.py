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
    """Remove dots from selection names (e.g., 'over_2.5' → 'over_25')."""
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
        features.update(self._odds_features(match_canonical_id))

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
    # Target variables
    # ------------------------------------------------------------------

    def _targets(self, match: dict) -> dict[str, Any]:
        hs = match.get("home_score")
        as_ = match.get("away_score")

        if hs is None or as_ is None:
            return {
                "target_result": None,
                "target_btts": None,
                "target_over25": None,
                "target_over15": None,
                "target_over35": None,
                "target_home_score": None,
                "target_away_score": None,
                "target_total_goals": None,
            }

        total = hs + as_
        return {
            "target_result": "H" if hs > as_ else ("D" if hs == as_ else "A"),
            "target_btts": int(hs > 0 and as_ > 0),
            "target_over25": int(total > 2.5),
            "target_over15": int(total > 1.5),
            "target_over35": int(total > 3.5),
            "target_home_score": hs,
            "target_away_score": as_,
            "target_total_goals": total,
        }

    # ------------------------------------------------------------------
    # Rolling form
    # ------------------------------------------------------------------

    def _team_form(
        self,
        team_id: str,
        before: datetime,
        window: int,
        venue_filter: str | None,
    ) -> dict[str, float | None]:

        if venue_filter == "home":
            params = [team_id, before.isoformat(), window]
            base_query = """
                SELECT
                    m.canonical_id,
                    m.home_score AS scored,
                    m.away_score AS conceded,
                    1 AS is_home
                FROM matches m
                WHERE home_team_canonical_id = ?
                  AND kickoff_utc < ?
                  AND status = 'finished'
                ORDER BY kickoff_utc DESC
                LIMIT ?
            """
        elif venue_filter == "away":
            params = [team_id, before.isoformat(), window]
            base_query = """
                SELECT
                    m.canonical_id,
                    m.away_score AS scored,
                    m.home_score AS conceded,
                    0 AS is_home
                FROM matches m
                WHERE away_team_canonical_id = ?
                  AND kickoff_utc < ?
                  AND status = 'finished'
                ORDER BY kickoff_utc DESC
                LIMIT ?
            """
        else:
            params = [team_id, team_id, team_id, team_id, team_id, before.isoformat(), window]
            base_query = """
                SELECT
                    m.canonical_id,
                    CASE WHEN home_team_canonical_id = ? THEN m.home_score
                         ELSE m.away_score END AS scored,
                    CASE WHEN home_team_canonical_id = ? THEN m.away_score
                         ELSE m.home_score END AS conceded,
                    CASE WHEN home_team_canonical_id = ? THEN 1 ELSE 0 END AS is_home
                FROM matches m
                WHERE (home_team_canonical_id = ? OR away_team_canonical_id = ?)
                  AND kickoff_utc < ?
                  AND status = 'finished'
                ORDER BY kickoff_utc DESC
                LIMIT ?
            """

        try:
            rows = self.con.execute(base_query, params).fetchall()
        except Exception as exc:
            logger.debug("Form query failed: %s", exc)
            return _empty_form()

        if not rows:
            return _empty_form()

        match_ids = [r[0] for r in rows]
        scored = [r[1] for r in rows if r[1] is not None]
        conceded = [r[2] for r in rows if r[2] is not None]

        n = len(rows)
        wins = sum(1 for s, c in zip(scored, conceded) if s > c)
        draws = sum(1 for s, c in zip(scored, conceded) if s == c)
        losses = sum(1 for s, c in zip(scored, conceded) if s < c)
        btts = sum(1 for s, c in zip(scored, conceded) if s > 0 and c > 0)
        over25 = sum(1 for s, c in zip(scored, conceded) if s + c > 2.5)
        clean = sum(1 for c in conceded if c == 0)
        pts = wins * 3 + draws

        xg_stats = self._xg_for_matches(match_ids, team_id)

        return {
            "matches_played": n,
            "goals_scored_avg": _safe_div(sum(scored), n),
            "goals_conceded_avg": _safe_div(sum(conceded), n),
            "win_rate": _safe_div(wins, n),
            "draw_rate": _safe_div(draws, n),
            "loss_rate": _safe_div(losses, n),
            "points_per_game": _safe_div(pts, n),
            "clean_sheet_rate": _safe_div(clean, n),
            "btts_rate": _safe_div(btts, n),
            "over25_rate": _safe_div(over25, n),
            "xg_avg": xg_stats.get("xg_avg"),
            "xga_avg": xg_stats.get("xga_avg"),
            "npxg_avg": xg_stats.get("npxg_avg"),
        }

    def _xg_for_matches(self, match_ids: list[str], team_id: str) -> dict:
        if not match_ids:
            return {}
        placeholders = ",".join("?" * len(match_ids))
        try:
            rows = self.con.execute(
                f"""
                SELECT AVG(xg), AVG(xga), AVG(npxg)
                FROM xg_records
                WHERE team_canonical_id = ?
                  AND match_canonical_id IN ({placeholders})
                """,
                [team_id] + match_ids,
            ).fetchone()
        except Exception:
            return {}
        if rows is None:
            return {}
        return {"xg_avg": rows[0], "xga_avg": rows[1], "npxg_avg": rows[2]}

    # ------------------------------------------------------------------
    # Head to head
    # ------------------------------------------------------------------

    def _h2h(self, home_id: str, away_id: str, before: datetime, n: int = 5) -> dict[str, Any]:
        try:
            rows = self.con.execute(
                """
                SELECT home_team_canonical_id, away_team_canonical_id,
                       home_score, away_score
                FROM matches
                WHERE (
                    (home_team_canonical_id = ? AND away_team_canonical_id = ?)
                    OR
                    (home_team_canonical_id = ? AND away_team_canonical_id = ?)
                )
                  AND kickoff_utc < ?
                  AND status = 'finished'
                ORDER BY kickoff_utc DESC
                LIMIT ?
                """,
                [home_id, away_id, away_id, home_id, before.isoformat(), n],
            ).fetchall()
        except Exception as exc:
            logger.debug("H2H query failed: %s", exc)
            return _empty_h2h()

        if not rows:
            return _empty_h2h()

        total = len(rows)
        home_wins = away_wins = draws = btts = over25 = 0
        total_goals_list = []

        for hid, aid, hs, as_ in rows:
            if hs is None or as_ is None:
                continue
            tg = hs + as_
            total_goals_list.append(tg)

            if hid == home_id:
                if hs > as_: home_wins += 1
                elif hs < as_: away_wins += 1
                else: draws += 1
            else:
                if as_ > hs: home_wins += 1
                elif as_ < hs: away_wins += 1
                else: draws += 1

            if hs > 0 and as_ > 0:
                btts += 1
            if tg > 2.5:
                over25 += 1

        n_valid = len(total_goals_list)
        return {
            "h2h_matches": total,
            "h2h_home_win_rate": _safe_div(home_wins, total),
            "h2h_draw_rate": _safe_div(draws, total),
            "h2h_away_win_rate": _safe_div(away_wins, total),
            "h2h_avg_total_goals": _safe_div(sum(total_goals_list), n_valid),
            "h2h_btts_rate": _safe_div(btts, total),
            "h2h_over25_rate": _safe_div(over25, total),
        }

    # ------------------------------------------------------------------
    # Squad value
    # ------------------------------------------------------------------

    def _squad_value(self, home_id: str, away_id: str, before: datetime) -> dict[str, Any]:
        def team_value(team_id: str) -> float | None:
            try:
                row = self.con.execute(
                    """
                    SELECT SUM(market_value)
                    FROM squad_valuations
                    WHERE team_canonical_id = ?
                      AND valuation_date = (
                          SELECT MAX(valuation_date)
                          FROM squad_valuations
                          WHERE team_canonical_id = ?
                            AND valuation_date < ?
                      )
                    """,
                    [team_id, team_id, before.date().isoformat()],
                ).fetchone()
                return row[0] if row and row[0] else None
            except Exception:
                return None

        home_val = team_value(home_id)
        away_val = team_value(away_id)
        ratio = _safe_div(home_val, away_val) if home_val and away_val else None

        return {
            "home_squad_value_eur": home_val,
            "away_squad_value_eur": away_val,
            "squad_value_ratio": ratio,
        }

    # ------------------------------------------------------------------
    # Odds features
    # ------------------------------------------------------------------

    def _odds_features(self, match_canonical_id: str) -> dict[str, Any]:
        features: dict[str, Any] = {}

        for timing, is_flag in [("opening", "is_opening"), ("closing", "is_closing")]:
            for market, selections in [
                ("1x2", ["home", "draw", "away"]),
                ("over_under", ["over_2.5", "under_2.5"]),
                ("btts", ["yes", "no"]),
            ]:
                for sel in selections:
                    key = f"odds_{timing}_{market}_{_clean_sel(sel)}"
                    try:
                        row = self.con.execute(
                            f"""
                            SELECT AVG(price_decimal)
                            FROM odds_snapshots
                            WHERE match_canonical_id = ?
                              AND market = ?
                              AND selection = ?
                              AND {is_flag} = TRUE
                            """,
                            [match_canonical_id, market, sel],
                        ).fetchone()
                        features[key] = row[0] if row and row[0] else None
                    except Exception:
                        features[key] = None

        # implied probabilities
        for sel, label in [("home", "home"), ("draw", "draw"), ("away", "away")]:
            odds_key = f"odds_closing_1x2_{sel}"
            prob_key = f"implied_prob_closing_{label}"
            o = features.get(odds_key)
            features[prob_key] = round(1 / o, 4) if o and o > 0 else None

        # line movement (closing - opening)
        for market, sel in [
            ("1x2", "home"),
            ("1x2", "draw"),
            ("1x2", "away"),
            ("over_under", "over_25"),
        ]:
            sel_clean = _clean_sel(sel)
            open_key = f"odds_opening_{market}_{sel_clean}"
            close_key = f"odds_closing_{market}_{sel_clean}"
            move_key = f"odds_movement_{market}_{sel_clean}"
            o = features.get(open_key)
            c = features.get(close_key)
            features[move_key] = round(c - o, 4) if o and c else None

        return features

    # ------------------------------------------------------------------
    # Weather
    # ------------------------------------------------------------------

    def _weather(self, match_canonical_id: str) -> dict[str, Any]:
        try:
            row = self.con.execute(
                """
                SELECT temp_c, precipitation_mm, wind_kph, humidity_pct
                FROM weather_records
                WHERE match_canonical_id = ?
                LIMIT 1
                """,
                [match_canonical_id],
            ).fetchone()
        except Exception:
            row = None

        if row is None:
            return {
                "weather_temp_c": None,
                "weather_precipitation_mm": None,
                "weather_wind_kph": None,
                "weather_humidity_pct": None,
            }

        return {
            "weather_temp_c": row[0],
            "weather_precipitation_mm": row[1],
            "weather_wind_kph": row[2],
            "weather_humidity_pct": row[3],
        }

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def _context(self, home_id: str, away_id: str, kickoff: datetime, match: dict) -> dict[str, Any]:
        return {
            "matchday": match.get("matchday"),
            "is_weekend": int(kickoff.weekday() >= 5),
            "kickoff_hour": kickoff.hour,
            "days_rest_home": self._days_since_last_match(home_id, kickoff),
            "days_rest_away": self._days_since_last_match(away_id, kickoff),
        }

    def _days_since_last_match(self, team_id: str, before: datetime) -> int | None:
        try:
            row = self.con.execute(
                """
                SELECT MAX(kickoff_utc)
                FROM matches
                WHERE (home_team_canonical_id = ? OR away_team_canonical_id = ?)
                  AND kickoff_utc < ?
                  AND status = 'finished'
                """,
                [team_id, team_id, before.isoformat()],
            ).fetchone()
        except Exception:
            return None

        if row is None or row[0] is None:
            return None

        last = row[0]
        if isinstance(last, str):
            last = datetime.fromisoformat(last)
        return (before - last).days

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_match(self, match_canonical_id: str) -> dict | None:
        try:
            row = self.con.execute(
                """
                SELECT canonical_id, kickoff_utc, status,
                    home_team_canonical_id, away_team_canonical_id,
                    home_score, away_score, matchday, competition, season
                FROM matches
                WHERE canonical_id = ?
                """,
                [match_canonical_id],
            ).fetchone()
        except Exception as exc:
            logger.error("Match lookup failed: %s", exc)
            return None

        if row is None:
            return None

        keys = [
            "canonical_id", "kickoff_utc", "status",
            "home_team_canonical_id", "away_team_canonical_id",
            "home_score", "away_score", "matchday", "competition", "season",
        ]
        d = dict(zip(keys, row))

        if isinstance(d["kickoff_utc"], str):
            d["kickoff_utc"] = datetime.fromisoformat(d["kickoff_utc"])

        return d

# ------------------------------------------------------------------
# Modular Helpers
# ------------------------------------------------------------------

def _safe_div(a, b) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return round(a / b, 4)

def _empty_form() -> dict:
    return {
        "matches_played": 0,
        "goals_scored_avg": None,
        "goals_conceded_avg": None,
        "win_rate": None,
        "draw_rate": None,
        "loss_rate": None,
        "points_per_game": None,
        "clean_sheet_rate": None,
        "btts_rate": None,
        "over25_rate": None,
        "xg_avg": None,
        "xga_avg": None,
        "npxg_avg": None,
    }
def _empty_h2h() -> dict:
    return {
        "h2h_matches": 0,
        "h2h_home_win_rate": None,
        "h2h_draw_rate": None,
        "h2h_away_win_rate": None,
        "h2h_avg_total_goals": None,
        "h2h_btts_rate": None,
        "h2h_over25_rate": None,
    }

def _prefix(d: dict, prefix: str) -> dict:
    return {f"{prefix}__{k}": v for k, v in d.items()}