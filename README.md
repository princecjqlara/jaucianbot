# Telegram group insights

This bot archives new messages from Telegram groups you approve. Telegram Desktop HTML exports can also be imported for earlier messages. Ask the assistant in this workspace for summaries, decisions, issues, trends, or a search; it can query the local archive. Message text and captions are stored in `telegram_insights.sqlite3` on this machine. Attachments are recorded by type but are not downloaded or transcribed.

## Deploy on Vercel

Vercel receives Telegram updates at `/api/webhook`. A hosted Postgres database stores the messages; Vercel's function filesystem is not used for persistence. The read-only `/api/status` and `/api/messages` endpoints require an API key. The local SQLite archive and exported chat files are excluded from Git.

1. Import this GitHub repository into Vercel. Add a Postgres database through Vercel Marketplace (for example Neon). Ensure it supplies `DATABASE_URL` or `POSTGRES_URL` to the Production environment.
2. Add these Production environment variables, then redeploy:

   | Name | Value |
   | --- | --- |
   | `TELEGRAM_WEBHOOK_SECRET` | A random URL-safe secret, at least 32 characters. |
   | `INSIGHTS_API_KEY` | A different random URL-safe secret, at least 32 characters. |
   | `ALLOWED_CHAT_IDS` | Comma-separated numeric IDs of groups to archive. Get them with `py telegram_insights.py groups`. |

   Generate a secret locally with `py -c "import secrets; print(secrets.token_urlsafe(32))"`. Store each value in Vercel, never in Git. The bot token is only needed locally to register the webhook; do not put it in the repository.
3. Install the Python dependency locally with `py -m pip install -r requirements.txt`. Run `./migrate_to_vercel.ps1` and paste the Postgres connection string at the hidden prompt. This copies the local archive, including imported history, without uploading the export files to Git. Rerunning is safe.
4. After the Production deployment and database are ready, stop the Windows poller: `Stop-ScheduledTask -TaskName TelegramGroupInsights` and `Disable-ScheduledTask -TaskName TelegramGroupInsights`. Then run `./telegram_windows.ps1 set-webhook https://YOUR-PRODUCTION-DOMAIN/api/webhook`. Paste the same webhook secret at the hidden prompt. Inspect delivery with `./telegram_windows.ps1 webhook-info`.
5. Run `./setup_remote_windows.ps1 https://YOUR-PRODUCTION-DOMAIN` and paste `INSIGHTS_API_KEY` at the hidden prompt. The assistant can then query the cloud archive with `./remote_windows.ps1 status` and `./remote_windows.ps1 messages --days 7`.

Telegram retries webhook requests that fail; the database uses the group ID and message ID to avoid duplicates. Keep the webhook secret, API key, bot token, and database URL private. If switching back to local polling, remove the webhook with `./telegram_windows.ps1 delete-webhook`, then re-enable and start the Windows task.

The webhook handles new messages as they arrive. Cron is not needed for message collection. Vercel Hobby cron runs at most once daily, so scheduled reports or checks can be added later without making ingestion depend on cron. See [Vercel's cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing) and [Telegram's webhook documentation](https://core.telegram.org/bots/api#setwebhook).

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
