"""Step 5: Compare adaptive and fixed Kalman corrections on validation data.

Run from the project root with:
    python -m src.electricity_forecasting.adaptive_kalman_backtest

Use the same fundamentals regression, training-fitted process variance Q and
measurement variance R, and daily auction replay as Step 4. For each hour,
let z be the latest pre-update standardized innovation. After observing the
auction, update a signed signal s = (1 - alpha) * s + alpha * clip(z, -2.5, 2.5).
The next auction uses Q + strength * R * max(abs(s) - 0.5, 0)**2. A missing
innovation decays the signal toward zero. Clipping and the 0.5 threshold keep
an isolated spike from raising Q from a neutral signal at either tested alpha.
The usual Kalman observation update still uses the actual residual.

Select alpha and strength by mean CRPS on the same observed validation hours
as the fixed model. Validation scores are used for selection. The held-out
period is reserved for Step 6.
"""

from itertools import product

import numpy as np
import pandas as pd

from .baseline_backtest import DELIVERY, PRICE, ROOT, prepare_backtest, score_forecasts
from .fixed_kalman_backtest import MODEL as FIXED_MODEL
from .fixed_kalman_backtest import fit_parameters, replay_kalman


MODEL = "fundamentals_kalman_adaptive"
OUTPUT = ROOT / "results" / "tables" / "adaptive_kalman_backtest"
ALPHAS = (0.10, 0.20)
STRENGTHS = (0.25, 1.0)
INNOVATION_CLIP = 2.5
SIGNAL_THRESHOLD = 0.5


def adaptive_variance_update(parameters, alpha, strength):
    """Create an independent process-variance update for a single replay."""
    base = parameters["process_variance"].reindex(range(24)).to_numpy()
    measurement = parameters["measurement_variance"].reindex(range(24)).to_numpy()
    signal = np.zeros(24)

    def update(innovations):
        clipped = np.nan_to_num(np.clip(innovations, -INNOVATION_CLIP, INNOVATION_CLIP))
        signal[:] = (1.0 - alpha) * signal + alpha * clipped
        excess = np.maximum(np.abs(signal) - SIGNAL_THRESHOLD, 0.0)
        return base + strength * measurement * excess**2

    return update


def tune_adaptation(points, calibration_start, parameters, fixed):
    """Return the best paired validation forecasts, their scores and the tuning grid."""
    trials = []
    for alpha, strength in product(ALPHAS, STRENGTHS):
        adaptive = replay_kalman(
            points,
            calibration_start,
            parameters,
            model=MODEL,
            variance_update=adaptive_variance_update(parameters, alpha, strength),
        )
        comparison = pd.concat([fixed, adaptive], ignore_index=True)
        scores = score_forecasts(comparison, [FIXED_MODEL, MODEL])
        mean_crps = scores.at[MODEL, "mean_crps"]
        if not trials or mean_crps < best_crps:
            best_crps, best_forecasts, best_scores = mean_crps, comparison, scores
            best_trial = len(trials)
        trials.append(
            {
                "alpha": alpha,
                "strength": strength,
                "innovation_clip": INNOVATION_CLIP,
                "signal_threshold": SIGNAL_THRESHOLD,
                "scored_hours": scores.at[MODEL, "scored_hours"],
                "adaptive_mean_crps": mean_crps,
                "fixed_mean_crps": scores.at[FIXED_MODEL, "mean_crps"],
            }
        )

    tuning = pd.DataFrame(trials)
    tuning["selected"] = tuning.index == best_trial
    return best_forecasts, best_scores, tuning


def response_diagnostics(alpha, strength):
    """Replay a single spike and an equal-sized lasting shift from a stable baseline."""
    days = pd.date_range("2000-01-01", periods=28, freq="D", name=DELIVERY)
    points = pd.DataFrame({PRICE: 0.0, "fundamentals": 0.0, "split": "validation"}, index=days)
    points.loc[days[:14], "split"] = "train"
    parameters = pd.DataFrame({"process_variance": [0.1], "measurement_variance": [1.0]}, index=[0])
    diagnostics = []
    for scenario, event_days in [("isolated_spike", days[14:15]), ("sustained_shift", days[14:])]:
        scenario_points = points.copy()
        scenario_points.loc[event_days, PRICE] = 8.0
        forecasts = replay_kalman(
            scenario_points,
            days[0],
            parameters,
            model=MODEL,
            variance_update=adaptive_variance_update(parameters, alpha, strength),
        )
        forecasts.insert(0, "scenario", scenario)
        forecasts.insert(1, "day_since_first_event", (forecasts[DELIVERY] - days[14]).dt.days)
        diagnostics.append(forecasts)

    return pd.concat(diagnostics, ignore_index=True)


def main():
    points, calibration_start = prepare_backtest()
    calibration = points.loc[calibration_start:]
    parameters = fit_parameters(calibration.loc[calibration["split"].eq("train")])
    fixed = replay_kalman(points, calibration_start, parameters)
    fixed["process_variance"] = fixed[DELIVERY].dt.hour.map(parameters["process_variance"])
    forecasts, scores, tuning = tune_adaptation(points, calibration_start, parameters, fixed)
    selected = tuning.loc[tuning["selected"]].iloc[0]
    diagnostics = response_diagnostics(selected["alpha"], selected["strength"])

    OUTPUT.mkdir(parents=True, exist_ok=True)
    forecasts.to_csv(OUTPUT / "validation_forecasts.csv.gz", index=False)
    scores.to_csv(OUTPUT / "validation_scores.csv")
    tuning.to_csv(OUTPUT / "validation_tuning.csv", index=False)
    diagnostics.to_csv(OUTPUT / "synthetic_response.csv", index=False)
    parameters.to_csv(OUTPUT / "kalman_parameters.csv")
    print(scores.round(4).to_string())
    print(f"\nSelected alpha={selected['alpha']:g}, strength={selected['strength']:g}")
    print(f"Saved the fixed-versus-adaptive comparison and spike diagnostics to {OUTPUT}")


if __name__ == "__main__":
    main()
