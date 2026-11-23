# TikTok LIVE – žebříček dárců

TikTok ukazuje žebříček dárců jen v rámci jednoho vysílání. Tahle aplikace sbírá dary
ze **všech vysílání** zvoleného účtu a skládá z nich dlouhodobý žebříček
(celkem / 30 dní / 7 dní / 24 h / poslední LIVE / konkrétní vysílání).

```
 TikTok LIVE ──► collector/ (Python, běží nepřetržitě) ──► Supabase (Postgres)
                                                              │  RPC (jen čtení)
                                              web/ (Netlify) ◄┘
```

| Část | Kde běží | Proč |
|---|---|---|
| `web/` | Netlify | statická stránka, čte ze Supabase přes veřejný klíč |
| `supabase/schema.sql` | Supabase | tabulky + funkce `leaderboard`, `list_streams`, `list_streamers` |
| `collector/` | PC / Raspberry Pi / VPS / Docker | drží WebSocket s LIVE celé hodiny – to serverless (Netlify Functions) neumí |

## 1. Supabase
> Nasazeno: projekt `rkyewweuirnnzrfpazbf` (org „Tiktok“, eu-west-1), migrace `init_gift_leaderboard`.

1. Založ projekt, otevři **SQL Editor** a spusť `supabase/schema.sql` (jde spustit opakovaně).
2. V **Project Settings → API Keys** si poznamenej:
   - **Project URL**
   - **Publishable key** (`sb_publishable_…`, příp. legacy `anon`) → pro web
   - **Secret key** (`sb_secret_…`, příp. legacy `service_role`) → jen pro sběrač, nikdy do webu ani gitu

Zabezpečení: tabulky mají zapnuté RLS bez politik, takže veřejný klíč je nepřečte ani nezmění.
Web volá jen tři read-only funkce. Zapisuje pouze sběrač secret klíčem.

## 2. Netlify
Připoj tohle GitHub repo (Add new site → Import from Git). Build nastavení se načte z `netlify.toml`.
V **Site configuration → Environment variables** nastav:
- `SUPABASE_URL`
- `SUPABASE_PUBLISHABLE_KEY`

Každý push do `main` se nasadí automaticky. Konkrétní účet lze otevřít odkazem `…/?u=ucet`.

## 3. Sběrač
```bash
cd collector
cp .env.example .env        # doplň SUPABASE_URL, SUPABASE_SECRET_KEY, STREAMERS
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python tracker.py           # nebo: python tracker.py @ucet1 @ucet2
```

**Docker:**
```bash
docker build -t tiktok-collector collector
docker run -d --name tiktok-collector --restart unless-stopped --env-file collector/.env tiktok-collector
```

**systemd:** viz `collector/tiktok-collector.service`.

## Poznámky
- Zachytí jen vysílání, během kterých sběrač běží.
- Používá neoficiální knihovnu [TikTokLive](https://github.com/isaackogan/TikTokLive) – TikTok
  oficiální API na LIVE dary nemá a takové čtení je v rozporu s jeho podmínkami. Po změnách na straně
  TikToku může přestat fungovat, dokud knihovna nevydá opravu (`pip install -U TikTokLive`).
- Při omezení požadavků podepisovacím serverem: zdarma klíč na eulerstream.com → `EULER_API_KEY`.
- Dárci se párují podle interního TikTok ID → přejmenování účtu nerozbije součty.
- Série darů (např. Rose ×10) se započítá jednou, až na konci série.
- Výpadek Supabase nevadí: dary se drží ve frontě a odešlou se, jakmile je spojení zpět.
- 💎 = cena daru v mincích podle TikToku (tvůrce z ní reálně dostane zhruba polovinu).
- Web je veřejný na své URL (vyhledávačům je indexace zakázaná přes `X-Robots-Tag`).
