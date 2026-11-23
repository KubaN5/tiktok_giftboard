-- TikTok LIVE gift leaderboard – schéma pro Supabase (Postgres)
-- Spusť v Supabase: SQL Editor -> New query -> vložit -> Run. Skript je idempotentní.
-- Pozn.: advisor hlásí 'SECURITY DEFINER function executable by anon' – je to záměr:
-- web smí volat jen tyto read-only funkce, tabulky samotné veřejně čitelné nejsou.

-- ---------------------------------------------------------------- tabulky
create table if not exists public.streamers (
    name        text primary key,
    created_at  timestamptz not null default now()
);

create table if not exists public.streams (
    id          bigint generated always as identity primary key,
    streamer    text        not null references public.streamers(name) on delete cascade,
    room_id     text        not null,
    started_at  timestamptz not null default now(),
    ended_at    timestamptz,
    unique (streamer, room_id)
);

create table if not exists public.gifts (
    id          bigint generated always as identity primary key,
    stream_id   bigint      not null references public.streams(id) on delete cascade,
    streamer    text        not null,
    ts          timestamptz not null default now(),
    user_id     text        not null,          -- interní TikTok ID (stabilní i po přejmenování)
    unique_id   text,                          -- @handle v době daru
    nickname    text,
    gift_id     bigint,
    gift_name   text,
    count       integer     not null,
    diamonds    integer     not null,
    msg_id      text        unique             -- TikTok msg_id: brání dvojímu započtení při 2 sběračích
);
alter table public.gifts add column if not exists msg_id text;  -- pro starší instalace
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'gifts_msg_id_key') then
    alter table public.gifts add constraint gifts_msg_id_key unique (msg_id);
  end if;
end $$;

-- Heartbeat sběračů: každý běžící sběrač sem každých ~30 s zapíše, že žije (web ukazuje online/offline)
create table if not exists public.collectors (
    name            text primary key,              -- COLLECTOR_NAME nebo název počítače
    started_at      timestamptz not null default now(),
    last_seen       timestamptz not null default now(),
    streamers       text[]      not null default '{}',  -- hlídané účty
    live            text[]      not null default '{}',  -- účty, jejichž LIVE právě sbírá
    recording       text[]      not null default '{}',  -- účty, jejichž LIVE právě nahrává
    record_enabled  boolean     not null default false
);

create index if not exists ix_gifts_streamer_ts on public.gifts (streamer, ts);
create index if not exists ix_gifts_stream      on public.gifts (stream_id);
create index if not exists ix_gifts_user_ts     on public.gifts (user_id, ts desc);
create index if not exists ix_streams_streamer  on public.streams (streamer, started_at desc);

-- ---------------------------------------------------------------- zabezpečení
-- RLS zapnuté bez politik = přes veřejný (anon/publishable) klíč nejdou tabulky číst ani měnit.
-- Web čte jen přes funkce níže. Sběrač zapisuje secret/service_role klíčem, který RLS obchází.
alter table public.streamers enable row level security;
alter table public.streams   enable row level security;
alter table public.gifts     enable row level security;
alter table public.collectors enable row level security;

-- Sběrač zapisuje secret klíčem = role service_role. Novější projekty jí práva k tabulkám
-- nepřidělují automaticky (jinak: "permission denied for table streamers", 403).
grant usage on schema public to service_role;
grant select, insert, update on public.streamers, public.streams, public.gifts to service_role;
grant select, insert, update on public.collectors to service_role;
grant usage, select on all sequences in schema public to service_role;

-- ---------------------------------------------------------------- funkce pro web
create or replace function public.list_streamers()
returns table (name text, live boolean)
language sql stable security definer set search_path = public as $$
    select s.name,
           exists (select 1 from streams st where st.streamer = s.name and st.ended_at is null)
    from streamers s
    order by s.name;
$$;

