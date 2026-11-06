# Current VEO workspace integration

When `VEO_WORKSPACE_ENABLED=true`, VEO's verified ledger supplies sales, personal commission and team capacity. Jaucian's existing bot and database supply approved VEO groups, observed identities and durable Telegram delivery. Archived receipt text never creates new VEO sales. Legacy polls, ready queues, referrals, freebies and warning automation do not run through the workspace dispatcher.

The rule is 40% when **both personal Closed and Done Deal quotas** are reached on the Philippine date; otherwise 35%. Team quotas do not override the personal rate. VEO calculates the entire day's commission and Jaucian reads that result.

`GET /api/veo/workspace?tenant_id=...` requires `VEO_JAUCIAN_SECRET` and the configured `VEO_WORKSPACE_TENANT_ID`. It reads only approved, active VEO groups and returns observed identity candidates and the latest 50 tenant-specific VEO outbox states. Directory coverage is bounded and never described as the complete current membership list. Message bodies, credentials, signed photo URLs and Suno records are excluded.

`resource=identity&chat_id=...&user_id=...` uses the existing bot's read-only `getChatMember` to verify current human membership before the Owner can save an explicit worker mapping. Queued sale delivery rechecks membership immediately before sending, including after a team is remapped. The directory/verification API never sends a message. A declared username is not a verified numeric identity. Membership lookup outages remain retryable; only uncertain message sends require reconciliation.

Jaucian also authenticates to VEO's `/api/workspace/handoff?resource=directory&tenant_id=...` to read the active team/workforce/page directory, numeric mappings, current/tomorrow declarations and commission policy. The directory connection summary reports missing mappings. Existing `report_date` reads and eligible `submission_id` handoffs retain the authoritative financial access. Each service keeps its own database key private.

Use the Owner's **Accounts & Teams** screen in VEO to map actual teams and people to existing Jaucian groups. Missing mappings block delivery; do not guess them from similar display names. Preserve scheduler ownership of Telegram delivery. Test via `py validate_project.py`, then check normal scheduler health; do not dispatch a manual test post.

Remaining differences from the older Jaucian feature set include Telegram attendance-vote ingestion, automatic offer/assignment queues, referral/freebie workflows, workload warnings, multipart large-team reports and a dedicated reconciliation-action screen. Their archived records are retained; they are not implied to be active in the new workspace.
