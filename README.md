# Telegram group insights

VEO automation is enabled across all six VEO teams. With
`VEO_NEW_CLIENTS_ENABLED=false`, availability and reports stay on while
new-client offers, Ready controls, acceptance and client lists stay off for VEO.
`VEO_PAGE_ACCESS_POLLS_ENABLED=true` independently enables a public multi-select
page-access poll in each active VEO team's New Clients topic, with a No access
option. Answers are tracked separately from attendance; the poll does not enable
assignments. Suno assignments use their own readiness and page
access. Existing ownership is preserved; archived offer buttons cannot accept
new work while the VEO feature is off. VEO availability counts all members.
Makata's client list shows when it is waiting for Ready members; tapping Ready
immediately queues an assignment check. All sends use the normal scheduler.

Suno and Makata use a provisional historical peer default for a member with fewer
than three personal output dates. The shared Suno default is the median of current members' recorded
receipts on declared Active dates divided by each member's declared Active dates
over the last 14 completed dates. Recorded zeros are included; Unknown/Inactive
attendance and identities without a usable Active-day denominator are excluded.
Each Telegram ID counts once. A small cohort displays its actual size without a
fixed fallback, percentage buffer or minimum-one rounding. No usable peer history
means Unknown. The provisional plan changes to a personal estimate after three
personal output dates; its safe target stays Unknown. Makata can use shared Suno
peer history without treating another team's votes as Makata attendance.

VEO retains its existing median Active-day planning default after five peers
with three Active and three output dates, with a reviewed 0.50 fallback.
Its provisional blending and 80% safe-target policy are unchanged. Existing
personal top-three estimates retain their own planning policy. These describe
recorded output and can be limited by demand; they do not verify production limits.

The daily sales/salary summary and staffing boards show tomorrow's Active total,
capacity totals and averages separately for VEO and Suno. Both show personal,
provisional and Unknown counts. Totals and averages include labelled provisional
plans and exclude Unknown members. Shared members count once within each
business; mixed business units are never added. Suno personal history combines
receipt counts by date across Suno teams before calculating a member estimate.
The summary silently refreshes the same post every five minutes as votes change.
Unverified membership and missing polls are shown as partial coverage.
Makata's authorized private daily report uses the same shared Suno default.
The correction edits its existing daily report where
available and retires any old extra parts, rather than duplicating the report.
Client lists contain every row without Next/Previous controls; long lists use
consecutive messages, updated in place through the scheduled outbox.

Suno capacity now uses readable receipt posts, including non-bot clients.
With organized reports enabled, the last 14 completed dates supply the top
three positive daily outputs. Their mean is the estimated planning capacity;
the safe target is floor(estimate × 80%). Receipt-day average and daily peak
are shown separately. Unknown attendance is explicit and does not become a
zero or Inactive day. Fewer than three eligible output dates leaves the plan
pending. These are payment-post estimates, not unique
clients, completed songs or verified production limits. Song confirmations do
not enter this formula. Makata reports go to DAILY REPORTS and @mmadss25.

With `SUNO_MAKATA_ENABLED=true`, Suno Makata `-1004498344479` receives the
Makata Studio/Studios source pages; Trabawho excludes those pages. Supplied
topics remain General 1, Announcements 4, Receipts 8 and NEW CLIENTS 13. The
scheduled outbox creates ACTIVE TOMORROW and SONGS and saves their IDs before
finalization; retries reuse the saved IDs. Availability waits for a real topic,
and Makata never gets hourly polls. Client lists/offers respect reservations in
both Suno groups so an existing accepted client cannot be offered again after
the page split. Old accepted ownership is retained.

Makata has separate receipt earnings, song confirmations and capacity history.
Daily reports go to DAILY REPORTS and the authorized recipient `@mmadss25`
after that account messages the bot privately. A stable Telegram user ID is bound
once from the authenticated webhook; a reused username cannot silently rebind
the recipient. Only Makata-tagged report actions may use that private delivery
permission. No arbitrary private chat gets archive approval or send permission.
`./remote_windows.ps1 makata-status` shows topic IDs, the connection status and
the bot's subscription link. Missing private registration leaves the public
report working and catches up that same saved report once connected.
`./remote_windows.ps1 makata-connect` recovers an earlier ignored private hello:
it checks a real Makata sender's current Telegram username and private chat,
then queues delivery through the normal scheduler. It cannot target another user.

VEO salary reports show both 35% and 40% amounts per member and team, alongside
the quota-based applied amount or a pending label. The combined member list
and sales/salary summary show both scenarios too; Suno written earnings stay
fixed. Team scenarios sum each member's rounded earnings.

VEO organized reports use the same 14-day top-three planning policy, with
Done Deals as the output unit. Output-day average, daily peak, top-three mean
and whole-number 80% safe target remain distinct. Explicit Inactive/output
conflicts are flagged and excluded from the plan. A positive output date can
support an observed estimate without inventing confirmed attendance. Known
Active dates with zero output remain in the separate Active-day average.
Availability votes do not verify full shifts or production maximums. The
earlier 30-day reviewed policy remains available only when organized reports
are disabled; its results must not be mixed into the current staffing board.

DAILY REPORTS also gets a dated tomorrow-capacity board. It uses actual Active
voter IDs, shows rated-history coverage, average per rated member, summed
unrounded output, and the conservative total. Missing histories and missing
polls stay unknown/pending. Overall totals deduplicate people active in multiple
VEO teams; team totals can overlap and must not be added. A scheduled five-minute
check silently edits the same post when votes/history change; old boards stop
repeating after their date expires. This does not enable paused team posting,
alter assignment limits, or change configured advertising/client-quota formulas.
Use `./remote_windows.ps1 tomorrow-capacity` for the read-only preview.

Suno / Trabawho now uses the same client policy as VEO: Yes/No tomorrow
availability from October 22, expiring 60-minute `/ready`, fresh `/ready` after
acceptance, 15-minute initial offers, then earliest eligible Ready member with
10 minutes on reassignment. Missing an offer pauses Ready; `/busy` stops new
offers. Handovers are disabled, accepted briefs stay visible, and NEW CLIENTS
lists only that Philippine day's activity while preserving older ownership.
The original 24-hour song deadline and owner-only SONG SENT/DONE confirmation
remain in place. Team notices are short.

Suno page access comes from Suno's own `pages` table, qualified with its project
identity. Multiple public page polls each show at most nine pages plus a none
choice; answers are combined across poll parts and withdrawals clear only that
part. No answer grants no pages. `/pages` links every part. Source metadata is
cached for 30 seconds; changed page versions invalidate earlier permissions.

Suno gets its own daily capacity report in DAILY REPORTS after 00:10 PHT.
`./remote_windows.ps1 capacity --group -1002894511895 --all-history` is read-only.
It uses readable receipt posts and declared availability, without querying
bot assignment or delivery confirmations. Non-bot clients' receipts count too.
The receipt-based planning formula and minimum evidence are described above;
missing attendance is labeled unverified, while fewer than three eligible
output dates leaves the plan unknown. Receipt-day averages and
daily peaks remain observed output, not verified maximum capacity. Deposits,
tips, extras and repeated payments do not establish unique paid orders or
completed songs. Image-only/unreadable receipts and unresolved ancestry are
excluded. Financial receipt and salary
calculations stay in their existing separate reports. Durable snapshots and
outbox part keys make retry delivery idempotent; source errors do not block due
messages. Capacity reports do not impose automatic assignment limits.

