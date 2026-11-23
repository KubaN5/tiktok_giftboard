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
| Web | https://tiktok-giftboard.netlify.app – chráněno přihlášením do Netlify týmu (Visitor access / SSO), `?u=ucet` otevře účet |
| Netlify env proměnné | `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY` (scope/context nastavovat nejde – Free plán vrací 403) |
| Sběrač | Jakubovo Windows PC: `C:\Users\Admin\Documents\tiktok\tiktok_giftboard\collector` |
| Návod pro Windows | Claude Doc „TikTok Giftboard – nasazení sběrače na Windows“ – https://claude.ai/artifact/Bhh615fNUiohb6xJnQjvJM |

## Struktura repa
| Cesta | Obsah |
|---|---|
| `supabase/schema.sql` | celé schéma, idempotentní (tabulky, RLS, granty, RPC funkce) |
| `collector/tracker.py` | sběrač: hlídání LIVE, zápis darů do Supabase přes frontu s retry |
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
- RPC pro web (SECURITY DEFINER, execute pro anon): `list_streamers()`, `list_streams(p_streamer)`,
  `leaderboard(p_streamer, p_period, p_stream_id, p_limit)`.
- Zabezpečení: RLS zapnuté bez politik → anon tabulky přímo nečte ani nezapisuje (ověřeno). Advisor hlásí
  „SECURITY DEFINER executable by anon“ – je to záměr.
- `service_role` potřebuje explicitní granty (tento projekt je nepřiděluje automaticky).
- Migrace: `init_gift_leaderboard`, `gifts_msg_id_dedup`, `grant_service_role`.

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

## Nápady na další úpravy (nic z toho není rozhodnuté)
- Detail dárce (historie darů, nejčastější dar), přehled podle typu daru.
- Supabase Realtime místo pollingu každých 10 s.
- Overlay žebříčku pro OBS (průhledné pozadí, top 5).
- Export CSV, přepočet diamantů na Kč.
