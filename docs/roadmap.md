# Project roadmap

**Research question.** Does adapting the state dynamics of a price correction improve out-of-sample probabilistic forecasts relative to a fixed correction and simpler residual updating?

**Initial scope.** At a single daily issue time before the day-ahead auction closes, forecast the distribution of the auction price for each delivery interval of the following delivery day. Score the marginal distribution for each interval. Use one market and a common information set across models.

## 1. Audit real data

Follow the [source and timing checklist](data-sources.md). Download a small sample, compare forecast issue and delivery timestamps, and determine whether the sources can support a longer backtest. Check the historical auction clock, time zone conventions, coverage, publication delays and reuse terms.

**Deliverable.** A decision on the market, price product, forecast issue time and study period, supported by a source inventory and a sample with verified forecast availability.

## 2. Prepare and explore the dataset

Keep source snapshots and retrieve the agreed interval of history. Align auction prices with archived input forecasts using delivery intervals and the daily issue cutoff. Record how revisions, duplicates, gaps and daylight saving transitions are handled. Explore seasonality, residual demand where definitions align, price spikes and negative prices. Restrict model design exploration to the development period.

**Deliverable.** A reproducible modelling dataset with a data dictionary, a coverage report and chronological training, validation and test windows.

## 3. Set up the daily backtest and fit baselines

Replay one auction day at a time. Establish a seasonal naive benchmark, a price-history model and an interpretable fundamentals model using the available load and renewable forecasts. Compare the fundamentals model with a simple rolling or exponentially weighted residual correction. Use historical forecast errors to construct predictive distributions for the simpler models. Document the baseline fitting and update schedules.

**Deliverable.** Baseline forecasts saved by issue and delivery time, plus validation scores that later models can reproduce and compare against.

## 4. Add the fixed state-space correction

Model persistent errors in the fundamentals baseline with a latent state. Specify the observation and transition equations, estimate fixed noise parameters on development data and check the innovations. For the day-ahead task, issue the full set of delivery-interval forecasts together, then update from auction results as they become available. Compare a scalar correction by delivery hour with the existing baselines if data coverage supports it.

**Deliverable.** A fixed Kalman model with validated uncertainty propagation and an explanation of its assumptions.

## 5. Add one adaptive correction

Pick one adaptation rule for the process variance using training and validation data. Keep the underlying baseline and backtest schedule the same. Test whether the rule responds to sustained forecast errors and how much it reacts to individual price spikes.

**Deliverable.** A fixed versus adaptive comparison with a documented update rule and tuning choices.

## 6. Evaluate and document the findings

Run the agreed protocol on the held-out period. Use CRPS as the primary score, with MAE and prediction interval coverage and width as supporting diagnostics. Compare errors on the same delivery intervals and account for dependence across nearby observations when reporting uncertainty in score differences. Define stress periods or event criteria before scoring the holdout. Report post hoc cases as exploratory evidence and distinguish observed forecast deterioration from verified structural changes.

**Deliverable.** Results tables, calibration and adaptation plots, a concise report, and instructions for reproducing the analysis with the original data sources.
