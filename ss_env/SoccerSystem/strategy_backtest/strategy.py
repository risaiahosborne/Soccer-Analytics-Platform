"""
Value betting strategy using the Kelly Criterion.

Architecture:
    KellyCriterion   — pure math, no DB dependency
    ValueBetFinder   — identifies value bets given model probs + market odds
    Backtester       — simulates bankroll growth over historical matches

Kelly formula:
    f* = (bp - q) / b
    where:
        b = decimal_odds - 1  (net profit per unit)
        p = model probability
        q = 1 - p
        f* = fraction of bankroll to stake (before fractional scaling)

Settings:
    kelly_fraction   = 0.25  (Quarter Kelly — conservative, recommended for
                              new models not yet fully validated)
    max_stake_pct    = 0.10  (hard cap: never stake more than 10% of bankroll)
    min_edge         = 0.02  (only bet when edge > 2% — filters noise)

Simultaneous Kelly:
    When multiple value bets land on the same matchday, stakes are scaled down
    proportionally so total exposure stays within bankroll limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

@dataclass
class KellyConfig:
    kelly_fraction: float = 0.25    # Quarter Kelly
    max_stake_pct: float = 0.10     # Hard cap: 10% of bankroll per bet
    min_edge: float = 0.02          # Minimum edge to place a bet (2%)
    min_odds: float = 1.30          # Skip very short-priced favourites
    max_odds: float = 15.0          # Skip long shots (model less reliable)


# ------------------------------------------------------------------
# Core Kelly math
# ------------------------------------------------------------------

class KellyCriterion:
    """
    Stateless Kelly calculator. No DB dependency.

    >>> kc = KellyCriterion(KellyConfig())
    >>> kc.stake_fraction(model_prob=0.55, decimal_odds=2.10)
    0.025  # example
    """

    def __init__(self, config: KellyConfig | None = None) -> None:
        self.config = config or KellyConfig()

    def edge(self, model_prob: float, decimal_odds: float) -> float:
        """
        Expected value edge as a fraction.
        Positive = value bet. Negative = no bet.

        edge = (model_prob * decimal_odds) - 1
        """
        return round(model_prob * decimal_odds - 1, 6)

    def full_kelly(self, model_prob: float, decimal_odds: float) -> float:
        """
        Full Kelly fraction (f*). Never negative.

        f* = (b*p - q) / b
           = (p * decimal_odds - 1) / (decimal_odds - 1)
        """
        b = decimal_odds - 1
        if b <= 0:
            return 0.0
        p = model_prob
        q = 1 - p
        fk = (b * p - q) / b
        return max(0.0, round(fk, 6))

    def stake_fraction(self, model_prob: float, decimal_odds: float) -> float:
        """
        Fractional Kelly stake as proportion of bankroll.
        Applies kelly_fraction multiplier and max_stake_pct cap.
        Returns 0.0 if no value or below min_edge threshold.
        """
        cfg = self.config

        # Filter by odds range
        if decimal_odds < cfg.min_odds or decimal_odds > cfg.max_odds:
            return 0.0

        # Filter by edge threshold
        ev_edge = self.edge(model_prob, decimal_odds)
        if ev_edge < cfg.min_edge:
            return 0.0

        fk = self.full_kelly(model_prob, decimal_odds)
        if fk <= 0:
            return 0.0

        # Apply fraction + hard cap
        stake = fk * cfg.kelly_fraction
        stake = min(stake, cfg.max_stake_pct)
        return round(stake, 6)

    def stake_amount(
        self,
        model_prob: float,
        decimal_odds: float,
        bankroll: float,
    ) -> float:
        """Stake in currency units given current bankroll."""
        return round(self.stake_fraction(model_prob, decimal_odds) * bankroll, 2)


# ------------------------------------------------------------------
# Value bet identification
# ------------------------------------------------------------------

@dataclass
class ValueBet:
    match_canonical_id: str
    kickoff_utc: datetime
    market: str          # "1x2", "over_under", "btts"
    selection: str       # "home", "draw", "away", "over_2.5", "yes", etc.
    model_prob: float
    decimal_odds: float
    bookmaker: str
    edge: float
    full_kelly: float
    stake_fraction: float    # after fractional Kelly + cap
    stake_amount: float      # in currency units
    bankroll_at_bet: float


@dataclass
class BetResult:
    bet: ValueBet
    won: bool
    pnl: float           # profit/loss in currency units
    bankroll_after: float


class ValueBetFinder:
    """
    Given model probabilities and market odds for a match,
    identifies bets with positive expected value.
    """

    def __init__(self, kelly: KellyCriterion) -> None:
        self.kelly = kelly

    def find(
        self,
        match_canonical_id: str,
        kickoff_utc: datetime,
        model_probs: dict[str, float],
        market_odds: dict[str, dict],
        bankroll: float,
    ) -> list[ValueBet]:
        """
        Args:
            model_probs: {"{market}__{selection}": probability}
                e.g. {"1x2__home": 0.52, "1x2__draw": 0.28, "1x2__away": 0.20,
                       "over_under__over_2.5": 0.58, "btts__yes": 0.61}
            market_odds: {"{market}__{selection}": {"bookmaker": ..., "odds": ...}}
                e.g. {"1x2__home": {"bookmaker": "pinnacle", "odds": 2.10}}
        """
        bets: list[ValueBet] = []

        for key, prob in model_probs.items():
            if key not in market_odds:
                continue

            odds_info = market_odds[key]
            decimal_odds = odds_info.get("odds", 0)
            bookmaker = odds_info.get("bookmaker", "unknown")

            if decimal_odds <= 1.0:
                continue

            sf = self.kelly.stake_fraction(prob, decimal_odds)
            if sf <= 0:
                continue

            market, selection = key.split("__", 1)
            bets.append(ValueBet(
                match_canonical_id=match_canonical_id,
                kickoff_utc=kickoff_utc,
                market=market,
                selection=selection,
                model_prob=prob,
                decimal_odds=decimal_odds,
                bookmaker=bookmaker,
                edge=self.kelly.edge(prob, decimal_odds),
                full_kelly=self.kelly.full_kelly(prob, decimal_odds),
                stake_fraction=sf,
                stake_amount=self.kelly.stake_amount(prob, decimal_odds, bankroll),
                bankroll_at_bet=bankroll,
            ))

        return bets

    def adjust_for_simultaneous(self, bets: list[ValueBet], bankroll: float) -> list[ValueBet]:
        """
        When multiple bets land on the same matchday, scale stakes down
        so total exposure stays within max_stake_pct * n_bets constraint.

        Simple approach: if total fractional stake > 50% of bankroll,
        scale all stakes proportionally.
        """
        if not bets:
            return bets

        total_fraction = sum(b.stake_fraction for b in bets)
        max_total = 0.50  # never put more than 50% of bankroll at risk on one day

        if total_fraction <= max_total:
            return bets

        scale = max_total / total_fraction
        adjusted = []
        for b in bets:
            new_sf = round(b.stake_fraction * scale, 6)
            new_amount = round(new_sf * bankroll, 2)
            adjusted.append(ValueBet(
                match_canonical_id=b.match_canonical_id,
                kickoff_utc=b.kickoff_utc,
                market=b.market,
                selection=b.selection,
                model_prob=b.model_prob,
                decimal_odds=b.decimal_odds,
                bookmaker=b.bookmaker,
                edge=b.edge,
                full_kelly=b.full_kelly,
                stake_fraction=new_sf,
                stake_amount=new_amount,
                bankroll_at_bet=bankroll,
            ))
        return adjusted


# ------------------------------------------------------------------
# Backtester
# ------------------------------------------------------------------

@dataclass
class BacktestResult:
    starting_bankroll: float
    ending_bankroll: float
    total_bets: int
    winning_bets: int
    total_staked: float
    total_pnl: float
    roi: float                  # total_pnl / total_staked
    yield_pct: float            # roi as percentage
    max_drawdown_pct: float
    win_rate: float
    avg_edge: float
    kelly_config: KellyConfig
    bet_log: list[BetResult] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Bankroll: £{self.starting_bankroll:,.2f} → £{self.ending_bankroll:,.2f} "
            f"({'+' if self.total_pnl >= 0 else ''}{self.total_pnl:,.2f})\n"
            f"Bets: {self.total_bets} | Won: {self.winning_bets} "
            f"({self.win_rate:.1%})\n"
            f"ROI: {self.yield_pct:+.2f}% | Staked: £{self.total_staked:,.2f}\n"
            f"Max Drawdown: {self.max_drawdown_pct:.1%} | Avg Edge: {self.avg_edge:.2%}"
        )


class Backtester:
    """
    Simulates value betting over historical match data.

    Expects a DataFrame from FeatureBuilder.build_matrix() plus
    model probability predictions for each row.

    Usage:
        bt = Backtester(starting_bankroll=1000.0, config=KellyConfig())
        result = bt.run(matches_df, model_probs_df, odds_df)
        print(result.summary())
    """

    def __init__(
        self,
        starting_bankroll: float = 1000.0,
        config: KellyConfig | None = None,
    ) -> None:
        self.starting_bankroll = starting_bankroll
        self.config = config or KellyConfig()
        self.kelly = KellyCriterion(self.config)
        self.finder = ValueBetFinder(self.kelly)

    def run(
        self,
        matches_df: pd.DataFrame,
        model_probs_df: pd.DataFrame,
        odds_df: pd.DataFrame,
    ) -> BacktestResult:
        """
        Args:
            matches_df:     One row per match. Must have columns:
                            match_canonical_id, kickoff_utc,
                            target_result, target_btts, target_over25

            model_probs_df: One row per match. Must have columns:
                            match_canonical_id,
                            prob_home, prob_draw, prob_away,
                            prob_over25, prob_btts_yes

            odds_df:        One row per match. Must have columns:
                            match_canonical_id,
                            odds_closing_1x2_home, odds_closing_1x2_draw,
                            odds_closing_1x2_away,
                            odds_closing_over_under_over_25,
                            odds_closing_btts_yes
                            (bookmaker column optional, defaults to "market")
        """
        # Merge all data on match_canonical_id
        df = matches_df.merge(model_probs_df, on="match_canonical_id", how="inner")
        df = df.merge(odds_df, on="match_canonical_id", how="inner")
        df = df.sort_values("kickoff_utc").reset_index(drop=True)

        bankroll = self.starting_bankroll
        peak_bankroll = bankroll
        max_drawdown = 0.0
        bet_log: list[BetResult] = []

        for _, row in df.iterrows():
            mid = row["match_canonical_id"]
            kickoff = row["kickoff_utc"]

            model_probs = _extract_model_probs(row)
            market_odds = _extract_market_odds(row)

            # Find value bets
            bets = self.finder.find(mid, kickoff, model_probs, market_odds, bankroll)
            bets = self.finder.adjust_for_simultaneous(bets, bankroll)

            for bet in bets:
                won = _did_bet_win(bet, row)
                if won:
                    pnl = round(bet.stake_amount * (bet.decimal_odds - 1), 2)
                else:
                    pnl = -bet.stake_amount

                bankroll = round(bankroll + pnl, 2)

                # Track drawdown
                if bankroll > peak_bankroll:
                    peak_bankroll = bankroll
                drawdown = (peak_bankroll - bankroll) / peak_bankroll
                max_drawdown = max(max_drawdown, drawdown)

                bet_log.append(BetResult(
                    bet=bet,
                    won=won,
                    pnl=pnl,
                    bankroll_after=bankroll,
                ))

                # Stop if bankroll is bust
                if bankroll <= 0:
                    logger.warning("Bankroll busted at match %s", mid)
                    return self._compile_result(
                        bet_log, bankroll, max_drawdown
                    )

        return self._compile_result(bet_log, bankroll, max_drawdown)

    def _compile_result(
        self,
        bet_log: list[BetResult],
        ending_bankroll: float,
        max_drawdown: float,
    ) -> BacktestResult:
        n = len(bet_log)
        if n == 0:
            return BacktestResult(
                starting_bankroll=self.starting_bankroll,
                ending_bankroll=ending_bankroll,
                total_bets=0,
                winning_bets=0,
                total_staked=0.0,
                total_pnl=0.0,
                roi=0.0,
                yield_pct=0.0,
                max_drawdown_pct=0.0,
                win_rate=0.0,
                avg_edge=0.0,
                kelly_config=self.config,
                bet_log=[],
            )

        wins = sum(1 for r in bet_log if r.won)
        staked = sum(r.bet.stake_amount for r in bet_log)
        pnl = sum(r.pnl for r in bet_log)
        avg_edge = sum(r.bet.edge for r in bet_log) / n

        return BacktestResult(
            starting_bankroll=self.starting_bankroll,
            ending_bankroll=ending_bankroll,
            total_bets=n,
            winning_bets=wins,
            total_staked=round(staked, 2),
            total_pnl=round(pnl, 2),
            roi=round(pnl / staked, 4) if staked > 0 else 0.0,
            yield_pct=round(100 * pnl / staked, 2) if staked > 0 else 0.0,
            max_drawdown_pct=round(max_drawdown, 4),
            win_rate=round(wins / n, 4),
            avg_edge=round(avg_edge, 4),
            kelly_config=self.config,
            bet_log=bet_log,
        )

    def to_dataframe(self, result: BacktestResult) -> pd.DataFrame:
        """Convert bet log to a DataFrame for analysis."""
        rows = []
        for r in result.bet_log:
            b = r.bet
            rows.append({
                "match_canonical_id": b.match_canonical_id,
                "kickoff_utc": b.kickoff_utc,
                "market": b.market,
                "selection": b.selection,
                "model_prob": b.model_prob,
                "decimal_odds": b.decimal_odds,
                "implied_prob": round(1 / b.decimal_odds, 4),
                "edge": b.edge,
                "full_kelly": b.full_kelly,
                "stake_fraction": b.stake_fraction,
                "stake_amount": b.stake_amount,
                "bankroll_before": b.bankroll_at_bet,
                "won": r.won,
                "pnl": r.pnl,
                "bankroll_after": r.bankroll_after,
            })
        return pd.DataFrame(rows)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _extract_model_probs(row: pd.Series) -> dict[str, float]:
    probs = {}
    mapping = {
        "1x2__home":       "prob_home",
        "1x2__draw":       "prob_draw",
        "1x2__away":       "prob_away",
        "over_under__over_2.5": "prob_over25",
        "btts__yes":       "prob_btts_yes",
    }
    for key, col in mapping.items():
        val = row.get(col)
        if pd.notna(val) and val is not None:
            probs[key] = float(val)
    return probs


def _extract_market_odds(row: pd.Series) -> dict[str, dict]:
    odds = {}
    mapping = {
        "1x2__home":             "odds_closing_1x2_home",
        "1x2__draw":             "odds_closing_1x2_draw",
        "1x2__away":             "odds_closing_1x2_away",
        "over_under__over_2.5":  "odds_closing_over_under_over_25",
        "btts__yes":             "odds_closing_btts_yes",
    }
    for key, col in mapping.items():
        val = row.get(col)
        if pd.notna(val) and val is not None and float(val) > 1.0:
            odds[key] = {
                "odds": float(val),
                "bookmaker": row.get("bookmaker", "market"),
            }
    return odds


def _did_bet_win(bet: ValueBet, row: pd.Series) -> bool:
    """Determine if a bet won based on actual match result."""
    market = bet.market
    selection = bet.selection

    if market == "1x2":
        actual = row.get("target_result")  # "H", "D", "A"
        return {
            "home": actual == "H",
            "draw": actual == "D",
            "away": actual == "A",
        }.get(selection, False)

    elif market == "over_under":
        actual = row.get("target_over25")  # 1 or 0
        return {
            "over_2.5":  actual == 1,
            "under_2.5": actual == 0,
        }.get(selection, False)

    elif market == "btts":
        actual = row.get("target_btts")  # 1 or 0
        return {
            "yes": actual == 1,
            "no":  actual == 0,
        }.get(selection, False)

    return False