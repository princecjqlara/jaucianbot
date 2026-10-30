-- Internal ownership of automatic contact permission changes; never a send queue.
CREATE TABLE IF NOT EXISTS public.telegram_client_standby (
    contact_id uuid PRIMARY KEY REFERENCES public.contacts(id) ON DELETE CASCADE,
    page_id uuid NOT NULL REFERENCES public.pages(id) ON DELETE CASCADE,
    original_enabled boolean NOT NULL,
    resumed_epoch bigint NOT NULL,
    active boolean NOT NULL DEFAULT true,
    enabled_at timestamptz NOT NULL,
    released_at timestamptz
);
ALTER TABLE public.telegram_client_standby ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.telegram_client_standby FROM anon, authenticated;
