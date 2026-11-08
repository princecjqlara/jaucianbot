# Current VEO workspace integration

When `VEO_WORKSPACE_ENABLED=true`, VEO's verified ledger supplies sales, personal commission and team capacity. Jaucian's existing bot and database supply approved VEO groups, observed identities and durable Telegram delivery. Archived receipt text never creates new VEO sales. Legacy polls, ready queues, referrals, freebies and warning automation do not run through the workspace dispatcher.

The rule is 40% when **both personal Closed and Done Deal quotas** are reached on the Philippine date; otherwise 35%. Team quotas do not override the personal rate. VEO calculates the entire day's commission and Jaucian reads that result. The existing authenticated `/api/reports/daily-summary` URL also reads the verified VEO report in workspace mode; it no longer falls back to archived Telegram receipt counts.

`GET /api/veo/workspace?tenant_id=...` requires `VEO_JAUCIAN_SECRET` and the configured `VEO_WORKSPACE_TENANT_ID`. It reads only approved, active VEO groups and returns observed identity candidates and the latest 50 tenant-specific VEO outbox states. Directory coverage is bounded and never described as the complete current membership list. Message bodies, credentials, signed photo URLs and Suno records are excluded.

`resource=identity&chat_id=...&user_id=...` uses the existing bot's read-only `getChatMember` to verify current human membership before the Owner can save an explicit worker mapping. Queued sale delivery rechecks membership immediately before sending, including after a team is remapped. The directory/verification API never sends a message. A declared username is not a verified numeric identity. Membership lookup outages remain retryable; only uncertain message sends require reconciliation.

Jaucian also authenticates to VEO's `/api/workspace/handoff?resource=directory&tenant_id=...` to read the active team/workforce/page directory, numeric mappings, current/tomorrow declarations and commission policy. The directory connection summary reports missing mappings. Existing `report_date` reads and eligible `submission_id` handoffs retain the authoritative financial access. Each service keeps its own database key private.

Use the Owner's **Accounts & Teams** screen in VEO to map actual teams and people to existing Jaucian groups. Missing mappings block delivery; do not guess them from similar display names. Preserve scheduler ownership of Telegram delivery. Test via `py validate_project.py`, then check normal scheduler health; do not dispatch a manual test post.

Remaining differences from the older Jaucian feature set include Telegram attendance-vote ingestion, referral/freebie workflows, workload warnings, multipart large-team reports and a dedicated reconciliation-action screen. Their archived records are retained; they are not implied to be active in the new workspace.
# Automatic Telegram groups and direct team members

VEO members can report directly to their team leader. The same Jaucian bot accepts an expiring `/veolink` command generated in the signed-in VEO profile to prove control of a Telegram identity. The command is verified against current human membership and is not archived. Existing supervisor-verified mappings remain supported.

The tenant-bound `automatic` directory resource checks up to four verified accounts per page across approved, enabled VEO groups. It returns current membership and Telegram administrator status, including explicit unknown lookups. VEO's scheduled sync discovers groups for existing team leaders and managers, then assigns routed Pages to verified members within the same app branch and supervisor permissions. This never promotes a Telegram administrator to an application role, creates an account from a username, moves an existing Page destination, or sends a Telegram message. Apply app migration `202611080007` and deploy both services before relying on the automatic setup status in VEO.

## Automatic fresh inquiry dispatch

The VEO app's scheduled workspace worker owns the editor rotation. Jaucian only reads pending inquiry notices and offers, renders the current authorized context and publishes through the existing outbox. Fresh inquiry notices are acknowledged back to VEO after the Telegram message ID is checkpointed. First offers wait 60 minutes from inquiry arrival by default. Ready lasts until Busy, acceptance or Philippine midnight. A delivered automatic offer allows 15 minutes for acceptance; missed offers rotate to another eligible Ready editor. Each editor has at most one automatic reservation. Automatic dispatch honors each approved group's new-client feature switch and never imports a historical backlog as new work.

The `veo_intake_notice_id` payload shares normal retry and acknowledgment handling. An acknowledged notice retries only its VEO checkpoint, preventing duplicate Telegram posts. Timed offers are queued before new notices so announcements cannot starve acceptance windows. Handovers continue to use the same VEO assignment and preserve existing sellers and commissions.

Jev interruption decisions run in VEO through OpenRouter after migrations `202611080008` and `202611080009`. The exact Jev model must be configured before activation. Reviews never send directly and cannot override manual bot-off controls, an assigned editor, a closed outcome or newer conversation activity.
