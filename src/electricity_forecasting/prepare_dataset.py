"""Step 2: Prepare the hourly Nordic Nord Pool benchmark."""

import hashlib
import json
from pathlib import Path

import pandas as pd

from audit_real_data import snapshot


ROOT = Path(__file__).resolve().parents[2]
SOURCE = "https://zenodo.org/records/4624805/files/NP.csv"
RAW = ROOT / "data" / "raw" / "nord_pool" / "NP.csv"
OUTPUT = ROOT / "data" / "processed" / "nord_pool"

START = "2013-01-01"
VALIDATION_START = "2015-12-29"
TEST_START = "2016-12-27"  # Published benchmark test window
END = "2018-12-24"

PRICE = "price_eur_per_mwh"
LOAD = "load_forecast_mw"
WIND = "wind_forecast_mw"
NET_LOAD = "load_minus_wind_mw"
SOURCE_FIELDS = [PRICE, LOAD, WIND]


def load_hourly_data(path):
    source = pd.read_csv(path, index_col=0)
    rename = {}

    for label, field in (("price", PRICE), ("load", LOAD), ("wind", WIND)):
        matches = [column for column in source.columns if label in column.casefold()]
        if len(matches) != 1:
            raise ValueError(f"Expected one {label} column. Found {source.columns.tolist()}")
        rename[matches[0]] = field

    data = source.rename(columns=rename)[SOURCE_FIELDS].apply(pd.to_numeric)
    data = data.replace([float("inf"), float("-inf")], float("nan"))
    data.index = pd.DatetimeIndex(
        pd.to_datetime(source.index, errors="raise"),
        name="delivery_start_local",
    )

    expected = pd.date_range(
        START, f"{END} 23:00", freq="h", name=data.index.name
    )
    unexpected = data.index.difference(expected)
    if len(unexpected):
        raise ValueError(f"Unexpected delivery timestamps: {unexpected[:5].tolist()}")

    original_rows = len(data)
    data = data.reset_index().drop_duplicates().set_index(expected.name)
    duplicates_removed = original_rows - len(data)

    conflicts = data.index[data.index.duplicated(keep=False)].unique()
    if len(conflicts):
        raise ValueError(f"Conflicting rows at: {conflicts[:5].tolist()}")

    observed = expected.isin(data.index)
    data = data.reindex(expected)
    data["source_row_present"] = observed
    return data, duplicates_removed


def main():
    RAW.parent.mkdir(parents=True, exist_ok=True)
    inventory = []
    snapshot(SOURCE, RAW, inventory)
    data, duplicates_removed = load_hourly_data(RAW)

    data["delivery_day"] = data.index.date
    data["hour"] = data.index.hour
    data["weekday"] = data.index.dayofweek
    data["split"] = "train"
    data.loc[data.index >= VALIDATION_START, "split"] = "validation"
    data.loc[data.index >= TEST_START, "split"] = "test"
    data[NET_LOAD] = data[LOAD] - data[WIND]
    data["complete_case"] = data[SOURCE_FIELDS].notna().all(axis=1)

    coverage = (
        data.assign(
            price_present=data[PRICE].notna(),
            load_present=data[LOAD].notna(),
            wind_present=data[WIND].notna(),
        )
        .groupby(["split", "delivery_day"], sort=False)
        .agg(
            expected_hours=("hour", "size"),
            source_hours=("source_row_present", "sum"),
            price_hours=("price_present", "sum"),
            load_hours=("load_present", "sum"),
            wind_hours=("wind_present", "sum"),
            complete_hours=("complete_case", "sum"),
        )
    )

    # Explore training and validation only. Estimate the spike threshold on training.
    train_prices = data.loc[data["split"].eq("train"), PRICE].dropna()
    if train_prices.empty:
        raise ValueError("The training window contains no prices.")

    spike_threshold = float(train_prices.quantile(0.99))
    development = data.loc[data["split"].ne("test")].dropna(subset=[PRICE]).copy()
    development["negative_price"] = development[PRICE].lt(0)
    development["high_price"] = development[PRICE].gt(spike_threshold)
    seasonality = development.groupby(["weekday", "hour"]).agg(
        price_hours=(PRICE, "size"),
        mean_price_eur_per_mwh=(PRICE, "mean"),
        mean_load_minus_wind_mw=(NET_LOAD, "mean"),
        negative_price_hours=("negative_price", "sum"),
        high_price_hours=("high_price", "sum"),
    )

    dictionary = {
        "source": inventory[0],
        "source_sha256": hashlib.sha256(RAW.read_bytes()).hexdigest(),
        "split_windows_inclusive": {
            "train": [START, "2015-12-28"],
            "validation": [VALIDATION_START, "2016-12-26"],
            "test": [TEST_START, END],
        },
        "timestamp_policy": (
            "Use the benchmark's local hourly labels. Its authors averaged "
            "the repeated autumn hour and interpolated the missing spring hour."
        ),
        "forecast_availability": (
            "Use the published day-ahead forecast columns. The CSV omits "
            "publication timestamps and revision history."
        ),
        "missing_data_policy": "Keep absent hours and missing values as blanks.",
        "exact_duplicate_rows_removed": duplicates_removed,
        "development_high_price_threshold_eur_per_mwh": spike_threshold,
        "columns": {
            "delivery_start_local": "Hourly delivery label on the benchmark clock.",
            "delivery_day": "Calendar date on the benchmark clock.",
            "hour": "Hour of day from 0 to 23.",
            "weekday": "Day of week from 0 (Monday) to 6 (Sunday).",
            "split": "Chronological train, validation, or test window.",
            PRICE: "Observed day-ahead auction price in EUR/MWh. Model target.",
            LOAD: "Day-ahead grid load forecast in MW.",
            WIND: "Day-ahead wind generation forecast in MW.",
            NET_LOAD: "Load forecast minus wind forecast in MW.",
            "source_row_present": "Whether the original CSV contained this hour.",
            "complete_case": "Whether price, load, and wind are all present.",
        },
    }

    OUTPUT.mkdir(parents=True, exist_ok=True)
    data.to_csv(OUTPUT / "model_dataset.csv")
    coverage.to_csv(OUTPUT / "daily_coverage.csv")
    seasonality.to_csv(OUTPUT / "development_seasonality.csv")
    (OUTPUT / "data_dictionary.json").write_text(
        json.dumps(dictionary, indent=2) + "\n",
        encoding="utf-8",
    )

    print(coverage.groupby(level="split", sort=False).sum().to_string())
    print(f"\nSaved dataset and reports to {OUTPUT}")


if __name__ == "__main__":
    main()
