# VEO recorded-DD forecasting

## Project-based capacity update — October 30, 2026

The primary capacity estimate in personal, staffing, page and tomorrow reports
uses linked **CD → DD** records. CD is the initial close deal and DD is the later
paid outcome. These are post timestamps; written dates and edit times do not
replace them or establish hands-on hours.

The matching key is a Telegram member identity, source team, canonical page,
exact normalized client and equal recorded deal price. Only the explicit
`repeat client` suffix is removed; no fuzzy names or price upgrades are guessed.
Same-day reposts use their earliest accepted timestamp for timing while the
existing sales-count policy stays intact. Daily CD summaries, ambiguous multiple
starts, reversed/zero/naive timestamps, transfers, and overlapping cross-team
copies of the same client are excluded. A later CD after a completed pair can
start a new project for the same repeat client. A live copy supersedes its
export. Name-only exports require a unique ID association in the same team's
evidence before entering a personal estimate.

For pairs with DD in the latest 56 completed PHT dates, retain the actual CD
timestamp even if it predates the window. The organized raw cache supplies up
to 192 dates of history; a CD outside available history stays unmatched.

1. Compute mean CD-to-DD elapsed days over matched completed projects.
2. Compute the union of their CD-to-DD intervals, excluding gaps with no matched
   project underway.
3. Time-weighted concurrent projects = total project elapsed time / union time.
4. Estimated DD per elapsed day with work available = time-weighted concurrency /
   mean CD-to-DD days, equivalently matched completions / union elapsed days.

At least three matched projects on three distinct completion dates are required
for a rate; a smaller sample may still show descriptive cycle times. Mean and
median cycle hours, time-weighted concurrency and the observed concurrent peak
are separate. A brief peak is not used as sustained capacity. Arithmetic keeps
decimals and calendar days (24 hours); no invented working shift is used.

Average DD per Active date includes zero-DD Active dates in the same 56-date
window. Average DD per output date is separate and may include output without a
verified Active vote. Unknown attendance does not become zero or Inactive and
is not needed to measure timestamp intervals. Quiet dates do not dilute the
project-based rate while matched evidence remains in the window.

Unresolved CDs, unmatched DDs and ambiguous matches are counted but do not prove
current active work. They cannot inflate concurrency. Excluding unresolved or
unrecorded outcomes can bias the completed-project estimate; coverage is labelled
partial when these exist. Calendar elapsed time includes nights, payment waits
and delayed posting. This is an observed workflow estimate, not verified
production completion, a maximum, an assignment cap, a sales forecast or an
independently validated improvement in future accuracy.

Team/page summaries sum personal rates once per Telegram ID, show known/unknown
coverage and leave incomplete totals Unknown. These are combined VEO personal
histories, so page/team totals overlap. Daily-DD output references and the guarded
recent-demand sales forecast remain separate. Cache v12 backreads both CD and DD
topics and qualifying exports. Old snapshots remain preserved and cannot supply
a fabricated project rate. Scheduler configuration, assignment limits, payroll
policy and the Suno model are unchanged.

A failed membership lookup now saves an explicit unknown bot/human flag for its
ten-minute retry window. The person remains unverified and excluded, while the
attempt no longer masquerades as an unfinished membership sweep that blocks the
entire nightly batch. Successful human verification is still required to include
a person, and expired failures are retried normally.

## Workload-aware output update — October 29, 2026

Reports separate the historical-demand sales forecast from evidence about staff
output when client work is available. Low sales, paused advertising, and limited
client supply do not prove a low production limit. No advertising activity is
inferred from a zero sale or an undelivered bot offer.

The personal capacity reference now describes **demonstrated repeatable output**:
the highest recorded DD level reached on at least three distinct declared-Active
dates in the last 56 completed dates. Each person keeps their own support count,
support dates and latest supporting date. A single peak cannot set the level.
It is an observed level, not expected daily sales or a verified maximum. Fewer
than three eligible personal dates means Unknown, never the 0.20 sales default.
An unknown-work zero does not erase demonstrated output. Support last observed
more than 14 days earlier is labelled older evidence.

Alongside that level, the report shows a recent personal selling-day average,
using the shortest of 14, 28 and 56 completed dates containing at least three
positive Active dates. Its sample size and window are displayed. The former
56-day selling-day median is retained as descriptive source data, not the main
capacity reference. These statistics deliberately answer different questions;
they must not be represented as predictions of everyday sales.

