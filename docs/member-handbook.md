# Member handbook
## Veo teams + Suno / Trabawho

Simple instructions for working together, owning clients, and recording your work.

Updated 14 October 2026. All work schedules and deadlines use Philippine time (PHT).

### Start your workday

- Open your own team's announcements and check any updates.
- Answer the dated availability polls. There are three parts covering all 24 hours. Select every hour you can work. Choose "Not available in these hours" for a block you cannot work; it overrides other choices in that block.
- In NEW CLIENTS, tap **Ready** or send `/ready` once when you can accept new clients. This stays on until `/busy` or a missed acceptance deadline. Keep your hourly polls accurate too.
- Review each offer before accepting. Wait for the bot's confirmation naming you as the editor before contacting the client.
- Tap **Busy** or send `/busy` before a break, leaving, or when your workload is full. Update your polls if your available hours change.

### Work together: one client, one confirmed editor

- Respect the confirmed editor shown in the latest client list. Do not message or work on another editor's client without an accepted handover.
- Check the existing client conversation before sending anything. Avoid duplicate messages, repeated questions, and duplicate freebies.
- Keep the client updated about progress, missing information, and delays. If you cannot finish, notify your team lead and arrange a handover early.
- Use your own team's topics and page. A Suno page and a Veo page may have similar names but are separate workflows.
- Keep client information inside the team and its approved work tools. Share only what the receiving editor needs.

**Remember:** `/busy` stops automatic new-client offers. It does not release your accepted clients, cancel a song deadline, or complete a freebie task. Existing responsibilities remain yours until properly finished or handed over.

<!-- page -->

# Getting and accepting clients

### What to do when an offer arrives

- Read the client name, page, brief, token, and deadline in the latest offer.
- If it is reserved for you, tap **Accept client** or send `WORKING A1B2C3D4` in NEW CLIENTS. Replace the sample token with the actual token in your offer.
- New offers allow **15 minutes from delivery** to accept. A reminder comes after 10 minutes. This is the acceptance window, not a deadline to finish the work. Older delivered offers keep their printed deadlines.
- Wait for the bot to confirm the owner. You may then contact the client and begin. Successful acceptance keeps your Ready queue running; you do not need to send `/ready` after every client.
- If the offer is unanswered, the bot pauses your automatic offers and sends a turn-ended notice before opening TAKE or arranging another assignment. Send `/ready` again when you are available.

### Taking an open offer

Tap **Take client** or send `TAKE A1B2C3D4` only during the announced 10-minute OPEN TAKE window. You must be eligible through the current hourly availability poll or an active Ready queue. The first eligible claim wins; wait for owner confirmation.

Never use an old offer to take a client back. Older offers keep their printed deadlines. The latest valid owner confirmation controls who handles the client.

### Read the client list

Send `/clients` or tap **Client list**. Use **Refresh**, **Next**, and **Previous** to browse. The overview updates automatically; it may take a short time for a scheduled update to appear.

| Status | What it means for you |
| --- | --- |
| Available | Waiting for assignment. Do not self-assign from this label. |
| Reserved | Only the named editor may accept this offer. |
| Available to TAKE | Use the latest OPEN TAKE offer and its deadline. |
| Assigned | The confirmed editor owns the client. It is unavailable to others. |
| Handover pending | The current editor still owns it while the chosen teammate decides. |
| Awaiting reassignment | Wait for the bot's next valid offer. |

The list covers tracked assignments and eligible leads. If the conversation already has another editor or conflicts with the list, stop and ask your team lead to review ownership.

<!-- page -->

# Handing a client to another editor

### Request a handover

- Only the current confirmed editor can start it. In NEW CLIENTS, tap **Hand over** on your owner confirmation or beside your client in the list.
- Choose a teammate in the same group. The receiving editor must be a current member of that team.
- To include a note, reply to the receiving member's Telegram message with `/handover A1B2C3D4` followed by the context. Use the actual client token.
- Explain what is finished, what is pending, the client's last request, where the files are, and any delivery promise or deadline.

### Accept, decline, or cancel

- The chosen teammate reviews the brief and note, then taps **Accept handover** or **Decline**.
- The requesting editor can tap **Cancel handover**. Resolve a pending request before choosing someone else.
- Until acceptance is confirmed, the original editor remains responsible. The receiver must not start messaging the client during a pending request.
- After acceptance, the new editor takes over. The previous editor stops handling that client and passes the files and context. Keep the same client token.

