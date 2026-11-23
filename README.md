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
> Nasazeno: projekt `galeriehanby` → https://galeriehanby.netlify.app (proměnné nastavené).
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

### Překryvná vrstva: dary, komentáře a bitvy ve videu
Video z TikToku obsahuje jen obraz a zvuk. Skóre bitvy, dary ani komentáře v něm nejsou, ty kreslí až aplikace.
Sběrač si je proto při nahrávání ukládá s přesným časem do `<záznam>.events.jsonl` (vedle videa; při
`RECORD_SHARE_DIR` se kopie přesune i do sdílené složky). Skript `collector/overlay.py` z nich vyrobí
`<video>_overlay.mp4`: nahoře pruh bitvy se skóre a odpočtem (po konci VÝHRA / PROHRA), vlevo dole oznámení
darů včetně průběhu série (x1 → x12), velké dary uprostřed a pod nimi poslední komentáře. Originál zůstane.

- **Ručně:** přetáhni MP4 myší na `collector/overlay.bat`. Nebo v PowerShellu ve složce `collector`:
  ```powershell
  .\.venv\Scripts\python.exe overlay.py "G:\Můj disk\Shopadel\ucet\2026-11-23_16-55.mp4"
  .\.venv\Scripts\python.exe overlay.py video.mp4 --start 600 --duration 60   # jen minuta na zkoušku
  .\.venv\Scripts\python.exe overlay.py video.mp4 --frame 615                 # jeden snímek jako PNG
  .\.venv\Scripts\python.exe overlay.py video.mp4 --delay 2                   # vrstva o 2 s později
  ```
- **Automaticky po vysílání:** `RECORD_OVERLAY=1` v `.env`. Video se překódovává, takže na slabším PC to může trvat
  i déle než samotné vysílání. Běží to se sníženou prioritou a sběr darů to neruší.
- **Starší záznamy bez souboru událostí:** `--from-db` stáhne ze Supabase aspoň dary (potřebuje secret klíč v `.env`),
  ideálně s přesným začátkem videa: `--start-time "2026-11-23 16:55:43"`.

Vrstva nevypadá jako TikTok, je to vlastní grafika ze stejných dat. Skóre bitvy chodí v jiných zprávách než dary;
pokud ho knihovna TikTokLive nepřečte, pruh se neukáže.

## 5. Sběrač u někoho jiného (bez secret klíče)
Web a databázi hostuješ ty, druhý člověk jen pustí sběrač u sebe. **Secret klíč mu nedávej** – měl by s ním plný
přístup k databázi. Místo toho dostane vlastní **token**, se kterým umí jen přidávat vysílání a dary k povoleným
účtům (nic nečte, nemění ani nemaže). Jeho sběrač se na webu ukáže pod jménem, které mu dáš.

1. **Ty** v Supabase → SQL Editor vytvoříš token (zobrazí se jen jednou):
   ```sql
   select public.create_collector_token('Petr', array['a_d_e_ll_o_ff_i_c_i_a_l_']);  -- null místo pole = libovolný účet
   ```
2. **On** stáhne repo, spustí `collector/start.bat` (vytvoří `.env`) a v `.env` vyplní jen
   `COLLECTOR_TOKEN=gbc_…` a `STREAMERS=` (povolené účty). `SUPABASE_SECRET_KEY` nechá prázdný / beze změny,
   `SUPABASE_URL` a veřejný `SUPABASE_PUBLISHABLE_KEY` jsou předvyplněné. V logu uvidí „Přihlášen tokenem jako sběrač …“.
3. Správa:
   ```sql
   select public.revoke_collector_token('Petr');               -- zablokovat (sběrač pak nic nezapíše)
   select public.create_collector_token('Petr', array['…']);   -- nový token / jiné účty (starý přestane platit)
   delete from public.gifts where collector = 'Petr';          -- smazat dary, které zapsal
   select name, streamers, created_at, revoked_at from public.collector_tokens;
   ```

Sbírá-li stejný účet jako ty, nevadí to – dar se díky `msg_id` započítá jen jednou.

## Stav sběrače
Sběrač každých 30 s zapíše do Supabase, že běží. Web pak v hlavičce ukazuje **online** (a jestli sbírá / nahrává LIVE),
nebo **offline** s časem poslední zprávy (po 2 min bez zprávy). Jméno sběrače = název PC, jinak `COLLECTOR_NAME` v `.env`
(hodí se při dvou sběračích, např. `PC` a `Raspberry`).

## Výpadky sběru
Databáze si z heartbeatů skládá, kdy sběrač vysílání opravdu sbíral. Web pak u každého vysílání ukáže, kolik minut
chybí a kdy (pád PC, výpadek internetu, pozdní spuštění – začátek vysílání se bere od TikToku), včetně časové osy a
upozornění nad žebříčkem. Výpadky kratší než 1 min se nezobrazují (heartbeat je á 30 s).

## Poznámky
- Zachytí jen vysílání, během kterých sběrač běží.
- Používá neoficiální knihovnu [TikTokLive](https://github.com/isaackogan/TikTokLive) – TikTok
  oficiální API na LIVE dary nemá a takové čtení je v rozporu s jeho podmínkami. Po změnách na straně
  TikToku může přestat fungovat, dokud knihovna nevydá opravu (`pip install -U TikTokLive`).
- Při omezení požadavků podepisovacím serverem: zdarma klíč na eulerstream.com → `EULER_API_KEY`.
- Dárci se párují podle interního TikTok ID → přejmenování účtu nerozbije součty.
- Série darů (např. Rose ×10) se započítá jednou, až na konci série.
- Výpadek Supabase nevadí: dary se drží ve frontě a odešlou se, jakmile je spojení zpět.
- Žlutá mince (CSS `.coin`) = cena daru v mincích podle TikToku (dřív ikona 💎). **Kč** = odhad výdělku tvůrce: ~0,005 USD za minci (TikTok si nechá zhruba
  polovinu), přepočteno aktuálním kurzem (frankfurter.dev, záložně 22,35). Neoficiální průměr, před daní a poplatky;
  dary z iPhonu vynesou méně. Konstanta `USD_PER_COIN` ve `web/index.html`.
- Web je veřejný na své URL (vyhledávačům je indexace zakázaná přes `X-Robots-Tag`).
