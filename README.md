# Telegram group insights

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
7. Create an external GET job for `https://YOUR-PRODUCTION-DOMAIN/api/cron/dispatch` every minute to process Veo's 30-minute timeouts promptly. No API key or headers are required for this existing dispatch endpoint. The configured Vercel fallback jobs each run once daily: midnight closeout and a separate job for every hour from 7 AM through 9 PM Philippine time. These jobs remain compatible with Hobby. Vercel's frequent cron schedules require Pro or Enterprise; see [Vercel's scheduling limits](https://vercel.com/docs/cron-jobs/usage-and-pricing). A timeout is processed on the next successful dispatcher run. Without a frequent external dispatcher, new completed contacts and the 30-minute deadline can wait for a later fallback run. To create or update the one-minute job through the cron-job.org API, run `py configure_cronjob.py https://YOUR-PRODUCTION-DOMAIN --dispatch`; it prompts securely for the cron-job.org API key when no key is configured locally.

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

## Daily quota and sales automation

The dispatcher also runs the four configured Veo group workflows in Philippine time:

- Around midnight it posts a dated, nonanonymous Active/Not Active poll for the following day in each **Active for Tomorrow** topic. If today's poll is missing, it posts a catch-up poll for today.
- At daily closeout it calculates `ceil(active workers × 0.8)`. The same number is the group target for Done Deals and Close Deals, and it posts the next day's target in each announcements topic.
- At **7:00 AM, 10:00 AM, 1:00 PM, 4:00 PM, 7:00 PM, and 9:00 PM Philippine time**, it posts one reminder in each Veo team's announcements topic. Each reminder reads the latest tracked Active poll and that day's Done Deals and Close Deals posts, shows the target, current totals, and how many DD and CD are still needed, then adds a short encouragement. From 10:00 AM onward it identifies Active voters with no readable CD/DD yet; possible unreadable posts are listed separately instead of treating their authors as inactive. Afternoon and evening reminders show the current sales leader and recommend whether the next focus should be new CDs or converting open CDs into paid DDs. If the poll or deal data is incomplete, it says so instead of showing an unverified gap.
- `vercel.json` schedules midnight closeout and hourly daytime dispatches. Dedupe keys prevent duplicate reminders if another dispatcher also runs. Vercel Hobby cron timing can drift within the scheduled hour.
- It reads that day's configured Done Deals and Close Deals topics, groups results by page, totals Price Deal amounts, and posts one organized report per group to **DAILY REPORTS**.
- Employees are ranked by Price Deal sales. When the group reaches both its DD and CD targets, each employee's pay is 40% of their Price Deal total; otherwise it is 35%. Profit is gross Price Deal value less those commissions. An unreadable post is still flagged, but pay stays available when the unresolved posts cannot possibly change the commission tier; only a genuinely undecidable tier remains pending review.
- Each report lists people who answered **Active** in that day's named poll but had no parsed CD or DD. Telegram user IDs match poll answers to live deal posts. Unreadable deal posts are flagged for review instead of counting their authors as having no deals. Historical samples use "active" replies from the previous Philippine day and match exported deal posts by display name.

The report flags messages it cannot parse. Deal entries should include `Page:` plus `Price Deal:` or `PD:`. Close Deal summaries should include `Page name:` and `Close Deal:`. The commission calculation uses Price Deal and excludes tips, revisions, down payments, and Total Payment differences.

Reports, quota reminders, and worker analytics share one deal parser. Price Deal additions and multiplication are calculated in full; a written equals total must agree. Repeated posts for the same named client, author, page, amount, and Philippine posting day count once. Posts claimed by different authors require ownership review. Daily CD summaries are reconciled with individual CDs rather than added twice; the latest summary is used, and zero is valid. A named client with a positive DP and blank PD counts as a CD without assigning sales. Only adjacent, same-author media with a readable caption are treated as supporting attachments; unmatched media stay flagged. Review items include message IDs and UTC timestamps. Daily reads paginate past 500 messages with a 10,000-message safety limit, and analytics preserve both date bounds. Commissions are rounded per employee before totaling; a missing poll leaves the commission tier pending.

The authenticated `workers` view is for manager coaching. It ranks recorded DD, CD, sales, confirmed freebies, acknowledged new clients, and a transparent activity score; reports Active days with no readable deals; estimates each person's strongest deal-posting hour; and supplies specific improvement suggestions. It does not treat chat volume or login time as productivity, does not penalize people who selected Not Active, and keeps unreadable posts visible as a data-quality issue. The score is operational guidance only and is never used for payroll.

Scheduled delivery preserves reply IDs and delivery timestamps when a reminder is deferred. Retries stop after five consecutive failures and reset on success; completion checks the current claim before changing an action. Queue failures are reported without blocking already due messages. Completed CRM client reads paginate and require a non-empty details object. Client details are truncated before HTML escaping. New-client timeouts start from first delivery, expired undelivered contacts become available again, and acknowledgment reports use the actual Philippine confirmation date. The authenticated status response includes an automation version for deployment verification. Local archive replays preserve newer edits and never rewind the collector cursor.

## Freebie assignments

The freebie dispatcher reads paid-tagged CRM contacts for the configured Veo pages and posts one assignment per currently Active poll voter in the team's freebie topic. The member checks the CRM conversation, sends a suitable freebie, and replies with `FREEBIE SENT <token>`. A member keeps one open freebie at a time, and a client receives a fourteen-day break after confirmation.

## New-client round robin

### Hourly availability and the 20 / 10-minute handoff

With NEW_CLIENT_VOLUNTEER_ENABLED=true, all four Veo teams and Trabawho use
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
the approved Daily Reports group. Internally, quota is `active members × 2` and
the planned ads budget is `active members × 2 × ₱150`; these settings do not apply
Veo's DD/CD targets, commission rules, or freebie topics.

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

Trabawho production uses `SUNO_MIN_DETAILS_PERCENT=26`: an active Suno chatbot
state may be assigned once at least 26% of its required fields are collected.
The total is the unique union of collected and missing field names; blanks and
fields still listed as missing do not count as collected. Assignments show all
collected information and the remaining fields. Refused and opted-out leads
remain excluded. The default of 100 retains the completed-brief rule. Status
reports distinguish eligible clients, complete briefs, and partial handoffs.
This setting applies only to Suno/Trabawho; Veo eligibility rules are unchanged.

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
members, the two-per-member quota, planned ads budget, accepted receipt
gross/salary, each member's recorded totals, labeled tips, receipts needing
review, and song deliveries/outstanding jobs.
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

Veo, Veo Jel, Veo Jessa, and Veo Ollie retain their existing quotas, sales parsing,
daily reports, CRM sources and freebie completion rules. Their availability and
new-client timing use the hourly and volunteer settings above. The Veo daily scheduler excludes
Trabawho; Trabawho source/planning/report failures are caught separately and do
not stop Veo delivery. A Trabawho poll-lookup failure does not consume a Veo poll
webhook. The existing authenticated Windows helper supports
`./remote_windows.ps1 dispatch` to run the shared dispatcher for all approved
teams when startup is explicitly requested.

Separately, the new-client dispatcher reads Supabase contacts whose chatbot has `stop_reason = details_collected` and a non-empty collected-details object. That final chatbot outcome is authoritative even when an older state retains stale entries in `missing_details`. It assigns those contacts in the new-client topics: Veo Jel `4180`, Veo `27622`, Veo Jessa `3725`, and Veo Ollie `5758`.

Only members who selected Active for that Philippine day participate. The bot gives each Active member one client before anyone can move ahead to the next round. A member replies `WORKING ABCD1234` as soon as they start handling the assigned client; a direct `WORKING` reply to the assignment is also accepted. That acknowledgment stops reminders and makes the member eligible when the round robin reaches them again. Before timing out an assignment, the bot reconciles archived replies so a confirmation cannot be missed and reassigned. If no valid `WORKING` reply arrives within one hour, the assignment is cancelled and the same contact is reassigned to a different Active member. If the original member replies after that timeout, the earliest valid `WORKING` reply wins and any duplicate open assignment is cancelled. Contacts are tracked by their stable page-scoped PSID, with legacy fallbacks, so changing contact details or replacing a CRM row does not create a new assignment. A faster member cannot receive a third client while another Active member has received only one. Acknowledged contacts are never assigned again.

Veo uses a 30-minute reply deadline measured from first delivery and rotation among ready Active members. Members with an unacknowledged assignment are skipped so they do not block the ready team. A member who times out enters a 30-minute cooldown, then automatically rejoins while still Active. Selecting Not Active and then Active in today's poll, or a newer valid WORKING reply, resumes availability sooner. Timed-out contacts are offered to a different eligible member before fresh contacts. Rotation favors fewer non-cancelled or acknowledged assignments today, then fewer assignments over the previous seven days, then the member who has waited longest since their last assignment; failed offers do not reduce future access after the member returns. One unacknowledged client per member is allowed. This does not track unfinished work after acknowledgment. The new-client status endpoint reports each team's reply deadline, ready count, paused members, and each Active member's offers, deliveries, confirmations, waiting reason, and cooldown retry time. All four teams carry assignment order across days using the previous seven days as a tie breaker, so scarce supply does not repeatedly favor the same names each morning. Veo's settings are in `daily_automation.py`; the one-hour deadline and full-team round rule above remain the defaults for the other configured groups.
