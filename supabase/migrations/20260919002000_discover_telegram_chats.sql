create or replace function public.insights_ingest_update(
    p_update jsonb,
    p_allowed_ids bigint[]
) returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_message jsonb;
    v_member jsonb;
    v_chat jsonb;
    v_sender jsonb;
    v_chat_id bigint;
    v_name text;
    v_kind text;
begin
    v_message := coalesce(p_update->'message', p_update->'edited_message');
    v_member := p_update->'my_chat_member';
    v_chat := coalesce(v_message->'chat', v_member->'chat');
    if v_chat is null or v_chat->>'type' not in ('group', 'supergroup') then
        return false;
    end if;
    v_chat_id := (v_chat->>'id')::bigint;
    if v_chat_id is null then
        return false;
    end if;

    insert into public.chats(chat_id, title, chat_type, membership, last_seen_utc)
    values (
        v_chat_id,
        coalesce(nullif(v_chat->>'title', ''), v_chat_id::text),
        v_chat->>'type',
        v_member->'new_chat_member'->>'status',
        now()
    )
    on conflict (chat_id) do update set
        title = excluded.title,
        chat_type = excluded.chat_type,
        membership = coalesce(excluded.membership, public.chats.membership),
        last_seen_utc = excluded.last_seen_utc;

    if not (v_chat_id = any(p_allowed_ids)) then
        return false;
    end if;

    if v_message is null then
        return true;
    end if;

    v_sender := coalesce(v_message->'from', v_message->'sender_chat', '{}'::jsonb);
    v_name := nullif(trim(concat_ws(' ', v_sender->>'first_name', v_sender->>'last_name')), '');
    v_name := coalesce(v_name, v_sender->>'title', v_sender->>'username');
    v_kind := case
        when v_message ? 'photo' then 'photo'
        when v_message ? 'video' then 'video'
        when v_message ? 'document' then 'document'
        when v_message ? 'audio' then 'audio'
        when v_message ? 'voice' then 'voice'
        when v_message ? 'animation' then 'animation'
        when v_message ? 'sticker' then 'sticker'
        when v_message ? 'poll' then 'poll'
        when v_message ? 'location' then 'location'
        when v_message ? 'contact' then 'contact'
        when v_message ? 'text' then 'text'
        else 'other'
    end;

    insert into public.messages(
        chat_id, message_id, sent_utc, edited_utc, author_id,
        author_name, text, content_type, reply_to_message_id,
        thread_id, source
    ) values (
        v_chat_id,
        (v_message->>'message_id')::bigint,
        to_timestamp((v_message->>'date')::double precision),
        case when v_message ? 'edit_date'
            then to_timestamp((v_message->>'edit_date')::double precision)
            else null end,
        nullif(v_sender->>'id', '')::bigint,
        v_name,
        coalesce(v_message->>'text', v_message->>'caption'),
        v_kind,
        nullif(v_message->'reply_to_message'->>'message_id', '')::bigint,
        nullif(v_message->>'message_thread_id', '')::bigint,
        'bot'
    )
    on conflict (chat_id, message_id) do update set
        edited_utc = excluded.edited_utc,
        author_id = excluded.author_id,
        author_name = excluded.author_name,
        text = excluded.text,
        content_type = excluded.content_type,
        reply_to_message_id = excluded.reply_to_message_id,
        thread_id = excluded.thread_id,
        source = 'bot',
        source_file = null
    where public.messages.edited_utc is null
       or (excluded.edited_utc is not null
           and excluded.edited_utc >= public.messages.edited_utc);
    return true;
end;
$$;

notify pgrst, 'reload schema';
