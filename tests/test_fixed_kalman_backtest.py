from math import sqrt
from unittest import TestCase

import pandas as pd

from src.electricity_forecasting.baseline_backtest import DELIVERY, PRICE
from src.electricity_forecasting.fixed_kalman_backtest import (
    MIN_MEASUREMENT_VARIANCE,
    fit_hour,
    replay_kalman,
)


class FixedKalmanBacktestTests(TestCase):
    def test_replay_propagates_variance_and_updates_after_issue(self):
        dates = pd.date_range("2015-12-28 08:00", periods=4, freq="D", name=DELIVERY)
        points = pd.DataFrame(
            {
                PRICE: [12.0, float("nan"), 30.0, 23.0],
                "fundamentals": 10.0,
                "split": ["train", "validation", "validation", "validation"],
            },
            index=dates,
        )
        parameters = pd.DataFrame(
            {"process_variance": [1.0], "measurement_variance": [4.0]}, index=[8]
        )

        forecasts = replay_kalman(points, dates[0].normalize(), parameters).set_index(DELIVERY)

        self.assertEqual(forecasts.loc[dates[2], "issue_time_local"], pd.Timestamp("2015-12-29 11:00"))
        self.assertAlmostEqual(forecasts.loc[dates[1], "predictive_sd_eur_per_mwh"], 3.0)
        self.assertAlmostEqual(forecasts.loc[dates[2], "p50_eur_per_mwh"], 12.0)
        self.assertAlmostEqual(forecasts.loc[dates[2], "predictive_sd_eur_per_mwh"], sqrt(10.0))
        self.assertAlmostEqual(forecasts.loc[dates[3], "p50_eur_per_mwh"], 22.8)
        self.assertAlmostEqual(forecasts.loc[dates[3], "predictive_sd_eur_per_mwh"], sqrt(7.4))

    def test_fit_accepts_zero_process_variance(self):
        dates = pd.date_range("2015-01-01", periods=30, freq="D")
        residuals = pd.Series(2.0, index=dates)

        process_variance, measurement_variance = fit_hour(residuals)

        self.assertEqual(process_variance, 0.0)
        self.assertEqual(measurement_variance, MIN_MEASUREMENT_VARIANCE)