create or replace function public.list_streams(p_streamer text)
returns table (id bigint, started_at timestamptz, ended_at timestamptz, diamonds bigint, gifters bigint)
language sql stable security definer set search_path = public as $$
    select st.id, st.started_at, st.ended_at,
           coalesce(sum(g.diamonds), 0)::bigint,
           count(distinct g.user_id)::bigint
    from streams st
    left join gifts g on g.stream_id = st.id
    where st.streamer = lower(ltrim(p_streamer, '@'))
    group by st.id
    order by st.started_at desc
    limit 200;
$$;

-- p_period: all | 24h | 7d | 30d | last (poslední vysílání); p_stream_id má přednost
create or replace function public.leaderboard(
    p_streamer  text,
    p_period    text   default 'all',
    p_stream_id bigint default null,
    p_limit     int    default 100
)
returns table (rank bigint, user_id text, unique_id text, nickname text,
               diamonds bigint, gifts bigint, streams bigint, last_ts timestamptz)
language sql stable security definer set search_path = public as $$
    with params as (
        select lower(ltrim(p_streamer, '@')) as streamer,
               coalesce(
                   p_stream_id,
                   case when p_period = 'last' then
                       coalesce((select id from streams
                                 where streamer = lower(ltrim(p_streamer, '@'))
                                 order by started_at desc limit 1), -1)
                   end
               ) as stream_id,
               case p_period
                   when '24h' then now() - interval '24 hours'
                   when '7d'  then now() - interval '7 days'
                   when '30d' then now() - interval '30 days'
               end as since
    ),
    agg as (
        select g.user_id,
               sum(g.diamonds)::bigint            as diamonds,
               sum(g.count)::bigint               as gifts,
               count(distinct g.stream_id)::bigint as streams,
               max(g.ts)                          as last_ts
        from gifts g, params p
        where g.streamer = p.streamer
          and (p.stream_id is null or g.stream_id = p.stream_id)
          and (p.stream_id is not null or p.since is null or g.ts >= p.since)
        group by g.user_id
    ),
    latest as (   -- poslední známý @handle a přezdívka
        select distinct on (g.user_id) g.user_id, g.unique_id, g.nickname
        from gifts g join agg a using (user_id)
        order by g.user_id, g.ts desc
    )
    select row_number() over (order by a.diamonds desc, a.last_ts),
           a.user_id, l.unique_id, l.nickname,
           a.diamonds, a.gifts, a.streams, a.last_ts
    from agg a join latest l using (user_id)
    order by a.diamonds desc, a.last_ts
    limit least(greatest(p_limit, 1), 500);
$$;

-- Stav sběračů pro web; online = heartbeat za poslední 2 minuty
create or replace function public.collector_status()
returns table (name text, online boolean, last_seen timestamptz, started_at timestamptz,
               streamers text[], live text[], recording text[], record_enabled boolean)
language sql stable security definer set search_path = public as $$
    select c.name, c.last_seen > now() - interval '2 minutes', c.last_seen, c.started_at,
           c.streamers, c.live, c.recording, c.record_enabled
    from collectors c
    where c.last_seen > now() - interval '30 days'
    order by c.last_seen desc;
$$;

revoke all on function public.list_streamers()                       from public, anon, authenticated;
revoke all on function public.list_streams(text)                     from public, anon, authenticated;
revoke all on function public.leaderboard(text, text, bigint, int)   from public, anon, authenticated;
revoke all on function public.collector_status()                     from public, anon, authenticated;
grant execute on function public.list_streamers()                     to anon, authenticated;
grant execute on function public.list_streams(text)                   to anon, authenticated;
grant execute on function public.leaderboard(text, text, bigint, int) to anon, authenticated;
grant execute on function public.collector_status()                   to anon, authenticated;

-- ---------------------------------------------------------------- sběrače u jiných lidí (tokeny)
-- Cizí sběrač nedostane secret klíč. Dostane jen veřejný (publishable) klíč + vlastní token a zapisuje
-- výhradně přes funkce collector_* níže: umí přidat vysílání, dary a heartbeat, nic nečte, nemění ani nemaže.
-- Token se ukládá jen jako SHA-256 hash. Správa (jen SQL Editor / secret klíč, ne veřejně):
--   select public.create_collector_token('Petr', array['a_d_e_ll_o_ff_i_c_i_a_l_']);  -- vrátí token (jen jednou)
--   select public.revoke_collector_token('Petr');                                       -- zablokuje
--   delete from public.gifts where collector = 'Petr';                                  -- smaže jeho dary
create table if not exists public.collector_tokens (
    name        text primary key,                 -- jméno sběrače na webu
    token_hash  text not null unique,             -- sha256(token) hex
    streamers   text[],                           -- povolené účty; null = libovolný
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz
);
alter table public.collector_tokens enable row level security;

