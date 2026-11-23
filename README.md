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
> Nasazeno: projekt `tiktok-giftboard` → https://tiktok-giftboard.netlify.app (proměnné nastavené).
> Web je chráněný přihlášením do Netlify týmu (Visitor access) – vypnutelné v Project configuration → Access & security.

Připoj tohle GitHub repo (Add new site → Import from Git). Build nastavení se načte z `netlify.toml`.
V **Site configuration → Environment variables** nastav:
- `SUPABASE_URL`
- `SUPABASE_PUBLISHABLE_KEY`

Každý push do `main` se nasadí automaticky. Konkrétní účet lze otevřít odkazem `…/?u=ucet`.

## 3. Sběrač
Ve složce `collector/` vytvoř `.env` podle `.env.example`. URL a účet jsou předvyplněné, doplň jen
`SUPABASE_SECRET_KEY` (Supabase → Project Settings → API Keys → Secret key).

**Windows PC:** stáhni repo, dvojklik na `collector/start.bat`. Při prvním spuštění vytvoří `.env`,
otevře ho v Notepadu, pak založí `.venv` a spustí sběrač. Potřeba Python 3.10+ (python.org).

**Raspberry Pi / Linux s Dockerem:**
```bash
git clone https://github.com/KubaN5/tiktok_giftboard && cd tiktok_giftboard/collector
cp .env.example .env && nano .env          # doplň secret klíč
docker compose up -d --build               # běží na pozadí, sám se restartuje
docker compose logs -f                     # logy
```
**Bez Dockeru:** `./start.sh` (případně jako služba přes `tiktok-collector.service`).

**Přepnutí PC → Raspberry:** spusť sběrač na Raspberry, ověř v logu „Hlídám: …“, pak vypni PC.
Překryv nevadí – každý dar má TikTok `msg_id` s unikátním klíčem v DB, takže se nezapočítá dvakrát.
Na Raspberry stačí 64bit Raspberry Pi OS (image `python:3.12-slim` je i pro ARM).

## 4. Nahrávání videa (volitelné)
Sběrač umí každé zachycené vysílání uložit jako video. Stačí mít **ffmpeg** a v `collector/.env` nastavit `RECORD=1`.

- **Windows:** `winget install ffmpeg`, pak zavři a znovu otevři PowerShell (kvůli PATH) a restartuj sběrač.
  Když ffmpeg nenajde, nastav `FFMPEG_PATH=C:\cesta\k\ffmpeg.exe`.
- **Raspberry (Docker):** ffmpeg je v image; jednorázově `mkdir -p recordings && sudo chown 10001 recordings`.

Záznamy se ukládají do `collector/recordings/<účet>/RRRR-MM-DD_HH-MM.mp4` (jiná složka: `RECORD_DIR`).
**Sdílení přes OneDrive / Google Drive:** `RECORD_SHARE_DIR=C:\Users\<ty>\OneDrive\TikTok` – nahrává se dál lokálně
a do sdílené složky se přesune až hotové MP4 (synchronizace tak nenahrává rozpracovaný soubor ani ho nezamyká).
Kvalita `RECORD_QUALITY`: `origin` (nejlepší, výchozí), `uhd` 720p, `hd` 540p, `sd`/`ld` 480p.
Video se nepřekódovává (nezatěžuje procesor); hodina má zhruba 0,5–2 GB.

Jak to funguje: během vysílání se zapisuje `.ts` (přehratelný i při pádu PC, např. ve VLC); po konci se bezztrátově
převede na `.mp4`. Při výpadku spojení se nahrávání naváže dalším dílem (`…_dil2.mp4`). Na dary nemá nahrávání vliv –
když selže, sběr darů běží dál. Záznamy jsou jen pro vlastní potřebu – video patří autorce vysílání.

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
