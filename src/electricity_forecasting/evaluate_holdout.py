"""Step 6: Evaluate the frozen Nord Pool forecasting protocol on the holdout."""

import json
from statistics import NormalDist

import matplotlib
import numpy as np
import pandas as pd

from .adaptive_kalman_backtest import MODEL as ADAPTIVE
from .adaptive_kalman_backtest import adaptive_variance_update, tune_adaptation
from .baseline_backtest import (
    DELIVERY,
    INPUT,
    MODELS,
    ROOT,
    common_scored_forecasts,
    prepare_backtest,
    replay_baselines,
    score_forecasts,
)
from .fixed_kalman_backtest import MODEL as FIXED
from .fixed_kalman_backtest import fit_parameters, replay_kalman

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter


ALL_MODELS = [*MODELS, FIXED, ADAPTIVE]
TABLES = ROOT / "results" / "tables" / "holdout_evaluation"
FIGURES = ROOT / "results" / "figures" / "holdout_evaluation"
ACTUAL = "actual_eur_per_mwh"
CALIBRATION_LEVELS = np.array([0.50, 0.70, 0.80, 0.90, 0.95])
BLOCK_LENGTHS = (7, 14, 28)
BOOTSTRAP_REPLICATES = 10_000
SEED = 20261116
LABELS = {
    "seasonal_naive": "Seasonal naive",
    "price_history": "Price history",
    "fundamentals": "Fundamentals",
    "fundamentals_ewma": "Fundamentals + EWMA",
    FIXED: "Fixed Kalman",
    ADAPTIVE: "Adaptive Kalman",
}
COLORS = dict(zip(ALL_MODELS, ["#777777", "#29774a", "#92553e", "#6d55a2", "#2469a0", "#bd5220"]))


def replay_holdout():
    """Freeze the variance fits and adaptation selection before replaying the test split."""
    development, start = prepare_backtest()
    training = development.loc[(development.index >= start) & development["split"].eq("train")]
    parameters = fit_parameters(training)
    fixed_validation = replay_kalman(development, start, parameters)
    _, _, tuning = tune_adaptation(development, start, parameters, fixed_validation)
    selected = tuning.loc[tuning["selected"]].iloc[0]

    points, _ = prepare_backtest(include_test=True)
    test_days = points.index[points["split"].eq("test")].normalize().unique()
    baselines = replay_baselines(points, start, forecast_split="test")
    fixed = replay_kalman(points, start, parameters, forecast_split="test")
    fixed["process_variance"] = fixed[DELIVERY].dt.hour.map(parameters["process_variance"])
    adaptive = replay_kalman(
        points, start, parameters, model=ADAPTIVE, forecast_split="test",
        variance_update=adaptive_variance_update(parameters, selected["alpha"], selected["strength"]),
    )
    forecasts = pd.concat([baselines, fixed, adaptive], ignore_index=True)
    return forecasts, test_days, tuning


def paired_crps(scored, test_days):
    """Compare adaptive CRPS with each reference using paired auction-day blocks."""
    hourly = scored.pivot(index=DELIVERY, columns="model", values="crps")
    references = [FIXED, *MODELS]
    difference = hourly[references].rsub(hourly[ADAPTIVE], axis=0)
    by_day = difference.groupby(difference.index.normalize())
    counts = by_day.size().reindex(test_days, fill_value=0)
    totals = by_day.sum().reindex(test_days, fill_value=0)
    daily = np.column_stack([counts, totals])
    n_days = len(test_days)
    rng = np.random.default_rng(SEED)
    rows = []

    for length in BLOCK_LENGTHS:
        # Circular blocks give every day equal sampling probability. Trim the
        # last sampled block to preserve the length of the holdout.
        offsets = np.arange(0, n_days, length)
        starts = rng.integers(n_days, size=(BOOTSTRAP_REPLICATES, len(offsets)))
        ends = starts + np.minimum(length, n_days - offsets)
        wrapped = np.concatenate([daily, daily[:length - 1]])
        prefix = np.vstack([np.zeros(daily.shape[1]), wrapped.cumsum(axis=0)])
        sampled = (prefix[ends] - prefix[starts]).sum(axis=1)
        valid = sampled[:, 0] > 0
        lower, upper = np.quantile(sampled[valid, 1:] / sampled[valid, :1], [0.025, 0.975], axis=0)

        for reference, mean, lo, hi in zip(references, difference.mean(), lower, upper):
            rows.append({
                "model": ADAPTIVE, "reference": reference, "scored_hours": len(hourly),
                "scored_days": (counts > 0).sum(), "block_days": length,
                "valid_replicates": valid.sum(), "mean_crps_difference": mean,
                "ci_lower_95": lo, "ci_upper_95": hi,
            })

    return pd.DataFrame(rows)


