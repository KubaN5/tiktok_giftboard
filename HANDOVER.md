# Předávací protokol – TikTok Giftboard

Stav k 23. 11. 2026. Tento soubor je určený Claudovi (i Jakubovi) na začátek nového vlákna: co projekt je, kde co běží, co je hotové a na co si dát pozor.

## Účel
Osobní aplikace pro Jakuba. TikTok ukazuje žebříček dárců jen pro aktuální LIVE. Tahle aplikace sbírá dary
ze **všech vysílání** sledovaného účtu a skládá z nich dlouhodobý žebříček (celkem / 30 dní / 7 dní / 24 h /
poslední LIVE / konkrétní vysílání).

Sledovaný účet: **`a_d_e_ll_o_ff_i_c_i_a_l_`**. Komunikace s Jakubem: česky.

## Architektura
```
TikTok LIVE ──(WebSocket, knihovna TikTokLive)──► collector/tracker.py (Python, běží nepřetržitě)
                                                     │ PostgREST, secret klíč (service_role)
                                                     ▼
                                               Supabase Postgres
                                                     │ RPC (jen čtení), publishable klíč
                                                     ▼
                                        web/index.html na Netlify
```
Sběrač **nemůže běžet na Netlify** (Functions mají časový limit, spojení s LIVE trvá hodiny).

## Kde co je
| Co | Hodnota |
|---|---|
| GitHub | `KubaN5/tiktok_giftboard` (veřejné, větev `main`) |
| Supabase | org „Tiktok“, projekt `rkyewweuirnnzrfpazbf`, eu-west-1, URL `https://rkyewweuirnnzrfpazbf.supabase.co` |
| Supabase publishable klíč (veřejný, pro web) | `sb_publishable_sqHEZ6m0Wi-9Ae5vbhlZQQ_aGTFKzvP` |
| Supabase secret klíč | jen v `collector/.env` na Jakubově PC – **nikdy ho nečíst, nežádat do chatu ani necommitovat** |
| Netlify | tým `kuba-kuban5` (Free), projekt `tiktok-giftboard`, site id `f65d4943-30a6-4c84-a364-26c68c4243cc` |
| Web | https://galeriehanby.netlify.app – chráněno přihlášením do Netlify týmu (Visitor access / SSO), `?u=ucet` otevře účet |
| Netlify env proměnné | `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` (scope/context nastavovat nejde – Free plán vrací 403) |
| Sběrač | Jakubovo Windows PC: `C:\Users\Admin\Documents\tiktok\tiktok_giftboard\collector` |
| Návod pro Windows | Claude Doc „TikTok Giftboard – nasazení sběrače na Windows“ – https://claude.ai/artifact/Bhh615fNUiohb6xJnQjvJM |

## Struktura repa
| Cesta | Obsah |
|---|---|
| `supabase/schema.sql` | celé schéma, idempotentní (tabulky, RLS, granty, RPC funkce) |
| `collector/tracker.py` | sběrač: hlídání LIVE, zápis darů do Supabase přes frontu s retry, volitelné nahrávání videa |
| `collector/start.bat` + `start.ps1` | Windows: založí `.venv`, nainstaluje závislosti, spustí |
| `collector/install-task.ps1` | Windows Plánovač úloh (po přihlášení, `pythonw.exe`, restart při pádu); `-Remove` odebere |
| `collector/start.sh`, `Dockerfile`, `docker-compose.yml`, `tiktok-collector.service` | Raspberry Pi / Linux |
| `collector/constraints.txt` | `pip<26`, `setuptools<81`, `packaging<26` (přes `PIP_CONSTRAINT`) |
| `collector/.env.example` | šablona `.env` (URL a účet předvyplněné) |
| `web/index.html` | statická stránka žebříčku, volá RPC přes `fetch` (žádná knihovna) |
| `scripts/build-config.sh` + `netlify.toml` | Netlify build vygeneruje `web/config.js` z env proměnných, publish `web/` |