**Suno deadline:** a handover does not restart the original 24-hour song delivery clock.

### Copyable example

Reply to the receiving editor's message in NEW CLIENTS:

```text
/handover A1B2C3D4
Finished: client brief confirmed; first draft ready.
Pending: client wants a shorter ending.
Files: [approved workspace link]
Promise: final version today by 6 PM PHT.
```

### Text alternatives

| Who | Command in NEW CLIENTS |
| --- | --- |
| Current editor | `/handover A1B2C3D4` to open the teammate picker |
| Chosen receiver | `/accept_handover A1B2C3D4` or `/decline_handover A1B2C3D4` |
| Requesting editor | `/cancel_handover A1B2C3D4` |

The handover buttons transfer accepted new-client assignments. They do not transfer a separate FREEBIE task or move a client across teams. Ask your team lead to arrange those cases and confirm who is responsible.

<!-- page -->

# Veo teams: freebies and client updates

### Complete a freebie task

- Follow the assignment in your team's FREEBIES topic. These tasks use hourly availability and their own token; Ready controls new clients separately.
- Review the conversation, prepare a suitable freebie, and send it to the client. Do not send a freebie another editor already sent.
- After sending it, reply to the task with `FREEBIE SENT A1B2C3D4` using that task's actual token. Only the assigned member can confirm it.
- If you cannot complete it, tell your team lead. Busy alone does not close the task. A completed freebie gives that client a 14-day break before another is assigned.

### Done Deals: post once, the bot summarizes

After a client pays, post your usual readable sale record in **DONE DEALS**. No sale command, client-token code, or Done Deal button is needed.

```text
Client: Maria Santos
Page: Vista Media Agency
Price Deal: 650
```

Use the actual client, page, and deal amount. Keep tips, revisions and down payments separate. A payment screenshot needs readable text for the bot to include its amount.

The bot reads these posts and adds the **deal count, recorded total, and average amount** to DAILY REPORTS at daily closeout. Identifiable duplicates count once. Unreadable or conflicting posts are flagged for review. These are observed sale amounts; earnings and combined recruiter-share calculations remain on hold.

### Keep close-deal updates simple

Use **CLOSE DEALS** for your close-deal record or daily summary. Enter your actual count and the page assigned to your team. Do not repeat individual entries and summaries as separate new deals.

```text
Page name: Vista Media Agency
Close Deal: 2
```

Replace both example values. Your team lead can help with the page name or a correction. Close-deal updates remain separate from client acceptance and handover.

### What you still need to do

- Tap **Ready** in NEW CLIENTS when able to take clients; tap **Busy** before a break or leaving.
- Accept the offer within 15 minutes and wait for owner confirmation.
- Use **Client list** to check who handles each client.
- Keep your client informed and use **Hand over** if another teammate needs to take over.
- Confirm a freebie after sending it. Keep hourly availability polls accurate.

<!-- page -->

# Suno / Trabawho: songs and receipts

Suno work happens in **Trabawho?**. Use NEW CLIENTS for assignment and handover, SONGS for delivery confirmation, and RECEIPTS for payment records.

### From acceptance to delivery

- Accept through the shared client process and wait for confirmation naming you as the owner.
- Read the Suno brief. Some briefs may still have missing details; collect them from the client before finalizing the song.
- Prepare and send the song to the client **today and within 24 hours of the original acceptance**. Follow the deadline shown by the bot. A new day or handover does not reset it.
- After the client receives the song, post `SONG SENT A1B2C3D4` in SONGS using the actual client token. The current assigned editor must confirm it.
- Wait for the bot to confirm the delivery. Uploading a song into Telegram alone does not mark it as delivered to the client.

Reminders show the remaining time and continue until delivery is recorded. If you will miss a deadline, tell the client and your team lead early. Arrange a handover if needed; do not wait for the overdue reminder.

### Record a payment in RECEIPTS

Post readable text with the correct Suno page and the approved amounts. Attach any required payment evidence. The amount **outside parentheses** is gross; the amount **inside parentheses** is recorded member earnings.

```text
HIRAYA
699 (140)
100 (50) tip
```

