-- TikTok LIVE gift leaderboard – schéma pro Supabase (Postgres)
-- Spusť v Supabase: SQL Editor -> New query -> vložit -> Run. Skript je idempotentní.
-- Pozn.: advisor hlásí 'SECURITY DEFINER function executable by anon' – je to záměr:
-- web smí volat jen tyto tři read-only funkce, tabulky samotné veřejně čitelné nejsou.

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
    diamonds    integer     not null
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

revoke all on function public.list_streamers()                       from public, anon, authenticated;
revoke all on function public.list_streams(text)                     from public, anon, authenticated;
revoke all on function public.leaderboard(text, text, bigint, int)   from public, anon, authenticated;
grant execute on function public.list_streamers()                     to anon, authenticated;
grant execute on function public.list_streams(text)                   to anon, authenticated;
grant execute on function public.leaderboard(text, text, bigint, int) to anon, authenticated;