## Databáze
- `streamers(name pk)` – sledované účty (web je bere odsud).
- `streams(id, streamer, room_id, started_at, ended_at)`, unique `(streamer, room_id)`; `ended_at null` = právě LIVE.
- `gifts(id, stream_id, streamer, ts, user_id, unique_id, nickname, gift_id, gift_name, count, diamonds, msg_id unique)`.
- `collectors(name pk, started_at, last_seen, streamers[], live[], recording[], record_enabled)` – heartbeat sběračů
  (sběrač upsertuje každých 30 s; `name` = `COLLECTOR_NAME` nebo hostname PC).
- RPC pro web (SECURITY DEFINER, execute pro anon): `list_streamers()`, `list_streams(p_streamer)`,
  `leaderboard(p_streamer, p_period, p_stream_id, p_limit)`, `collector_status()` (online = heartbeat < 2 min).
- Zabezpečení: RLS zapnuté bez politik → anon tabulky přímo nečte ani nezapisuje (ověřeno). Advisor hlásí
  „SECURITY DEFINER executable by anon“ – je to záměr.
- `service_role` potřebuje explicitní granty (tento projekt je nepřiděluje automaticky).
- Migrace: `init_gift_leaderboard`, `gifts_msg_id_dedup`, `grant_service_role`, `collector_heartbeat`, `collector_tokens`.

## Logika sběrače (důležité detaily)
- Dárce se páruje podle `user_id` (interní TikTok ID) → přejmenování účtu nerozbije součty; web ukazuje poslední známý @handle.
- Streakovatelné dary (Rose…) se počítají až na konci série (`ev.streaking == False`), `diamonds = repeat_count × gift.diamond_count`.
- `msg_id` (z `ev.common.msg_id`) + unique v DB + upsert `on_conflict=msg_id, ignore-duplicates` → dva souběžné
  sběrače (PC + Raspberry při přepnutí) nezapočítají dar dvakrát.