Supplied-work output uses the arithmetic mean over at least three Active dates
with a manager-confirmed, member-specific record of sufficient work, including
zero sales on those dates. Limited/no-work dates and unknown workload are excluded
from this measure, without rewriting recorded output or the sales forecast.
Team-level ads running does not imply sufficient work for each person; ads paused
does not imply no organic, repeat, or backlog clients. For combined VEO history,
sufficient personal work in any source team counts; no-work/limited-work needs
coverage for every source team. The records describe supplied work, not verified
full shifts. Paid-sale counts still do not measure completed videos or a maximum.

Read and record context through the authenticated wrapper; these commands never
queue or send Telegram messages:

```powershell
.\remote_windows.ps1 workload --group=-1003647732254 --date 2026-10-28
.\remote_windows.ps1 workload --group=-1003647732254 --date 2026-10-28 --save --ads paused --workload unknown --note 'Ads paused; organic/backlog work not established'
.\remote_windows.ps1 workload --group=-1003647732254 --date 2026-10-28 --member 123456789 --save --ads running --workload sufficient --note 'Confirmed enough client briefs supplied for the shift'
```

Use actual member IDs and verified records. An optional `--source-message` links
the evidence to that group's message ID. Saving replaces that date/group/member
record; omitted status fields become Unknown. Future dates are plans (`actual=false`)
and must be confirmed again on/after the work date before informing a measure.
No historical ad/workload status has been guessed or backfilled automatically.

Team/page references count shared people once, report evidence coverage, and keep
unknown people out of supported subtotals; incomplete totals remain Unknown.
Page-access/poll gaps are still explicit. Page references describe people's
combined DD and must not be added as exclusive page sales. The original sales
forecast and its 80% *sales* buffer remain separate. The change does not claim a
sales-forecast accuracy improvement: future demand and completed-work data are
needed to validate that. Frozen nightly reports/forecasts remain unchanged.
Makata's private receipt report waits for membership checks in its own shared
Suno cohort; an unrelated VEO membership retry cannot block that report.

The forecast estimates paid-sale records for people who declare themselves
Active. It does not measure completed videos, available client demand, work
hours, or maximum production capacity. The policy is a trial pending future
validation. Suno uses its existing separate receipt policy.

## Live policy update — October 24, 2026

The organized VEO capacity reports and tomorrow preview now use the guarded v7
offer model described below, falling back to guarded v6 if offer evidence cannot
be read. Each model retains its earlier model unless the personal error checks
pass. The live cache keeps 192 completed dates, checks current human membership,
and saves plans by history, target work date, cohort and model source signature.
The first calculation is slower; recurring staffing refreshes reuse those plans.
During membership rechecks, a companion snapshot for the same raw history,
scope, model sources and target date preserves the prepared personal model.
Only currently verified humans enter displayed counts; pending membership stays
visible. No complete snapshot means a labelled pending initial forecast rather
than fitting a peer model on a partially checked cohort.

Melo (`7475223633`) is included again in all newly computed forecasts, peer
priors and corrections. All five policy IDs carry an `all-members-v2` suffix
to separate the changed population. Existing frozen files are preserved and
scored using the population actually saved in each file. Earlier development
scores below used the earlier population and are not accuracy claims for this
updated cohort. Future-day performance remains unvalidated.

PepFactory is hidden from page-capacity lists; historical sales/access records
are retained. Missing page-access votes show pending/Unknown capacity rather
than zero. Active tomorrow remains separate from Ready now. Assignment quotas,
payroll and historical receipts are unchanged.

## Formula

1. The **reference model** uses the last 14 completed PHT dates. Sum DD
   on declared Active dates and divide by the number of those dates, including
   Active dates with zero recorded DD. Unknown and Inactive dates are excluded.
2. With fewer than three Active dates, the reference uses **0.20 DD/day as a
   labelled trial starting rate**. The **recent alternative** uses seven completed
   dates and `(Active-date DD + 3 × 0.20) / (Active dates + 3)`. This blends personal
   evidence with a weak prior from the first observation. Under three Active dates
   remains provisional. Recorded zero and expected future output stay separate.
3. Replay each component's raw formula on its preceding seven completed dates.
   Each replay uses only records before its own target date. Only matched
   Active observations enter either the predicted or recorded total.
4. With at least three scored dates and 20 Active person-days, multiply raw
   forecasts by `min(1, recorded_DD / raw_predicted_DD)` from those earlier
   observations. Without sufficient history, leave the multiplier at 1 and
   label that component uncalibrated. Each component has its own correction;
   the final selected forecasts do not share one universal multiplier.
