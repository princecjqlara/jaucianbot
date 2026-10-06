# Telegram group insights

This bot archives new messages from Telegram groups you approve. Telegram Desktop HTML exports can also be imported for earlier messages. Ask the assistant in this workspace for summaries, decisions, issues, trends, or a search. The local archive is `telegram_insights.sqlite3`; the cloud archive uses Supabase. Attachments are recorded by type but are not downloaded or transcribed.

## Deploy on Vercel

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
7. Optionally create a second GET job for `https://YOUR-PRODUCTION-DOMAIN/api/cron/dispatch` every minute for tighter delivery timing. The configured Vercel crons run at midnight and every five minutes from 7 AM through 9:55 PM Philippine time. The endpoint atomically claims due scheduled actions and returns only delivery counts. Vercel's frequent cron schedules require Pro or Enterprise; Hobby needs an external dispatcher. See [Vercel's scheduling limits](https://vercel.com/docs/cron-jobs/usage-and-pricing). A timeout is processed on the next successful dispatcher run, rather than by a continuously running timer.

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

Separately, the new-client dispatcher reads Supabase contacts whose chatbot has `stop_reason = details_collected` and a non-empty collected-details object. That final chatbot outcome is authoritative even when an older state retains stale entries in `missing_details`. It assigns those contacts in the new-client topics: Veo Jel `4180`, Veo `27622`, Veo Jessa `3725`, and Veo Ollie `5758`.

Only members who selected Active for that Philippine day participate. The bot gives each Active member one client before anyone can move ahead to the next round. A member replies `WORKING ABCD1234` as soon as they start handling the assigned client; a direct `WORKING` reply to the assignment is also accepted. That acknowledgment stops reminders and makes the member eligible when the round robin reaches them again. Before timing out an assignment, the bot reconciles archived replies so a confirmation cannot be missed and reassigned. If no valid `WORKING` reply arrives within one hour, the assignment is cancelled and the same contact is reassigned to a different Active member. If the original member replies after that timeout, the earliest valid `WORKING` reply wins and any duplicate open assignment is cancelled. Contacts are tracked by their stable page-scoped PSID, with legacy fallbacks, so changing contact details or replacing a CRM row does not create a new assignment. A faster member cannot receive a third client while another Active member has received only one. Acknowledged contacts are never assigned again.

Veo uses a 30-minute reply deadline measured from first delivery and rotation among ready Active members. Members with an unacknowledged assignment are skipped so they do not block the ready team. A member who times out is paused for that Philippine day until they select Not Active and then Active in today's poll, or a newer valid WORKING reply confirms availability. Timed-out contacts are offered to a different eligible member before fresh contacts. Rotation favors fewer non-cancelled or acknowledged assignments, then the member who has waited longest since their last assignment; failed offers do not reduce future access after the member returns. One unacknowledged client per member is allowed. This does not track unfinished work after acknowledgment. The new-client status endpoint reports each team's reply deadline, ready count, and paused members. Veo's settings are in `daily_automation.py`; the one-hour deadline and full-team round rule above remain the defaults for the other configured groups.