- Dary jdou přes `asyncio.Queue`, zápis po dávkách, při výpadku Supabase exponenciální retry (max 60 s).
- Konfigurace z `collector/.env`: `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `STREAMERS`, `CHECK_INTERVAL`,
  `EULER_API_KEY` (volitelné, při `SignatureRateLimitError`), `LOG_FILE` (log do souboru, rotace 3× 5 MB).
- Nové klíče `sb_secret_…`/`sb_publishable_…` se posílají jen v hlavičce `apikey`; `Authorization: Bearer` jen u legacy JWT (`eyJ…`).

## Aktuální stav
- [x] Supabase schéma nasazené a otestované (RPC, RLS, dedup, granty service_role).
- [x] Web nasazený. Hlavička webu: „Galerie hanby – Shopaholic Adel“ (commit `e7e411e`).
- [x] Netlify blokoval buildy z gitu („Unrecognized Git contributor“ – Free plán u soukromého repa pouští jen commity
  člena týmu s propojeným GitHubem). Vyřešeno 23. 11.: repo `KubaN5/tiktok_giftboard` je **veřejné** → každý push do `main`
  se nasadí sám (ověřeno commitem `5bc94be`). Repo proto nesmí obsahovat tajné klíče (secret klíč je jen v `collector/.env`).
- [x] Sběrač na Windows PC nainstalovaný a spuštěný, spojení se Supabase funguje („Hlídám: …“).
- [ ] **Neověřeno na skutečném LIVE** – první vysílání účtu ukáže, jestli TikTokLive 7.0.1 a sign server fungují.
- [ ] Běh na pozadí přes `install-task.ps1` zatím nezaregistrovaný.
- [ ] Přechod na Raspberry Pi (Docker) – plánovaný později; postup v README a v návodu.
- [ ] Otevřená otázka v návodu: stačí běh po přihlášení, nebo má běžet i po odhlášení (pak služba)?

## Poučení z nasazení (ať se to neopakuje)
- **Windows PowerShell 5.1** čte `.ps1` bez BOM jako ANSI → čeština rozbije parser. `.ps1` držet **ASCII + UTF-8 BOM + CRLF** (`.gitattributes`).
- Jakub měl **Python 3.12.0b4** (beta z 2023; `winget install` ji považoval za nainstalovanou). Beta má chybu v possessive
  regex kvantifikátorech → `packaging` 26.x padá na `InvalidVersion: '0.dev0'` (i pip 26). Teď má stabilní Python;
  `constraints.txt` nechán jako pojistka.
- `protobuf3-to-dict` nemá wheel → staví se ze sdist; skripty ho instalují s `--no-build-isolation`.
- Supabase projekt nepřiděluje práva k tabulkám rolím `anon`/`service_role` automaticky → granty explicitně ve schématu.
- Netlify přes MCP: založení projektu a env proměnné jdou; deploy příkazem `npx @netlify/mcp` z workspace je blokovaný
  oprávněním → nasazuje se přes propojení s GitHubem. Na iPhonu OAuth GitHubu nevrátí výsledek do původní záložky – pomohlo obnovit stránku.
- Z workspace Claude nedosáhne na `*.supabase.co`, `netlify.app` ani TikTok (síťový allowlist) – testovat přes Supabase MCP (`execute_sql` v transakci s `rollback`) a lokální Postgres.

## Jak pracovat dál
- Změny kódu: upravit v repu, commit + push do `main` (web se nasadí sám). Jakub pak na PC: `git pull`, a pokud se měnil sběrač,
  restart (`.\start.bat`, nebo Stop/Start úlohy „TikTok Gift Collector“). Při změně závislostí:
  `.\.venv\Scripts\python.exe -m pip install -c constraints.txt -U -r requirements.txt`.
- Změny DB: `apply_migration` přes Supabase MCP **a** stejnou změnu do `supabase/schema.sql` (musí zůstat idempotentní); pak `get_advisors`.
- Kontrola nasazení webu: Netlify MCP `get-project` → `currentDeploy`, příp. `get-deploy-for-site`.
- Jakubovy chyby posílá jako screenshot z PowerShellu; log: `Get-Content .\collector.log -Tail 30`.

## Sběrače u jiných lidí – tokeny (od 23. 11. 2026)
- Cizí sběrač nemá secret klíč: v `.env` má `COLLECTOR_TOKEN` + veřejný `SUPABASE_PUBLISHABLE_KEY` a zapisuje jen přes RPC
  `collector_register / collector_open_stream / collector_close_stream / collector_close_streamer / collector_heartbeat / collector_add_gifts`
  (SECURITY DEFINER, execute jen pro `anon`). Každá ověří token přes `_collector_auth` (není veřejná).
- Tabulka `collector_tokens(name pk, token_hash sha256, streamers[] null = libovolný, revoked_at)`, RLS bez politik.
  Správa jen jako vlastník: `create_collector_token(name, streamers)` (vrátí token `gbc_…` jen jednou),
  `revoke_collector_token(name)`. Jméno na webu = jméno tokenu (ne `COLLECTOR_NAME`).
- `gifts.collector` = jméno tokenového sběrače (u sběrače vlastníka `null`) → `delete from gifts where collector = '…'`.
- `collector_add_gifts`: max 500 v dávce, dar jen k vysílání povoleného účtu, `ts` nesmí být v budoucnu, duplicitní `msg_id` se přeskočí.
- Migrace `collector_tokens`, `collector_close_streamer`. Sběrač při startu a po každém LIVE uzavře visící vysílání, jakmile účet nevysílá (výpadek sítě při konci LIVE). Otestováno: lokální Postgres + Supabase (jako `anon`, v transakci s rollbackem) a sběrač
  v režimu s tokenem proti mocku PostgREST. Na skutečném PC u druhého člověka zatím ne.

## Překryvná vrstva do videa (od 23. 11. 2026)
- Recorder zapisuje při `RECORD=1` do `recordings/<účet>/<base>.events.jsonl` (JSON na řádek, `t` = epoch PC):
  `meta`, `owner` (id hostitelky z room info), `part` (čas spuštění ffmpeg = čas 0 dílu), `gift` (každá zpráva vč.
  `streak` pro průběh série), `comment`, `battle` (LinkMicBattleEvent: action OPEN/FINISH…, start_ms, duration,
  anchors, scores, result), `score` (LinkMicArmiesEvent: hostscore podle anchor id); `st` = čas serveru TikTok (ms).
- `collector/overlay.py`: ASS titulky (libass) + `ffmpeg -vf ass=… -c:v libx264` → `<video>_overlay.mp4`. Volby
  `--start/--duration` (ukázka), `--frame` (PNG), `--delay`, `--from-db` (dary ze Supabase pro staré záznamy),
  `--ass-only`. `overlay.bat` = přetažení MP4. `RECORD_OVERLAY=1` → po vysílání automaticky (BELOW_NORMAL priorita,
  jedno video po druhém, výsledek do `RECORD_SHARE_DIR`).
- Přezdívky: NFKC + vyhození emoji (font je nemá). Naše strana v bitvě: `owner` → display_id v anchors → heuristika.
- Otestováno jen s ukázkovými daty (skutečné dary + vymyšlené komentáře/bitva). **Komentáře a bitvy z ostrého LIVE zatím
  neověřené** – parsování LinkMic zpráv v TikTokLive 7.0.1 může selhat (viz HashtagNamespace).

## Výpadky sběru (od 23. 11. 2026)
- Tabulka `coverage(collector, streamer, stream_id, started_at, last_seen)` – plní ji trigger `trg_track_coverage` na
  `collectors` (každý heartbeat s účtem v `live` prodlouží úsek; > 90 s bez heartbeatu = nový úsek). Funguje pro oba typy sběračů.
- `streams.room_started_at` (create_time z room info, sběrač po připojení přes `set_room_start` / `collector_room_start`)
  → vidět i výpadek na začátku; `streams.end_uncertain` + `_close_streamer`: visící vysílání se uzavře k poslednímu
  zachycenému okamžiku (+15 s). Vlastník: RPC `close_streamer` (service_role), token: `collector_close_streamer`.
- Web: RPC `stream_coverage(p_streamer, p_limit)` → u každého vysílání `total_seconds`, `missing_seconds`, `gaps`
  (≥ 60 s); karta „Výpadky sběru“ s časovou osou, ⚠️ ve výběru vysílání a upozornění nad žebříčkem. Migrace `collector_coverage`.
- Karta i upozornění se řídí filtrem: konkrétní vysílání / Poslední LIVE = detail jednoho vysílání; 24 h / 7 dní /
  30 dní / Celkem = souhrn za období (vysílání zasahující do období, bráno celé) + jen vysílání s výpadkem, ostatní
  sbalená v `<details>`. Web načítá `p_limit: 2000` a filtruje období v prohlížeči (strop funkce 2000, migrace
  `stream_coverage_limit_2000`).

## Odhad výdělku v Kč (od 23. 11. 2026)
- Jen web: `USD_PER_COIN = 0.005` × kurz USD/CZK (`api.frankfurter.dev`, záložně 22,35) – dlaždice „Výdělek ≈“
  a „≈ X Kč“ pod mincemi u každého dárce (na webu 🪙 „Mincí“, do 23. 11. ikona 💎). `diamonds` v DB = cena daru v mincích (`gift.diamond_count`).
- Žebříček: web ukazuje po 50 dárcích (tlačítko „Zobrazit dalších 50“, počet rozbalených drží i obnovení po 10 s),
  pole „Hledat dárce“ volá `leaderboard(..., p_search)` (pořadí zůstává celkové, max 50 výsledků). Součty nahoře
  (mince, Kč, dárců, max pro šířku pruhu) počítá `leaderboard_totals(p_streamer, p_period, p_stream_id)` ze všech darů.
  Strop `leaderboard` p_limit 5000. Migrace `leaderboard_search_and_totals` (stará 4parametrová verze dropnutá).

## Stav sběrače na webu (od 23. 11. 2026)
- Sběrač: `Store.heartbeat()` každých 30 s (globální množiny `LIVE`, `RECORDING`); chyba se loguje jen jednou, ne každých 30 s.
- Web: štítky v hlavičce (`#collectors`) – zelený „online · čeká na LIVE / sbírá LIVE / ⏺ nahrává“, červený „offline · před X“.
  Při chybě RPC se štítky jen schovají, žebříček běží dál. Sběrače starší než 30 dní se nezobrazují.

