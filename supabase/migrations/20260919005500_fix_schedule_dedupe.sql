drop index if exists public.scheduled_actions_dedupe;
create unique index scheduled_actions_dedupe
    on public.scheduled_actions(dedupe_key);

notify pgrst, 'reload schema';
