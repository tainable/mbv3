"""
specials_strategies.py
----------------------
Pure-function evaluators for the three strategy types defined in
specials_registry.py.  Each evaluator takes:

  strategy : the registry strategy dict
  prices   : {(provider, outcome_key, side): raw_decimal_odds}
  catalog  : the event entry from specials_registry (for outcome metadata)
  settings : config.Settings (for commission lookups)

and returns a structured evaluation dict.  The dict always carries a
'status' field — one of:

    disabled                strategy.enabled is False
    no_price                a required outcome has no price in the input
    incomplete_catalog      basket='*' but some catalog outcome lacks a price
    excluded_prob_exceeded  selective basket: excluded tail too thick to fire
    below_threshold         math computed, edge < min_edge_pct
    fires                   strategy would trigger

Effective odds (commission-adjusted) are computed via the helpers in
calculator.py — single source of truth for the maths.
"""
from __future__ import annotations

from typing import Any

# Reuse the sports-side commission math.  The leading-underscore name is
# intentional in calculator.py (module-private) but they are the canonical
# implementation and we want the specials engine to track any future tweaks
# to commission formulas automatically.
from matched_betting.calculator import _eff_back_odds, _eff_lay_odds
from matched_betting import kelly as _kelly


PriceKey     = tuple[str, str, str]   # (provider, outcome_key, side)
PriceMap     = dict[PriceKey, float]
LiquidityMap = dict[PriceKey, float]  # same key, value = available size/amount at best price


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def evaluate(
    strategy:   dict[str, Any],
    prices:     PriceMap,
    catalog:    dict[str, Any],
    settings,
    liquidity:  LiquidityMap | None = None,
) -> dict[str, Any]:
    """Route a strategy to its type-specific evaluator."""
    if not strategy.get("enabled"):
        return _result(strategy, status="disabled")

    stype = strategy.get("type")
    if stype == "cross_hedge":
        return evaluate_cross_hedge(strategy, prices, catalog, settings, liquidity)
    if stype == "same_provider_basket":
        return evaluate_same_provider_basket(strategy, prices, catalog, settings)
    if stype == "selective_basket":
        return evaluate_selective_basket(strategy, prices, catalog, settings)
    if stype == "cross_provider_basket":
        return evaluate_cross_provider_basket(strategy, prices, catalog, settings, liquidity)
    return _result(strategy, status="unknown_strategy_type", strategy_type=stype)


# ---------------------------------------------------------------------------
# cross_hedge
# ---------------------------------------------------------------------------