5. For each person, compare both components' predictions on that person's earlier
   Active dates within the preceding 14 dates. Each historical prediction and its
   correction must train before its own target date. Require at least three matched
   personal predictions, strictly lower absolute error and no higher squared error
   to select the recent alternative. Otherwise retain the reference. Other people's
   error improvements cannot justify switching this member. A truncated cache
   cannot provide fabricated comparison history.
6. Sum fractional forecasts for the confirmed Active roster before rounding a
   total. An optional `0.80 × forecast` planning buffer retains decimals and is
   not a prediction interval or guaranteed safe target.

Best-three output remains a **stretch reference**. It is not the normal forecast.
Melo (`7475223633`) is included in new forecasts and their calibration. Actual sales,
attendance, commissions, payroll, and assignment limits are unchanged. An observed
zero average is retained as an observation. A smoothed forecast can remain positive
after recorded zeros. Neither value establishes whether the member can do the work.

Current membership is checked before the organized report computes the reference.
The live raw capacity cache retains 192 completed dates for selection and calibration. Shared people
count once in the VEO business cohort. They may appear on multiple page reports,
so page/team totals overlap and must not be added. Access and availability do not
allocate a person's output exclusively to a page. Missing availability/access or
membership verification remains visible. Old top-three caches cannot supply a
personal forecast. A change in policy starts a new nightly comparison baseline.

## Read-only local use

Save current data using the configured cloud wrapper. These commands do not queue
or deliver messages:

```powershell
.\remote_windows.ps1 status
.\remote_windows.ps1 capacity --days 90 > .audit\veo-history.json
.\remote_windows.ps1 report-members > .audit\veo-members.json
py veo_forecast_cli.py --history .audit\veo-history.json --membership .audit\veo-members.json --work-date 2026-10-24 --backtest-start 2026-10-03 --output .audit\veo-forecast-2026-10-24.json
```

Use the intended future work date and a new output filename for each run.
`--history-through YYYY-MM-DD` can impose an earlier history cutoff. History must
end before the forecast date. UTF-8 and Windows PowerShell UTF-16 JSON both work.

CLI backtests now default to the **same history lag as the saved forecast**.
For example, forecasting October 24 from completed history through October 22
uses a two-calendar-day lag, so each historical replay also cuts history off two
dates before its target. `--backtest-horizon-days 1` explicitly requests a
one-day comparison. The direct Python `backtest()` function retains a one-day
default and accepts `history_lag_days=2`. Saved rows include their history cutoff.
An explicit override may differ from the saved forecast's lag; both are printed.
Earlier v3/v5 backtests in this document used one-day history, while their
October 24 frozen forecasts used two-day history. Those horizons must be compared
separately; the earlier files remain unchanged.

Omitting `--group` forecasts each person's **combined VEO output**. To forecast
team-only DD, use `--group=-1003647732254` for Veo or `--group=-1004423057797`
for Veo Ollie. This reads that team's own DD and attendance and checks current
membership in that team. Saved forecasts retain their scope and are later scored
against that same scope. A missing team source never falls back to combined DD.
Team-only and business-wide results are not interchangeable. Page-access boards
show the available people's combined personal reference; exclusive page sales
remain unestimated without appropriate page evidence/allocation.

Without `--roster`, the file contains **conditional per-person forecasts** and
leaves Active count and team total unknown. To calculate a team total, pass
`--roster path.json`, a JSON list such as
`[{"user_id": 123, "user_name": "Member"}]` containing confirmed Active voters.
An explicitly empty roster produces zero Active members and zero expected output.
Members with no personal records receive a labelled provisional default. A missing
poll never becomes an empty roster. Historical membership files are cached present
checks, not evidence of past or future membership.

The command exclusively creates a new file, retaining creation time, history and
model source hashes, policy, history cutoff, raw rates and correction evidence.
Backtests contain a person-level error table, RMSE and the individual daily rows.
It never overwrites an existing forecast. Files are local audit records, not
externally timestamped or immutable certificates. `.audit/` is ignored by Git and
excluded from deployment.

## Check future performance

After a forecast date has completed, fetch new history and use
`--score-forecast path-to-earlier-forecast.json` alongside the next forecast's
arguments. Scoring reads the earlier file's saved predictions; it does not rerun
that model with later data. It reports matched Active results, Unknown attendance,
Inactive members, missing forecasts and Active members outside the frozen cohort.
An unfinished work date stays pending. A single future day is not enough to
validate long-run accuracy.

