"""Step 3: Replay daily auctions and score the Nordic Nord Pool baselines."""

import json
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "data" / "processed" / "nord_pool" / "model_dataset.csv"
OUTPUT = ROOT / "results" / "tables" / "baseline_backtest"

DELIVERY = "delivery_start_local"
PRICE = "price_eur_per_mwh"
LOAD = "load_forecast_mw"
WIND = "wind_forecast_mw"

MODELS = ["seasonal_naive", "price_history", "fundamentals", "fundamentals_ewma"]

# The benchmark omits publication timestamps and revision history. Treat its
# load and wind forecasts as available at issue. Use 11:00 on the preceding
# day as a nominal issue label on the benchmark's local clock.
ISSUE_HOUR = 11
CALIBRATION_DAYS = 90
ERROR_HISTORY = 90  # Most recent available errors for each model and hour.
MIN_ERRORS = 20
EWMA_ALPHA = 0.10


def fit_and_predict(features, prices, fit_mask):
    """Fit a fixed OLS model with an intercept using complete training rows."""
    design = features.copy()
    design.insert(0, "intercept", 1.0)
    usable = design.notna().all(axis=1)
    train = fit_mask & usable & prices.notna()

    if train.sum() < design.shape[1]:
        raise ValueError("Insufficient complete training rows for the regression.")

    coefficients = np.linalg.lstsq(
        design.loc[train].to_numpy(),
        prices.loc[train].to_numpy(),
        rcond=None,
    )[0]

    predictions = pd.Series(np.nan, index=features.index)
    predictions.loc[usable] = design.loc[usable].to_numpy() @ coefficients
    return predictions


def forecast_distribution(center, history, actual):
    """Add historical forecast errors to the point forecast and score the ensemble."""
    errors = np.sort(np.asarray(history, dtype=float))
    ensemble = center + errors
    lower, median, upper = np.quantile(ensemble, [0.05, 0.50, 0.95])

    crps = mae = covered = np.nan
    if pd.notna(actual):
        n = len(ensemble)
        ranks = 2 * np.arange(1, n + 1) - n - 1
        crps = np.abs(ensemble - actual).mean() - (ranks @ errors) / n**2
        mae = abs(actual - median)
        covered = float(lower <= actual <= upper)

    return {
        "center_eur_per_mwh": center,
        "p05_eur_per_mwh": lower,
        "p50_eur_per_mwh": median,
        "p95_eur_per_mwh": upper,
        "calibration_count": len(errors),
        "error_samples_eur_per_mwh": json.dumps(errors.tolist(), separators=(",", ":")),
        "crps": crps,
        "absolute_error": mae,
        "covered_90": covered,
        "interval_width_90": upper - lower,
    }


def prepare_backtest(*, include_test=False):
    """Return fixed model predictions and the start of the training replay."""
    data = pd.read_csv(
        INPUT,
        usecols=[DELIVERY, PRICE, LOAD, WIND, "split"],
        parse_dates=[DELIVERY],
        index_col=DELIVERY,
    )
    splits = ["train", "validation", "test"] if include_test else ["train", "validation"]
    data = data.loc[data["split"].isin(splits)].sort_index()
    validation = data["split"].eq("validation")
    if not validation.any():
        raise ValueError("The dataset has no validation rows.")

    # Step 2 preserves missing observations as blanks on a regular hourly grid.
    hourly_grid = pd.date_range(data.index.min(), data.index.max(), freq="h")
    if not data.index.equals(hourly_grid):
        raise ValueError("Expected one row per hour on the Step 2 benchmark clock.")

    validation_start = data.index[validation].min().normalize()
    calibration_start = validation_start - pd.Timedelta(days=CALIBRATION_DAYS)
    fit_mask = data["split"].eq("train") & (data.index < calibration_start)

    calendar = pd.get_dummies(
        pd.DataFrame(
            {
                "hour": data.index.hour.astype(str),
                "weekday": data.index.dayofweek.astype(str),
            },
            index=data.index,
        ),
        drop_first=True,
        dtype=float,
    )
    lags = pd.DataFrame(
        {"price_lag_24h": data[PRICE].shift(24), "price_lag_168h": data[PRICE].shift(168)}
    )

    points = data[[PRICE, "split"]].copy()
    points["seasonal_naive"] = lags["price_lag_168h"]
    points["price_history"] = fit_and_predict(calendar.join(lags), data[PRICE], fit_mask)
    points["fundamentals"] = fit_and_predict(
        calendar.join(data[[LOAD, WIND]] / 1_000), data[PRICE], fit_mask
    )
    return points, calibration_start


