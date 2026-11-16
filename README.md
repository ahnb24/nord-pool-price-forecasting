# Nord Pool Day-Ahead Price Forecasting

Does adapting the process variance of a Kalman correction improve probabilistic day-ahead electricity-price forecasts? This project compares four baselines with fixed and adaptive Kalman corrections using hourly Nordic Nord Pool prices and day-ahead load and wind forecasts.

## Results

On 17,472 held-out delivery hours from December 2016 through December 2018, adaptive Kalman had the lowest mean CRPS at 1.9347 EUR/MWh. It improved on fixed Kalman by 0.20% and on the EWMA residual correction by 9.3%. Fixed Kalman scored slightly better during high-price hours. The adaptive model's nominal 90% interval covered 87.49% of holdout prices. [Evaluation report and plots](docs/step-6-evaluation.md)

## Reproduce the evaluation

From the repository root, run the following with Python 3.10 or later. The preparation script downloads the Nordic Nord Pool benchmark from its original source. The evaluation script regenerates the forecasts, scores and figures. [Original benchmark file](https://zenodo.org/records/4624805/files/NP.csv)

```bash
python -m pip install -r requirements.txt
python src/electricity_forecasting/prepare_dataset.py
python -m src.electricity_forecasting.evaluate_holdout
python -m unittest discover -s tests -v
```

The repository includes published summary tables, figures and source metadata. The preparation and evaluation commands generate the local data and hourly outputs.

| Folder | Purpose |
| --- | --- |
| `docs/` | [Project roadmap](docs/roadmap.md), [data-source notes](docs/data-sources.md) and [holdout report](docs/step-6-evaluation.md) |
| `data/` | Source metadata, data dictionary and coverage summaries |
| `src/electricity_forecasting/` | Python modules for data preparation, models and evaluation |
| `tests/` | Checks for timestamp handling, forecast timing and model behaviour |
| `results/` | Summary tables and figures from the model comparisons |
