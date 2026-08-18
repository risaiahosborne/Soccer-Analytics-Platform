import streamlit as st
import pandas as pd
import subprocess
import sys

sys.path.append("ss_env/SoccerSystem")

# ------------------------------------------------------------
# Pipeline trigger
# ------------------------------------------------------------
if st.button("Refresh System"):
    subprocess.run("python3 run_pipeline.py", shell=True)
    st.success("Pipeline completed!")

# ------------------------------------------------------------
# Shared resources
# ------------------------------------------------------------

@st.cache_resource
def get_store():
    from data_storage.duckdb_store import DuckDBStore
    return DuckDBStore("ss_env/SoccerSystem/data/soccer_smoketest.duckdb")


# ------------------------------------------------------------
# Data loaders
# ------------------------------------------------------------

@st.cache_data
def load_predictions():
    return pd.read_csv("model_training/predictions.csv")


@st.cache_data
def load_matches_and_teams():
    store = get_store()

    matches = store.conn.execute("SELECT * FROM matches").fetchdf()
    teams = store.conn.execute("SELECT * FROM teams").fetchdf()

    # Join home team
    matches = matches.merge(
        teams[["canonical_id", "name", "short_name"]],
        left_on="home_team_canonical_id",
        right_on="canonical_id",
        how="left",
    ).rename(
        columns={
            "name": "home_team",
            "short_name": "home_team_short",
        }
    )

    # Join away team
    matches = matches.merge(
        teams[["canonical_id", "name", "short_name"]],
        left_on="away_team_canonical_id",
        right_on="canonical_id",
        how="left",
    ).rename(
        columns={
            "name": "away_team",
            "short_name": "away_team_short",
        }
    )

    # Standardize kickoff column
    matches = matches.rename(columns={"kickoff_utc": "kickoff_time_utc"})

    return matches


@st.cache_data
def load_odds_snapshots():
    store = get_store()
    df = store.conn.execute(
        """
        SELECT
            match_canonical_id,
            market,
            selection,
            price_decimal,
            captured_at,
            is_closing
        FROM odds_snapshots
        """
    ).fetchdf()
    return df


@st.cache_data
def load_backtest(preds, matches, odds):
    # Merge predictions + match metadata + odds
    df = preds.merge(
        matches[["match_canonical_id", "kickoff_time_utc", "home_score", "away_score"]],
        on="match_canonical_id",
        how="left",
    ).merge(
        odds,
        on="match_canonical_id",
        how="left",
    )

    # Actual match result (H/D/A)
    df["actual_result"] = df.apply(
        lambda r: "H"
        if r.home_score > r.away_score
        else "A"
        if r.away_score > r.home_score
        else "D",
        axis=1,
    )

    # Value betting rule
    df["bet_home"] = df["prob_home"] > (1 / df["decimal_odds_home"])
    df["bet_draw"] = df["prob_draw"] > (1 / df["decimal_odds_draw"])
    df["bet_away"] = df["prob_away"] > (1 / df["decimal_odds_away"])

    def pnl(row, bet_flag, market, odds):
        if not bet_flag:
            return 0
        if row.actual_result == market:
            return odds - 1
        return -1

    df["pnl_home"] = df.apply(
        lambda r: pnl(r, r.bet_home, "H", r.decimal_odds_home), axis=1
    )
    df["pnl_draw"] = df.apply(
        lambda r: pnl(r, r.bet_draw, "D", r.decimal_odds_draw), axis=1
    )
    df["pnl_away"] = df.apply(
        lambda r: pnl(r, r.bet_away, "A", r.decimal_odds_away), axis=1
    )

    df["pnl"] = df["pnl_home"] + df["pnl_draw"] + df["pnl_away"]

    df["market"] = df.apply(
        lambda r: "Home"
        if r.bet_home
        else "Draw"
        if r.bet_draw
        else "Away"
        if r.bet_away
        else "No Bet",
        axis=1,
    )

    df["won"] = df["pnl"] > 0

    return df