## Nahrávání videa (od 23. 11. 2026)
- `RECORD=1` v `collector/.env` → třída `Recorder` v `tracker.py` po `ConnectEvent` načte `client.web.fetch_room_info()`,
  vybere FLV URL podle `RECORD_QUALITY` a spustí **vlastní ffmpeg subprocess** (`-c copy -f mpegts` → `.ts`).
- Vestavěný `client.web.fetch_video_data` z TikTokLive záměrně nepoužitý: zastavuje ffmpeg přes `os.kill(SIGTERM)`,
  což na Windows = TerminateProcess → poškozené MP4. Tady se ffmpeg ukončí klávesou `q` přes stdin.
- Výpadek → nový díl `…_dilN.ts` (max 5 neúspěchů po sobě); `room_info.status == 4` = konec vysílání.
- Po konci: remux každého dílu na `.mp4` (`-movflags +faststart`), `.ts` se smaže; když remux selže, `.ts` zůstane.
- Windows: `CREATE_NO_WINDOW`, jinak by `pythonw.exe` (Plánovač úloh) otevíral okno konzole.
- Otestováno v workspace se simulovaným FLV streamem (nahrání, stop, výpadek + navázání, konec). Na skutečném LIVE zatím ne.
- Docker: ffmpeg v image, záznamy do volume `./recordings` (uid 10001).
- `RECORD_SHARE_DIR` (volitelné): hotové MP4 se po převodu přesune (`shutil.move`) do `<share>/<účet>/` – pro sdílení
  přes OneDrive/Google Drive. Do sync složky se záměrně nenahrává přímo (rostoucí `.ts`, zámky souborů).
  Jakub chce záznamy sdílet s dalším člověkem přes OneDrive/Google Drive (23. 11.); varianta „jen nahrávat bez
  Supabase klíče“ na druhém PC zatím neudělaná.

