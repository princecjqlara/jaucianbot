-- Apply to the CRM project, not the Telegram archive or the Suno project.
-- CONCURRENTLY requires an autocommit connection, outside a transaction.
-- This matches the read-only assignment guard and does not change client rows.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_outbound_human_ownership_contact
ON public.outbound_message_events (page_id, contact_id, id)
WHERE actor_user_id IS NOT NULL
  AND source_type NOT IN ('campaign', 'chatbot', 'workflow');