@st.cache_data
def get_team_form(team_id, n=5):
    store = get_store()
    df = store.conn.execute(
        """
        SELECT
            m.match_canonical_id,
            m.kickoff_utc,
            m.home_team_canonical_id,
            m.away_team_canonical_id,
            m.home_score,
            m.away_score
        FROM matches m
        WHERE m.home_team_canonical_id = ? OR m.away_team_canonical_id = ?
        ORDER BY m.kickoff_utc DESC
        LIMIT ?
        """,
        [team_id, team_id, n],
    ).fetchdf()

    if df.empty:
        return None

    results = []
    rows = []

    matches = load_matches_and_teams()

    for _, r in df.iterrows():
        if r.home_team_canonical_id == team_id:
            gf, ga = r.home_score, r.away_score
            opponent = matches[
                matches["match_canonical_id"] == r.match_canonical_id
            ]["away_team"].iloc[0]
            venue = "Home"
        else:
            gf, ga = r.away_score, r.home_score
            opponent = matches[
                matches["match_canonical_id"] == r.match_canonical_id
            ]["home_team"].iloc[0]
            venue = "Away"

        if gf > ga:
            res = "W"
        elif gf < ga:
            res = "L"
        else:
            res = "D"

        results.append(res)

        rows.append(
            {
                "Date": pd.to_datetime(r.kickoff_utc).strftime("%Y-%m-%d"),
                "Venue": venue,
                "Opponent": opponent,
                "Score": f"{gf}-{ga}",
                "Result": res,
            }
        )

    form_strength = results.count("W") * 3 + results.count("D")

    return {
        "form": " ".join(results),
        "wins": results.count("W"),
        "draws": results.count("D"),
        "losses": results.count("L"),
        "form_strength": form_strength,
        "matches": pd.DataFrame(rows),
    }


def extract_1x2_odds(odds_df):
    out = []

    for match_id, group in odds_df.groupby("match_canonical_id"):
        home = group[
            (group.market == "match_1x2") & (group.selection == "home")
        ]["price_decimal"]
        draw = group[
            (group.market == "match_1x2") & (group.selection == "draw")
        ]["price_decimal"]
        away = group[
            (group.market == "match_1x2") & (group.selection == "away")
        ]["price_decimal"]

        out.append(
            {
                "match_canonical_id": match_id,
                "odds_home": home.iloc[-1] if len(home) else None,
                "odds_draw": draw.iloc[-1] if len(draw) else None,
                "odds_away": away.iloc[-1] if len(away) else None,
            }
        )

    return pd.DataFrame(out).rename(
        columns={
            "odds_home": "decimal_odds_home",
            "odds_draw": "decimal_odds_draw",
            "odds_away": "decimal_odds_away",
        }
    )


# ------------------------------------------------------------
# Sidebar navigation
# ------------------------------------------------------------

st.sidebar.title("Soccer Prediction Dashboard")
page = st.sidebar.radio(
    "Navigate",
    ["Match Predictions", "Backtest Results", "Bankroll Curve", "Model Overview"],
)