`--backtest-start` is a chronological **development replay**, not unseen validation.
Report daily absolute error and bias separately; neither a close grand total nor
error reduction versus an inflated baseline is an accuracy percentage. Final edited
posts/votes, cached present membership, missing attendance, and correlated workers
limit retrospective evidence. Do not raise defaults automatically or infer that
forecast changes measure changes in staff ability.

The v3 personal-choice rule was developed after inspecting all available dates.
It is not an independently validated winner. Report both the October 3–15 and
October 16–22 development blocks, the number of people with better/worse/unchanged
absolute error, and person-level as well as daily-total metrics. A close grand
total is not an accuracy percentage. Some individuals and teams still worsen.

## Explicit experimental personal model

`veo_forecast_hurdle.py` adds a separate occurrence/size trial:

`expected DD = model probability of positive DD × expected DD on a positive date`.

Personal observations span 28 completed dates. Occurrence uses a 7-day half-life;
positive-date count uses a 14-day half-life. Both shrink sparse personal evidence
toward other members' prior seven dates, leaving the person's own data out of the
peer starting values. The trial anchors are `0.16 × 1.25 = 0.20 DD/day`.
Occurrence uses seven pseudo-Active dates and positive count three pseudo-positive
dates. Peer anchors use ten Active and five positive pseudo-dates. These strengths
are development choices, not verified facts. New members get a labelled peer
blend that can differ from 0.20; it is not a raised assignment quota or a validated
personal capacity. Under three personal Active dates remains provisional.

The seven-date correction replays the complete personal/peer model strictly
before each historical target. It requires at least three scored dates and 20
Active person-days, caps its ratio at 1, and needs 35 cached dates. Active zeros
count and Unknown/Inactive attendance is excluded. Melo enters new peer
priors and corrections. Probabilities are model estimates; probability calibration
and future performance are unvalidated.

Select this model explicitly for a **private offline trial**:

```powershell
py veo_forecast_cli.py --history .audit\veo-history.json --membership .audit\veo-members.json --model hurdle --work-date 2026-10-24 --backtest-start 2026-10-03 --output .audit\veo-hurdle-forecast-2026-10-24.json
```

Omitting `--model`, or passing `--model current`, keeps the v3 personal-choice
policy. This option does not change organized reports, schedules or client flags.
Saved files include the chosen model and dependency hashes. Both models support
team-only scope, conditional forecasts without a roster, and later scoring of
frozen numbers. Existing forecast files cannot be overwritten.

In the October 3–22 development replay (518 matched person-days), the selected
hurdle recipe lowers person-day RMSE from 0.6278 to 0.6067 and daily-total MAE
from 3.1052 to 2.9383. It **raises person-day MAE from 0.4526 to 0.4837** and
overpredicts the matched total, 196.20 versus 181 DD. In October 16–22, RMSE
improves slightly but individual and daily-total absolute errors worsen. The
recipe was chosen by earlier-block RMSE among the tested standalone advanced
recipes; all dates had already been inspected during development. This is not an
unseen holdout, and the higher absolute errors do not justify replacing v3.

`veo_forecast_advanced.py` adds a **personal guard** around that component. For
each person, replay current v3 and the hurdle model on their own prior 28
completed dates. Switch only with at least three matched personal predictions,
strictly lower absolute error and no higher squared error. Otherwise retain v3.
The complete comparison requires 63 earlier cached dates. Its per-replay cache
is scoped to an immutable cohort; it is not shared globally or between runs.

Use `--model personal` for this guarded local trial. It lowers personal MAE and
RMSE in both inspected October blocks. October 3–22 MAE changes from 0.4526 to
0.4505, RMSE from 0.6278 to 0.6181 and daily-total MAE from 3.1052 to 2.9044.
October 16–22 predicts 31.74 versus 30 recorded, compared with v3's 32.80.
The improvement is small for individual error and not universal across teams.
These variants were developed after inspecting the same dates; new future
outcomes must test whether the gains persist. V3 remains the organized-report
default and `--model current` remains the CLI default. Neither experimental
choice publishes or changes client assignments.

Backtest metrics also show half-up rounded count matches, with zero and positive
days separated. An expected count is fractional; rounding is a diagnostic rather
than the forecast's purpose. Always show a zero-forecast comparator: zero-heavy
data can make an unhelpful all-zero prediction appear to "get it right" often.

