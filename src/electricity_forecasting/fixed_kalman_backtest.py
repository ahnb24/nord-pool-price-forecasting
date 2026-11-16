"""Step 4: Compare a fixed Kalman correction with the four Step 3 baselines.

Run from the project root with:
    python -m src.electricity_forecasting.fixed_kalman_backtest

For each delivery hour, the auction residual is the latent bias plus
independent Gaussian measurement noise with variance R. The bias follows a
daily random walk with independent Gaussian increments of variance Q. The
model forecasts marginal prices and assumes independent states across hours.

Fit Q and R once per hour using only the final 90 training days, after the
fundamentals regression's fitting period. Initialize with the first available
residual and posterior variance R, as under a diffuse prior. During replay,
forecast the entire auction before updating with its results. Missing
residuals skip the update while the state variance still grows by Q per day.
The predictive price variance is the prior state variance plus R.
"""

from math import erf, exp, pi, sqrt
from statistics import NormalDist

import numpy as np
import pandas as pd

from .baseline_backtest import (
    DELIVERY,
    ISSUE_HOUR,
    MIN_ERRORS,
    MODELS,
    PRICE,
    ROOT,
    prepare_backtest,
    replay_baselines,
    score_forecasts,
)


MODEL = "fundamentals_kalman_fixed"
OUTPUT = ROOT / "results" / "tables" / "fixed_kalman_backtest"
Z_95 = NormalDist().inv_cdf(0.95)
MIN_MEASUREMENT_VARIANCE = 1e-8  # (EUR/MWh)^2
VARIANCE_RATIOS = np.r_[0.0, np.logspace(-6, 3, 181)]  # Candidate Q/R values.


def update_state(mean, prior_variance, residual, measurement_variance):
    """Condition a predicted bias on one observed auction residual."""
    gain = prior_variance / (prior_variance + measurement_variance)
    return mean + gain * (residual - mean), gain * measurement_variance


def fit_hour(residuals):
    """Minimize the conditional Gaussian innovation likelihood over Q/R and R."""
    values = residuals.to_numpy(dtype=float)
    day_gaps = np.diff(residuals.index.to_numpy(dtype="datetime64[D]")).astype(int)

    # With R scaled to one, each candidate ratio fixes the gain and the
    # innovation variance in units of R. Estimate the scale R analytically.
    mean = np.full_like(VARIANCE_RATIOS, values[0])
    variance = np.ones_like(VARIANCE_RATIOS)
    sum_squared = np.zeros_like(VARIANCE_RATIOS)
    log_determinant = np.zeros_like(VARIANCE_RATIOS)
    for residual, gap in zip(values[1:], day_gaps):
        prior_variance = variance + gap * VARIANCE_RATIOS
        innovation_variance = prior_variance + 1.0
        sum_squared += (residual - mean) ** 2 / innovation_variance
        log_determinant += np.log(innovation_variance)
        mean, variance = update_state(mean, prior_variance, residual, 1.0)

    count = len(day_gaps)
    measurement_variance = np.maximum(sum_squared / count, MIN_MEASUREMENT_VARIANCE)
    likelihood_cost = (
        count * np.log(measurement_variance)
        + log_determinant
        + sum_squared / measurement_variance
    )
    best = np.argmin(likelihood_cost)
    return VARIANCE_RATIOS[best] * measurement_variance[best], measurement_variance[best]


def fit_parameters(calibration):
    """Estimate fixed per-hour variances wherever training coverage supports it."""
    residuals = (calibration[PRICE] - calibration["fundamentals"]).dropna()
    fits = []
    for hour, hourly_residuals in residuals.groupby(residuals.index.hour):
        if len(hourly_residuals) < MIN_ERRORS:
            continue
        process_variance, measurement_variance = fit_hour(hourly_residuals)
        fits.append(
            {
                "delivery_hour": hour,
                "training_residuals": len(hourly_residuals),
                "process_variance": process_variance,
                "measurement_variance": measurement_variance,
            }
        )

    if not fits:
        raise ValueError("No delivery hour has enough training residuals to fit the Kalman correction.")
    return pd.DataFrame(fits).set_index("delivery_hour")


def gaussian_forecast(center, variance, actual):
    """Return quantiles and proper scores for a Gaussian predictive distribution."""
    sd = sqrt(variance)
    half_width = Z_95 * sd
    lower = center - half_width
    upper = center + half_width
    innovation = crps = absolute_error = covered = np.nan
    if pd.notna(actual):
        error = actual - center
        innovation = error / sd
        standard_density = exp(-0.5 * innovation**2) / sqrt(2.0 * pi)
        crps = sd * (
            innovation * erf(innovation / sqrt(2.0)) + 2.0 * standard_density - 1.0 / sqrt(pi)
        )
        absolute_error = abs(error)
        covered = float(lower <= actual <= upper)

    return {
        "center_eur_per_mwh": center,
        "p05_eur_per_mwh": lower,
        "p50_eur_per_mwh": center,
        "p95_eur_per_mwh": upper,
        "predictive_sd_eur_per_mwh": sd,
        "standardized_innovation": innovation,
        "crps": crps,
        "absolute_error": absolute_error,
        "covered_90": covered,
        "interval_width_90": 2.0 * half_width,
    }