def stress_scores(scored, high_price):
    """Use the event criteria fixed before scoring to summarize realized stress hours."""
    cohorts = {
        "high_price": scored[ACTUAL].gt(high_price),
        "negative_price": scored[ACTUAL].lt(0),
    }
    tables = {}
    for event, mask in cohorts.items():
        cohort = scored.loc[mask]
        if cohort.empty:
            scores = pd.DataFrame(
                {"scored_hours": 0}, index=pd.Index(ALL_MODELS, name="model")
            )
        else:
            scores = score_forecasts(cohort, ALL_MODELS)
        scores.insert(1, "scored_days", cohort[DELIVERY].dt.normalize().nunique())
        tables[event] = scores
    return pd.concat(tables, names=["event"])


def calibration(scored):
    """Measure central interval coverage from the saved ensemble or Gaussian law."""
    tails = (1 - CALIBRATION_LEVELS) / 2
    probabilities = np.r_[tails, 1 - tails]
    normal_quantiles = np.array([NormalDist().inv_cdf(p) for p in probabilities])
    rows = []
    for model, forecasts in scored.groupby("model"):
        if model in (FIXED, ADAPTIVE):
            offsets = normal_quantiles[:, None] * forecasts["predictive_sd_eur_per_mwh"].to_numpy()
            batches = [(forecasts, offsets)]
        else:
            batches = []
            for _, batch in forecasts.groupby("calibration_count"):
                errors = np.asarray(batch["error_samples_eur_per_mwh"].map(json.loads).tolist())
                batches.append((batch, np.quantile(errors, probabilities, axis=1)))

        below = np.zeros(len(CALIBRATION_LEVELS), dtype=int)
        above = np.zeros_like(below)
        for batch, offsets in batches:
            centered = (batch[ACTUAL] - batch["center_eur_per_mwh"]).to_numpy()
            lower, upper = np.split(offsets, 2)
            below += (centered < lower).sum(axis=1)
            above += (centered > upper).sum(axis=1)

        count = len(forecasts)
        for nominal, low, high in zip(CALIBRATION_LEVELS, below, above):
            rows.append({
                "model": model, "nominal_coverage": nominal, "scored_hours": count,
                "empirical_coverage": 1 - (low + high) / count,
                "below_interval": low / count, "above_interval": high / count,
            })

    by_hour = scored.groupby(["model", scored[DELIVERY].dt.hour.rename("delivery_hour")]).agg(
        scored_hours=("crps", "size"), coverage_90=("covered_90", "mean")
    )
    return pd.DataFrame(rows), by_hour


def adaptation_by_day(scored, test_days):
    """Summarize the variance used at issue and paired errors as they evolve."""
    kalman = scored.loc[scored["model"].isin([FIXED, ADAPTIVE])]
    paired = kalman.pivot(index=DELIVERY, columns="model", values=["crps", "process_variance"])
    daily = pd.DataFrame({
        "scored_hours": 1,
        "hours_with_increased_variance": paired["process_variance"][ADAPTIVE].gt(
            paired["process_variance"][FIXED]
        ),
        "crps_difference_sum": paired["crps"][ADAPTIVE] - paired["crps"][FIXED],
    }).groupby(paired.index.normalize()).sum().reindex(test_days, fill_value=0)
    daily.index.name = "delivery_day"
    rolling = daily.rolling(28, min_periods=28).sum()
    daily["trailing_28d_activation_share"] = rolling["hours_with_increased_variance"] / rolling["scored_hours"]
    daily["trailing_28d_mean_crps_difference"] = rolling["crps_difference_sum"] / rolling["scored_hours"]
    return daily


