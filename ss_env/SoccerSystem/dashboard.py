import streamlit as st
import pandas as pd

# ------------------------------------------------------------
# Data Loaders
# ------------------------------------------------------------

@st.cache_data
def load_predictions():
    # Correct path for your repo structure
    return pd.read_csv("model_training/predictions.csv")

@st.cache_data
def load_matches_and_teams():
    from data_storage.duckdb_store import DuckDBStore
    store = DuckDBStore("data/soccer_smoketest.duckdb")

    matches = store.conn.execute("SELECT * FROM matches").fetchdf()
    teams   = store.conn.execute("SELECT * FROM teams").fetchdf()
    store.close()

    # Join home team
    matches = matches.merge(
        teams[["canonical_id", "name", "short_name"]],
        left_on="home_team_canonical_id",
        right_on="canonical_id",
        how="left"
    ).rename(columns={
        "name": "home_team",
        "short_name": "home_team_short"
    })

    # Join away team
    matches = matches.merge(
        teams[["canonical_id", "name", "short_name"]],
        left_on="away_team_canonical_id",
        right_on="canonical_id",
        how="left"
    ).rename(columns={
        "name": "away_team",
        "short_name": "away_team_short"
    })

    matches = matches.rename(columns={
        "kickoff_utc": "kickoff_time_utc"
    })

    return matches

@st.cache_data
def load_odds_snapshots():
    from data_storage.duckdb_store import DuckDBStore
    store = DuckDBStore("data/soccer_smoketest.duckdb")
    df = store.conn.execute("""
        SELECT
            match_canonical_id,
            market,
            selection,
            price_decimal,
            captured_at,
            is_closing
        FROM odds_snapshots
    """).fetchdf()
    store.close()
    return df

@st.cache_data
def load_backtest(preds, matches, odds):
    # Merge predictions + match metadata + odds
    df = preds.merge(
        matches[["canonical_id", "kickoff_time_utc", "home_score", "away_score"]],
        left_on="match_canonical_id",
        right_on="canonical_id",
        how="left"
    ).merge(
        odds,
        on="match_canonical_id",
        how="left"
    )

    # ------------------------------------------------------------
    # Actual match result (H/D/A)
    # ------------------------------------------------------------
    df["actual_result"] = df.apply(
        lambda r: "H" if r.home_score > r.away_score
        else "A" if r.away_score > r.home_score
        else "D",
        axis=1
    )

    # ------------------------------------------------------------
    # Value betting rule using renamed odds columns
    # ------------------------------------------------------------
    df["bet_home"] = df["prob_home"] > (1 / df["decimal_odds_home"])
    df["bet_draw"] = df["prob_draw"] > (1 / df["decimal_odds_draw"])
    df["bet_away"] = df["prob_away"] > (1 / df["decimal_odds_away"])

    # ------------------------------------------------------------
    # PnL calculation
    # ------------------------------------------------------------
    def pnl(row, bet_flag, market, odds):
        if not bet_flag:
            return 0
        if row.actual_result == market:
            return odds - 1
        return -1

    df["pnl_home"] = df.apply(lambda r: pnl(r, r.bet_home, "H", r.decimal_odds_home), axis=1)
    df["pnl_draw"] = df.apply(lambda r: pnl(r, r.bet_draw, "D", r.decimal_odds_draw), axis=1)
    df["pnl_away"] = df.apply(lambda r: pnl(r, r.bet_away, "A", r.decimal_odds_away), axis=1)

    df["pnl"] = df["pnl_home"] + df["pnl_draw"] + df["pnl_away"]

    # ------------------------------------------------------------
    # Optional: label markets for summary tables
    # ------------------------------------------------------------
    df["market"] = df.apply(
        lambda r: "Home" if r.bet_home else
                  "Draw" if r.bet_draw else
                  "Away" if r.bet_away else
                  "No Bet",
        axis=1
    )

    # ------------------------------------------------------------
    # Optional: mark winning bets
    # ------------------------------------------------------------
    df["won"] = df["pnl"] > 0

    return df



def extract_1x2_odds(odds_df):
    """Convert OddsSnapshot rows into home/draw/away decimal odds per match."""
    out = []

    for match_id, group in odds_df.groupby("match_canonical_id"):
        home = group[(group.market == "match_1x2") & (group.selection == "home")]["price_decimal"]
        draw = group[(group.market == "match_1x2") & (group.selection == "draw")]["price_decimal"]
        away = group[(group.market == "match_1x2") & (group.selection == "away")]["price_decimal"]

        out.append({
            "match_canonical_id": match_id,
            "odds_home": home.iloc[-1] if len(home) else None,
            "odds_draw": draw.iloc[-1] if len(draw) else None,
            "odds_away": away.iloc[-1] if len(away) else None,
        })

    return pd.DataFrame(out).rename(columns={
        "odds_home": "decimal_odds_home",
        "odds_draw": "decimal_odds_draw",
        "odds_away": "decimal_odds_away",
    })