## Příprava pro YouTube + Telegram (od 24. 11. 2026)
- Automatický upload přes YouTube Data API zamítnut: videa z neověřeného API projektu jsou **zamčená jako soukromá**
  (nejde změnit ani odvolat), veřejné jen po auditu. Jakub chce veřejná videa → ruční upload, sběrač jen připraví.
- `YOUTUBE_PREP=1` (jen s `RECORD_OVERLAY=1`, jinak varování a vypnuto): v `render_overlays` po úspěšném overlayi
  `prepare_youtube()` → `.txt` (UTF-8 BOM, název + popis) a přesun MP4 do `YOUTUBE_DIR` (výchozí `<share|out>/YouTube`)
  jako `<účet>_<stem>_overlay.mp4`. Popis: top 10 + součty z RPC `leaderboard` / `leaderboard_totals` s `p_stream_id`
  (veřejné RPC → funguje se secret klíčem i s tokenem). `Recorder` proto dostává `stream_id` a `store`.
  Název `YOUTUBE_TITLE` (`{ucet} {datum} {cas} {dil}`), díl > 1 se přidá sám; znaky `< >` nahrazeny (YouTube je nepovolí),
  název max 100 znaků. Délka z výpisu `ffmpeg -i`. Při chybě přesunu video zůstane na místě + Telegram varování.
