alter table public.scheduled_actions
    add column if not exists dedupe_key text;

create unique index if not exists scheduled_actions_dedupe
    on public.scheduled_actions(dedupe_key)
    where dedupe_key is not null;

create table if not exists public.daily_polls (
    poll_id text primary key,
    chat_id bigint not null references public.chats(chat_id),
    thread_id bigint not null,
    work_date date not null,
    telegram_message_id bigint not null,
    created_at timestamptz not null default now(),
    unique (chat_id, work_date)
);

create table if not exists public.daily_poll_answers (
    poll_id text not null references public.daily_polls(poll_id) on delete cascade,
    user_id bigint not null,
    user_name text,
    active boolean not null,
    updated_at timestamptz not null default now(),
    primary key (poll_id, user_id)
);

alter table public.daily_polls enable row level security;
alter table public.daily_poll_answers enable row level security;
revoke all on public.daily_polls, public.daily_poll_answers from public, anon, authenticated;
grant select, insert, update on public.daily_polls, public.daily_poll_answers to service_role;

create or replace function public.insights_register_daily_poll(
    p_poll_id text,
    p_chat_id bigint,
    p_thread_id bigint,
    p_work_date date,
    p_telegram_message_id bigint
) returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
begin
    insert into public.daily_polls(poll_id, chat_id, thread_id, work_date, telegram_message_id)
    values (p_poll_id, p_chat_id, p_thread_id, p_work_date, p_telegram_message_id)
    on conflict (chat_id, work_date) do nothing;
    return true;
end;
$$;

create or replace function public.insights_ingest_poll_answer(
    p_update jsonb,
    p_allowed_ids bigint[]
) returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_answer jsonb := p_update->'poll_answer';
    v_poll_id text;
    v_chat_id bigint;
    v_user jsonb;
    v_user_id bigint;
    v_user_name text;
    v_active boolean;
begin
    if v_answer is null then
        return false;
    end if;
    v_poll_id := v_answer->>'poll_id';
    select p.chat_id into v_chat_id
      from public.daily_polls p
     where p.poll_id = v_poll_id;
    if v_chat_id is null or not (v_chat_id = any(p_allowed_ids)) then
        return false;
    end if;
    v_user := v_answer->'user';
    v_user_id := (v_user->>'id')::bigint;
    if v_user_id is null then
        return false;
    end if;
    v_user_name := nullif(trim(concat_ws(' ', v_user->>'first_name', v_user->>'last_name')), '');
    v_user_name := coalesce(v_user_name, v_user->>'username', v_user_id::text);
    select exists (
        select 1
          from jsonb_array_elements_text(coalesce(v_answer->'option_ids', '[]'::jsonb)) as choices(value)
         where value::integer = 0
    ) into v_active;
    insert into public.daily_poll_answers(poll_id, user_id, user_name, active, updated_at)
    values (v_poll_id, v_user_id, v_user_name, v_active, now())
    on conflict (poll_id, user_id) do update set
        user_name = excluded.user_name,
        active = excluded.active,
        updated_at = excluded.updated_at;
    return true;
end;
$$;

create or replace function public.insights_daily_poll_counts(
    p_work_date date,
    p_allowed_ids bigint[]
) returns table (
    chat_id bigint,
    poll_id text,
    active_workers bigint,
    total_responses bigint
)
language sql
stable
security invoker
set search_path = ''
as $$
    select p.chat_id, p.poll_id,
           count(a.user_id) filter (where a.active),
           count(a.user_id)
      from public.daily_polls p
      left join public.daily_poll_answers a using (poll_id)
     where p.work_date = p_work_date
       and p.chat_id = any(p_allowed_ids)
     group by p.chat_id, p.poll_id;
$$;

revoke all on function public.insights_register_daily_poll(text, bigint, bigint, date, bigint) from public, anon, authenticated;
revoke all on function public.insights_ingest_poll_answer(jsonb, bigint[]) from public, anon, authenticated;
revoke all on function public.insights_daily_poll_counts(date, bigint[]) from public, anon, authenticated;
grant execute on function public.insights_register_daily_poll(text, bigint, bigint, date, bigint) to service_role;
grant execute on function public.insights_ingest_poll_answer(jsonb, bigint[]) to service_role;
grant execute on function public.insights_daily_poll_counts(date, bigint[]) to service_role;

notify pgrst, 'reload schema';
