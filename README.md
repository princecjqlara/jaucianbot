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
   | `ALLOWED_CHAT_IDS` | Comma-separated numeric IDs of the approved groups. |
   | `TELEGRAM_WEBHOOK_SECRET` | Random secret generated in `.env.local`. |
   | `INSIGHTS_API_KEY` | Separate random secret generated in `.env.local`. |

   The `SUPABASE_ANON_KEY` is not needed by this server. Never put the service role key or bot token in Git or a browser-facing variable.
3. Run `./migrate_to_supabase.ps1` in this workspace. It copies the local archive, including imported history, to Supabase. Rerunning skips duplicates.
4. After the Production deployment and database are ready, stop the Windows poller: `Stop-ScheduledTask -TaskName TelegramGroupInsights` and `Disable-ScheduledTask -TaskName TelegramGroupInsights`. Then run `./telegram_windows.ps1 set-webhook https://YOUR-PRODUCTION-DOMAIN/api/webhook`. It reads the same webhook secret from `.env.local`. Inspect delivery with `./telegram_windows.ps1 webhook-info`.
5. Run `./setup_remote_windows.ps1 https://YOUR-PRODUCTION-DOMAIN`. The assistant can then query the cloud archive with `./remote_windows.ps1 status` and `./remote_windows.ps1 messages --days 7`.
6. In the cron-job.org Console, create a GET job for `https://YOUR-PRODUCTION-DOMAIN/api/health` every 15 minutes. The endpoint checks Vercel and Supabase and returns only `{"ok": true}`. Dashboard setup does not need an API key or request headers. The optional `configure_cronjob.py` script uses a cron-job.org API key only when creating the job through its REST API.
7. Create a second GET job for `https://YOUR-PRODUCTION-DOMAIN/api/cron/dispatch` every minute. It atomically claims due scheduled actions and sends them. It accepts no user content, needs no cron-job.org API key or headers, and returns only delivery counts.

Telegram retries webhook requests that fail; the database uses the group ID and message ID to avoid duplicates. Keep the webhook secret, API key, bot token, and service role key private. If switching back to local polling, remove the webhook with `./telegram_windows.ps1 delete-webhook`, then re-enable and start the Windows task.

When the bot is added to a new group, send one message there and run `./remote_windows.ps1 groups`. The webhook records the group's ID and title for approval but does not save its messages until its exact ID is added to `ALLOWED_CHAT_IDS` and Vercel is redeployed.

The webhook handles new messages as they arrive. The cron-job.org task only monitors `/api/health`; it does not consume Telegram updates. See [cron-job.org's documentation](https://docs.cron-job.org/) and [Telegram's webhook documentation](https://core.telegram.org/bots/api#setwebhook).

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
```

Add `--repeat-minutes 1440` for a daily repeat, `--silent` to suppress notifications, or `--thread TOPIC_ID` for a forum topic. Polls also accept `--multiple` and `--public`. Failed deliveries retry after five minutes up to five attempts. A cron run sends up to ten due actions, so the one-minute job catches newly due work promptly.

## Daily quota and sales automation

The one-minute dispatcher also runs the four configured Veo group workflows in Philippine time:

- At 12:00 AM it posts a dated, nonanonymous Active/Not Active poll for the following day in each **Active for Tomorrow** topic.
- At 11:59 PM it calculates `ceil(active workers × 0.8)`. The same number is the group target for Done Deals and Close Deals, and it posts the following day's target in each announcements topic.
- It reads that day's configured Done Deals and Close Deals topics, groups results by page, totals Price Deal amounts, and posts one organized report per group to **DAILY REPORTS**.
- Employees are ranked by Price Deal sales. When the group reaches both its DD and CD targets, each employee's pay is 40% of their Price Deal total; otherwise it is 35%. Profit is gross Price Deal value less those commissions.

The report flags messages it cannot parse. Deal entries should include `Page:` plus `Price Deal:` or `PD:`. Close Deal summaries should include `Page name:` and `Close Deal:`. The commission calculation uses Price Deal and excludes tips, revisions, down payments, and Total Payment differences.
