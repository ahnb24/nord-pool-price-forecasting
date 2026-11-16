# Data sources and timing checklist

## Proposed first market

**Target.** The hourly Great Britain N2EX day-ahead auction price, in £/MWh. NESO publishes hourly delivery periods and an official rolling archive of Nord Pool auction prices. Confirm the actual dates and data resolution from the file during the audit. [NESO dataset and field definitions](https://www.neso.energy/data-portal/day-ahead-power-exchange-prices-nordpool/_n2ex_gb_day-ahead_price)

**Forecast timing.** Issue one batch before the relevant auction's gate closure. Verify the rules for the chosen dates and record both the cutoff and when auction results became available. Keep delivery and publication timestamps as separate fields. Use UTC to align sources and explicit market time for delivery-day and calendar features. [Nord Pool auction timing](https://support.nordpoolgroup.com/support/solutions/articles/8000088463-about-the-n2ex-day-ahead-auction)

| Input | Candidate source | What to verify |
| --- | --- | --- |
| Total load forecast | [Elexon DATL archive](https://bmrs.elexon.co.uk/api-documentation/endpoint/datasets/DATL/stream) | Publication time, revisions, delivery intervals and coverage before the auction. |
| Wind and solar generation forecasts | [Elexon wind and solar forecast](https://bmrs.elexon.co.uk/generation-forecast-for-wind-and-solar), with other archived runs from the [Elexon API catalogue](https://bmrs.elexon.co.uk/api-documentation/endpoint) to investigate | An archived run for the same delivery day must be available before the issue cutoff. Inspect the timestamps to establish this. |
| Lagged prices and calendar variables | N2EX auction series and the market delivery calendar | A lagged auction price is eligible once its auction result was available. |

Consider fuel prices after establishing a reliable data pipeline and verifying their historical availability at the chosen cutoff.

## First audit

1. Take a one-week sample of N2EX auction prices and candidate forecast records around the same delivery dates. Check a daylight saving transition separately.
2. Record source URLs, retrieval dates, field definitions, units and reuse terms. Preserve original files under `data/raw/`.
3. For a sample delivery day, match prices and inputs by delivery interval. Record when each input forecast was published. Compare the archived revisions and retain the latest version available at or before the cutoff. Apply the same information cutoff to historical price features.
4. Verify how half-hourly MW forecasts can be aligned to hourly price intervals, where applicable. Check for missing prices, incomplete forecast days and repeated local clock hours.
5. Measure how often each input would have been available across a longer representative sample. Record the final dataset choice, issue time, coverage and any limitations before preparing the modelling dataset.

## Real-data fallback

If the GB archive cannot support a fundamentals model with verified pre-auction inputs, use the published EPF Toolbox Nordic Nord Pool benchmark. The authors describe hourly prices and day-ahead load and wind forecasts from 2013 to 2018. Document its timestamp conventions and daylight saving adjustments. Use the authors' description of the forecast inputs and report any limits on reconstructing their original publication times. [Lago et al., open-access benchmark, Sections 3.1–3.2](https://www.dcsc.tudelft.nl/~bdeschutter/pub/rep/21_011.pdf) · [Dataset access](https://epftoolbox.readthedocs.io/en/latest/modules/data_extract.html)