def plot_calibration(overall, by_hour):
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.8), layout="constrained")
    fig.suptitle("Central prediction interval calibration")
    left.plot([0.46, 0.98], [0.46, 0.98], "--", color="#a0a0a0", label="Perfect calibration")
    right.axhline(0.9, linestyle="--", color="#a0a0a0")
    for model in ALL_MODELS:
        model_overall = overall.loc[overall["model"].eq(model)]
        left.plot(
            model_overall["nominal_coverage"], model_overall["empirical_coverage"],
            marker="o", ms=4, label=LABELS[model], color=COLORS[model],
        )
        hourly = by_hour.loc[model]
        right.plot(hourly.index, hourly["coverage_90"], color=COLORS[model], linewidth=1.6)

    left.set(xlabel="Nominal coverage", ylabel="Empirical coverage", xlim=(0.46, 0.98))
    left.set_xticks(CALIBRATION_LEVELS)
    left.xaxis.set_major_formatter(PercentFormatter(1))
    left.legend(loc="lower right", frameon=False, fontsize=8)
    right.set(xlabel="Delivery hour (benchmark clock)", ylabel="Empirical 90% interval coverage")
    right.set_xticks([0, 4, 8, 12, 16, 20, 23])
    for ax in (left, right):
        ax.yaxis.set_major_formatter(PercentFormatter(1))
        ax.grid(axis="y", alpha=0.2)
    fig.savefig(FIGURES / "calibration.png", dpi=170)
    plt.close(fig)


def plot_adaptation(daily):
    fig, (top, bottom) = plt.subplots(2, 1, sharex=True, figsize=(10, 5.8), layout="constrained")
    fig.suptitle("Kalman adaptation over the Nord Pool holdout")
    top.plot(daily.index, daily["trailing_28d_activation_share"], color=COLORS[ADAPTIVE])
    top.set(ylabel="Forecasts with higher\nprocess variance")
    top.yaxis.set_major_formatter(PercentFormatter(1))
    bottom.plot(daily.index, daily["trailing_28d_mean_crps_difference"], color=COLORS[FIXED])
    bottom.axhline(0, linestyle="--", color="#777777", linewidth=1)
    bottom.set(xlabel="Delivery date (trailing 28-day windows)", ylabel="Adaptive − fixed CRPS\n(EUR/MWh)")
    locator = mdates.AutoDateLocator(minticks=6, maxticks=9)
    bottom.xaxis.set_major_locator(locator)
    bottom.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    for ax in (top, bottom):
        ax.grid(axis="y", alpha=0.2)
    fig.savefig(FIGURES / "adaptation.png", dpi=170)
    plt.close(fig)


def main():
    dictionary = json.loads(INPUT.with_name("data_dictionary.json").read_text(encoding="utf-8"))
    high_price = dictionary["development_high_price_threshold_eur_per_mwh"]
    forecasts, test_days, tuning = replay_holdout()
    scored = common_scored_forecasts(forecasts, ALL_MODELS)
    scores = score_forecasts(scored, ALL_MODELS)
    differences = paired_crps(scored, test_days)
    stress = stress_scores(scored, high_price)
    levels, by_hour = calibration(scored)
    daily = adaptation_by_day(scored, test_days)

    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    forecasts.drop(columns="error_samples_eur_per_mwh").to_csv(
        TABLES / "test_forecasts.csv.gz", index=False, compression={"method": "gzip", "mtime": 0}
    )
    scores.to_csv(TABLES / "test_scores.csv")
    differences.to_csv(TABLES / "paired_crps.csv", index=False)
    stress.to_csv(TABLES / "stress_scores.csv")
    levels.to_csv(TABLES / "calibration_by_level.csv", index=False)
    by_hour.to_csv(TABLES / "calibration_by_hour.csv")
    daily.to_csv(TABLES / "adaptation_by_day.csv")
    tuning.to_csv(TABLES / "validation_tuning.csv", index=False)
    plot_calibration(levels, by_hour)
    plot_adaptation(daily)
    print(scores.round(4).to_string())
    print(f"\nSaved holdout tables to {TABLES} and figures to {FIGURES}")


if __name__ == "__main__":
    main()