alter table public.gifts add column if not exists collector text;  -- kdo dar zapsal (jen u tokenových sběračů)

-- Ověří token (a volitelně povolený účet); vrátí jméno sběrače. Jen pro interní použití.
create or replace function public._collector_auth(p_token text, p_streamer text default null)
returns text
language plpgsql stable security definer set search_path = public as $$
declare t collector_tokens;
begin
    select * into t from collector_tokens
    where token_hash = encode(sha256(convert_to(coalesce(p_token, ''), 'UTF8')), 'hex')
      and revoked_at is null;
    if not found then
        raise exception 'Neplatný nebo zablokovaný token sběrače' using errcode = '28000';
    end if;
    if p_streamer is not null and t.streamers is not null and not (p_streamer = any (t.streamers)) then
        raise exception 'Sběrač % nemá povolený účet @%', t.name, p_streamer using errcode = '42501';
    end if;
    return t.name;
end $$;

-- Při startu: ověří token a účty, založí účty pro web; vrátí jméno sběrače.
create or replace function public.collector_register(p_token text, p_streamers text[])
returns text
language plpgsql volatile security definer set search_path = public as $$
declare n text; s text;
begin
    n := _collector_auth(p_token);
    foreach s in array coalesce(p_streamers, '{}') loop
        s := lower(ltrim(s, '@'));
        perform _collector_auth(p_token, s);
        insert into streamers(name) values (s) on conflict do nothing;
    end loop;
    return n;
end $$;

-- Začátek / znovupřipojení k vysílání; vrátí id vysílání.
create or replace function public.collector_open_stream(p_token text, p_streamer text, p_room_id text)
returns bigint
language plpgsql volatile security definer set search_path = public as $$
declare s text := lower(ltrim(p_streamer, '@')); sid bigint;
begin
    perform _collector_auth(p_token, s);
    insert into streamers(name) values (s) on conflict do nothing;
    insert into streams(streamer, room_id) values (s, p_room_id)
    on conflict (streamer, room_id) do update set ended_at = null
    returning id into sid;
    return sid;
end $$;

create or replace function public.collector_close_stream(p_token text, p_stream_id bigint)
returns void
language plpgsql volatile security definer set search_path = public as $$
declare s text;
begin
    select streamer into s from streams where id = p_stream_id;
    if s is null then return; end if;
    perform _collector_auth(p_token, s);
    update streams set ended_at = now() where id = p_stream_id and ended_at is null;
end $$;

create or replace function public.collector_heartbeat(
    p_token text, p_started_at timestamptz, p_streamers text[],
    p_live text[], p_recording text[], p_record_enabled boolean)
returns void
language plpgsql volatile security definer set search_path = public as $$
declare n text;
begin
    n := _collector_auth(p_token);
    insert into collectors(name, started_at, last_seen, streamers, live, recording, record_enabled)
    values (n, coalesce(p_started_at, now()), now(), coalesce(p_streamers, '{}'),
            coalesce(p_live, '{}'), coalesce(p_recording, '{}'), coalesce(p_record_enabled, false))
    on conflict (name) do update set
        started_at = excluded.started_at, last_seen = now(), streamers = excluded.streamers,
        live = excluded.live, recording = excluded.recording, record_enabled = excluded.record_enabled;
end $$;