# ------------------------------------------------------------
# Backtest Generator
# ------------------------------------------------------------

@st.cache_data
def generate_backtest(preds, matches):
    # We need actual match results
    if "home_score" not in matches.columns or "away_score" not in matches.columns:
        return pd.DataFrame()

    df = preds.merge(
        matches[["canonical_id", "kickoff_utc", "home_score", "away_score"]],
        left_on="match_canonical_id",
        right_on="canonical_id",
        how="left"
    )

    # Compute actual result
    df["actual_result"] = df.apply(
        lambda r: "H" if r.home_score > r.away_score
        else "A" if r.away_score > r.home_score
        else "D",
        axis=1
    )

    # Simple odds assumption (replace with real odds later)
    df["odds_home"] = 1 / df["prob_home"]
    df["odds_draw"] = 1 / df["prob_draw"]
    df["odds_away"] = 1 / df["prob_away"]

    # Value betting rule: bet when model probability > implied probability
    df["bet_home"] = df["prob_home"] > (1 / df["decimal_odds_home"])
    df["bet_draw"] = df["prob_draw"] > (1 / df["decimal_odds_draw"])
    df["bet_away"] = df["prob_away"] > (1 / df["decimal_odds_away"])


    # Compute PnL for each market
    def pnl(row, bet_flag, market, odds):
        if not bet_flag:
            return 0
        if row.actual_result == market:
            return odds - 1
        return -1

    df["pnl_home"] = df.apply(lambda r: pnl(r, r.bet_home, "H", r.decimal_odds_home), axis=1)
    df["pnl_draw"] = df.apply(lambda r: pnl(r, r.bet_draw, "D", r.decimal_odds_draw), axis=1)
    df["pnl_away"] = df.apply(lambda r: pnl(r, r.bet_away, "A", r.decimal_odds_away), axis=1)

    # Total PnL per match
    df["pnl"] = df["pnl_home"] + df["pnl_draw"] + df["pnl_away"]

    # Sort chronologically
    df["kickoff_time_utc"] = pd.to_datetime(df["kickoff_time_utc"])
    df = df.sort_values("kickoff_time_utc")

    # Compute bankroll curve
    df["bankroll"] = df["pnl"].cumsum()

    return df

# ------------------------------------------------------------
# Sidebar Navigation
# ------------------------------------------------------------

st.sidebar.title("Soccer Prediction Dashboard")
page = st.sidebar.radio(
    "Navigate",
    ["Match Predictions", "Backtest Results", "Bankroll Curve", "Model Overview"]
)


# ------------------------------------------------------------
# Page: Match Predictions
# ------------------------------------------------------------

if page == "Match Predictions":
    st.title("Match Predictions")

    preds = load_predictions()
    matches = load_matches_and_teams()

    # Correct match ID columns
    preds_id_col = "match_canonical_id"
    matches_id_col = "canonical_id"

    # Merge predictions with match metadata
    merged = preds.merge(
        matches[[matches_id_col, "home_team", "away_team", "kickoff_time_utc"]],
        left_on=preds_id_col,
        right_on=matches_id_col,
        how="left"
    )

    # UI selector
    match_id = st.selectbox(
        "Select Match",
        merged[preds_id_col].unique()
    )

    # Selected row
    row = merged[merged[preds_id_col] == match_id].iloc[0]

    st.subheader(f"{row.home_team} vs {row.away_team} — Match Preview")
    st.write(f"Kickoff (UTC): {row.kickoff_time_utc}")

    # Result probabilities
    st.metric("Home Win Probability (1)", f"{row.prob_home:.2%}")
    st.metric("Draw Probability (X)", f"{row.prob_draw:.2%}")
    st.metric("Away Win Probability (2)", f"{row.prob_away:.2%}")

    # Auto-detect BTTS column
    btts_cols = [c for c in merged.columns if "btts" in c.lower()]
    if btts_cols:
        st.metric("Both Teams to Score — Yes", f"{row[btts_cols[0]]:.2%}")
    else:
        st.warning("BTTS probability column not found.")

    # Auto-detect Over 2.5 column
    over_cols = [c for c in merged.columns if "over" in c.lower()]
    if over_cols:
        st.metric("Over 2.5 Goals Probability", f"{row[over_cols[0]]:.2%}")
    else:
        st.warning("Over 2.5 probability column not found.")

    st.write("Raw Data")
    st.dataframe(row.to_frame().T)


# ------------------------------------------------------------
# Page: Backtest Results
# ------------------------------------------------------------