## Day-specific personal trial

`veo_forecast_day.py`, selected explicitly with `--model day`, adds a regularized
next-date correction to v5. It learns from at least 100 matched Active person-days
in 14 earlier label dates, using a 14-day training half-life and ridge penalty 20.
Inputs are personal recent/longer rates, positive frequency, last Active outcome,
time since Active/positive observations, other members' prior rate, and the known
target weekday. No target sales or target attendance enters a feature. Training
examples use the same history lag as the requested forecast. A calendar weekday
feature uses the actual work date, including when the history lag exceeds one.

A seven-date correction replays raw predictions at that same horizon using only
outcomes available by the history cutoff. A personal guard then requires five
earlier fitted comparisons, including two positive-output dates, lower overall
absolute error, no higher squared error, and no higher positive-date absolute
error. Otherwise it keeps v5. These checks prevent selecting a change based only
on better zero-date forecasts. New/sparse members keep v5's provisional labels.
Unknown/Inactive attendance is excluded; Melo is included in new computations.

Use a **365-day saved source** for these nested comparisons; 128 cached dates
support the usual one/two-day horizons. An incomplete training cache causes an
explicit v5 fallback, rather than claiming validated comparisons. The learned
count correction supplies an expected DD count, not a calibrated event probability
or verified production capacity. The October 24 update uses this as the live
fallback beneath v7; assignment quotas remain unchanged.

```powershell
.\remote_windows.ps1 capacity --days 365 > .audit\veo-day-history.json
py veo_forecast_cli.py --history .audit\veo-day-history.json --membership .audit\veo-members.json --model day --work-date 2026-10-24 --backtest-start 2026-10-03 --output .audit\veo-day-forecast-2026-10-24.json
```

On the same October 3–22 development observations, one-day personal MAE falls
from v5's 0.4505 to 0.4469, RMSE from 0.6181 to 0.6138 and daily-total MAE from
2.9044 to 2.7777. At a two-day lag, personal MAE falls from 0.4495 to 0.4459,
RMSE from 0.6163 to 0.6133 and daily-total MAE from 2.9171 to 2.8482. Gains are
small, individual/team outcomes remain mixed, and rounded positive-count matches
remain weak. The recipe was chosen by earlier-block one-day RMSE among guarded
development variants; all dates were already inspected. Future evidence is needed.

## Delivered-offer personal trial

`veo_forecast_offers.py`, selected explicitly with `--model offers`, tests a
new input: **dated delivery of client offers** on the person's prior 1/3/7
completed dates. Counts are transformed as `log1p(count) / 3` and accompanied
by a source-coverage flag. Final acceptance, missed and withdrawn outcome labels
are not used as features or filters, since they can contain later information.
An offer is exposure to a possible client; it does not prove available demand,
an accepted assignment, production work or a subsequent sale.

The candidate adds these four features to the day model's ten causal inputs,
with the same 14-date training window, ridge penalty 20 and 100-observation gate.
It fits a correction to the v5 base and is compared with **v6 as the fallback**.
Keep the prior 28-date personal checks (five earlier fitted comparisons,
including two positive dates, lower absolute error, no higher squared error and
no higher positive-date absolute error). Additionally, the previous seven dates
must show no worse absolute and squared error for both that person (at least
two comparisons) and the matched cohort (20 person-days over three dates).
Those recent checks compare the first guarded proposal with v6; every compared
forecast is generated before its own historical target.

Supply a saved `workers --days 31` response for **every requested team** using
repeated `--offers path.json`. A team-only request needs that team's source;
combined-person scope needs all six sources. Missing source groups fail
explicitly; short caches, incomplete/stale source coverage or failed comparison
checks retain v6. A full 365-date capacity source is recommended; the trial's
192-date cache recommendation supports ordinary one/two-day lags. Each worker
source's end date includes its incomplete today, so only earlier whole dates
are treated as covered. Duplicate deliveries are deduplicated by group,
assignment, message, timestamp and recipient; Melo is included in new runs. Changing final
outcome labels has no effect on accepted delivery facts or predictions.

```powershell
.\remote_windows.ps1 workers --group=-1004423057797 --days 31 > .audit\ollie-offers.json
py veo_forecast_cli.py --history .audit\veo-day-history.json --membership .audit\veo-members.json --group=-1004423057797 --model offers --offers .audit\ollie-offers.json --work-date 2026-10-24 --backtest-start 2026-10-03 --output .audit\ollie-offer-trial-2026-10-24.json
```