DAILY REPORTS now receives a member output/capacity update after **00:10 PHT**
every day, covering the latest completed Philippine date. Set
`MEMBER_CAPACITY_REPORTS_ENABLED=true` and allow the manager-report destination
in `AUTOMATION_CHAT_IDS`. Sources use approved VEO archives even while team
posting remains paused. The first scheduled run publishes the latest completed
day immediately; recovery catches up the latest report without flooding older
days. A cancelled snapshot fixes the report's parts before enqueue, and durable
part keys prevent duplicate retries. Source failures retry and do not block due
deliveries. The public health endpoint remains read-only.

The current report lists members seen within the last 14 completed dates,
using output-day average, observed peak, the mean of their top three eligible
output dates and the requested floor(mean × 80%) safe target. A member may
appear in multiple team topics; combined VEO identity uses Telegram IDs.
Original all-history descriptive results remain available through the read-only
API, while the planning window stays 14 dates. These are estimates, not
statistical guarantees. Below one per day does not mean zero ability. Missing
attendance is labeled unverified; output dates never become confirmed full days.
These estimates count paid-sale posts, not verified completed projects, full
working shifts or maximum production capacity. They do not alter assignment
limits or round robin. Members without readable Done Deals appear in one
separate list. Linked names open source messages; full source details stay in
the saved snapshot.
Format upgrades correct the previous format's posts for the same report date
through the scheduled outbox, reusing message IDs and deduplicating retries.
It backreads known DONE DEALS topics plus qualifying paid export posts and
direct export replies to DONE DEALS. A live copy supersedes the same exported
message. Inactive and Unknown dates stay separate; ordinary votes never prove
full shifts or maximum production capacity. The full archive view is available
read-only with `./remote_windows.ps1 capacity --all-history`.

VEO page access is now declared in a saved, public multiple-choice poll in each
team's NEW CLIENTS topic (`/pages` opens it). Members must select a connected
page before receiving its clients. No response, a withdrawn response, or
**No access** grants no pages. Newer answers replace older ones, and a changed
page configuration requires a new poll. Assignment, first delivery, and
acceptance all check access. Existing accepted ownership is preserved.

VEO's NEW CLIENTS board is now dated by Philippine workday and grouped into
available clients, pending offers, and members' accepted clients. Acceptance
refreshes the same daily board; a new workday gets a new board. Previous days'
clients remain owned and cannot re-enter the available pool. Missing any
delivered offer, including a 10-minute reassignment, stops eligibility until a
new `/ready` command.

`./remote_windows.ps1 capacity --days 14 [--group CHAT_ID]` computes read-only
member output for completed Philippine days. It shows declared Active days
(including zero recorded output), unknown attendance days, recorded Done Deals
per output day, and the observed daily peak. Known-Active averages use only
Done Deals from the matching known-Active dates. It does not infer verified
maximum capacity, finished creative work, or full working shifts from sales or
availability polls. The top-three full-day measure stays unavailable without
three verified full days. Reports include message IDs, UTC dates and archive
sources and remain available while VEO automation is paused. The combined view
merges staff by Telegram ID, sums their output on each date across teams, and
counts each Active date once; name-only records stay separate across teams.

VEO client rules, updated October 22, 2026 (all six teams): tomorrow's
availability uses one public **Yes / No** poll, replacing hourly selections.
`/ready` expires 60 minutes after the Telegram command, and accepting a client
consumes that readiness. Send a new `/ready` after acceptance to request another
client; `/busy` stops offers. Fresh clients follow round robin. Initial offers
allow 15 minutes from delivery; missed offers go directly to the earliest
eligible `/ready` member with a fresh 10-minute acceptance window. No open TAKE
race or accepted-client handover is offered for VEO. Accepted offers retain the
client brief, page and CRM ID, with a Client details link on the confirmation.
Instructions and status notices are kept short. Previously delivered offers
retain their printed deadlines. Suno now follows the same policy described
above. These VEO rules also apply while the teams are paused; they
do not change `AUTOMATION_CHAT_IDS`.

The VEO rules above replace the earlier VEO readiness and hourly instructions
below; those older sections describe the previous behavior.

`AUTOMATION_CHAT_IDS` optionally limits outgoing Telegram automation to an
approved subset. Production uses Trabawho `-1002894511895` and DAILY REPORTS
`-1004362432984`. All six Veo teams and other groups are paused for bot posts, offers, polls,
reminders and button/command actions. `ALLOWED_CHAT_IDS` remains the archive
approval list, so messages and poll answers can still be collected and reviewed.
Existing assignments and queued actions are preserved. Paused actions are not
claimed or counted as overdue active deliveries; real failures in active groups
still fail health. Authenticated `status` reports active and paused IDs. Removing
the setting resumes the approved groups; an explicitly empty value pauses all.

Ready commands accept `/ready`, `/ready@Jaucianbot`, and `/ready @Jaucianbot`;
Busy accepts the same formats. A command in another topic receives a link to
NEW CLIENTS without silently changing availability. The member menu includes
**Why no client?**, also available as `/queue`, showing the member's current
waiting reason, offers/acceptances today, and eligible unassigned supply.
Explicit continuous readiness determines eligibility; old availability-poll
hours do not incorrectly label a Ready member as outside working hours.
If an assignment write fails, its member reservation is released. The scheduler
also recovers orphaned reservations after five minutes, checking for a committed
assignment and using a revision guard so BUSY and newer changes win. Recovery
never bypasses missed-offer pauses or releases an existing client assignment.
Round-robin priority still favors eligible members with fewer clients; zero
available briefs means the team must wait for new eligible clients.

Veo and Trabawho also accept today's current-hour poll selections as client
availability. These selections last only for the selected hours; `/ready` still
starts continuous mode. An explicit `/busy` excludes the member until `/ready`.
A missed offer requires `/ready` or a newer selection in the poll part covering
the current hour. Editing another part, a prior-day vote, or the next hour alone
cannot resume a missed offer. A withdrawn selection or an hour ending cancels
an hourly offer before delivery; delivered offers retain their acceptance window.
Reservations remain atomic, and status reads never create readiness records.
Reservation releases also require the exact acquired revision: a delayed retry
cannot release a newer reservation even if the same client key is reused.
Authenticated new-client status reports a team `queue_state` and
`member_waiting_reasons`, distinguishing waiting clients, member availability,
queued offers and unanswered offers. Legitimate Busy/missed-offer pauses are
not treated as scheduler outages.

Concerns posted in the six Veo teams and Trabawho are collected in a separate
manager report in DAILY REPORTS after 00:05 PHT for the previous Philippine day.
The first scheduled run publishes a current-day setup preview. Each report
groups posts by team, shows the member and an excerpt, and links to the original
message with its UTC timestamp and archive source. Attachments need manual
review; replies remain visible and the bot does not infer whether an issue is
resolved. Empty placeholders and the standard topic-opening instruction are
omitted. Long reports split into safe Telegram messages, with durable keys to
prevent duplicate delivery. Archive failures retry rather than report no issues.
Coverage begins October 15, 2026; after downtime the latest completed day is
recovered. Earlier dates can be reviewed with
`./remote_windows.ps1 concerns --date YYYY-MM-DD` (read-only, no delivery).
Lyn topic 91 and Ollie 2 topic 43 are monitored as candidates awaiting title
verification. Future named topic creation/rename events identify Concerns topics
in approved groups automatically, including groups outside the seven teams.
Other topics and historical messages absent from the archive are not included.

Member-facing instructions: [Veo and Suno handbook (PDF)](docs/Veo-and-Suno-Member-Handbook.pdf)
and [editable handbook source](docs/member-handbook.md). The handbook includes
client ownership, handovers, availability, Veo freebies and deals, Suno delivery
and receipts, and links to each team's topics. Rebuild the PDF with
`docs/build_member_handbook.py` in a separate Python environment with `reportlab`.