elif page == "Backtest Results":
    st.title("Generated Backtest — No CSV Required")

    preds = load_predictions()
    matches = load_matches_and_teams()
    odds_raw = load_odds_snapshots()
    odds = extract_1x2_odds(odds_raw)

    bt = load_backtest(preds, matches, odds)

    if bt.empty:
        st.warning("Backtest could not be generated.")
    else:
        st.subheader("Summary Statistics")
        st.dataframe(bt.describe())

        if "market" in bt.columns:
            st.subheader("Performance by Market")
            market_stats = bt.groupby("market").agg(
                bets=("won", "count"),
                wins=("won", "sum"),
                pnl=("pnl", "sum"),
                avg_odds=("decimal_odds", "mean")
            ).round(2)

            market_stats = market_stats.rename(columns={
                "bets": "Total Bets",
                "wins": "Winning Bets",
                "pnl": "Total Profit/Loss (£)",
                "avg_odds": "Average Odds"
            })
            st.dataframe(market_stats)

        st.subheader("All Bets")
        st.dataframe(bt)

# ------------------------------------------------------------
# Page: Bankroll Curve
# ------------------------------------------------------------

elif page == "Bankroll Curve":
    st.title("Bankroll Performance — Value Betting Strategy")

    preds = load_predictions()
    matches = load_matches_and_teams()
    odds_raw = load_odds_snapshots()
    odds = extract_1x2_odds(odds_raw)

    df = preds.merge(
        matches[["canonical_id", "kickoff_time_utc", "home_score", "away_score"]],
        left_on="match_canonical_id",
        right_on="canonical_id",
        how="left"
    ).merge(
        odds,
        on="match_canonical_id",
        how="left"
    )

    # ------------------------------------------------------------
    # Compute actual result + bets + PnL
    # ------------------------------------------------------------

    df["actual_result"] = df.apply(
        lambda r: "H" if r.home_score > r.away_score
        else "A" if r.away_score > r.home_score
        else "D",
        axis=1
    )

    # Use renamed odds columns
    df["bet_home"] = df["prob_home"] > (1 / df["decimal_odds_home"])
    df["bet_draw"] = df["prob_draw"] > (1 / df["decimal_odds_draw"])
    df["bet_away"] = df["prob_away"] > (1 / df["decimal_odds_away"])

    def pnl(row, bet_flag, market, odds):
        if not bet_flag:
            return 0
        if row.actual_result == market:
            return odds - 1
        return -1

    df["pnl_home"] = df.apply(lambda r: pnl(r, r.bet_home, "H", r.decimal_odds_home), axis=1)
    df["pnl_draw"] = df.apply(lambda r: pnl(r, r.bet_draw, "D", r.decimal_odds_draw), axis=1)
    df["pnl_away"] = df.apply(lambda r: pnl(r, r.bet_away, "A", r.decimal_odds_away), axis=1)

    df["pnl"] = df["pnl_home"] + df["pnl_draw"] + df["pnl_away"]

    # ------------------------------------------------------------
    # Bankroll curve
    # ------------------------------------------------------------

    df["kickoff_time_utc"] = pd.to_datetime(df["kickoff_time_utc"])
    df = df.sort_values("kickoff_time_utc")
    df["bankroll"] = df["pnl"].cumsum()

    # ------------------------------------------------------------
    # Rename columns for display (AFTER calculations)
    # ------------------------------------------------------------

    df_display = df.rename(columns={
        "actual_result": "Actual Match Result (H/D/A)",
        "pnl": "Profit/Loss (£)",
        "bankroll": "Cumulative Bankroll (£)",
        "kickoff_time_utc": "Kickoff Time (UTC)",
        "decimal_odds_home": "Home Odds (Decimal)",
        "decimal_odds_draw": "Draw Odds (Decimal)",
        "decimal_odds_away": "Away Odds (Decimal)",
    })

    # ------------------------------------------------------------
    # Chart
    # ------------------------------------------------------------

    import altair as alt
    chart = (
        alt.Chart(df_display)
        .mark_line(color="#1f77b4", strokeWidth=3)
        .encode(
            x=alt.X("Kickoff Time (UTC):T", title="Date"),
            y=alt.Y("Cumulative Bankroll (£):Q", title="Bankroll (£)"),
            tooltip=["Kickoff Time (UTC)", "Profit/Loss (£)", "Cumulative Bankroll (£)"]
        )
        .interactive()
    )

    st.altair_chart(chart, use_container_width=True)

    st.dataframe(df_display[[
        "Kickoff Time (UTC)",
        "Actual Match Result (H/D/A)",
        "Profit/Loss (£)",
        "Cumulative Bankroll (£)",
        "Home Odds (Decimal)",
        "Draw Odds (Decimal)",
        "Away Odds (Decimal)"
    ]])

# ------------------------------------------------------------
# Page: Model Overview
# ------------------------------------------------------------

elif page == "Model Overview":
    st.title("Model Overview — Probability Engine")

    st.write("""
    This dashboard displays predictions generated by the calibrated Logistic Regression models
    trained on multiple seasons of Premier League data.

    **Training Data:** 2023/24 + 2024/25  
    **Testing Data:** 2025/26  
    **Model Type:** Calibrated Logistic Regression  
    **Training Seasons:** 2023/24 and 2024/25  
    **Evaluation Season:** 2025/26  
    """)

    preds = load_predictions()
    st.subheader("Prediction Sample")
    st.dataframe(preds.head())