def evaluate_cross_hedge(
    strategy:  dict[str, Any],
    prices:    PriceMap,
    catalog:   dict[str, Any],
    settings,
    liquidity: LiquidityMap | None = None,
) -> dict[str, Any]:
    """Back on one provider, lay on another.  Direction is taken verbatim from
    the strategy — we do NOT infer hedges from outcome equivalence."""
    back_cfg = strategy["back"]
    lay_cfg  = strategy["lay"]

    back_raw = prices.get((back_cfg["provider"], back_cfg["outcome"], back_cfg["side"]))
    lay_raw  = prices.get((lay_cfg["provider"],  lay_cfg["outcome"],  lay_cfg["side"]))

    if back_raw is None:
        return _result(strategy, status="no_price", missing_side="back",
                       missing_key=f"{back_cfg['provider']}:{back_cfg['outcome']}:{back_cfg['side']}")
    if lay_raw is None:
        return _result(strategy, status="no_price", missing_side="lay",
                       missing_key=f"{lay_cfg['provider']}:{lay_cfg['outcome']}:{lay_cfg['side']}")

    back_eff = _eff_back_odds(back_raw, back_cfg["provider"])

    # When the lay leg has side="no" (e.g. buying NO on Polymarket), the position
    # is a back bet on the opposite outcome — not a traditional exchange lay.
    # Buying NO at PM NO odds N converts to an equivalent lay at L = N_eff/(N_eff-1).
    no_eff: float | None = None
    if lay_cfg["side"] == "no":
        no_eff = _eff_back_odds(lay_raw, lay_cfg["provider"])
        if no_eff <= 1.0:
            return _result(strategy, status="degenerate_odds",
                           back_eff=back_eff, lay_eff=no_eff)
        lay_eff = no_eff / (no_eff - 1.0)
    else:
        lay_eff = _eff_lay_odds(lay_raw, lay_cfg["provider"])

    # Back-lay arb is profitable when effective back odds > effective lay odds.
    if lay_eff <= 1.0 or back_eff <= 1.0:
        return _result(strategy, status="degenerate_odds",
                       back_eff=back_eff, lay_eff=lay_eff)

    profit_per_back = back_eff / lay_eff - 1.0

    # Edge as % of TOTAL CAPITAL deployed (not just back stake).
    # Capital structure differs by direction:
    #   mb-pm (PM NO lay): total = back + PM NO spend;  NO spend = back * back_eff / no_eff
    #   pm-mb (MB lay):    total = back + MB liability; liability = lay_stake * (lay_raw - 1)
    if no_eff is not None:
        capital_ratio = 1.0 + back_eff / no_eff
    else:
        capital_ratio = 1.0 + back_eff * (lay_raw - 1.0) / lay_eff

    edge_pct = profit_per_back / capital_ratio * 100

    fires = edge_pct >= strategy.get("min_edge_pct", 0.0)

    back_key = (back_cfg["provider"], back_cfg["outcome"], back_cfg["side"])
    lay_key  = (lay_cfg["provider"],  lay_cfg["outcome"],  lay_cfg["side"])
    back_liq = liquidity.get(back_key) if liquidity else None
    lay_liq  = liquidity.get(lay_key)  if liquidity else None

    return _result(
        strategy,
        status="fires" if fires else "below_threshold",
        edge_pct=round(edge_pct, 4),
        back={
            "provider": back_cfg["provider"], "outcome": back_cfg["outcome"], "side": back_cfg["side"],
            "raw_odds": back_raw, "eff_odds": round(back_eff, 4),
            "liquidity": back_liq,
        },
        lay={
            "provider": lay_cfg["provider"],  "outcome": lay_cfg["outcome"],  "side": lay_cfg["side"],
            "raw_odds": lay_raw,  "eff_odds": round(lay_eff, 4),
            "liquidity": lay_liq,
        },
        gap_outcomes=strategy.get("gap_outcomes"),
    )


# ---------------------------------------------------------------------------
# same_provider_basket
# ---------------------------------------------------------------------------

def evaluate_same_provider_basket(
    strategy: dict[str, Any],
    prices:   PriceMap,
    catalog:  dict[str, Any],
    settings,
) -> dict[str, Any]:
    """Back every outcome (or a listed subset, when include_outcomes != '*')
    on one provider.  Sure bet iff sum of effective implied probabilities
    < 1.0 across the basket.

    A '*' include marker means the basket must be complete — if any catalog
    outcome has no price we refuse to fire, because a partial basket on
    full-basket settings would silently expose us to the missing outcome.
    """
    provider = strategy["provider"]
    side     = strategy["side"]
    include  = strategy.get("include_outcomes", "*")

    catalog_outcomes = list(catalog["providers"].get(provider, {}).get("outcomes", {}).keys())

    if include == "*":
        target_outcomes = catalog_outcomes
        if not target_outcomes:
            return _result(strategy, status="empty_catalog", provider=provider)
        missing = [o for o in catalog_outcomes if (provider, o, side) not in prices]
        if missing:
            return _result(strategy, status="incomplete_catalog",
                           missing=missing, provider=provider, side=side)
    else:
        target_outcomes = list(include)
        if not target_outcomes:
            return _result(strategy, status="empty_include_list")

    legs = []
    sum_implied = 0.0
    for outcome in target_outcomes:
        raw = prices.get((provider, outcome, side))
        if raw is None:
            return _result(strategy, status="no_price",
                           missing_key=f"{provider}:{outcome}:{side}")
        eff = _eff_back_odds(raw, provider) if side in ("back", "yes") else _eff_lay_odds(raw, provider)
        if eff <= 1.0:
            return _result(strategy, status="degenerate_odds",
                           outcome=outcome, eff_odds=eff)
        sum_implied += 1.0 / eff
        legs.append({"outcome": outcome, "raw_odds": raw, "eff_odds": round(eff, 4)})

    # Sure-bet edge: profit per $1 returned = 1 - Σ(1/eff_odds).  Convert to
    # profit per $1 STAKED, which is the figure operators are used to:
    edge_pct = ((1.0 / sum_implied) - 1.0) * 100 if sum_implied > 0 else 0.0

    fires = (sum_implied < 1.0) and (edge_pct >= strategy.get("min_edge_pct", 0.0))
    return _result(
        strategy,
        status="fires" if fires else "below_threshold",
        edge_pct=round(edge_pct, 4),
        sum_implied=round(sum_implied, 6),
        provider=provider,
        side=side,
        legs=legs,
    )


