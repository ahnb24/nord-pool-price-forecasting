"""Audit GB N2EX prices and archived Elexon forecasts before a backtest."""

import argparse
import csv
import json
import time as wall_clock
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
HALF_HOUR = timedelta(minutes=30)
PRICE_PAGE = (
    "https://www.neso.energy/data-portal/day-ahead-power-exchange-prices-nordpool/"
    "_n2ex_gb_day-ahead_price"
)
PRICE_URL = "https://api.neso.energy/datastore/dump/4f27eea5-7038-4f73-9740-e3e4ad47c26a"
ELEXON_API = "https://data.elexon.co.uk/bmrs/api/v1/datasets"
ELEXON_DOCS = "https://bmrs.elexon.co.uk/api-documentation/endpoint/datasets"
AUCTION_RULES = (
    "https://www.nordpoolgroup.com/48e283/globalassets/download-center/"
    "rules-and-regulations/day-ahead-market-regulations-gb-market.pdf"
)
AUCTION_CLOCK = (
    "https://www.nordpoolgroup.com/4ab576/globalassets/download-center/"
    "rules-and-regulations/product-specifications-gb-market.pdf"
)
SERIES = ("load", "wind_onshore", "wind_offshore", "solar")
WIND_SOLAR = {
    "Wind Onshore": "wind_onshore",
    "Wind Offshore": "wind_offshore",
    "Solar": "solar",
}


@dataclass(frozen=True)
class Forecast:
    published: datetime
    document: str
    revision: int
    mw: float


def timestamp(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError(f"Forecast timestamp has no UTC offset: {value}")
    return instant.astimezone(UTC)


def days(first, last):
    return (first + i * DAY for i in range((last - first).days + 1))


def local_utc(day, clock, london):
    return datetime.combine(day, clock, london).astimezone(UTC)


def snapshot(url, path, inventory):
    """Keep the original response and retrieval metadata for repeatable runs."""
    metadata_path = path.with_suffix(path.suffix + ".source.json")
    if path.is_file() and metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        print(f"Downloading {path.name}", flush=True)
        for attempt in range(3):
            try:
                request = Request(url, headers={"User-Agent": "GB-N2EX-research-audit/1.0"})
                with urlopen(request, timeout=45) as response:
                    content = response.read()
                break
            except (URLError, TimeoutError) as exc:
                retryable = not isinstance(exc, HTTPError) or exc.code in (429, 500, 502, 503, 504)
                if attempt == 2 or not retryable:
                    raise RuntimeError(f"Download failed for {url}: {exc}") from exc
                wall_clock.sleep(2 ** (attempt + 1))
        path.write_bytes(content)
        metadata = {"file": path.name, "url": url, "retrieved_utc": datetime.now(UTC).isoformat()}
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    inventory.append(metadata)
    return path


def load_prices(path):
    prices = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"Date", "Delivery Period", "Price"}.issubset(reader.fieldnames or ()):
            raise ValueError("Unexpected NESO CSV columns. See the raw price snapshot.")
        for row in reader:
            start_text, end_text = row["Delivery Period"].split(" - ")
            start = datetime.combine(date.fromisoformat(row["Date"]), time.fromisoformat(start_text), UTC)
            if start.minute or end_text != (start + HOUR).strftime("%H:%M"):
                raise ValueError(f"Unexpected NESO delivery period: {row}")
            price = row["Price"].strip()
            prices[start].append(float(price) if price else None)
    if not prices:
        raise ValueError("The NESO price snapshot is empty.")
    return prices