This bot archives new messages from Telegram groups you approve. Telegram Desktop HTML exports can also be imported for earlier messages. Ask the assistant in this workspace for summaries, decisions, issues, trends, or a search. The local archive is `telegram_insights.sqlite3`; the cloud archive uses Supabase. Attachments are recorded by type but are not downloaded or transcribed.

## Deploy on Vercel

The archive can also use its existing PostgreSQL connection by setting
`ARCHIVE_TRANSPORT=postgres` and the server-only `SUPABASE_DB_URL`. Use the
IPv4-compatible Supabase session pooler on port 5432 for Vercel; prepared
statements are disabled for that connection. This uses the same archive tables
and functions without a migration or billing change. The CRM retains its separate
Data API connection. Database errors are sanitized before logging.

Routine assignment-history reads fetch ownership, contact identities, dates,
and acknowledgment metadata without downloading message bodies or collected
details. Updates reload just the affected assignment and preserve its full payload
under a revision check. Periodic new-client scans run at most once per minute across instances; freebie
scans retain a separate five-minute slot. Team CRM pages are read concurrently; Active votes and confirmations still trigger immediate
assignment checks. Due deliveries continue on every dispatcher call. Completed
daily reports are checkpointed to avoid rebuilding them throughout midnight.
Checkpoints are cancelled scheduler records and cannot be sent to Telegram.

Deployments run `validate_project.py` as a required build step. Missing or failing
regression tests stop the build. The tests run with production credentials removed
from their process environment.
The gate explicitly requires the client/poll queue, recovery, scheduler, and
validation regression suites; missing or empty critical files stop deployment
even when unrelated tests are still present.

The database has an independent two-minute watchdog that restores the named
dispatcher if it is missing, disabled, rescheduled, or given an incorrect command.
It also recreates its cancelled checkpoint when needed and prunes these jobs'
completed execution logs after seven days when its database role permits deletion.
Each dispatcher checks its scheduler guard
at most once per five minutes; the hourly Vercel fallback can restore both named
jobs. Guard failures are reported while due deliveries continue.

`/api/health` now returns HTTP 503 if dispatcher requests or successful responses
are older than three minutes, if the watchdog has failed or has not run within
five minutes, or if there are failed deliveries, stale claims, or deliveries more
than five minutes overdue. Its public response contains no group details. Read
`./remote_windows.ps1 scheduler-status` for authenticated diagnostic reasons.
After a scheduler change, run `./remote_windows.ps1 configure-scheduler --verify-recovery`.
It checks restoration of a disabled dispatcher's cadence, command, and active state
inside a rolled-back savepoint; the scheduler never observes a changed job. Deliberate maintenance pauses must
disable both named database jobs and the Vercel fallback to stay paused.

Vercel receives Telegram updates at `/api/webhook` and saves them to Supabase through its server-side Data API. The read-only `/api/status` and `/api/messages` endpoints require an API key. The local `.env.local`, SQLite archive, and exported chat files are excluded from Git. `.env.example` lists the variable names without values.

1. Open this Supabase project's SQL Editor and run [`supabase/schema.sql`](supabase/schema.sql). It creates the archive tables and functions, enables row-level security, and grants access only to the server-side service role. Run `py verify_supabase.py` afterward; it should report that the archive is ready.
2. Import this GitHub repository into Vercel. Add these **Production** environment variables from the local `.env.local`, then deploy or redeploy:

   | Name | Value |
   | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | Bot token used by the outbound scheduler. |
   | `SUPABASE_URL` | This project's HTTPS Supabase URL. |
   | `SUPABASE_SERVICE_ROLE_KEY` | Service role key, for server-side use only. |
   | `CRM_SUPABASE_URL` | Separate CRM Supabase project URL for freebie and new-client assignments. |
   | `CRM_SUPABASE_SERVICE_ROLE_KEY` | CRM service role key, server-side only. Never use a `NEXT_PUBLIC_` variable for it. |
   | `ALLOWED_CHAT_IDS` | Comma-separated numeric IDs of the approved groups. |
   | `TELEGRAM_WEBHOOK_SECRET` | Random secret generated in `.env.local`. |
   | `INSIGHTS_API_KEY` | Separate random secret generated in `.env.local`. |

   The `SUPABASE_ANON_KEY` is not needed by this server. Never put the service role key or bot token in Git or a browser-facing variable.