# ---------------------------------------------------------------------------
# selective_basket
# ---------------------------------------------------------------------------

def evaluate_selective_basket(
    strategy: dict[str, Any],
    prices:   PriceMap,
    catalog:  dict[str, Any],
    settings,
) -> dict[str, Any]:
    """Back a chosen subset of outcomes on one provider.  Not risk-free —
    every excluded outcome that wins is a total stake loss.

    The strategy auto-suspends (returns 'excluded_prob_exceeded') if the
    excluded tail probability is above max_excluded_prob.

    Edge is reported as the expected return per $1 staked when an INCLUDED
    outcome wins, scaled by (1 - P(excluded)):

        edge_pct = ((1 - excluded_prob) / Σ implied_prob_included - 1) * 100

    This is the metric used by selective-basket bettors elsewhere — positive
    edge_pct means positive EV conditional on the included tail, after
    deducting expected losses to excluded outcomes.
    """
    provider = strategy["provider"]
    side     = strategy["side"]
    include  = list(strategy.get("include_outcomes") or [])
    if not include:
        return _result(strategy, status="empty_include_list")

    catalog_outcomes = list(catalog["providers"].get(provider, {}).get("outcomes", {}).keys())
    if not catalog_outcomes:
        return _result(strategy, status="empty_catalog", provider=provider)

    excluded = [o for o in catalog_outcomes if o not in include]

    # We MUST have prices for every excluded outcome to compute the tail.
    excluded_implied = 0.0
    excluded_detail = []
    for outcome in excluded:
        raw = prices.get((provider, outcome, side))
        if raw is None:
            return _result(strategy, status="no_price_excluded",
                           missing_key=f"{provider}:{outcome}:{side}",
                           note="Cannot verify excluded-tail threshold without all excluded prices.")
        eff = _eff_back_odds(raw, provider) if side in ("back", "yes") else _eff_lay_odds(raw, provider)
        if eff <= 1.0:
            return _result(strategy, status="degenerate_odds",
                           outcome=outcome, eff_odds=eff)
        p = 1.0 / eff
        excluded_implied += p
        excluded_detail.append({"outcome": outcome, "implied_prob": round(p, 6)})

    threshold = strategy.get("max_excluded_prob", 1.0)
    if excluded_implied > threshold:
        return _result(
            strategy,
            status="excluded_prob_exceeded",
            excluded_prob=round(excluded_implied, 6),
            threshold=threshold,
            excluded_detail=excluded_detail,
        )

    # Now the included basket
    included_implied = 0.0
    included_detail = []
    for outcome in include:
        raw = prices.get((provider, outcome, side))
        if raw is None:
            return _result(strategy, status="no_price_included",
                           missing_key=f"{provider}:{outcome}:{side}")
        eff = _eff_back_odds(raw, provider) if side in ("back", "yes") else _eff_lay_odds(raw, provider)
        if eff <= 1.0:
            return _result(strategy, status="degenerate_odds",
                           outcome=outcome, eff_odds=eff)
        included_implied += 1.0 / eff
        included_detail.append({"outcome": outcome, "raw_odds": raw, "eff_odds": round(eff, 4)})

    if included_implied <= 0:
        return _result(strategy, status="degenerate_basket", included_implied=included_implied)

    # Conditional edge: expected payout / cost, given the excluded tail.
    edge_pct = ((1.0 - excluded_implied) / included_implied - 1.0) * 100

    fires = edge_pct >= strategy.get("min_edge_pct", 0.0)
    return _result(
        strategy,
        status="fires" if fires else "below_threshold",
        edge_pct=round(edge_pct, 4),
        excluded_prob=round(excluded_implied, 6),
        excluded_threshold=threshold,
        included_implied=round(included_implied, 6),
        provider=provider,
        side=side,
        included=included_detail,
        excluded=excluded_detail,
    )