def load_forecasts(audit_days, raw, inventory, wanted):
    index = defaultdict(set)
    publication_days = sorted({day for delivery_day in audit_days
                               for day in days(delivery_day - 3 * DAY, delivery_day)})
    for day in publication_days:
        query = urlencode({
            "publishDateTimeFrom": f"{day}T00:00:00Z",
            "publishDateTimeTo": f"{day + DAY}T00:00:00Z",
        })
        for dataset in ("DATL", "DGWS"):
            path = snapshot(f"{ELEXON_API}/{dataset}/stream?{query}",
                            raw / f"{dataset.lower()}_{day}.json", inventory)
            records = json.loads(path.read_bytes())
            if not isinstance(records, list):
                raise ValueError(f"Unexpected Elexon response in {path}")
            for row in records:
                series = "load"
                if dataset == "DGWS":
                    if row["processType"] != "Day ahead":
                        continue
                    series = WIND_SOLAR.get(row["psrType"])
                    if series is None:
                        continue
                start = timestamp(row["startTime"])
                if start in wanted and row["quantity"] is not None:
                    index[(start, series)].add(Forecast(
                        timestamp(row["publishTime"]), row["documentId"] or "",
                        int(row["documentRevisionNumber"] or 0), float(row["quantity"]),
                    ))
    return index