- Telegram: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, `sendMessage` s `parse_mode=HTML` (`tg_escape`), název v `<code>`
  (klepnutím kopíruje), popis v `<pre>`. `notify()` posílá na pozadí (task v `BACKGROUND`), retry po 10 s / 1 min / 5 min,
  4xx se neopakuje. Zprávy: video k nahrání, overlay hotový (bez YOUTUBE_PREP), overlay selhal.
  `tracker.py --telegram-test`: bez chat id vypíše chaty z `getUpdates`, s ním pošle zkušební zprávu.
- Otestováno v workspace: skutečný overlay.py + ffmpeg na testovacím videu (2 díly), mock PostgREST + Telegram API.
  RPC s `p_stream_id` ověřeno přes Supabase MCP. **Na skutečném Telegramu / Google Drive zatím neověřeno.**

## Overlay – oprava pádu na prvním ostrém LIVE (24. 11. 2026)
- 24. 11. overlay 72min vysílání spadl po 45 s: ffmpeg „Error muxing a packet … Not yet implemented in FFmpeg, patches welcome“
  (Jakubův novější ffmpeg na Windows). Příčina: `-c:a copy` – stream TikToku mění uprostřed parametry zvuku (AAC extradata,
  typicky při bitvě / co-hostu) a muxer MP4 to při kopírování neumí.
- Oprava v `overlay.py`: zvuk se znovu kóduje (`-c:a aac -b:a 160k -ac 2 -ar 48000`), obraz se drží v pevném rozměru
  (`scale=w:h:force_original_aspect_ratio=decrease,pad=…,setsar=1` před `ass`). Ve workspace (ffmpeg 6.1) chyba nejde
  zopakovat, ověřeno jen, že nový příkaz projde i se změnou zvuku uprostřed (mono 44,1 kHz → stereo 48 kHz).
- Ruční pokus pak spadl znovu, chyba ale u obrazu (`vost#0:0/libx264 … Error submitting a packet to the muxer`).
  Ve zdrojácích FFmpeg (`libavformat/movenc.c`) je pro H.264 v běžném MP4 jediná cesta k AVERROR_PATCHWELCOME
  `mov_cts_out_of_range` → „pts/dts pair unsupported“ (rozdíl pts−dts mimo int32) – B-snímky + skok v časových značkách
  záznamu. Oprava: `-bf 0` (pts == dts) a `-loglevel warning`, ať je příčina příště vidět v logu.
- Třetí pokus (s warningy) ukázal skutečnou příčinu: kolem 45. s skok v časových značkách obrazu o desítky dní
  („Packet duration: 16759380582 / dts: 93596879691 … out of range“, „pts has no value“), zvuk mírně pozpátku
  („Non-monotonic DTS“). Oprava: obraz `setpts` – čas běží o skutečný rozdíl mezi snímky, nesmyslný skok (záporný,
  > 5 s, NaN) = +1/30 s; zvuk `aresample=48000,asetpts=N/SR/TB` (čas podle vzorků). Test ve workspace: video se skokem
  o ~35 dní (41264 s podle kontejneru) → výstup 65 s; běžné video beze změny délky.
- Čtvrtý pokus: „Application provided duration: 16901409231 … invalid“ – `setpts` mění jen pts, ne `frame->duration`;
  ffmpeg (fftools/ffmpeg_filter.c, VSYNC_VFR) bere délku snímku z dekodéru a libx264 ji předá do paketu.
  Oprava: za setpts filtr `fps=30` (vf_fps.c nastavuje `frame->duration = 1`, pracuje jen s pts) a v setpts se
  podezřele malé rozdíly (< 5 ms) berou taky jako skok. Novější ffmpeg ve workspace není (instalace zamítnuta),
  ověřeno na ffmpeg 6.1: skok o 35 dní → 65 s / 1950 snímků, běžné video 60 s.