This example records 799 gross, 190 member earnings, and a labeled 100 tip with a 50/50 tip share. It is a format example, not a promise that your sale has those amounts. Use the split approved for the actual transaction.

Label tips clearly. Put each amount on its own line. Do not add a total/subtotal line or combine a corrected receipt with a new receipt. Ask your lead to review corrections, refunds, unpaid items, or disputed amounts. An image-only receipt cannot be read by the report.

**Receipt recorded and song delivered are separate actions.** A receipt does not stop song reminders; SONG SENT does not record payment. Complete both when they apply.

<!-- page -->

# Find your team's topics

Tap a link in the PDF to open Telegram. You need membership in that group. Use your own team row; do not put another team's client or sale in these topics.

| Veo team | Availability | New clients | Freebies | Close deals | Done deals | News |
| --- | --- | --- | --- | --- | --- | --- |
| Veo | [Open](https://t.me/c/3647732254/25893) | [Open](https://t.me/c/3647732254/27622) | [Open](https://t.me/c/3647732254/26249) | [Open](https://t.me/c/3647732254/1132) | [Open](https://t.me/c/3647732254/1135) | [Open](https://t.me/c/3647732254/248) |
| Veo Ollie | [Open](https://t.me/c/4423057797/4536) | [Open](https://t.me/c/4423057797/5758) | [Open](https://t.me/c/4423057797/4811) | [Open](https://t.me/c/4423057797/16) | [Open](https://t.me/c/4423057797/17) | [Open](https://t.me/c/4423057797/4794) |
| Veo Jessa | [Open](https://t.me/c/3962888977/3050) | [Open](https://t.me/c/3962888977/3725) | [Open](https://t.me/c/3962888977/3240) | [Open](https://t.me/c/3962888977/6) | [Open](https://t.me/c/3962888977/7) | [Open](https://t.me/c/3962888977/5) |
| Veo Jel | [Open](https://t.me/c/4461399292/2917) | [Open](https://t.me/c/4461399292/4180) | [Open](https://t.me/c/4461399292/3003) | [Open](https://t.me/c/4461399292/8) | [Open](https://t.me/c/4461399292/6) | [Open](https://t.me/c/4461399292/2) |
| Veo Lyn | [Open](https://t.me/c/4364797420/16) | [Open](https://t.me/c/4364797420/20) | [Open](https://t.me/c/4364797420/22) | [Open](https://t.me/c/4364797420/19) | [Open](https://t.me/c/4364797420/15) | [Open](https://t.me/c/4364797420/18) |

Veo pages: Veo - Azshinari / SAMA KANA MEDIA; Ollie - Vista Media Agency / Loki Media; Jessa - Manawari Studios; Jel - Onset Media Agency.

**Veo Lyn:** page setup is pending as of this handbook's date. Polls, reminders, and client controls are live. Client and freebie assignments stay paused until its page is connected. [General chat](https://t.me/c/4364797420/1).

| Suno / Trabawho topic | Link |
| --- | --- |
| Availability polls | [Open](https://t.me/c/2894511895/7581) |
| NEW CLIENTS: accept, view owners, hand over | [Open](https://t.me/c/2894511895/7673) |
| SONGS: confirm delivery to the client | [Open](https://t.me/c/2894511895/7692) |
| RECEIPTS: record payment | [Open](https://t.me/c/2894511895/4) |
| Announcements / General | [Announcements](https://t.me/c/2894511895/16) / [General](https://t.me/c/2894511895/1) |

### If something goes wrong

For an old button, expired offer, duplicate owner, wrong page, or missing confirmation: use the latest message, check `/clients`, and send the message link plus client token to your team lead. Do not keep claiming or contact the client while ownership is unclear. `/clienthelp` opens the guide in NEW CLIENTS.

Buttons not responding? Type `/ready`, `/busy`, `WORKING TOKEN`, `TAKE TOKEN` in NEW CLIENTS. Use your personal Telegram account, not anonymous-admin mode. Only the named editor can accept a reserved client; only the chosen teammate can accept a handover.

If you are a new recruit, choose the person who invited you in the [recruiter poll topic](https://t.me/c/3555676168/510) when ready to start. Ask an admin if your inviter is missing.

**Before leaving:** send `/busy`, update your polls, update your clients, confirm completed work, and arrange any unfinished handovers. You remain responsible until the bot confirms a new owner.