The explicit trial retains source hashes, source scope/coverage, history cutoffs,
training coefficients and earlier personal/recent-cohort evidence. It does not
change organized-report defaults, client flags or assignment quotas. Missing
availability still leaves Active count and forecast total unknown.

On the same October 3–22 development observations at a two-day history lag,
personal MAE decreases from v6's 0.4459 to 0.4437 and RMSE from 0.6133 to 0.6121;
daily-total MAE changes from 2.8482 to 2.8409. Rounded matches stay at 68.9%, with
44/148 positive-date matches. Of 518 person-dates, 29 are closer, 15 worse and
474 unchanged. Some recent one-day daily-total results worsen. These small,
mixed gains and repeated selection on the same history do not validate future
accuracy. The conservative recipe retains the prior penalty and adds both
recent checks; it is not claimed to be an independently selected winner.

For the saved October 24 forecast, all 66 members retain their v6 rates because
the offer model does not pass the current checks. Do not force a new estimate
merely to make a formula change visible. Offer delivery history comes from the
current archive, not immutable historical as-of snapshots, so later edits or
source gaps remain possible.

## Further study and prospective scoring

Twelve additional regularized blend variants and six causal model-choice
variants were compared with v7 on the same inspected October development data.
None consistently improved personal MAE, personal RMSE and daily-total MAE at
both forecast delays and in both development blocks. V7 was retained. Small
one-day gains often worsened two-day squared errors; repeated searching on these
dates is not new validation evidence.

`veo_forecast_evaluate.py` scores a manifest of **already frozen forecasts**
without rerunning any model. The manifest records each file's SHA-256. A changed
file, duplicate policy/date/scope/horizon, invalid counts, inconsistent scope or
history-lag metadata fails explicitly. Historical dates must have been completed
by the forecast's saved creation time. A forecast created after its target PHT
date began is retrospective-only; absent/unzoned creation times are unverified.
Local timestamps and hashes are not externally immutable certificates.

The evaluator keeps missing attendance, unmatched people and group scope visible.
It preserves each frozen file's cohort, compares shared person/date keys within the same scope
and history lag, and reports positive-date errors and an all-zero comparator
alongside overall errors. Rounded hits alone cannot justify a stronger model.
Its descriptive comparison rules require 14 scored dates, 100 Active person-days
and 20 positive person-days, with no worse personal MAE/RMSE, daily-total MAE and
positive-date MAE, plus a strict gain in at least one overall error. Those are
planning choices, not statistical significance tests. It never promotes a model,
changes defaults or schedules delivery.

The local manifest `.audit/veo-future-evaluation-manifest-20261023.json` registers
five October 24 forecasts (v3–v7), all saved before their target date. With current
history ending October 22, all five remain **pending**. Once new dates complete:

```powershell
.\remote_windows.ps1 capacity --days 365 > .audit\veo-future-actuals.json
py veo_forecast_evaluate.py --history .audit\veo-future-actuals.json --manifest .audit\veo-future-evaluation-manifest-20261023.json --output .audit\veo-new-evaluation.json --markdown .audit\veo-new-evaluation.md
```

Choose new output filenames each time; evaluations cannot overwrite earlier
results. This is an on-demand offline tool, not a scheduled task. One future date
cannot meet the comparison minimum. Preserve subsequent forecasts before their
work dates if extending the manifest to additional dates.

The Telegram `messages` response inspected in this study lacks sender IDs, and
the Veo WORKING query reached its 500-row limit. Those messages cannot reliably
add identity-verified acceptance features or complete historical acceptance
rates. Neither message names nor final worker outcome labels were substituted
for verified causal acceptance events.

## Validation and deployment

Run `py validate_project.py`. The required deployment regression suites include
`tests/test_veo_forecast.py`, `tests/test_veo_person_choice.py`, `tests/test_veo_hurdle.py`,
`tests/test_veo_advanced.py`, `tests/test_veo_day.py`, `tests/test_veo_offers.py`
and `tests/test_veo_forecast_evaluate.py`;
preserve the Vercel build test gate. This change does
not alter schedules or invoke the dispatcher. Local previews and future scoring
cannot send to Telegram. Deploying the report integration would affect subsequent
scheduled report text; a request for a private analysis is not permission to publish it.