# ------------------------------------------------------------
# Page: Match Predictions
# ------------------------------------------------------------
if page == "Match Predictions":
    st.title("Match Predictions")

    preds = load_predictions()
    matches = load_matches_and_teams()
    odds = extract_1x2_odds(load_odds_snapshots())

    merged = (
        preds.merge(
            matches[
                [
                    "match_canonical_id",
                    "home_team",
                    "away_team",
                    "kickoff_time_utc",
                    "home_team_canonical_id",
                    "away_team_canonical_id",
                ]
            ],
            on="match_canonical_id",
            how="left",
        )
        .merge(
            odds,
            on="match_canonical_id",
            how="left",
        )
    )

    match_id = st.selectbox(
        "Select Match",
        merged["match_canonical_id"].unique(),
    )

    row = merged[merged["match_canonical_id"] == match_id].iloc[0]

    st.subheader(f"{row.home_team} vs {row.away_team}")
    st.write(row.kickoff_time_utc.strftime("%Y-%m-%d %H:%M UTC"))

    # Team form
    home_id = row.home_team_canonical_id
    away_id = row.away_team_canonical_id

    home_form = get_team_form(home_id)
    away_form = get_team_form(away_id)

    st.subheader("Team Form — Last 5 Matches")
    form_cols = st.columns(2)

    if home_form:
        form_cols[0].markdown(f"### {row.home_team}")
        form_cols[0].write(f"Form: **{home_form['form']}**")
        form_cols[0].write(f"Form Strength: **{home_form['form_strength']}**")
        form_cols[0].write(
            f"W-D-L: {home_form['wins']}-{home_form['draws']}-{home_form['losses']}"
        )
        form_cols[0].write("Recent Matches:")
        form_cols[0].dataframe(home_form["matches"])
    else:
        form_cols[0].warning("No recent matches found.")

    if away_form:
        form_cols[1].markdown(f"### {row.away_team}")
        form_cols[1].write(f"Form: **{away_form['form']}**")
        form_cols[1].write(f"Form Strength: **{away_form['form_strength']}**")
        form_cols[1].write(
            f"W-D-L: {away_form['wins']}-{away_form['draws']}-{away_form['losses']}"
        )
        form_cols[1].write("Recent Matches:")
        form_cols[1].dataframe(away_form["matches"])
    else:
        form_cols[1].warning("No recent matches found.")

    # Probability metrics
    col1, col2, col3 = st.columns(3)
    col1.metric("Home Win (1)", f"{row.prob_home:.2%}")
    col2.metric("Draw (X)", f"{row.prob_draw:.2%}")
    col3.metric("Away Win (2)", f"{row.prob_away:.2%}")

    if "prob_btts_yes" in merged.columns:
        st.metric("BTTS Yes", f"{row.prob_btts_yes:.2%}")

    if "prob_over25" in merged.columns:
        st.metric("Over 2.5 Goals", f"{row.prob_over25:.2%}")

    confidence = max(row.prob_home, row.prob_draw, row.prob_away)
    st.subheader("Model Confidence")
    st.metric("Confidence Score", f"{confidence:.2%}")

    # Odds + value
    st.subheader("Market Odds & Value Analysis")

    if not pd.isna(row.decimal_odds_home):
        odds_cols = st.columns(3)
        odds_cols[0].metric("Home Odds", row.decimal_odds_home)
        odds_cols[1].metric("Draw Odds", row.decimal_odds_draw)
        odds_cols[2].metric("Away Odds", row.decimal_odds_away)

        imp_home = 1 / row.decimal_odds_home
        imp_draw = 1 / row.decimal_odds_draw
        imp_away = 1 / row.decimal_odds_away

        st.write("### Implied Probabilities")
        st.write(f"- Home: {imp_home:.2%}")
        st.write(f"- Draw: {imp_draw:.2%}")
        st.write(f"- Away: {imp_away:.2%}")

        value_home = row.prob_home > imp_home
        value_draw = row.prob_draw > imp_draw
        value_away = row.prob_away > imp_away

        st.write("### Value Bet Signals")
        if value_home:
            st.success("Value Bet: Home")
        if value_draw:
            st.success("Value Bet: Draw")
        if value_away:
            st.success("Value Bet: Away")
        if not (value_home or value_draw or value_away):
            st.info("No value bets detected for this match.")

        st.subheader("Expected Value (EV)")
        ev_home = row.prob_home * (row.decimal_odds_home - 1) - (1 - row.prob_home)
        ev_draw = row.prob_draw * (row.decimal_odds_draw - 1) - (1 - row.prob_draw)
        ev_away = row.prob_away * (row.decimal_odds_away - 1) - (1 - row.prob_away)

        ev_cols = st.columns(3)
        ev_cols[0].metric("EV Home", f"{ev_home:.3f}")
        ev_cols[1].metric("EV Draw", f"{ev_draw:.3f}")
        ev_cols[2].metric("EV Away", f"{ev_away:.3f}")

        from betting.kelly import KellyCriterion

        kc = KellyCriterion()
        st.subheader("Kelly Criterion — Recommended Stakes (Bankroll £1000)")

        kelly_home = kc.stake_amount(row.prob_home, row.decimal_odds_home, 1000)
        kelly_draw = kc.stake_amount(row.prob_draw, row.decimal_odds_draw, 1000)
        kelly_away = kc.stake_amount(row.prob_away, row.decimal_odds_away, 1000)

        stake_cols = st.columns(3)
        stake_cols[0].metric("Stake — Home", f"£{kelly_home}")
        stake_cols[1].metric("Stake — Draw", f"£{kelly_draw}")
        stake_cols[2].metric("Stake — Away", f"£{kelly_away}")
    else:
        st.warning("Odds not available for this match.")

    st.subheader("Probability Breakdown")
    chart_df = pd.DataFrame(
        {
            "Outcome": ["Home", "Draw", "Away"],
            "Probability": [row.prob_home, row.prob_draw, row.prob_away],
        }
    )

    import altair as alt

    chart = (
        alt.Chart(chart_df)
        .mark_bar()
        .encode(
            x="Outcome",
            y="Probability",
            color="Outcome",
        )
    )
    st.altair_chart(chart, use_container_width=True)

    st.write("Raw Data")
    st.json(row.to_dict())

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
                avg_home_odds=("decimal_odds_home", "mean"),
            ).round(2)

            market_stats = market_stats.rename(
                columns={
                    "bets": "Total Bets",
                    "wins": "Winning Bets",
                    "pnl": "Total Profit/Loss (£)",
                    "avg_home_odds": "Average Home Odds",
                }
            )
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
        matches[
            ["match_canonical_id", "kickoff_time_utc", "home_score", "away_score"]
        ],
        on="match_canonical_id",
        how="left",
    ).merge(
        odds,
        on="match_canonical_id",
        how="left",
    )

    df["actual_result"] = df.apply(
        lambda r: "H"
        if r.home_score > r.away_score
        else "A"
        if r.away_score > r.home_score
        else "D",
        axis=1,
    )

    df["bet_home"] = df["prob_home"] > (1 / df["decimal_odds_home"])
    df["bet_draw"] = df["prob_draw"] > (1 / df["decimal_odds_draw"])
    df["bet_away"] = df["prob_away"] > (1 / df["decimal_odds_away"])

    def pnl(row, bet_flag, market, odds):
        if not bet_flag:
            return 0
        if row.actual_result == market:
            return odds - 1
        return -1

    df["pnl_home"] = df.apply(
        lambda r: pnl(r, r.bet_home, "H", r.decimal_odds_home), axis=1
    )
    df["pnl_draw"] = df.apply(
        lambda r: pnl(r, r.bet_draw, "D", r.decimal_odds_draw), axis=1
    )
    df["pnl_away"] = df.apply(
        lambda r: pnl(r, r.bet_away, "A", r.decimal_odds_away), axis=1
    )

    df["pnl"] = df["pnl_home"] + df["pnl_draw"] + df["pnl_away"]

    df["kickoff_time_utc"] = pd.to_datetime(df["kickoff_time_utc"])
    df = df.sort_values("kickoff_time_utc")
    df["bankroll"] = df["pnl"].cumsum()

    df_display = df.rename(
        columns={
            "actual_result": "Actual Match Result (H/D/A)",
            "pnl": "Profit/Loss (£)",
            "bankroll": "Cumulative Bankroll (£)",
            "kickoff_time_utc": "Kickoff Time (UTC)",
            "decimal_odds_home": "Home Odds (Decimal)",
            "decimal_odds_draw": "Draw Odds (Decimal)",
            "decimal_odds_away": "Away Odds (Decimal)",
        }
    )

    import altair as alt

    chart = (
        alt.Chart(df_display)
        .mark_line(color="#1f77b4", strokeWidth=3)
        .encode(
            x=alt.X("Kickoff Time (UTC):T", title="Date"),
            y=alt.Y("Cumulative Bankroll (£):Q", title="Bankroll (£)"),
            tooltip=[
                "Kickoff Time (UTC)",
                "Profit/Loss (£)",
                "Cumulative Bankroll (£)",
            ],
        )
        .interactive()
    )

    st.altair_chart(chart, use_container_width=True)

    st.dataframe(
        df_display[
            [
                "Kickoff Time (UTC)",
                "Actual Match Result (H/D/A)",
                "Profit/Loss (£)",
                "Cumulative Bankroll (£)",
                "Home Odds (Decimal)",
                "Draw Odds (Decimal)",
                "Away Odds (Decimal)",
            ]
        ]
    )

# ------------------------------------------------------------
# Page: Model Overview
# ------------------------------------------------------------
elif page == "Model Overview":
    st.title("Model Overview — Probability Engine")

    st.write(
        """
    This dashboard displays predictions generated by the calibrated Logistic Regression models
    trained on multiple seasons of Premier League data.

    **Training Data:** 2023/24 + 2024/25  
    **Testing Data:** 2025/26  
    **Model Type:** Calibrated Logistic Regression  
    **Training Seasons:** 2023/24 and 2024/25  
    **Evaluation Season:** 2025/26  
    """
    )

    preds = load_predictions()
    st.subheader("Prediction Sample")
    st.dataframe(preds.head())