def replay_baselines(points, calibration_start, *, forecast_split="validation"):
    """Replay the four baselines and return forecasts for the requested split."""
    # Reserve the last 90 training days for the initial replay. Model
    # coefficients stay fixed. Residual histories and the EWMA update daily.
    replay = points.loc[calibration_start:]
    history = defaultdict(lambda: deque(maxlen=ERROR_HISTORY))
    correction = np.zeros(24)
    records = []

    for delivery_day, batch in replay.groupby(replay.index.normalize(), sort=True):
        batch = batch.copy()
        batch["fundamentals_ewma"] = batch["fundamentals"] + correction[batch.index.hour]
        issue_time = delivery_day - pd.Timedelta(days=1) + pd.Timedelta(hours=ISSUE_HOUR)

        # Issue the full batch using only errors from earlier auctions.
        for delivery, row in batch.loc[batch["split"].eq(forecast_split)].iterrows():
            for model in MODELS:
                samples = history[model, delivery.hour]
                if pd.isna(row[model]) or len(samples) < MIN_ERRORS:
                    continue

                records.append(
                    {
                        "issue_time_local": issue_time,
                        DELIVERY: delivery,
                        "model": model,
                        "actual_eur_per_mwh": row[PRICE],
                        **forecast_distribution(row[model], samples, row[PRICE]),
                    }
                )

        # Make this auction's results available to the next issue.
        errors = batch[MODELS].rsub(batch[PRICE], axis="index")
        for delivery, row in errors.iterrows():
            hour = delivery.hour
            for model in MODELS:
                if pd.notna(row[model]):
                    history[model, hour].append(row[model])

            if pd.notna(row["fundamentals"]):
                correction[hour] += EWMA_ALPHA * (row["fundamentals"] - correction[hour])

    if not records:
        raise ValueError(f"No {forecast_split} forecasts had enough historical errors.")

    return pd.DataFrame(records)


def common_scored_forecasts(forecasts, models):
    """Return the observed delivery hours with scored forecasts from all models."""
    scored = forecasts.loc[forecasts["model"].isin(models)].dropna(
        subset=["actual_eur_per_mwh", "crps"]
    )
    common = scored.groupby(DELIVERY)["model"].transform("nunique").eq(len(models))
    if not common.any():
        raise ValueError("No observed delivery hours have forecasts from all models.")
    return scored.loc[common]


def score_forecasts(forecasts, models):
    """Score all models on the same observed delivery hours."""
    return (
        common_scored_forecasts(forecasts, models)
        .groupby("model")
        .agg(
            scored_hours=("crps", "size"),
            mean_crps=("crps", "mean"),
            mae=("absolute_error", "mean"),
            coverage_90=("covered_90", "mean"),
            mean_width_90=("interval_width_90", "mean"),
        )
        .sort_values("mean_crps")
    )


def main():
    points, calibration_start = prepare_backtest()
    forecasts = replay_baselines(points, calibration_start)
    scores = score_forecasts(forecasts, MODELS)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    forecasts.to_csv(OUTPUT / "validation_forecasts.csv.gz", index=False)
    scores.to_csv(OUTPUT / "validation_scores.csv")
    print(scores.round(4).to_string())
    print(f"\nSaved validation forecasts and scores to {OUTPUT}")


if __name__ == "__main__":
    main()