# ---------------------------------------------------------------------------
# cross_provider_basket
# ---------------------------------------------------------------------------

def evaluate_cross_provider_basket(
    strategy:  dict[str, Any],
    prices:    PriceMap,
    catalog:   dict[str, Any],
    settings,
    liquidity: LiquidityMap | None = None,
) -> dict[str, Any]:
    """Back each outcome on (potentially different) providers.

    Legs are specified as a list of {"provider", "outcome", "side"} dicts.
    Sure bet iff Σ(1/eff_back_odds) < 1 across all legs.

    Use when there are only N mutually exclusive outcomes and you want to
    cover each outcome on whichever platform offers the best price — e.g.
    a 2-team final where you back Team A on PM and Team B on MB.
    """
    leg_specs = strategy.get("legs", [])
    if not leg_specs:
        return _result(strategy, status="empty_legs")

    legs = []
    sum_implied = 0.0
    for spec in leg_specs:
        provider = spec["provider"]
        outcome  = spec["outcome"]
        side     = spec["side"]
        raw = prices.get((provider, outcome, side))
        if raw is None:
            return _result(strategy, status="no_price",
                           missing_key=f"{provider}:{outcome}:{side}")
        eff = _eff_back_odds(raw, provider)
        if eff <= 1.0:
            return _result(strategy, status="degenerate_odds",
                           outcome=outcome, eff_odds=eff)
        implied = 1.0 / eff
        sum_implied += implied
        liq = liquidity.get((provider, outcome, side)) if liquidity else None
        legs.append({
            "provider": provider, "outcome": outcome, "side": side,
            "raw_odds": raw, "eff_odds": round(eff, 4),
            "implied_prob": round(implied, 6),
            "liquidity": liq,
        })

    edge_pct = ((1.0 / sum_implied) - 1.0) * 100 if sum_implied > 0 else 0.0
    fires = (sum_implied < 1.0) and (edge_pct >= strategy.get("min_edge_pct", 0.0))

    return _result(
        strategy,
        status="fires" if fires else "below_threshold",
        edge_pct=round(edge_pct, 4),
        sum_implied=round(sum_implied, 6),
        legs=legs,
    )


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def size_cross_hedge(
    eval_result: dict,
    bankroll_usd:  float | None,
    settings,
    override_back_stake: float | None = None,
) -> dict:
    """
    Compute stakes and capital commitment for a fired cross_hedge.

    Returns a dict with keys:
        back_stake      USDC — stake on the back platform
        lay_stake       USDC — exchange-equivalent lay stake (used for risk-free formula)
        pm_no_spend     USDC — actual PM NO purchase cost (mb-pm only, else None)
        lay_liability   USDC — MB lay liability = lay_stake*(lay_raw-1) (pm-mb only, else None)
        total_capital   USDC — real capital deployed across both legs
        profit          USDC — guaranteed profit at current prices
        kelly_frac      fraction applied (0 if override used)

    Capital structures differ by direction:
        pm-mb  (back PM YES, lay MB):  total = back_stake + lay_stake*(lay_raw-1)
        mb-pm  (back MB,     lay PM NO): total = back_stake + pm_no_spend
    """
    ks = getattr(settings, "kelly", None)

    back = eval_result.get("back") or {}
    lay  = eval_result.get("lay")  or {}
    back_eff  = back.get("eff_odds", 0.0)
    lay_eff   = lay.get("eff_odds",  0.0)
    lay_raw   = lay.get("raw_odds",  0.0)
    lay_prov  = lay.get("provider",  "")
    lay_side  = lay.get("side",      "")
    edge_pct  = eval_result.get("edge_pct", 0.0)

    if override_back_stake is not None:
        back_stake = override_back_stake
        kelly_frac = 0.0
    elif bankroll_usd is not None and ks is not None:
        frac = _kelly.kelly_fraction(
            edge_pct,
            ks.low_profit, ks.high_profit, ks.low_fraction, ks.high_fraction,
        )
        back_stake = min(frac * bankroll_usd, ks.max_stake_usdc)
        kelly_frac = frac
    else:
        back_stake = getattr(ks, "max_stake_usdc", 200.0) if ks else 200.0
        kelly_frac = 0.0

    if lay_eff > 0 and back_eff > 0:
        lay_stake = back_stake * back_eff / lay_eff
    else:
        lay_stake = 0.0

    profit = back_stake * (back_eff / lay_eff - 1.0) if lay_eff > 0 and back_eff > 0 else 0.0

    # Direction-specific capital commitment
    pm_no_spend:   float | None = None
    lay_liability: float | None = None

    if lay_prov == "polymarket" and lay_side == "no" and lay_raw > 0:
        # mb-pm: actual PM NO spend = back_stake * back_eff / pm_no_eff
        pm_no_eff   = _eff_back_odds(lay_raw, "polymarket")
        pm_no_spend = (back_stake * back_eff / pm_no_eff) if pm_no_eff > 0 else 0.0
        total_capital = back_stake + pm_no_spend

    elif lay_prov == "matchbook" and lay_side == "lay" and lay_raw > 0:
        # pm-mb: MB holds the full lay liability as margin
        lay_liability = lay_stake * (lay_raw - 1.0)
        total_capital = back_stake + lay_liability

    else:
        total_capital = back_stake + lay_stake

    return {
        "back_stake":    round(back_stake,    2),
        "lay_stake":     round(lay_stake,     2),
        "pm_no_spend":   round(pm_no_spend,   2) if pm_no_spend   is not None else None,
        "lay_liability": round(lay_liability, 2) if lay_liability is not None else None,
        "total_capital": round(total_capital, 2),
        "profit":        round(profit,        2),
        "kelly_frac":    round(kelly_frac,    4),
    }