def choose_forecast(records, cutoff):
    eligible = [record for record in records if record.published <= cutoff]
    if not eligible:
        return None, "late_only" if records else "missing"
    latest_time = max(record.published for record in eligible)
    latest = [record for record in eligible if record.published == latest_time]
    # Revision numbers are meaningful within a document.
    latest_revision = defaultdict(int)
    for record in latest:
        latest_revision[record.document] = max(latest_revision[record.document], record.revision)
    winners = [record for record in latest if record.revision == latest_revision[record.document]]
    if len({record.mw for record in winners}) != 1:
        return None, "conflicting_revisions"
    return min(winners, key=lambda record: record.document), "available"


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2025, 11, 3))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2025, 11, 9))
    parser.add_argument("--dst-date", type=date.fromisoformat, action="append",
                        help="Extra transition day. Repeat to check more than one.")
    parser.add_argument("--issue", type=time.fromisoformat, default=time(9, 45),
                        help="Forecast issue time on auction day, in UK local time.")
    args = parser.parse_args()
    if args.end < args.start:
        parser.error("--end must be on or after --start")
    if args.issue.tzinfo is not None or args.issue >= time(9, 50):
        parser.error("--issue must be a UK local time before 09:50")
    try:
        london = ZoneInfo("Europe/London")
    except ZoneInfoNotFoundError:
        parser.exit(1, "Europe/London timezone data is missing. Run: python -m pip install tzdata\n")

    transitions = args.dst_date or [date(2025, 3, 30), date(2025, 10, 26)]
    audit_days = sorted(set(days(args.start, args.end)) | set(transitions))
    run_name = f"{args.start}_to_{args.end}_{args.issue:%H%M%S}"
    raw = ROOT / "data" / "raw" / "audit_real_data"
    output = ROOT / "results" / "tables" / "audit_real_data" / run_name
    raw.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)

    # The auction batch starts at 23:00 UK time on the preceding calendar date.
    hours = {}
    for day in audit_days:
        start = local_utc(day - DAY, time(23), london)
        end = local_utc(day, time(23), london)
        hours[day] = [start + i * HOUR for i in range((end - start) // HOUR)]
    wanted = {instant + offset for batch in hours.values() for instant in batch
              for offset in (timedelta(0), HALF_HOUR)}

    inventory = []
    prices = load_prices(snapshot(PRICE_URL, raw / "neso_n2ex.csv", inventory))
    forecasts = load_forecasts(audit_days, raw, inventory, wanted)
    hourly_rows, daily_rows = [], []
    for day, batch in hours.items():
        auction_day = day - DAY
        cutoff = local_utc(auction_day, args.issue, london)
        daily = {
            "auction_delivery_day": day,
            "daylight_saving_check": day in transitions,
            "expected_hours": len(batch),
            "price_hours": 0,
            "duplicate_price_hours": 0,
            **{f"{series}_hours_before_cutoff": 0 for series in SERIES},
            "joint_hours_before_cutoff": 0,
        }
        for start in batch:
            values = prices.get(start, [])
            price = values[0] if len(set(values)) == 1 else None
            daily["price_hours"] += price is not None
            daily["duplicate_price_hours"] += len(values) > 1
            row = {
                "auction_delivery_day": day,
                "delivery_start_utc": start.isoformat(),
                "delivery_start_uk": start.astimezone(london).isoformat(),
                "issue_cutoff_utc": cutoff.isoformat(),
                "gate_closure_scheduled_utc": local_utc(auction_day, time(9, 50), london).isoformat(),
                "preliminary_results_scheduled_utc": local_utc(auction_day, time(10), london).isoformat(),
                "price_gbp_per_mwh": price,
                "price_source_rows": len(values),
            }
            for series in SERIES:
                selected = []
                for label, half_start in (("first", start), ("second", start + HALF_HOUR)):
                    records = forecasts.get((half_start, series), ())
                    record, status = choose_forecast(records, cutoff)
                    prefix = f"{series}_{label}_half"
                    row[f"{prefix}_status"] = status
                    row[f"{prefix}_published_utc"] = record.published.isoformat() if record else None
                    row[f"{prefix}_document"] = record.document if record else None
                    row[f"{prefix}_revision"] = record.revision if record else None
                    row[f"{prefix}_candidate_count"] = len(records)
                    earliest = min((candidate.published for candidate in records), default=None)
                    row[f"{prefix}_first_observed_publication_utc"] = (
                        earliest.isoformat() if earliest else None
                    )
                    if record:
                        selected.append(record.mw)
                complete = len(selected) == 2
                row[f"{series}_mw"] = sum(selected) / 2 if complete else None
                daily[f"{series}_hours_before_cutoff"] += complete
            joint = price is not None and all(row[f"{series}_mw"] is not None for series in SERIES)
            daily["joint_hours_before_cutoff"] += joint
            hourly_rows.append(row)
        daily_rows.append(daily)

    write_csv(output / "hourly_audit.csv", hourly_rows)
    write_csv(output / "daily_coverage.csv", daily_rows)
    report = {
        "candidate_market": "GB hourly N2EX day-ahead auction",
        "issue_time_uk": args.issue.isoformat(),
        "sample_from": str(args.start),
        "sample_through": str(args.end),
        "dst_dates": [str(day) for day in transitions],
        "price_archive_first_hour_utc": min(prices).isoformat(),
        "price_archive_last_hour_utc": max(prices).isoformat(),
        "sources": {
            "price": {"docs": PRICE_PAGE, "unit": "GBP/MWh", "clock": "GMT/UTC",
                      "terms": "https://www.neso.energy/data-portal/neso-open-licence",
                      "attribution": "Supported by National Energy SO Open Data"},
            "forecasts": {"load_docs": f"{ELEXON_DOCS}/DATL/stream",
                          "renewables_docs": f"{ELEXON_DOCS}/DGWS/stream", "unit": "MW",
                          "terms": "https://developer.data.elexon.co.uk/"},
            "auction": {"rulebook": AUCTION_RULES, "product_specifications": AUCTION_CLOCK,
                        "normal_results_time": "https://www.nordpoolgroup.com/en/message-center-container/"
                        "newsroom/exchange-message-list/2021/q4/nord-pool-welcomes-nsl-launch/"},
        },
        "limitations": [
            "Forecast availability is based on Elexon's archived publishTime fields.",
            "The publication query spans D-3 through D for each sampled auction delivery day D.",
            "DGWS coverage uses the Day ahead process. Other processes remain in the raw snapshots.",
            "The NESO archive has no per-auction publication timestamps. The schedule columns give "
            "normal timings. Actual availability of lagged auction results requires separate evidence.",
            "Use daily coverage across a longer sample to choose a study period.",
        ],
        "snapshots": inventory,
    }
    (output / "source_inventory.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for daily in daily_rows:
        print(f"{daily['auction_delivery_day']}  price {daily['price_hours']}/{daily['expected_hours']}  "
              f"price + all forecasts {daily['joint_hours_before_cutoff']}/{daily['expected_hours']}")
    print(f"Audit files saved in {output}")


if __name__ == "__main__":
    main()