- Pátý pokus doběhl, ale ve výpisu stálo `frame=1124` od 0:37 do 1:15 a `drop` 8 → 1140 (≈ 38 s obrazu zahozeno).
  Příčina: první snímek se poznával přes `isnan(PREV_INPTS)` – po snímku bez pts je PREV_INPTS NaN a výraz se vrátil
  na původní (rozbitý, o 38 s pozadu) čas. Oprava: první snímek = `eq(N,0)`.
- Výpis zaplavilo „fontselect: failed to find any fallback with glyph 0xAA04 for font: (Arial, 700, 0)“ (libass, u každého
  snímku) – ozdobné „srdíčko“ ꨄ z písma Cham v přezdívce/komentáři. `clean()` nahrazuje ꨄ / ᥫ᭡ → ♥ (`DECOR`),
  `run_ffmpeg()` propouští výpis ffmpeg a stejné fontselect varování ukáže jen jednou.

## Nápady na další úpravy (nic z toho není rozhodnuté)
- Detail dárce (historie darů, nejčastější dar), přehled podle typu daru.
- Supabase Realtime místo pollingu každých 10 s.
- Overlay žebříčku pro OBS (průhledné pozadí, top 5).
- Export CSV, přepočet diamantů na Kč.

## Top 3 ve vavřínových věncích + profilovky (od 23. 11. 2026)
- Web: nad „Hledat dárce“ `#podium` – zlatý (uprostřed, výš), stříbrný (vlevo), bronzový (vpravo) věnec
  (vlastní SVG kresba, funkce `wreath()`), uvnitř profilovka nebo první písmeno @účtu, pod tím @účet, mince, Kč.
  Řídí se filtrem; při hledání skryté; méně než 3 dárci → jen tolik věnců (pevné `grid-column`).
  Překresluje se jen při změně (fotky neblikají při obnově po 10 s).
- Profilovky: sběrač (jen se secret klíčem) u daru vezme `ev.user.avatar_medium/avatar_thumb.url_list[0]`
  (`avatar_url()`), na pozadí (`Store.avatar_worker`) ji stáhne a nahraje do veřejného bucketu **`avatars`**
  jako `avatars/<user_id>` (upsert), pak upsert řádku `public.avatars(user_id, updated_at)`.
  Na dárce nejvýš 1× za 24 h a běh sběrače; neúspěch → další pokus nejdřív za 10 min. Do logu jen první úspěch
  („Profilovka dárce … uložena“) a první chyba za běh, zbytek v debug. Tokenový sběrač fotky neukládá.
- `leaderboard` vrací navíc `avatar_ver` (epoch `avatars.updated_at`); web: 
  `<SUPABASE_URL>/storage/v1/object/public/avatars/<user_id>?v=<avatar_ver>`, při chybě načtení zůstane písmeno.
- Migrace `donor_avatars` (tabulka, bucket 512 KB, image/*; leaderboard drop + create kvůli novému sloupci).

## Největší dar + obrázky darů (od 24. 11. 2026)
- Dlaždice „Největší dar“ (vedle „Vysílání“, na mobilu přes celou šířku): RPC `top_gift(p_streamer, p_period, p_stream_id)`
  = nejdražší jednotlivý řádek `gifts` (série = jeden dar, „Rose ×99“); vrací i `gift_id`, `image_ver`.
- Obrázky darů: sběrač u daru vezme `ev.gift.image/icon.url_list[0]` (`gift_image_url()`), uloží do veřejného bucketu
  **`gifts`** jako `gifts/<gift_id>` a upsertne `public.gift_images(gift_id, updated_at)` – na dar nejvýš 1× za týden a běh.
  Společná fronta s profilovkami: `Store.add_image(kind, key, src)` + `Store.image_worker()` (`IMAGE_KINDS`).
  Log: „Obrázek daru … uložen“ / „… se nepodařilo uložit“ (první za běh). Web: bez obrázku 🎁.
- Migrace `top_gift`, `gift_images`.