def back_stake_for_total_capital(eval_result: dict, total_capital_usd: float) -> float:
    """Invert the capital-ratio formula: given a desired total capital, return the back stake.

    Mirrors the direction-specific capital structure in size_cross_hedge:
        mb-pm  (lay side=no):  total = back * (1 + back_eff / pm_no_eff)
        pm-mb  (lay side=lay): total = back * (1 + back_eff * (lay_raw-1) / lay_eff)
        other:                 total = back * (1 + back_eff / lay_eff)
    """
    back     = eval_result.get("back") or {}
    lay      = eval_result.get("lay")  or {}
    back_eff = back.get("eff_odds", 0.0)
    lay_eff  = lay.get("eff_odds",  0.0)
    lay_raw  = lay.get("raw_odds",  0.0)
    lay_prov = lay.get("provider",  "")
    lay_side = lay.get("side",      "")

    if lay_prov == "polymarket" and lay_side == "no" and lay_raw > 0:
        pm_no_eff     = _eff_back_odds(lay_raw, "polymarket")
        capital_ratio = 1.0 + back_eff / pm_no_eff if pm_no_eff > 0 else 1.0
    elif lay_eff > 0:
        if lay_prov == "matchbook" and lay_side == "lay" and lay_raw > 0:
            capital_ratio = 1.0 + back_eff * (lay_raw - 1.0) / lay_eff
        else:
            capital_ratio = 1.0 + back_eff / lay_eff
    else:
        capital_ratio = 1.0

    return total_capital_usd / capital_ratio if capital_ratio > 0 else total_capital_usd


# ---------------------------------------------------------------------------
# Result helper
# ---------------------------------------------------------------------------

def _result(strategy: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    """Build a uniform evaluation result.  Always carries name/type/status/
    safety-flag fields so downstream consumers (logging, alerting) can treat
    the dict generically."""
    return {
        "name":         strategy.get("name"),
        "type":         strategy.get("type"),
        "status":       status,
        "enabled":      strategy.get("enabled", False),
        "alert_only":   strategy.get("alert_only", True),
        "risk_class":   strategy.get("risk_class"),
        "min_edge_pct": strategy.get("min_edge_pct"),
        "notes":        strategy.get("notes"),
        **extra,
    }