3. Run `./migrate_to_supabase.ps1` in this workspace. It copies the local archive, including imported history, to Supabase. Rerunning skips duplicates.
4. After the Production deployment and database are ready, stop the Windows poller: `Stop-ScheduledTask -TaskName TelegramGroupInsights` and `Disable-ScheduledTask -TaskName TelegramGroupInsights`. Then run `./telegram_windows.ps1 set-webhook https://YOUR-PRODUCTION-DOMAIN/api/webhook`. It reads the same webhook secret from `.env.local`. Inspect delivery with `./telegram_windows.ps1 webhook-info`.
5. Run `./setup_remote_windows.ps1 https://YOUR-PRODUCTION-DOMAIN`. The assistant can then query the cloud archive with `./remote_windows.ps1 status` and `./remote_windows.ps1 messages --days 7`.
6. In the cron-job.org Console, create a GET job for `https://YOUR-PRODUCTION-DOMAIN/api/health` every 15 minutes. The endpoint checks Vercel and Supabase and returns only `{"ok": true}`. Dashboard setup does not need an API key or request headers. The optional `configure_cronjob.py` script uses a cron-job.org API key only when creating the job through its REST API.
7. Run `./remote_windows.ps1 configure-scheduler` to install or repair the archive database's named one-minute dispatcher using Supabase `pg_cron` and `pg_net`. It uses the server's existing `SUPABASE_DB_URL`; database credentials stay inside the service. The authenticated maintenance endpoint installs the extensions if needed, creates a cancelled checkpoint that cannot enter Telegram delivery, and configures one job for `https://jaucianassitant.vercel.app/api/cron/dispatch`. The first request runs on a scheduled tick. Repeating the command updates the same job. Read `./remote_windows.ps1 scheduler-status` for the job's schedule, active state, and recent dispatcher HTTP result. The Vercel fallback also runs once during every hour, including overnight, plus the existing closeout. Hobby timing can drift within the scheduled hour; see [Vercel's scheduling limits](https://vercel.com/docs/cron-jobs/usage-and-pricing). If database scheduling is unavailable, configure an external one-minute GET job instead with `py configure_cronjob.py https://YOUR-PRODUCTION-DOMAIN --dispatch` and a cron-job.org API key. A timeout is processed on the next successful dispatcher run.

Telegram retries webhook requests that fail; the database uses the group ID and message ID to avoid duplicates. Keep the webhook secret, API key, bot token, and service role key private. If switching back to local polling, remove the webhook with `./telegram_windows.ps1 delete-webhook`, then re-enable and start the Windows task.

When the bot is added to a new group, send one message there and run `./remote_windows.ps1 groups`. The webhook records the group's ID and title for approval but does not save its messages until its exact ID is added to `ALLOWED_CHAT_IDS` and Vercel is redeployed.

The webhook handles new messages as they arrive. A cron-job.org task does not consume Telegram updates. See [cron-job.org's documentation](https://docs.cron-job.org/) and [Telegram's webhook documentation](https://core.telegram.org/bots/api#setwebhook).

## Windows setup

1. In Telegram, create a bot with [@BotFather](https://t.me/BotFather) using `/newbot`. Keep the token private.
2. In PowerShell in this folder, run `./setup_windows.ps1`. Paste the token at the hidden prompt. The script encrypts it for your Windows account, verifies the bot, and starts a scheduled collector at logon. Keep this PC on and signed in for continuous collection.
3. Add the bot to each group you manage. To let it receive ordinary messages, either make it an admin or disable group privacy in @BotFather with `/setprivacy` and then remove and re-add it. Give only the permissions you need; the collector never sends messages.
4. Run `py telegram_insights.py groups` after the collector sees the group. Approve each chosen group with `py telegram_insights.py approve CHAT_ID`. Only messages received **after approval** are archived.
5. Run `py telegram_insights.py status` to check that messages are arriving.

Telegram's Bot API provides new updates, not a complete backfill of group history. Telegram holds uncollected updates for at most 24 hours. This collector must keep running, and another process must not consume the same bot's `getUpdates` stream or set a webhook. It only archives messages in approved groups. Revoking a group stops future collection; it does not delete existing records.

If the PC is off or signed out, the collector cannot save messages immediately. When it resumes, it can collect pending Telegram updates that are still within Telegram's 24-hour retention window. Older missed updates require another chat export.

## Earlier history

The uploaded `veo messages files` Telegram Desktop HTML export has been imported. To import another extracted export with folders named for approved groups, run:

```powershell
py import_telegram_html.py 'PATH_TO_EXPORT_FOLDER'
```

The importer matches each export's chat title to one approved group, preserves message IDs and timestamps, and skips duplicates. It imports text and media placeholders; it does not download or transcribe media. `status` reports bot and export message counts separately.

## Query commands

```powershell
py telegram_insights.py groups
py telegram_insights.py status
py telegram_insights.py recent --days 1 --limit 200
py telegram_insights.py recent --group CHAT_ID --days 7 --limit 200
py telegram_insights.py search "deadline" --days 30 --limit 100
```

Outputs are JSON. The local archive remains available in this folder. Once Vercel is configured, use `./remote_windows.ps1 messages --group CHAT_ID --days 7 --limit 200` for current cloud data.

## Schedule announcements and polls

Schedule controls use the private `INSIGHTS_API_KEY` stored for this Windows account. The public cron endpoint can only dispatch actions that were already created through the authenticated schedule endpoint, and it only sends to approved groups.

```powershell
./remote_windows.ps1 schedule-message --group CHAT_ID --at '2026-09-20T09:30:00+08:00' --text 'Team update'
./remote_windows.ps1 schedule-poll --group CHAT_ID --at '2026-09-20T10:00:00+08:00' --question 'Lunch?' --option 'Pizza' --option 'Rice'
./remote_windows.ps1 schedules --status pending
./remote_windows.ps1 cancel-schedule SCHEDULE_ID
./remote_windows.ps1 workers --group CHAT_ID --days 14
```

Add `--repeat-minutes 1440` for a daily repeat, `--silent` to suppress notifications, or `--thread TOPIC_ID` for a forum topic. Polls also accept `--multiple` and `--public`. Failed deliveries retry after five minutes up to five attempts. A cron run sends up to 25 due actions.

## Daily availability and client activity

Done Deal tracking uses a simple read-only summary by default:
`DONE_DEAL_SUMMARY_ENABLED=true`, `DONE_DEAL_TRACKING_ENABLED=false`.
Members post their usual readable sale text in their team's DONE DEALS topic;
Client, Page and Price Deal fields are enough. No client token, bot command,
draft button or acceptance step is needed for a sale record. The bot counts
readable posts, sums recorded deal amounts, computes the average per deal, and
includes per-page counts/totals in the daily closeout sent to DAILY REPORTS.
The shared parser excludes identifiable duplicates and flags unreadable,
conflicting or unsupported posts. Partial coverage is disclosed; no readable
sales means the average is unavailable, rather than guessed.

Advanced templates, paid-sale targets, payroll and client-conversion workflows
remain disabled. Old template buttons give a short explanation of read-only
tracking. Pending legacy drafts, advanced sales reports and old guides are
retired before delivery. Assignment owners, tokens, acceptance deadlines,
handovers and historical records are preserved.

All six Veo teams continue availability polls, Ready/Busy, fair client rotation,
15-minute acceptance (warning after 10 minutes), client lists, handovers and
freebies. Veo Lyn reads Habi Media Agency and Salin Media Agency; Veo Ollie 2
reads AdsMoto Media Agency and AiTayo. Announcements use close-deal targets only, calculated as
`ceil(active members × 1.6)`, and summarize readable close-deal progress at the
existing Philippine reminder hours: 7, 10, 13, 16, 19 and 21. Daily closeout posts
availability, close-deal activity and the simple Done Deal summary to Daily Reports. Missing polls, unreadable
posts and capped message reads are disclosed rather than guessed.

Close-deal summaries use `Page name:` and `Close Deal:`. The shared parser
reconciles summaries and individual posts to avoid duplicate counts. Manager
worker analytics continue client acceptance/response timing, availability,
freebies and close deals. Paid-sales and conversion fields are `null` with a
paused coverage note, never misleading zero-earnings values.

Recruiter shares run independently of advanced Done Deal controls. Readable Veo
Done Deal posts and identifiable Suno receipts feed the existing 5% rules,
first-eight order cap and 72-hour bonus window. Existing registration dates,
locked credit, duplicate and ownership checks remain unchanged. A one-time
scheduled recovery report restores the last completed day after the pause;
regular private reports continue at 23:59 PHT. No sale template or client-token
code is required for a readable sale record with a client or order ID.
Suno/Trabawho receipts, receipt analytics, songs and delivery reminders continue.

The optional advanced paid-sale workflows and regression coverage remain available
for a future rollout; enabling them requires an explicit configuration change.
Authenticated managers can preview summaries without sending messages using
`./remote_windows.ps1 daily-summary --date YYYY-MM-DD` or the read-only
`GET /api/reports/daily-summary` endpoint. It requires Daily Reports approval and
includes only approved Veo teams; Suno receipts remain separate.

Scheduled delivery preserves reply IDs and delivery timestamps when a reminder is deferred. Retries stop after five consecutive failures and reset on success; completion checks the current claim before changing an action. Queue failures are reported without blocking already due messages. Completed CRM client reads paginate and require a non-empty details object. Client details are truncated before HTML escaping. New-client timeouts start from first delivery, expired undelivered contacts become available again, and acknowledgment reports use the actual Philippine confirmation date. The authenticated status response includes an automation version for deployment verification. Local archive replays preserve newer edits and never rewind the collector cursor.

## Freebie assignments

The freebie dispatcher reads paid-tagged CRM contacts for the configured Veo pages and posts one assignment per currently Active poll voter in the team's freebie topic. The member checks the CRM conversation, sends a suitable freebie, and replies with `FREEBIE SENT <token>`. A member keeps one open freebie at a time, and a client receives a fourteen-day break after confirmation.

## New-client round robin

### Hourly availability and the 20 / 10-minute handoff

With `NEW_CLIENT_READY_ENABLED=true`, **all six Veo teams and Suno/Trabawho**
use `/ready` and `/busy` in their own New Clients topics: Veo Ollie `5758`,
Veo Jessa `3725`, Veo `27622`, Veo Jel `4180`, and Trabawho `7673`.
`/ready` starts continuous automatic assignments, independently of the hourly poll.
Veo and Trabawho additionally join the queue during the hours selected in today's
poll, unless explicitly Busy or paused after a later missed offer. A newer vote
in the poll part covering the current hour can resume that pause; `/ready`
remains the way to clear an explicit Busy state.
After valid `WORKING`, the member stays in the queue for the next client until
`/busy`. Only one unanswered offer is allowed at a time. Missing a delivered
acceptance deadline pauses automatic offers until a newer `/ready`, even across
work dates and even if another member later takes the missed client.
Legacy 15-minute readiness records keep their original expiry until a fresh `/ready`.
Delayed READY commands older than 15 minutes cannot activate continuous mode.
`/busy` removes readiness and leaves accepted clients
with their existing owner. Hourly availability and quota reports continue;
members available in the hourly poll can still use `TAKE` without `/ready`.

New ready-queue offers allow five minutes **from Telegram delivery** to accept
with `WORKING <token>`. One warning is queued after three minutes. Before opening
the ten-minute TAKE window or reassigning an unanswered delivered offer, the bot
tags its previous editor with a turn-ended notice and waits for that notice's
recorded delivery. Undelivered notices keep the client reserved. Older offers
retain their printed acceptance deadline. Delayed warnings are suppressed after
acceptance. Confirmations name the client, page and current owner and remind members
to use `/busy` when unavailable. Old offers show CLIENT STATUS with the original
brief, without the OFFER CLOSED banner.
If Telegram cannot edit an old offer, its closure is posted as a short reply in
the same client topic. This uses the original scheduled-action retry and does
not repeat the brief or keep failing delivery.

Unclaimed clients keep rotating without an attempt cap. Legacy automatic
two-missed-offer holds rejoin the queue after the previous end notice is delivered.
Each new offer gets a fresh token; old tokens cannot replace the current owner.
Missed clients take priority over fresh clients. If nobody is ready, they wait
for an available editor rather than repeatedly assigning to an absent member.
CRM paid/converted tags and pipeline outcomes, human outbound
events and human-intervention records exclude clients from the Veo queue. Eligibility
is checked again before offer delivery and acceptance. This protects **recorded**
human handling; inbox activity missing from the CRM cannot establish ownership.
The CRM's client-facing chatbot is a separate service: this repository does not
change its answer extraction, inbound restart rules or queued Messenger replies.
The CRM ownership lookup requires the partial index recorded in
[`crm/20261013_human_ownership_lookup.sql`](crm/20261013_human_ownership_lookup.sql).
Apply it to the verified CRM database using an autocommit connection, outside a
transaction; it is separate from archive and Suno migrations. It supports the
page/contact-filtered human-message lookup without changing client records.
Without the index, large event histories can cause Data API statement timeouts
(`57014`). Source status exposes sanitized HTTP, SQLSTATE and table diagnostics
only through the authenticated endpoint.
Suno rechecks its own mapped contact source, page and customer identity before
delivery and acceptance. Its existing brief threshold is retained; paid and
converted pipeline outcomes block partial handoffs. WORKING
still starts the song job's separate 24-hour deadline. Confirm song delivery
with `SONG SENT <token>` in Trabawho's SONGS topic (`7692`).

The readiness and handover text commands use the current message webhook; no new
poll stream, schema migration or scheduler cadence change is needed. Inline buttons
also require `callback_query` in the existing webhook's `allowed_updates`. The
registration helper includes it, but changing webhook configuration requires an
explicit exception to the workspace's AGENTS.md instruction. Commands and buttons
are restricted to an approved configured team and its client topic;
message edits and stale updates cannot extend readiness. The scheduler delivers
the daily instruction and all ready/handoff notices. Do not dispatch manually
to test them.

### Client list and editor handovers

New ready-mode offers use a **15-minute** acceptance window, with one warning
after **10 minutes**. Previously delivered offers retain their printed deadline
and their original warning timing. TAKE remains a separate 10-minute window.
Warning text recomputes the remaining time at delivery so a deferred notice does
not display an old countdown.

Done Deal templates and `/ddtemplate` are disabled in read-only mode. Owner confirmations and
client lists show Client list controls without sale buttons. Handover controls
are available only in Trabawho. Old sale buttons and text
commands explain that summaries read existing posts without sale codes. Pending sale drafts are suppressed before delivery.
Suno retains its separate receipts process.

If a callback lacks Telegram topic metadata, the handler recovers the topic from
the exact delivered bot message in the same group. Unknown messages and wrong
topics are rejected. Acceptance clicks avoid synchronous CRM supply scans and
return a clear already-accepted or unavailable result for repeated clicks.
Matching text commands remain available in NEW CLIENTS. Callback errors log only
the exception type, group/message IDs, action name and source locations.

Veo Lyn (`-1004364797420`) uses General `1`, FREEBIES `22`, NEW CLIENTS `20`,
CLOSE DEALS `19`, announcements `18`, ACTIVE FOR TOMORROW `16`, and DONE DEALS `15`.
It reads Habi Media Agency (`e210bb1d-9326-4d02-b278-a3b625e16a06`) and
Salin Media Agency (`c4f6f7c4-6ce7-46b4-a063-19c537a20481`). Page aliases
support Habi and Salin in readable deal posts. Assignment follows the shared
eligibility and Ready queue rules.

Veo Ollie 2 (`-1004435878080`) uses General `1`, announcements `26`, Active for
Tomorrow `22`, Close Deals `18`, Done Deals `17`, New Clients `15` and Freebies
`14`. Its pages are AdsMoto Media Agency (`48b42f01-c9de-4afa-992b-2f743d21c81d`)
and AiTayo (`ddf67561-5dad-42ba-88e2-ef5534b45d6e`). The team participates in
availability polls, client rotation, 15-minute acceptance, owner directories,
freebies, quotas, planned ads and recruiter shares. Reads remain
scoped to its exact pages and topics.

All six Veo teams and Trabawho share a client list in their NEW CLIENTS topic.
It shows eligible unassigned clients, reserved offers, open TAKE offers, confirmed
editors, and (in Trabawho only) pending handovers. Each tracked client links to its brief and includes
its token. Long lists have pages. The scheduler edits the same overview message
every five minutes and refreshes it on assignment or ownership changes. `/clients`
queues the latest list; `/clienthelp` queues the short guide. The authenticated
`./remote_windows.ps1 clients --group CHAT_ID` command reads the full list and audit
history through `/api/clients` without changing state.

Handovers are disabled in all six Veo teams: Veo, Veo Jel, Veo Jessa, Veo Ollie,
Veo Ollie 2, and Veo Lyn. Their confirmed editors retain ownership. Old buttons,
text commands, and queued transfer notices cannot transfer a Veo client. Historical
handover records remain in the audit history.

In Trabawho, the current editor can tap **Hand over**, select a current team member, and wait
for that member to **Accept handover** or **Decline**. The current editor can
**Cancel handover**. To add context, reply to the receiving member's message in
NEW CLIENTS with `/handover TOKEN Your handover note`. `/handover TOKEN` without a
member reply opens the teammate picker. Text alternatives for the recipient are
`/accept_handover TOKEN` and `/decline_handover TOKEN`; the requester can use
`/cancel_handover TOKEN`.

The original editor remains responsible during a pending request. Acceptance
changes the editor atomically and records both editors, the note, timestamps, and
the Telegram update identifiers. Declines and cancellations are also retained.
Old buttons, replies predating a new request, nonmembers, and conflicting updates
cannot take ownership. Notices recover through the scheduled outbox after webhook
failures. Client lists are rebuilt at delivery to show current owners.

Handovers retain the client token and initial offer/response evidence. Optional
legacy sale attribution remains dormant in read-only Done Deal mode. Trabawho's unfinished
song jobs follow the new editor while retaining the original 24-hour deadline;
previous-owner reminders and completion replies are rejected. Song jobs that
were already completed keep their completion evidence.

The directory covers bot-tracked assignments and currently eligible CRM/Suno
briefs. An external or untracked assignment does not appear automatically. A
source failure is reported rather than presented as an empty client supply.

The scheduler posts a daily Taglish guide with command examples in each team's
New Clients topic and one announcement per guide version in its Announcements.
Suno's guide includes song-delivery instructions; Veo's guide includes freebie
instructions and the read-only Done Deal instructions. Other teams' notices refer to their team lead;
Ollie's notices continue to name Ollie.
The daily guide and rollout announcement emphasize `/ready` once, `WORKING` to
continue, `/busy` before breaks or shift end, and `/ready` to resume after a missed
deadline. The guide explains where to use commands, how READY differs from hourly polls,
the acceptance and TAKE windows, official ownership confirmation, expired offers,
and how to flag an existing owner or conflicting updates to the team lead. Guide-version
keys publish updated instructions even if the previous guide was sent that day.

When readiness is disabled and NEW_CLIENT_VOLUNTEER_ENABLED=true, all six Veo teams and Trabawho use
20-minute direct offers followed by a 10-minute volunteer window. The direct
timer starts at the first delivery of the client details. If the assigned member
does not reply WORKING with their token, the bot mentions members scheduled for
the current hour in New Client. The first eligible TAKE with that token,
or a direct TAKE reply to the invitation, claims the client through an atomic
database update. The volunteer timer starts when the invitation is sent.
An unanswered invitation releases the client to the next available member for
a fresh 20-minute turn. Rescued contacts take priority over fresh leads.
When nobody is available, the client waits for an eligible hour. Existing offers
retain the deadline stated in their original message.

HOURLY_AVAILABILITY_START_DATE sets the first Philippine work date using hourly
votes; production starts on **2026-10-09**. Each day's availability topic gets
three dated, nonanonymous polls covering midnight–8 AM, 8 AM–4 PM, and
4 PM–midnight. Each poll contains eight one-hour slots and a
“Not available in these hours” option. Select every available hour, using multiple
votes, or select Not available for that block. That option overrides other
selections in the same block. Changing or withdrawing a vote updates that block
without erasing hours selected in another poll.

Assignment and volunteer eligibility follow the current selected PHT hour.
An original assignee keeps their full 20-minute response window across an hour
boundary. Daily quotas and reports count each member once across all poll parts.
The existing webhook and durable schedule table store registrations and answers;
no second collector or new database table is required. The dispatcher must run
every minute, including overnight; expiry is handled on the next successful run.
Trabawho's 26% Suno threshold remains separate from Veo's completed-brief rule.
The older behavior described below applies when the new settings are disabled.

Ownership always follows the newest assignment for the same stable client in
that team. After reassignment, a WORKING reply to an older offer cannot take the
client back. The previous editor receives a friendly reply naming the current
editor and asking them to wait for their next turn. The first eligible volunteer
still wins within a single current volunteer window; a later volunteer reply
cannot replace that winner.

Undelivered offers are released when their editor leaves the selected work hour,
and an offer that exhausts delivery retries can move to another available editor.
An editor can receive a client again on a later work date, or when an earlier
offer was cancelled before delivery because their selected hour ended. Cancelled
offers retain their audit history and assignment attempt numbers. The status view
distinguishes awaiting delivery, delivery failure, volunteer claims, and WORKING
replies. The dispatcher can scan up to 100 due actions past deferred reminders
while retaining its limit of 25 delivery attempts per call.

Reconciliation repairs incorrect older confirmations, preserves their timestamps
for audit, and accepts a valid archived reply to the latest offer. Pending
acknowledgments and outstanding song jobs for revoked ownership are stopped.
If an old reply incorrectly cancelled the latest offer and its editor has not
answered, the latest offer resumes with the same client details and token and a
fresh 20-minute window measured from its updated delivery.

### Trabawho / Suno

Trabawho (`-1002894511895`) has a separate Suno workflow. General chat is topic
`1`, announcements `16`, Active for Tomorrow `7581`, and new clients `7673`.
The bot creates dated, nonanonymous availability polls for today and tomorrow in
topic `7581`. Votes for tomorrow affect tomorrow's plan; clients are offered when
that dated workday arrives, using only that workday's Active voters.

Announcements update when the recorded Active count changes. Group announcements
use short, friendly status messages and do not expose the quota formula or ads
budget. The detailed plan, receipt, salary, review, and song figures are kept in
the approved Daily Reports group. Suno's quota is `active members × 4` and its
planned ads budget is `active members × 4 × ₱150`. Veo uses a whole-client quota
of `ceil(active members × 1.6)` and budget `active members × 1.6 × ₱400`.
The Veo budget uses the exact multiplier before quota rounding: one Active
member means 2 planned clients and ₱640, not ₱800. Plans use each dated workday's
distinct availability voters; tomorrow's votes do not change today's plan.

At the daily check-in times, Trabawho's general topic receives a gentle
availability reminder. It mentions people who selected Active but have not yet
received a client, thanks people who selected Not Active, and reminds anyone who
has not voted to choose an option. The announcements topic receives a friendly
client-progress update showing how many planned assignments are complete and how
many remain. These messages do not include salary or ads calculations.

Suno uses its own server-only `SUNO_SUPABASE_URL` and
`SUNO_SUPABASE_SERVICE_ROLE_KEY`; the archive and Veo CRM retain their existing
credentials. Define the Suno table, ID/name/details columns, and the completion
column/value using the `SUNO_*` settings in `.env.example` after verifying the
schema. Optional customer-identity and date columns support stable deduplication
and oldest-first assignment. The reader paginates, requires the mapped complete
outcome and a nonempty details object, and leaves assignment disabled if the
mapping is absent. It never guesses a client table or treats partial details as
complete. A Suno connection failure is reported separately and does not stop Veo
queues or due deliveries.

Trabawho assignments read only this Suno Supabase source. They do not read Veo's
CRM pages. The `pages.name` relation is shown as `Suno page: ...` in assignments,
so a Suno page with a name similar to a Veo page remains clearly separated.
The verified Suno project is `cxgynadprukyeuqbchbs`; production pins it with
`SUNO_EXPECTED_PROJECT_REF`. `SUNO_ASSIGNMENTS_ENABLED=true` enables assignments
after verification. Disabled sources suppress queued assignment reminders as
well. Contact identities include the Suno project and actual page ID, and states
with outstanding `missing_details` are excluded from complete clients.

Trabawho production uses `SUNO_MIN_DETAILS_PERCENT=26`: a Suno client may be
assigned once at least 26% of its required fields are collected, even when the
chatbot is stopped or the client still has missing fields. Eligibility uses the
collected data rather than requiring an active status or a particular positive
handoff label. For example, a stopped `details_collected` contact with 7 collected
fields and 13 missing fields qualifies at 35%; its brief remains partial.
The total is the unique union of collected and missing field names; blanks and
fields still listed as missing do not count as collected. Assignments show all
collected information and the remaining fields. Refused and opted-out leads
remain excluded. The default of 100 retains the completed-brief rule. Status
reports distinguish eligible clients, complete briefs, and partial handoffs.
This setting applies only to Suno/Trabawho; Veo eligibility rules are unchanged.

`./remote_windows.ps1 suno-supply` provides an authenticated, read-only supply
audit: contact inventory, latest source interaction, and per-page counts of
eligible briefs and exclusions. Compare it with `new-clients` to distinguish
already assigned clients from insufficient details or a source that stopped
receiving inquiries. The audit does not include client names, identities or
brief contents and never changes availability, assignments or delivery.
Add `--chatbot` for chatbot enablement, trial settings, completion rules and
the latest 24 hours of processing counts. Each processing source is capped at
1000 rows and explicitly reports partial coverage when capped.

Trabawho follows ready-member rotation with a 30-minute WORKING reply deadline
and a 30-minute cooldown after a missed reply. Assignments use topic `7673` and
stable Suno customer identities. Its client checks have a separate one-minute
checkpoint. Production activation requires the Suno settings in Vercel's
Production environment and Trabawho in `ALLOWED_CHAT_IDS`. Deployment and live
schema verification are still required for this new workflow.

Trabawho sales and recorded salary share topic `4` (Receipts). The authenticated
`GET /api/workers/activity?group=-1002894511895&days=14` manager report reads that
topic instead of Veo DD/CD topics. `699 (140)` records 699 gross and 140 salary;
`100 (50) tip` records another 100 gross and 50 salary, once. An explicitly
labeled tip with no parenthesized share uses the user's 50/50 rule. Written
shares are authoritative for videos, revisions, and other extras; a 50% share
alone does not identify a tip. The report separates labeled tips and ranks gross
receipt totals, including extras and tips. Receipt-post counts are not client
counts or quota completion. This report is read-only and does not post payroll.

Malformed amounts, conflicting tip shares, unpaid/correction comments, and
challenged receipts are held for review and excluded from accepted totals.
Image-only receipts cannot be read. Export topic membership is reconstructed
from reply ancestry; unresolved membership and source/date coverage are reported.
Identical message IDs are counted once, but equal amounts on different receipt
posts are retained. Earnings are recorded amounts, not confirmed salary payouts.

Daily Trabawho reports are queued at 00:05 PHT for the previous calendar day;
a later cron run that day catches up if necessary. Announcements topic `16`
receives a short, friendly summary, while the approved Daily Reports group
receives the organized detailed report. The detailed report includes Active
members, the four-per-member quota, planned ads budget, accepted receipt
gross/salary, each member's recorded totals, labeled tips, receipts needing
review, and song deliveries/outstanding jobs.
Detailed reports for all seven teams also show tomorrow's active members and
targets using the Philippine calendar date when the report is prepared.
Hourly poll votes count each member once within their team. Trabawho's planned
ads budget uses Active × 4 × ₱150; Veo uses Active × 1.6 × ₱400.
All formulas come from `team_planning.py`. Missing polls leave both quota and
budget pending; a confirmed zero-member poll produces a zero plan. Plans and
reminders prepared before a formula change are refreshed before delivery.
The scheduled rollout publishes current-day and tomorrow plans once per day
and formula version, with budgets only in approved Daily Reports. Actual ad spending is explicitly unreported
until a verified spending source is supplied. Profit figures exclude those
unreported advertising costs and other expenses. The authenticated read-only
`./remote_windows.ps1 daily-plan` command previews current tomorrow staffing
and planned ads budgets without sending or changing any messages.
Reports and song reminders run independently of Suno connection availability.
Report markers mean all report parts are queued; dispatcher retries handle
delivery failures. Amounts with unresolved receipts are explicitly partial.

Song delivery uses topic `7692`. A member's accepted `WORKING` assignment starts
one persistent 24-hour deadline. Initial and 12/20/23-hour reminders show the
deadline and time remaining; a 21:00 PHT reminder encourages same-day delivery
when acceptance precedes that time. Same-day delivery is the target; the tracked
hard deadline is exactly 24 hours after acceptance, including across midnight.
Overdue work gets one reminder per 24-hour overdue period until confirmed.
The bot schedules only the current milestone after downtime and recomputes the
countdown before sending. A new day or another cron run never resets a deadline.

After delivery to the client, the assigned member posts `SONG SENT ABCD1234` (or
`DONE ABCD1234`) in topic `7692`, using their client token. A direct `DONE` or
`SONG SENT` reply to a tracked bot reminder also works. The bot replies to the
confirmation, records its timestamp, and suppresses pending reminders for that
client. Token, topic and member identity are required; plain `DONE` without a
tracked reply and an uploaded file alone do not establish client delivery.
Archived confirmations reconcile missed webhook handling before new reminders.
Deadlines and confirmations reuse durable scheduled-action markers, requiring
no new database table or collector/webhook setup.

Veo teams retain close-deal targets, client activity reports, CRM sources and
freebie completion rules. Done Deal counts and averages are read-only; related earnings reports remain paused. Their availability and
new-client timing use the hourly and volunteer settings above. The Veo daily scheduler excludes
Trabawho; Trabawho source/planning/report failures are caught separately and do
not stop Veo delivery. A Trabawho poll-lookup failure does not consume a Veo poll
webhook. The existing authenticated Windows helper supports
`./remote_windows.ps1 dispatch` to run the shared dispatcher for all approved
teams when startup is explicitly requested.

Recruiter incentives use a non-anonymous, single-answer poll in Veo Recruits
(`-1003555676168`, topic `510`). Members who have not started should wait;
they vote just before their first sale and select their inviter from the eleven
configured names, including Eri (`@exoxo4`). When the roster changes, the
poll is replaced in the same topic; stored attribution and first-vote dates
remain intact. Each poll retains its own option order for decoding answers.
The recruiter receives 5% of recorded paid sale amounts on
the recruit's first 8 identifiable orders, with no expiry. Completing those
8 within 72 hours of the earliest recorded work-team join or first recruiter
vote unlocks 8 additional commissioned orders (16 total), also with no expiry.
Withdrawing or changing a vote never restarts that clock. Recruiter credit is
locked after the first commissioned sale is calculated in a report.
Veo Recruits receives only the inviter-selection poll and onboarding/work
instructions. Commission rates, bonus rules, credited sales and share amounts
are sent exclusively to the recruiter report topic. The previous public rules
post and General-topic poll are removed; the neutral poll and instructions go
in topic `510`;
stored attribution and original vote dates are preserved. Pending legacy
disclosures are suppressed, and share-report delivery requires the exact
recruiter chat and topic.

`REFERRAL_INCENTIVES_ENABLED=true`, `REFERRAL_START_DATE`,
`REFERRAL_COMMISSION_BASIS=gross`, and
`REFERRAL_WINDOW_START=join_or_vote` enable this workflow. Reports refresh from
recorded receipts at delivery and post to Team Recuiters (`-1004389276294`),
topic `2`, at 23:59 PHT. A dedicated `59 15 * * *` UTC dispatch supplements the
existing schedules. Both target groups must be in `ALLOWED_CHAT_IDS`.
Reports lead with today's shares and cumulative recorded shares, then group
recruits under their inviter. Each recruit shows daily shares, cumulative
shares, commissioned-sales progress, remaining sales and bonus deadline or
unlock status. Recruiters without linked recruits appear in one compact list;
held receipts and conflicting orders have a separate review section. Recorded
shares remain distinct from confirmed payouts.
`./remote_windows.ps1 referrals` shows the poll, policy, and report delivery state.
`./remote_windows.ps1 referrals --audit` runs an authenticated, read-only preview
against the same live sales sources. It sends no messages, queues no reports,
and does not lock or change recruiter credit. Unpaid/correction variants and
conflicting or placeholder order/client/page details are held for review.
Out-of-order joining updates retain the earliest event and never extend the
bonus window.

Recruiter poll/instruction maintenance and report scheduling have a durable
daily setup checkpoint. After every scheduling operation succeeds, later
dispatches perform one checkpoint read instead of repeating setup reads and
writes. A partial failure retries on the next dispatch. Approval changes and
the next Philippine day trigger setup again. Due deliveries, client checks
and reply processing continue on every normal dispatch. Bump the
`referral-setup` key version whenever changing poll/instruction scheduling
keys so a same-day deployment does not skip the new setup.

Counts carry across the six configured Veo teams and Trabawho by Telegram
user ID. Order IDs, or page-scoped client names when an ID is absent, prevent
reposts from consuming another commissioned sale. Include an order ID for
separate repeat orders from one client. Tips, extras, deposits/close-deal
summaries, image-only receipts, unclear amounts and conflicting seller credit
are excluded or held for review. Reported shares are recorded commissions,
not confirmation that payment has been made. Historical joins outside the
collector's coverage cannot be inferred; the first recorded vote supplies the
start date when no earlier joining event is available.

Separately, the new-client dispatcher reads Supabase contacts whose chatbot has `stop_reason = details_collected` and a non-empty collected-details object. That final chatbot outcome is authoritative even when an older state retains stale entries in `missing_details`. It assigns those contacts in the new-client topics: Veo Jel `4180`, Veo `27622`, Veo Jessa `3725`, and Veo Ollie `5758`.

Veo Ollie reads only Loki Media and Vista Media Agency for new-client topic `5758`; Veo Jessa reads only Manawari Studios for new-client topic `3725`. Both teams participate in the shared hourly availability polls, WORKING confirmations, volunteer claims, assignment recovery, paid-client freebies in their separate freebie topics, quota reminders, daily reports, staffing plans, worker activity, and recruiter attribution. New-client assignment requires an available completed brief and a member available in the current selected Philippine hour; a connected page with no eligible contacts does not generate an assignment.

Only members who selected Active for that Philippine day participate. The bot gives each Active member one client before anyone can move ahead to the next round. A member replies `WORKING ABCD1234` as soon as they start handling the assigned client; a direct `WORKING` reply to the assignment is also accepted. That acknowledgment stops reminders and makes the member eligible when the round robin reaches them again. Before timing out an assignment, the bot reconciles archived replies so a confirmation cannot be missed and reassigned. If no valid `WORKING` reply arrives within one hour, the assignment is cancelled and the same contact is reassigned to a different Active member. If the original member replies after that timeout, the earliest valid `WORKING` reply wins and any duplicate open assignment is cancelled. Contacts are tracked by their stable page-scoped PSID, with legacy fallbacks, so changing contact details or replacing a CRM row does not create a new assignment. A faster member cannot receive a third client while another Active member has received only one. Acknowledged contacts are never assigned again.

When volunteer mode is disabled, Veo, Veo Ollie, and Veo Jessa use a 30-minute reply deadline measured from first delivery and rotation among ready Active members. Members with an unacknowledged assignment are skipped so they do not block the ready team. A member who times out enters a 30-minute cooldown, then automatically rejoins while still Active. Selecting Not Active and then Active in today's poll, or a newer valid WORKING reply, resumes availability sooner. Timed-out contacts are offered to a different eligible member before fresh contacts. Rotation favors fewer non-cancelled or acknowledged assignments today, then fewer assignments over the previous seven days, then the member who has waited longest since their last assignment; failed offers do not reduce future access after the member returns. One unacknowledged client per member is allowed. This does not track unfinished work after acknowledgment. The new-client status endpoint reports each team's reply deadline, ready count, paused members, and each Active member's offers, deliveries, confirmations, waiting reason, and cooldown retry time. All six teams carry assignment order across days using the previous seven days as a tie breaker, so scarce supply does not repeatedly favor the same names each morning. These fallback settings are in `daily_automation.py`; Veo Jel retains the one-hour deadline and full-team round rule when volunteer mode is disabled. With `NEW_CLIENT_VOLUNTEER_ENABLED=true`, all six Veo teams use the shared 20-minute direct offer followed by a 10-minute volunteer window described above.
# Organized manager reports

Capacity names now require a fresh Telegram `getChatMember` result for that
team. Former members and unknown identities are omitted. Five-minute cached
checks are bounded per scheduler run; join/leave service messages update the
cache sooner. A membership change edits the current capacity posts, while
unchanged report text stays quiet. Historical sales and payable earnings retain
their original attribution. The staffing forecast likewise omits former
members. `report-members` reads verification status without refreshing it.

Capacity reports include the number of declared Active dates on which the safe
target was actually recorded, plus any higher older recorded peak. Neither is
proof of production limits. The current planning formula remains the requested
14-day top-three mean and floor(mean × 80%) target.

General also receives MEMBER SALARY TOTALS for the report date: one daily total
per Telegram identity, summed across teams. Pending VEO tiers show conditional
35%–40% ranges on accepted recorded sales plus any known earnings. They are not
presented as final payroll. Name-only records remain separate by team.

`ORGANIZED_REPORTS_ENABLED=true` sends public reports into the verified DAILY
REPORTS team topics: Jessa 191, Jel 193, Veo 195, Ollie 197, Lyn 199, Ollie 2
201, Makata 203, Trabawho 205. Each receives sales, recorded salary, capacity,
problems, and a staffing summary. General receives combined sales and staffing
summaries. A report about a paused team does not enable its assignment bot.

Daily reports cover the previous Philippine date after 00:10 PHT. Staffing
summaries show today's Active voters and tomorrow's Active voters, average
output per rated voter and the conservative rated total. Five-minute checks
edit the same message only when votes or its text change. Missing attendance
or member history stays unknown; receipt and Done Deal units stay separate.

Suno salaries use the written receipt earnings, including outside-bot clients.
VEO manager salary reports use the existing 40% commission when both team CD
and DD quotas are met, otherwise 35%. The quota remains the existing rounded-up
Active × 1.6 formula. Missing polls or records that could change the tier keep
the calculation pending unless dated, non-conflicting availability answers
can recover the recorded Active count. These reports do not enable member sale buttons or
assignment automation in paused groups. Sales less recorded salary is
before advertising and other expenses, not net profit or proof of payout.
Existing private Makata reports to Maddie continue separately.

`./remote_windows.ps1 report-topics` reads the captured topic names/IDs without
sending messages or consuming Telegram's update stream.

Capacity reports now use the last 14 completed Philippine dates. Estimated
planning capacity is the average of the top three positive daily outputs on
distinct dates, excluding explicit Inactive/output conflicts. The safe target
is floor(estimate × 80%). Output-day average and recorded daily peak remain
visible. Fewer than three eligible output dates leaves the estimate pending;
unknown attendance is never changed to Inactive. Best-day estimates do not
establish verified full shifts or production maximums, and no utilization
uplift is applied. Suno output remains receipt posts, including outside-bot
clients; VEO output remains readable Done Deals. Active-day averages separately
include declared Active dates with zero output. Staffing summaries use these
same estimates for tomorrow's actual voters. Corrections reuse existing report
and staffing messages where available.

With `AVAILABILITY_REMINDERS_ENABLED=true`, tomorrow-availability reminders tag known current human members who have not
answered the team's poll at 08:00, 11:00, 14:00, 17:00, 20:00 and 22:00 PHT.
The scheduler can deliver within that hour; missed earlier hours are skipped.
VEO and Suno use the same reminder schedule, independently of new-client mode.
Either Yes or No stops future tags. Withdrawal makes a member unanswered again.
Votes and current membership are checked immediately before each delivery.
Team administrators and owners are excluded from reminder mentions, using their
current Telegram role in that team.
Messages stay in ACTIVE TOMORROW with a poll link, split into at most twenty
names per message for readability, without a pagination interface. Durable
date/hour/team keys prevent repeated scheduler ticks from duplicating reminders.
Candidate IDs come from captured authors, previous voters and observed joins;
Telegram's bot API cannot enumerate silent members who have never been captured.
