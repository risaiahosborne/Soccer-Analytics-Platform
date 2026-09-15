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
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ss_env.SoccerSystem.data_storage.duckdb_store import DuckDBStore

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
        features.update(self._volatility(features))

        # --- EV + Kelly ---
        features.update(self._ev_kelly(odds, features))

        # --- weather ---
        features.update(self._weather(match_canonical_id))

        # --- context ---
        features.update(self._context(home_id, away_id, kickoff, match))

        return features
    def build_for_fixture(
        self,
        home_team_id: str,
        away_team_id: str,
        kickoff: datetime,
        competition: str | None = None,
        season: str | None = None,
        match_canonical_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Build features for a FUTURE fixture (no scores yet).

        Uses only data BEFORE the given kickoff:
        - past matches
        - team form
        - head-to-head
        - squad valuations
        - odds snapshots (if any exist before kickoff)
        - context (rest days)
        - weather (if pre-fetched)
        """

        features: dict[str, Any] = {}

        if match_canonical_id:
            features["match_canonical_id"] = match_canonical_id

        # --- team metadata ---
        features.update(self._team_metadata(home_team_id, prefix="home"))
        features.update(self._team_metadata(away_team_id, prefix="away"))

        # --- match metadata (no scores, no status) ---
        features.update({
            "competition": competition,
            "season": season,
            "kickoff_hour": kickoff.hour,
            "kickoff_weekday": kickoff.weekday(),
            "is_weekend": int(kickoff.weekday() >= 5),
            "matchday": None,  # can be filled later if you have it
        })

        # --- NO targets for future fixtures ---
        features.update({
            "target_result": None,
            "target_btts": None,
            "target_over25": None,
            "target_home_score": None,
            "target_away_score": None,
            "target_total_goals": None,
        })

        # --- rolling form ---
        for window in FORM_WINDOWS:
            home_overall = self._team_form(home_team_id, kickoff, window, None)
            away_overall = self._team_form(away_team_id, kickoff, window, None)
            home_home = self._team_form(home_team_id, kickoff, window, "home")
            away_away = self._team_form(away_team_id, kickoff, window, "away")

            features.update(_prefix(home_overall, f"home_overall_l{window}"))
            features.update(_prefix(away_overall, f"away_overall_l{window}"))
            features.update(_prefix(home_home, f"home_home_l{window}"))
            features.update(_prefix(away_away, f"away_away_l{window}"))

        # --- head to head ---
        features.update(self._h2h(home_team_id, away_team_id, kickoff, n=5))

        # --- squad value ---
        features.update(self._squad_value(home_team_id, away_team_id, kickoff))

        # --- odds (if any pre-match odds exist) ---
        if match_canonical_id:
            odds = self._odds_features(match_canonical_id)
        else:
            odds = {}
        features.update(odds)

        # --- implied probabilities ---
        features.update(self._implied_probabilities(odds))

        # --- volatility ---
        features.update(self._volatility(features))

        # --- EV + Kelly (only if odds exist) ---
        features.update(self._ev_kelly(odds, features))

        # --- weather (if pre-fetched for this fixture) ---
        if match_canonical_id:
            features.update(self._weather(match_canonical_id))

        # --- context (rest days, etc.) ---
        features.update(self._context(home_team_id, away_team_id, kickoff, match={}))

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
            "matchday": match.get("matchday"),
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
        """
        Compute rolling form for a team before a given kickoff time.
        window = number of past matches to include
        venue_filter = None | "home" | "away"
        """
        params = [team_id, team_id, before]

        if venue_filter == "home":
            params = [team_id, before]
            sql = f"""
                SELECT home_score, away_score
                FROM matches
                WHERE home_team_canonical_id = ?
                  AND kickoff_utc < ?
                  AND status = 'finished'
                ORDER BY kickoff_utc DESC
                LIMIT {window}
            """
        elif venue_filter == "away":
            params = [team_id, before]
            sql = f"""
                SELECT home_score, away_score
                FROM matches
                WHERE away_team_canonical_id = ?
                  AND kickoff_utc < ?
                  AND status = 'finished'
                ORDER BY kickoff_utc DESC
                LIMIT {window}
            """
        else:
            sql = f"""
                SELECT home_score, away_score,
                       home_team_canonical_id, away_team_canonical_id
                FROM matches
                WHERE (home_team_canonical_id = ? OR away_team_canonical_id = ?)
                  AND kickoff_utc < ?
                  AND status = 'finished'
                ORDER BY kickoff_utc DESC
                LIMIT {window}
            """

        rows = self.con.execute(sql, params).fetchall()

        if not rows:
            return {
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "goals_for": 0,
                "goals_against": 0,
                "points": 0,
            }

        wins = draws = losses = gf = ga = 0

        for r in rows:
            if venue_filter in ("home", "away"):
                home_score, away_score = r
                if venue_filter == "home":
                    gf += home_score
                    ga += away_score
                    if home_score > away_score:
                        wins += 1
                    elif home_score == away_score:
                        draws += 1
                    else:
                        losses += 1
                else:
                    gf += away_score
                    ga += home_score
                    if away_score > home_score:
                        wins += 1
                    elif away_score == home_score:
                        draws += 1
                    else:
                        losses += 1
            else:
                home_score, away_score, home_id, away_id = r
                if home_id == team_id:
                    gf += home_score
                    ga += away_score
                    if home_score > away_score:
                        wins += 1
                    elif home_score == away_score:
                        draws += 1
                    else:
                        losses += 1
                else:
                    gf += away_score
                    ga += home_score
                    if away_score > home_score:
                        wins += 1
                    elif away_score == home_score:
                        draws += 1
                    else:
                        losses += 1

        form_dict = {
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "goals_for": gf,
            "goals_against": ga,
            "points": wins * 3 + draws,
        }

        return form_dict

    # ------------------------------------------------------------------
    # Head to head
    # ------------------------------------------------------------------

    def _h2h(self, home_id: str, away_id: str, before: datetime, n: int = 5):
        """
        Head-to-head record between two teams before a given kickoff.
        """
        rows = self.con.execute(
            f"""
            SELECT home_team_canonical_id, away_team_canonical_id,
                   home_score, away_score
            FROM matches
            WHERE status = 'finished'
              AND kickoff_utc < ?
              AND (
                    (home_team_canonical_id = ? AND away_team_canonical_id = ?)
                 OR (home_team_canonical_id = ? AND away_team_canonical_id = ?)
              )
            ORDER BY kickoff_utc DESC
            LIMIT {n}
            """,
            [before, home_id, away_id, away_id, home_id],
        ).fetchall()

        if not rows:
            return {
                "h2h_matches": 0,
                "h2h_home_wins": 0,
                "h2h_away_wins": 0,
                "h2h_draws": 0,
                "h2h_home_goals_for": 0,
                "h2h_home_goals_against": 0,
                "h2h_away_goals_for": 0,
                "h2h_away_goals_against": 0,
            }

        home_wins = away_wins = draws = 0
        hgf = hga = agf = aga = 0

        for h_id, a_id, hs, as_ in rows:
            if h_id == home_id:
                hgf += hs
                hga += as_
                agf += as_
                aga += hs
                if hs > as_:
                    home_wins += 1
                elif hs < as_:
                    away_wins += 1
                else:
                    draws += 1
            else:
                hgf += as_
                hga += hs
                agf += hs
                aga += as_
                if as_ > hs:
                    home_wins += 1
                elif as_ < hs:
                    away_wins += 1
                else:
                    draws += 1

        return {
            "h2h_matches": len(rows),
            "h2h_home_wins": home_wins,
            "h2h_away_wins": away_wins,
            "h2h_draws": draws,
            "h2h_home_goals_for": hgf,
            "h2h_home_goals_against": hga,
            "h2h_away_goals_for": agf,
            "h2h_away_goals_against": aga,
        }

    # ------------------------------------------------------------------
    # Squad value
    # ------------------------------------------------------------------

    def _squad_value(self, home_id: str, away_id: str, before: datetime):
        """
        Latest squad valuation for each team before kickoff.
        Uses valuation_date instead of captured_at.
        """

        rows = self.con.execute(
            """
            SELECT team_canonical_id, market_value, currency, valuation_date
            FROM squad_valuations
            WHERE team_canonical_id IN (?, ?)
            AND valuation_date <= ?
            ORDER BY valuation_date DESC
            """,
            [home_id, away_id, before],
        ).fetchall()

        home_val = away_val = None
        home_cur = away_cur = None

        for team_id, value, currency, vdate in rows:
            if team_id == home_id and home_val is None:
                home_val = value
                home_cur = currency
            elif team_id == away_id and away_val is None:
                away_val = value
                away_cur = currency

        return {
            "home_squad_value": home_val,
            "home_squad_currency": home_cur,
            "away_squad_value": away_val,
            "away_squad_currency": away_cur,
            "squad_value_diff": (
                home_val - away_val if home_val is not None and away_val is not None else None
            ),
        }

    # ------------------------------------------------------------------
    # Odds features
    # ------------------------------------------------------------------

    def _odds_features(self, match_canonical_id: str) -> dict[str, Any]:
        """
        Closing 1x2 odds per selection; fallback to latest snapshot if no closing flag.
        """
        rows = self.con.execute(
            """
            SELECT market, selection, price_decimal, is_opening, is_closing, captured_at
            FROM odds_snapshots
            WHERE match_canonical_id = ?
              AND market = '1x2'
            ORDER BY is_closing DESC, captured_at DESC
            """,
            [match_canonical_id],
        ).fetchall()

        odds_dict: dict[str, Any] = {
            "odds_closing_1x2_home": None,
            "odds_closing_1x2_draw": None,
            "odds_closing_1x2_away": None,
        }

        for market, sel, price, is_opening, is_closing, ts in rows:
            key = f"odds_closing_1x2_{_clean_sel(sel)}"
            if odds_dict.get(key) is None and price is not None:
                odds_dict[key] = float(price)

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

    def _volatility(self, features: dict) -> dict[str, Any]:
        import math

        p_home = features.get("implied_prob_home")
        p_draw = features.get("implied_prob_draw")
        p_away = features.get("implied_prob_away")

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
        row = self.con.execute(
            """
            SELECT *
            FROM weather_records
            WHERE match_canonical_id = ?
            ORDER BY ingested_at DESC
            LIMIT 1
            """,
            [match_canonical_id],
        ).fetchone()

        if row is None:
            return {
                "weather_temp_c": None,
                "weather_wind_speed": None,
                "weather_precip_mm": None,
            }

        cols = [c[0] for c in self.con.execute("DESCRIBE weather_records").fetchall()]
        d = dict(zip(cols, row))

        out = {}
        for k, v in d.items():
            if k in ("match_canonical_id",):
                continue
            out[f"weather_{k}"] = v
        return out

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    def _context(self, home_id: str, away_id: str, kickoff: datetime, match: dict):
        """
        Simple contextual features: rest days, season, competition.
        """
        def last_match(team_id: str):
            row = self.con.execute(
                """
                SELECT kickoff_utc
                FROM matches
                WHERE status = 'finished'
                  AND kickoff_utc < ?
                  AND (home_team_canonical_id = ? OR away_team_canonical_id = ?)
                ORDER BY kickoff_utc DESC
                LIMIT 1
                """,
                [kickoff, team_id, team_id],
            ).fetchone()
            return row[0] if row else None

        home_last = last_match(home_id)
        away_last = last_match(away_id)

        home_rest = (kickoff - home_last).days if home_last else None
        away_rest = (kickoff - away_last).days if away_last else None

        return {
            "home_rest_days": home_rest,
            "away_rest_days": away_rest,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_match(self, match_canonical_id: str) -> dict | None:
        row = self.con.execute(
            """
            SELECT *
            FROM matches
            WHERE canonical_id = ?
            """,
            [match_canonical_id],
        ).fetchone()

        if row is None:
            return None

        cols = [c[0] for c in self.con.execute("DESCRIBE matches").fetchall()]
        match_dict = dict(zip(cols, row))
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

    store = DuckDBStore("ss_env/SoccerSystem/data/soccer_smoketest.duckdb")
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