-- Dávka darů (max 500). Dar se zapíše jen k vysílání povoleného účtu; duplicitní msg_id se přeskočí.
-- Vrátí počet nově zapsaných darů.
create or replace function public.collector_add_gifts(p_token text, p_gifts jsonb)
returns integer
language plpgsql volatile security definer set search_path = public as $$
declare n text; t collector_tokens; cnt integer;
begin
    n := _collector_auth(p_token);
    select * into t from collector_tokens where name = n;
    if jsonb_typeof(p_gifts) <> 'array' or jsonb_array_length(p_gifts) > 500 then
        raise exception 'p_gifts musí být pole s nejvýš 500 dary' using errcode = '22023';
    end if;
    insert into gifts(stream_id, streamer, ts, user_id, unique_id, nickname,
                      gift_id, gift_name, count, diamonds, msg_id, collector)
    select g.stream_id, st.streamer, least(coalesce(g.ts, now()), now()), g.user_id,
           left(g.unique_id, 100), left(g.nickname, 200), g.gift_id, left(g.gift_name, 100),
           g.count, g.diamonds, g.msg_id, n
    from jsonb_to_recordset(p_gifts) as g(stream_id bigint, streamer text, ts timestamptz, user_id text,
             unique_id text, nickname text, gift_id bigint, gift_name text,
             count integer, diamonds integer, msg_id text)
    join streams st on st.id = g.stream_id and st.streamer = lower(ltrim(g.streamer, '@'))
    where g.user_id is not null and g.count between 1 and 100000 and g.diamonds between 0 and 100000000
      and (t.streamers is null or st.streamer = any (t.streamers))
    on conflict (msg_id) do nothing;
    get diagnostics cnt = row_count;
    return cnt;
end $$;

-- Správa tokenů – jen pro vlastníka (SQL Editor / secret klíč), ne pro web ani cizí sběrač.
create or replace function public.create_collector_token(p_name text, p_streamers text[] default null)
returns text
language plpgsql volatile security definer set search_path = public as $$
declare tok text; n text := btrim(p_name);
begin
    if n is null or n = '' or length(n) > 60 then
        raise exception 'Jméno sběrače: 1–60 znaků';
    end if;
    if exists (select 1 from collectors c where c.name = n)
       and not exists (select 1 from collector_tokens ct where ct.name = n) then
        raise exception 'Jméno % už používá jiný sběrač', n;
    end if;
    tok := 'gbc_' || replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '');
    insert into collector_tokens(name, token_hash, streamers, revoked_at)
    values (n, encode(sha256(convert_to(tok, 'UTF8')), 'hex'),
            case when p_streamers is not null then coalesce(
                (select array_agg(lower(ltrim(x, '@'))) from unnest(p_streamers) x), '{}') end, null)
    on conflict (name) do update set token_hash = excluded.token_hash, streamers = excluded.streamers,
                                     created_at = now(), revoked_at = null;
    return tok;
end $$;

create or replace function public.revoke_collector_token(p_name text)
returns boolean
language sql volatile security definer set search_path = public as $$
    with u as (update collector_tokens set revoked_at = now()
               where name = p_name and revoked_at is null returning 1)
    select exists (select 1 from u);
$$;

-- Supabase dává nové funkce automaticky k dispozici anon/authenticated → vše nejdřív odebrat.
revoke all on function public._collector_auth(text, text)                        from public, anon, authenticated;
revoke all on function public.collector_register(text, text[])                   from public, anon, authenticated;
revoke all on function public.collector_open_stream(text, text, text)            from public, anon, authenticated;
revoke all on function public.collector_close_stream(text, bigint)               from public, anon, authenticated;
revoke all on function public.collector_heartbeat(text, timestamptz, text[], text[], text[], boolean)
                                                                                  from public, anon, authenticated;
revoke all on function public.collector_add_gifts(text, jsonb)                   from public, anon, authenticated;
revoke all on function public.create_collector_token(text, text[])               from public, anon, authenticated;
revoke all on function public.revoke_collector_token(text)                       from public, anon, authenticated;
grant execute on function public.collector_register(text, text[])                to anon;
grant execute on function public.collector_open_stream(text, text, text)         to anon;
grant execute on function public.collector_close_stream(text, bigint)            to anon;
grant execute on function public.collector_heartbeat(text, timestamptz, text[], text[], text[], boolean) to anon;
grant execute on function public.collector_add_gifts(text, jsonb)                to anon;