def replay_kalman(
    points, calibration_start, parameters, *, model=MODEL, variance_update=None,
    forecast_split="validation",
):
    """Replay the auctions, optionally updating process variance after each auction.

    The update receives the per-hour pre-update standardized innovations, with
    NaN for hours without an observation or an initialized state.
    """
    process_variance = parameters["process_variance"].reindex(range(24)).to_numpy()
    measurement_variance = parameters["measurement_variance"].reindex(range(24)).to_numpy()
    mean = np.full(24, np.nan)
    variance = np.full(24, np.nan)
    records = []
    replay = points.loc[calibration_start:]

    for delivery_day, batch in replay.groupby(replay.index.normalize(), sort=True):
        variance += process_variance
        issue_time = delivery_day - pd.Timedelta(days=1) + pd.Timedelta(hours=ISSUE_HOUR)

        for delivery, row in batch.loc[batch["split"].eq(forecast_split)].iterrows():
            hour = delivery.hour
            base = row["fundamentals"]
            if pd.isna(base) or pd.isna(mean[hour]):
                continue
            record = {
                "issue_time_local": issue_time,
                DELIVERY: delivery,
                "model": model,
                "actual_eur_per_mwh": row[PRICE],
                **gaussian_forecast(
                    base + mean[hour], variance[hour] + measurement_variance[hour], row[PRICE]
                ),
            }
            if variance_update is not None:
                record["process_variance"] = process_variance[hour]
            records.append(record)

        # Auction results become available to subsequent forecasts only.
        innovations = np.full(24, np.nan)
        residuals = (batch[PRICE] - batch["fundamentals"]).dropna()
        for delivery, residual in residuals.items():
            hour = delivery.hour
            noise = measurement_variance[hour]
            if pd.isna(noise):
                continue
            if pd.isna(mean[hour]):
                mean[hour], variance[hour] = residual, noise
            else:
                if variance_update is not None:
                    innovations[hour] = (residual - mean[hour]) / sqrt(variance[hour] + noise)
                mean[hour], variance[hour] = update_state(
                    mean[hour], variance[hour], residual, noise
                )

        if variance_update is not None:
            process_variance = variance_update(innovations)

    if not records:
        raise ValueError(f"No {forecast_split} forecasts could be issued with the Kalman correction.")
    return pd.DataFrame(records)


def innovation_diagnostics(forecasts):
    """Report per-hour validation innovation moments and adjacent-day correlation."""
    diagnostics = []
    by_hour = forecasts.groupby(forecasts[DELIVERY].dt.hour)
    for hour, batch in by_hour:
        daily = batch.set_index(DELIVERY)["standardized_innovation"].asfreq("D")
        pairs = pd.DataFrame({"current": daily, "previous": daily.shift()}).dropna()
        lag_one = np.nan
        if pairs.nunique().gt(1).all():
            lag_one = pairs["current"].corr(pairs["previous"])
        diagnostics.append(
            {
                "delivery_hour": hour,
                "observed_innovations": daily.count(),
                "standardized_mean": daily.mean(),
                "standardized_sd": daily.std(),
                "lag1_autocorrelation": lag_one,
            }
        )
    return pd.DataFrame(diagnostics).set_index("delivery_hour")


def main():
    points, calibration_start = prepare_backtest()
    calibration = points.loc[calibration_start:]
    parameters = fit_parameters(calibration.loc[calibration["split"].eq("train")])
    kalman = replay_kalman(points, calibration_start, parameters)
    baselines = replay_baselines(points, calibration_start)
    forecasts = pd.concat([baselines, kalman], ignore_index=True)
    scores = score_forecasts(forecasts, [*MODELS, MODEL])
    diagnostics = innovation_diagnostics(kalman)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    kalman.to_csv(OUTPUT / "validation_forecasts.csv.gz", index=False)
    scores.to_csv(OUTPUT / "validation_scores.csv")
    parameters.to_csv(OUTPUT / "kalman_parameters.csv")
    diagnostics.to_csv(OUTPUT / "validation_innovations_by_hour.csv")
    print(scores.round(4).to_string())
    print(f"\nSaved fixed Kalman forecasts, comparison scores and diagnostics to {OUTPUT}")


if __name__ == "__main__":
    main()
