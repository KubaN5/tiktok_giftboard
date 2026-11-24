#!/usr/bin/env python3
"""
Sběrač darů z TikTok LIVE -> Supabase
-------------------------------------
Hlídá zadané účty. Jakmile některý začne vysílat, připojí se k LIVE a každý dar
(kdo, co, kolik diamantů) zapíše do Supabase. Žebříček pak zobrazuje web na Netlify.

Konfigurace přes proměnné prostředí (nebo soubor .env vedle skriptu):
    SUPABASE_URL          https://xxxx.supabase.co
    SUPABASE_SECRET_KEY   secret (sb_secret_...) nebo legacy service_role klíč – sběrač vlastníka
    COLLECTOR_TOKEN       místo secret klíče: token od vlastníka (sběrač u někoho jiného) –
                          pak stačí veřejný SUPABASE_PUBLISHABLE_KEY; zapisuje jen přes funkce collector_*
    EULER_API_KEY         volitelné – klíč pro podepisovací server TikTokLive
    LOG_FILE              volitelné – zapisovat log i do souboru (např. collector.log)
    RECORD                volitelné – 1 = nahrávat video vysílání (potřeba ffmpeg)
    RECORD_DIR            kam ukládat záznamy (výchozí složka recordings vedle skriptu)
    RECORD_QUALITY        origin | uhd | hd | sd | ld (výchozí origin = nejlepší dostupná)
    RECORD_SHARE_DIR      volitelné – hotová MP4 přesunout sem (např. složka OneDrive / Google Drive)
    RECORD_OVERLAY        volitelné – 1 = po vysílání vyrobit i *_overlay.mp4 s dary, komentáři a bitvami
                          (události se při nahrávání ukládají vždy do <záznam>.events.jsonl; ručně: overlay.py)
    FFMPEG_PATH           volitelné – cesta k ffmpeg, pokud není v PATH
    YOUTUBE_PREP          volitelné – 1 = hotové *_overlay.mp4 přesunout do složky pro ruční nahrání na YouTube
                          a vedle něj uložit .txt s názvem a popisem (top dárci vysílání); vyžaduje RECORD_OVERLAY=1
    YOUTUBE_DIR           kam (výchozí <RECORD_SHARE_DIR>\\YouTube, bez sdílené složky recordings\\YouTube)
    YOUTUBE_TITLE         šablona názvu: {ucet} {datum} {cas} {dil} (výchozí „{ucet} – TikTok LIVE {datum}“)
    YOUTUBE_LINK          volitelné – odkaz připojený do popisu (např. žebříček na webu)
    TELEGRAM_BOT_TOKEN    volitelné – token bota od @BotFather; s TELEGRAM_CHAT_ID pošle zprávu o hotovém videu
    TELEGRAM_CHAT_ID      komu psát (zjistí: python tracker.py --telegram-test)
    COLLECTOR_NAME        volitelné – jméno sběrače na webu (výchozí název počítače)

Použití:
    python tracker.py @ucet1 [@ucet2 ...] [--interval 60]
    python tracker.py --telegram-test      # ověří Telegram / vypíše TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import re
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger("collector")
HEARTBEAT_EVERY = 30   # s; web považuje sběrač za offline po 2 min bez zprávy
LIVE: set[str] = set()        # účty, jejichž LIVE právě sbíráme
RECORDING: set[str] = set()   # účty, jejichž LIVE právě nahráváme
AVATAR_REFRESH = 24 * 3600    # s; profilovku dárce obnovit nejvýš jednou za den (za běh sběrače)
GIFT_IMAGE_REFRESH = 7 * 24 * 3600   # s; obrázek daru se skoro nemění – stačí jednou za běh / týden
# Obrázky ukládané do Supabase Storage: druh -> (bucket, tabulka, klíčový sloupec, (log úspěch, log chyba), obnova)
IMAGE_KINDS = {
    "avatar": ("avatars", "/avatars", "user_id",
               ("Profilovka dárce %s uložena", "Profilovku dárce %s se nepodařilo uložit"), AVATAR_REFRESH),
    "gift":   ("gifts", "/gift_images", "gift_id",
               ("Obrázek daru %s uložen", "Obrázek daru %s se nepodařilo uložit"), GIFT_IMAGE_REFRESH),
}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_dotenv(path: Path) -> None:
    """Minimalistické načtení .env (KEY=VALUE), bez další závislosti."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def norm(username: str) -> str:
    return username.strip().lstrip("@").lower()


def env_on(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "ano")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def image_url(obj, attrs: tuple[str, ...]) -> str | None:
    """První http adresa z prvního vyplněného obrázku (ImageModel.url_list; starší názvy jako záloha)."""
    for attr in attrs:
        img = getattr(obj, attr, None)
        if img is None:
            continue
        for list_attr in ("url_list", "m_urls", "url", "urls"):
            for u in getattr(img, list_attr, None) or []:
                if isinstance(u, str) and u.startswith("http"):
                    return u
    return None


def avatar_url(user) -> str | None:
    """Adresa profilové fotky dárce (TikTokLive 7: avatar_medium / avatar_thumb)."""
    return image_url(user, ("avatar_medium", "avatar_thumb", "avatar_large", "profile_picture"))


def gift_image_url(gift) -> str | None:
    """Adresa obrázku daru (TikTokLive 7: Gift.image / icon)."""
    return image_url(gift, ("image", "icon", "preview_image"))


# --------------------------------------------------------------------------
# Supabase (PostgREST) úložiště
# --------------------------------------------------------------------------

class Store:
    """Zápisy do Supabase. Dary jdou přes frontu, takže výpadek sítě nic neztratí.

    Dva režimy: se secret klíčem zapisuje přímo do tabulek (sběrač vlastníka), s tokenem
    (COLLECTOR_TOKEN + veřejný klíč) jen přes funkce collector_* – nic nečte, nemění ani nemaže.
    """

    def __init__(self, url: str, key: str, token: str | None = None):
        self.token = token
        headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):  # legacy JWT klíč; nové sb_secret_ klíče jdou jen v 'apikey'
            headers["Authorization"] = f"Bearer {key}"
        self.http = httpx.AsyncClient(base_url=url.rstrip("/") + "/rest/v1",
                                      headers=headers, timeout=20)
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        # Profilovky dárců a obrázky darů -> Supabase Storage (buckety avatars, gifts). Jen se secret klíčem.
        self.storage = httpx.AsyncClient(base_url=url.rstrip("/") + "/storage/v1", timeout=30,
                                         headers={"apikey": key, "Authorization": f"Bearer {key}"})
        self.image_q: asyncio.Queue[tuple[str, str, str]] = asyncio.Queue(maxsize=500)   # (druh, klíč, adresa)
        self.image_done: dict[tuple[str, str], float] = {}   # (druh, klíč) -> kdy naposledy (i neúspěšně) zpracováno
        self.image_logged: set[str] = set()                   # do logu jen první úspěch / chyba za druh a běh

    async def _req(self, method: str, path: str, **kw) -> httpx.Response:
        r = await self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Supabase {r.status_code}: {r.text[:300]}")
        return r

    async def _rpc(self, fn: str, **args):
        r = await self._req("POST", f"/rpc/{fn}", json={"p_token": self.token, **args})
        return r.json() if r.content else None

    async def register(self, streamers: list[str]) -> str | None:
        """Založí účty pro web. V režimu s tokenem ověří token a vrátí jméno sběrače od vlastníka."""
        if self.token:
            return await self._rpc("collector_register", p_streamers=streamers)
        for name in streamers:
            await self._req("POST", "/streamers", params={"on_conflict": "name"},
                            headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                            json={"name": name})
        return None

    async def open_stream(self, streamer: str, room_id: str) -> int:
        """Založí vysílání; při znovupřipojení do stejné místnosti vrátí existující."""
        if self.token:
            return int(await self._rpc("collector_open_stream", p_streamer=streamer, p_room_id=room_id))
        r = await self._req("POST", "/streams", params={"on_conflict": "streamer,room_id", "select": "id"},
                            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                            json={"streamer": streamer, "room_id": room_id, "ended_at": None})
        return int(r.json()[0]["id"])

    async def close_stream(self, stream_id: int) -> None:
        if self.token:
            await self._rpc("collector_close_stream", p_stream_id=stream_id)
            return
        await self._req("PATCH", "/streams",
                        params={"id": f"eq.{stream_id}", "ended_at": "is.null"},
                        headers={"Prefer": "return=minimal"},
                        json={"ended_at": now_iso()})

    async def close_open_streams(self, streamer: str) -> None:
        """Uzavře vysílání účtu, která zůstala otevřená (výpadek sítě při konci LIVE, pád/restart PC).
        Konec = poslední zachycený okamžik, označený v DB jako nejistý."""
        if self.token:
            await self._rpc("collector_close_streamer", p_streamer=streamer)
        else:
            await self._req("POST", "/rpc/close_streamer", json={"p_streamer": streamer})

    async def set_room_start(self, stream_id: int, started: float) -> None:
        """Skutečný začátek vysílání podle TikToku – web pak ukáže i výpadek na začátku (sběrač zapnutý pozdě)."""
        iso = datetime.fromtimestamp(started, timezone.utc).isoformat()
        if self.token:
            await self._rpc("collector_room_start", p_stream_id=stream_id, p_started_at=iso)
        else:
            await self._req("POST", "/rpc/set_room_start", json={"p_stream_id": stream_id, "p_started_at": iso})

    async def heartbeat(self, name: str, streamers: list[str], record_enabled: bool) -> None:
        """Každých 30 s zapíše do tabulky collectors, že sběrač žije (web ukazuje online/offline)."""
        started, failed = now_iso(), False
        while True:
            try:
                if self.token:                                  # jméno určuje token, ne COLLECTOR_NAME
                    await self._rpc("collector_heartbeat", p_started_at=started, p_streamers=streamers,
                                    p_live=sorted(LIVE), p_recording=sorted(RECORDING),
                                    p_record_enabled=record_enabled)
                else:
                    await self._req("POST", "/collectors", params={"on_conflict": "name"},
                                    headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                                    json={"name": name, "started_at": started, "last_seen": now_iso(),
                                          "streamers": streamers, "live": sorted(LIVE),
                                          "recording": sorted(RECORDING), "record_enabled": record_enabled})
                if failed:
                    log.info("Heartbeat zase funguje")
                failed = False
            except Exception as ex:
                if not failed:                                  # nezahlcovat log každých 30 s
                    log.warning("Heartbeat se nepodařilo zapsat: %s", ex)
                failed = True
            await asyncio.sleep(HEARTBEAT_EVERY)

    async def stream_top(self, streamer: str, stream_id: int, limit: int = 10) -> tuple[list[dict], dict]:
        """Top dárci a součty jednoho vysílání (veřejné RPC webu – funguje se secret klíčem i s tokenem)."""
        top = (await self._req("POST", "/rpc/leaderboard", json={
            "p_streamer": streamer, "p_stream_id": stream_id, "p_limit": limit})).json() or []
        totals = (await self._req("POST", "/rpc/leaderboard_totals", json={
            "p_streamer": streamer, "p_stream_id": stream_id})).json() or []
        return top, (totals[0] if totals else {})

    def add_gift(self, row: dict) -> None:
        row.setdefault("ts", now_iso())
        self.queue.put_nowait(row)

    def add_image(self, kind: str, key, src: str | None) -> None:
        """Naplánuje stažení obrázku (profilovka / dar) – nejvýš jednou za dobu obnovy daného druhu."""
        if self.token or not src or key in (None, "", 0):
            return
        key = str(key)
        refresh = IMAGE_KINDS[kind][4]
        last = self.image_done.get((kind, key))
        if last and time.time() - last < refresh:
            return
        self.image_done[(kind, key)] = time.time()
        try:
            self.image_q.put_nowait((kind, key, src))
        except asyncio.QueueFull:
            self.image_done.pop((kind, key), None)

    def add_avatar(self, user_id: str, src: str | None) -> None:
        self.add_image("avatar", user_id, src)

    async def image_worker(self) -> None:
        """Na pozadí stáhne obrázek z TikToku a uloží ho do Supabase (<bucket>/<klíč>) – odkazy TikToku
        po čase vyprší, uložená kopie ne. Selhání nevadí: zkusí se znovu při dalším daru (za 10 min+)."""
        if self.token:
            return
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": BROWSER_UA}) as dl:
            while True:
                kind, key, src = await self.image_q.get()
                bucket, table, col, label, refresh = IMAGE_KINDS[kind]
                try:
                    r = await dl.get(src)
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if r.status_code != 200 or not ctype.startswith("image/") or not 0 < len(r.content) <= 500_000:
                        raise RuntimeError(f"stažení: HTTP {r.status_code}, {ctype or '?'}, {len(r.content)} B")
                    up = await self.storage.post(f"/object/{bucket}/{key}", content=r.content,
                                                 headers={"Content-Type": ctype, "x-upsert": "true",
                                                          "cache-control": "max-age=604800"})
                    if up.status_code >= 400:
                        raise RuntimeError(f"úložiště {up.status_code}: {up.text[:200]}")
                    await self._req("POST", table, params={"on_conflict": col},
                                    headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                                    json={col: int(key) if col == "gift_id" else key, "updated_at": now_iso()})
                    first = f"{kind}:ok" not in self.image_logged
                    log.log(logging.INFO if first else logging.DEBUG, label[0] + " (%s B)", key, len(r.content))
                    self.image_logged.add(f"{kind}:ok")
                except Exception as ex:
                    first = f"{kind}:err" not in self.image_logged
                    log.log(logging.WARNING if first else logging.DEBUG, label[1] + ": %s", key, ex)
                    self.image_logged.add(f"{kind}:err")
                    self.image_done[(kind, key)] = time.time() - refresh + 600   # další pokus nejdřív za 10 min

    async def writer(self) -> None:
        """Na pozadí posílá dary po dávkách; při chybě to zkusí znovu."""
        while True:
            batch = [await self.queue.get()]
            await asyncio.sleep(0.5)                       # chvíli počkej, ať se dávka naplní
            while not self.queue.empty() and len(batch) < 200:
                batch.append(self.queue.get_nowait())
            delay = 2
            while True:
                try:
                    # on_conflict=msg_id: stejný dar zapsaný druhým sběračem se tiše přeskočí
                    if self.token:
                        await self._rpc("collector_add_gifts", p_gifts=batch)
                    else:
                        await self._req("POST", "/gifts", params={"on_conflict": "msg_id"},
                                        headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                                        json=batch)
                    break
                except Exception as ex:
                    log.warning("Zápis %s darů selhal (%s) – zkusím za %ss", len(batch), ex, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)


# --------------------------------------------------------------------------
# Nahrávání videa (volitelné, RECORD=1)
# --------------------------------------------------------------------------

QUALITIES = ["origin", "uhd", "hd", "sd", "ld"]
# Bez CREATE_NO_WINDOW by pythonw.exe (běh na pozadí) otevíral okno konzole pro ffmpeg
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


DEFAULT_YT_TITLE = "{ucet} – TikTok LIVE {datum}"


class RecordConfig:
    def __init__(self, ffmpeg: str, out_dir: Path, quality: str, share_dir: Path | None = None,
                 overlay: bool = False, youtube_dir: Path | None = None,
                 youtube_title: str = DEFAULT_YT_TITLE, youtube_link: str = ""):
        self.ffmpeg, self.out_dir, self.quality, self.share_dir = ffmpeg, out_dir, quality, share_dir
        self.overlay = overlay
        self.youtube_dir = youtube_dir          # None = příprava pro YouTube vypnutá
        self.youtube_title, self.youtube_link = youtube_title, youtube_link

    @classmethod
    def from_env(cls) -> "RecordConfig | None":
        if os.getenv("RECORD", "").strip().lower() not in ("1", "true", "yes", "ano"):
            return None
        ffmpeg = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
        if not ffmpeg or not Path(ffmpeg).exists():
            log.warning("RECORD=1, ale ffmpeg nebyl nalezen (nainstaluj ho, nebo nastav FFMPEG_PATH) – nahrávání vypnuto")
            return None
        out = Path(os.getenv("RECORD_DIR") or "recordings")
        if not out.is_absolute():
            out = Path(__file__).resolve().parent / out
        quality = (os.getenv("RECORD_QUALITY") or "origin").strip().lower()
        if quality not in QUALITIES:
            quality = "origin"
        share = Path(os.getenv("RECORD_SHARE_DIR")) if os.getenv("RECORD_SHARE_DIR") else None
        overlay = env_on("RECORD_OVERLAY")
        yt_dir = None
        if env_on("YOUTUBE_PREP"):
            if not overlay:
                log.warning("YOUTUBE_PREP=1 vyžaduje RECORD_OVERLAY=1 (na YouTube jdou videa s overlayem) – "
                            "příprava pro YouTube vypnuta")
            else:
                yt_dir = Path(os.getenv("YOUTUBE_DIR")) if os.getenv("YOUTUBE_DIR") else (share or out) / "YouTube"
                if not yt_dir.is_absolute():
                    yt_dir = Path(__file__).resolve().parent / yt_dir
        title = (os.getenv("YOUTUBE_TITLE") or "").strip() or DEFAULT_YT_TITLE
        return cls(ffmpeg, out, quality, share, overlay, yt_dir, title, (os.getenv("YOUTUBE_LINK") or "").strip())


def pick_stream_url(room_info: dict, quality: str) -> str | None:
    """Najde FLV adresu streamu v room info; zkouší zvolenou kvalitu a pak horší."""
    su = room_info.get("stream_url") or {}
    try:
        data = json.loads(su["live_core_sdk_data"]["pull_data"]["stream_data"])["data"]
        order = QUALITIES[QUALITIES.index(quality):] + QUALITIES[:QUALITIES.index(quality)]
        for q in order:
            url = ((data.get(q) or {}).get("main") or {}).get("flv")
            if url:
                return url
    except (KeyError, TypeError, ValueError):
        pass
    for key in ("FULL_HD1", "HD1", "SD1", "SD2"):          # starší formát odpovědi
        if (su.get("flv_pull_url") or {}).get(key):
            return su["flv_pull_url"][key]
    return su.get("rtmp_pull_url") or None


class Recorder:
    """
    Nahrává LIVE přes ffmpeg (bez překódování) do .ts – ten zůstane přehratelný i při pádu PC.
    Když se spojení přeruší a vysílání pořád běží, naváže dalším dílem. Po skončení převede
    díly na .mp4 (bezztrátově) a .ts smaže.
    """

    def __init__(self, streamer: str, client, cfg: RecordConfig, room_id: str = "",
                 stream_id: int | None = None, store: "Store | None" = None):
        self.streamer, self.client, self.cfg = streamer, client, cfg
        self.stream_id, self.store = stream_id, store      # pro popis videa na YouTube (top dárci)
        self.proc: asyncio.subprocess.Process | None = None
        self.stopping = False
        self.parts: list[Path] = []
        self.task: asyncio.Task | None = None
        self.folder = cfg.out_dir / streamer
        self.base = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.events_path = self.folder / f"{self.base}.events.jsonl"
        self._events_failed = False
        self.event({"type": "meta", "streamer": streamer, "room_id": room_id, "v": 1})

    def event(self, data: dict) -> None:
        """Zapíše událost (dar, komentář, bitva…) s časem do <záznam>.events.jsonl – podklad pro overlay.py."""
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": round(time.time(), 3), **data}, ensure_ascii=False) + "\n")
        except Exception as ex:
            if not self._events_failed:
                log.warning("@%s: nepodařilo se zapsat události: %s", self.streamer, ex)
            self._events_failed = True

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        folder, base = self.folder, self.base
        folder.mkdir(parents=True, exist_ok=True)
        failures = 0
        while not self.stopping and failures < 5:
            try:   # s limitem: při výpadku spojení dotaz visel donekonečna a záznam se nikdy nedokončil (24. 11.)
                info = await asyncio.wait_for(self.client.web.fetch_room_info(), 30)
            except Exception as ex:
                log.warning("@%s: nahrávání – nepodařilo se načíst info o vysílání: %s: %s",
                            self.streamer, type(ex).__name__, ex)
                info = None
            if self.stopping:
                break
            if info is not None and info.get("status") == 4:           # 4 = vysílání skončilo
                break
            if info is not None and not self.parts:                    # kdo je hostitel (strana v bitvě)
                owner = info.get("owner") or {}
                self.event({"type": "owner", "id": str(owner.get("id_str") or owner.get("id") or ""),
                            "display_id": owner.get("display_id") or self.streamer,
                            "nick": owner.get("nickname") or ""})
            url = pick_stream_url(info or {}, self.cfg.quality)
            if not url:
                failures += 1
                await asyncio.sleep(10)
                continue

            n = len(self.parts) + 1
            part = folder / (f"{base}.ts" if n == 1 else f"{base}_dil{n}.ts")
            self.parts.append(part)
            log.info("@%s: nahrávám video → %s", self.streamer, part)
            RECORDING.add(self.streamer)
            self.event({"type": "part", "file": part.stem})            # čas začátku dílu = čas 0 videa
            self.proc = await asyncio.create_subprocess_exec(
                self.cfg.ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error",
                "-rw_timeout", "20000000",                            # 20 s bez dat → ffmpeg skončí
                "-user_agent", "Mozilla/5.0", "-i", url,
                "-c", "copy", "-f", "mpegts", "-y", str(part),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
            tail: collections.deque[str] = collections.deque(maxlen=5)
            async for line in self.proc.stderr:                     # čte do konce ffmpeg
                tail.append(line.decode(errors="replace").strip())
            code = await self.proc.wait()
            self.proc = None
            RECORDING.discard(self.streamer)
            if self.stopping:
                break
            size = part.stat().st_size if part.exists() else 0
            failures = 0 if size > 1_000_000 else failures + 1
            log.info("@%s: ffmpeg skončil (kód %s, %.1f MB)%s – zkusím navázat", self.streamer, code,
                     size / 1e6, (": " + " | ".join(tail)) if tail else "")
            # Kód 0 = TikTok stream ukončil (typicky začátek/konec duelu, kdy přepne obraz) → navázat hned,
            # ať se ztratí co nejméně; při chybě nebo prázdném dílu chvíli počkat.
            await asyncio.sleep(0.5 if code == 0 and size > 100_000 else 5)
        log.info("@%s: nahrávání skončilo, dokončuji záznam (%s díl/y)", self.streamer, len(self.parts))
        await self._finalize()

    async def stop(self) -> None:
        """Ukončí nahrávání slušně (klávesa q), aby se soubor korektně uzavřel, a počká na převod."""
        self.stopping = True
        RECORDING.discard(self.streamer)
        proc = self.proc
        if proc and proc.returncode is None:
            try:
                proc.stdin.write(b"q")
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), 20)
            except Exception:
                proc.kill()
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 600)
            except asyncio.TimeoutError:            # shield: převod/přesun/overlay pokračuje na pozadí
                log.warning("@%s: převod nebo přesun záznamu trvá déle než 10 min – běží dál na pozadí "
                            "(sběrač nevypínej, hotovo = řádek „záznam uložen“)", self.streamer)
            except Exception as ex:
                log.warning("@%s: dokončení nahrávky selhalo: %s: %s", self.streamer, type(ex).__name__, ex)

    async def _finalize(self) -> None:
        """Po vysílání: díly .ts → MP4. Víc dílů (TikTok stream ukončí při začátku/konci duelu) se spojí do jednoho
        videa <začátek>.mp4; když spojit nejdou (jiný rozměr, rozbité časy), zůstanou díly. Overlay se vyrábí
        z jednotlivých dílů (každý má vlastní čas začátku) a pak se taky spojí."""
        parts = [p for p in self.parts if p.exists() and p.stat().st_size >= 100_000]
        for p in self.parts:                                  # prázdné pokusy o navázání
            if p not in parts:
                p.unlink(missing_ok=True)
        multi = len(parts) > 1
        local: list[Path] = []
        for part in parts:
            try:
                name = f"{self.base}_dil1.mp4" if multi and part.stem == self.base else part.stem + ".mp4"
                mp4 = await self._remux(part, part.with_name(name))
                if mp4:
                    local.append(mp4)
            except Exception as ex:                       # např. soubor zamčený antivirem / synchronizací
                log.warning("@%s: dokončení %s selhalo: %s", self.streamer, part.name, ex)
        if not local:
            return
        merged = None
        if len(local) > 1:
            merged = await concat_videos(self.cfg.ffmpeg, local, self.folder / f"{self.base}.mp4", self.streamer)
        overlay = self.cfg.overlay and self.events_path.exists()
        raws = [merged] if merged else local
        keep_local = overlay and len(local) > 1           # overlay se vyrábí z dílů → díly zatím nechat
        saved: list[Path] = []
        for f in raws:
            dest = f
            try:
                if self.cfg.share_dir:                    # do sdílené složky až hotový soubor
                    dest = self.cfg.share_dir / self.streamer / f.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    log.info("@%s: přesouvám %s do %s…", self.streamer, f.name, dest.parent)
                    if keep_local and f in local:
                        await asyncio.to_thread(shutil.copyfile, str(f), str(dest))
                    else:
                        await asyncio.to_thread(shutil.move, str(f), str(dest))
                log.info("@%s: záznam uložen: %s (%.1f MB)", self.streamer, dest, dest.stat().st_size / 1e6)
            except Exception as ex:
                log.warning("@%s: přesun %s do sdílené složky selhal (%s) – zůstává %s", self.streamer, f.name, ex, f)
                dest = f
            saved.append(dest)
        if merged and not keep_local:                     # díly jsou ve spojeném videu
            for f in local:
                f.unlink(missing_ok=True)
        events = self.events_path
        if events.exists() and self.cfg.share_dir:        # kopie událostí k videu (ruční overlay.py)
            try:
                dest = self.cfg.share_dir / self.streamer / events.name
                await asyncio.to_thread(shutil.copyfile, str(events), str(dest))
            except Exception as ex:
                log.warning("@%s: kopie událostí do sdílené složky selhala: %s", self.streamer, ex)
        if overlay:
            videos = local if len(local) > 1 else saved
            task = asyncio.create_task(render_overlays(
                self.streamer, videos, events, self.cfg, self.store, self.stream_id,
                combine=self.base if len(local) > 1 else None, cleanup=local if len(local) > 1 else []))
            OVERLAY_TASKS.add(task)
            task.add_done_callback(OVERLAY_TASKS.discard)

    async def _remux(self, part: Path, mp4: Path) -> Path | None:
        """Bezztrátový převod dílu .ts → .mp4 (v pracovní složce); .ts se po úspěchu smaže."""
        log.info("@%s: převádím %s na MP4 (%.1f MB)…", self.streamer, part.name, part.stat().st_size / 1e6)
        proc = await asyncio.create_subprocess_exec(
            self.cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(part),
            "-c", "copy", "-movflags", "+faststart", str(mp4),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, creationflags=NO_WINDOW)
        if await proc.wait() == 0 and mp4.exists() and mp4.stat().st_size > 0:
            part.unlink(missing_ok=True)
            return mp4
        mp4.unlink(missing_ok=True)
        log.warning("@%s: převod na MP4 se nepovedl, záznam zůstává jako %s", self.streamer, part)
        return None


async def media_info(ffmpeg: str, video: Path) -> dict:
    """Základní parametry videa z výpisu `ffmpeg -i`: délka, kodek a rozměr obrazu, parametry zvuku."""
    info: dict = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-i", str(video), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        _, err = await proc.communicate()
        for line in err.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("Duration:") and "duration" not in info:
                try:
                    h, m, sec = line.split(",")[0].split()[1].split(":")
                    info["duration"] = int(h) * 3600 + int(m) * 60 + float(sec)
                except ValueError:
                    pass
            mv = re.search(r"Video: (\w+).*?, (\d{2,5})x(\d{2,5})", line)
            if mv and "video" not in info:
                info["video"] = (mv.group(1), int(mv.group(2)), int(mv.group(3)))
            ma = re.search(r"Audio: (\w+).*?, (\d+) Hz, ([\w.]+)", line)
            if ma and "audio" not in info:
                info["audio"] = (ma.group(1), int(ma.group(2)), ma.group(3))
    except Exception as ex:
        log.debug("parametry videa %s: %s", video.name, ex)
    return info


async def concat_videos(ffmpeg: str, videos: list[Path], out: Path, streamer: str) -> Path | None:
    """Spojí videa bez překódování (concat demuxer). Jen když mají stejný kodek, rozměr a zvuk – jinak by výsledek
    nešel správně přehrát. Při jakékoli chybě vrátí None a díly zůstanou samostatně."""
    infos = [await media_info(ffmpeg, v) for v in videos]
    keys = {(i.get("video"), i.get("audio")) for i in infos}
    if len(keys) != 1 or not infos[0].get("video"):
        log.warning("@%s: díly nejdou spojit bez překódování (různé parametry: %s) – zůstanou samostatně",
                    streamer, "; ".join(f"{v.name} {i.get('video')}" for v, i in zip(videos, infos)))
        return None
    lst = out.with_suffix(".concat.txt")
    try:
        lst.write_text("".join("file '" + str(v.resolve()).replace("'", "'\\''") + "'\n" for v in videos),
                       encoding="utf-8")
        log.info("@%s: spojuji %s dílů → %s…", streamer, len(videos), out.name)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-c", "copy", "-movflags", "+faststart", str(out),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        _, err = await proc.communicate()
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
        log.warning("@%s: spojení dílů se nepovedlo – zůstanou samostatně: %s", streamer,
                    err.decode(errors="replace").strip()[-300:])
    except Exception as ex:
        log.warning("@%s: spojení dílů se nepovedlo – zůstanou samostatně: %s", streamer, ex)
    finally:
        lst.unlink(missing_ok=True)
    out.unlink(missing_ok=True)
    return None


OVERLAY_TASKS: set[asyncio.Task] = set()
BACKGROUND: set[asyncio.Task] = set()
OVERLAY_LOCK = asyncio.Lock()
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if sys.platform == "win32" else 0


async def render_overlays(streamer: str, videos: list[Path], events: Path, cfg: RecordConfig,
                          store: "Store | None" = None, stream_id: int | None = None,
                          combine: str | None = None, cleanup: list[Path] | None = None) -> None:
    """Na pozadí (a se sníženou prioritou) vyrobí *_overlay.mp4 přes overlay.py – jedno video po druhém.
    combine = díly jednoho vysílání: všechny se vyrobí ve stejném rozměru a spojí do <combine>_overlay.mp4.
    S YOUTUBE_PREP=1 hotové video přesune do složky pro YouTube (+ .txt s názvem a popisem), s Telegramem pošle zprávu.
    cleanup = pracovní soubory (díly), které se po úspěchu smažou."""
    script = Path(__file__).resolve().with_name("overlay.py")
    size = None
    if combine:                                          # rozměr nejdelšího dílu (největší soubor)
        info = await media_info(cfg.ffmpeg, max(videos, key=lambda v: v.stat().st_size))
        if info.get("video"):
            size = f"{info['video'][1]}x{info['video'][2]}"
    async with OVERLAY_LOCK:
        rendered: list[tuple[Path, Path]] = []           # (zdrojové video, overlay)
        failed = False
        for video in videos:
            out = cfg.out_dir / streamer / f"{video.stem}_overlay.mp4"
            log.info("@%s: vyrábím video s dary a komentáři → %s (může trvat dlouho)", streamer, out.name)
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(script), str(video), "--events", str(events), "--out", str(out),
                    "--ffmpeg", cfg.ffmpeg, *(["--size", size] if size else []), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    creationflags=NO_WINDOW | BELOW_NORMAL)
                _, err = await proc.communicate()
                if proc.returncode != 0 or not out.exists():
                    failed = True
                    log.warning("@%s: overlay se nepovedl: %s", streamer, err.decode(errors="replace")[-500:])
                    notify(f"⚠️ <b>Video s overlayem se nepovedlo</b>\n@{tg_escape(streamer)} · "
                           f"<code>{tg_escape(video.name)}</code>\nPodrobnosti v logu sběrače.")
                    continue
                rendered.append((video, out))
            except Exception as ex:
                failed = True
                log.warning("@%s: overlay %s selhal: %s", streamer, video.name, ex)
                notify(f"⚠️ <b>Video s overlayem se nepovedlo</b>\n@{tg_escape(streamer)} · "
                       f"<code>{tg_escape(video.name)}</code>\n{tg_escape(str(ex)[:300])}")
        if combine and len(rendered) > 1 and not failed:  # všechny díly hotové → jedno video
            merged = await concat_videos(cfg.ffmpeg, [o for _, o in rendered],
                                         cfg.out_dir / streamer / f"{combine}_overlay.mp4", streamer)
            if merged:
                for _, o in rendered:
                    o.unlink(missing_ok=True)
                rendered = [(cfg.out_dir / streamer / f"{combine}.mp4", merged)]
        for video, out in rendered:
            try:
                if cfg.youtube_dir:
                    await prepare_youtube(streamer, video, out, cfg, store, stream_id)
                    continue
                if cfg.share_dir:
                    dest = cfg.share_dir / streamer / out.name
                    await asyncio.to_thread(shutil.move, str(out), str(dest))
                    out = dest
                log.info("@%s: video s overlayem hotové: %s", streamer, out)
                notify(f"🎬 <b>Video s overlayem hotové</b>\n@{tg_escape(streamer)}\n"
                       f"📁 <code>{tg_escape(str(out))}</code>")
            except Exception as ex:
                log.warning("@%s: přesun overlaye %s selhal: %s", streamer, out.name, ex)
        if cleanup and not failed:                       # díly už nejsou potřeba (jsou ve spojeném videu)
            for f in cleanup:
                f.unlink(missing_ok=True)
        elif cleanup:
            log.info("@%s: díly pro ruční overlay zůstávají v %s", streamer, cleanup[0].parent)


# --------------------------------------------------------------------------
# Příprava pro YouTube (YOUTUBE_PREP=1) – nahrává se ručně přes YouTube Studio
# (automatický upload přes API z neověřeného projektu by video natrvalo zamkl jako soukromé)
# --------------------------------------------------------------------------

def fmt_int(n) -> str:
    return f"{int(n or 0):,}".replace(",", " ")


def fmt_duration(sec: float | None) -> str:
    if not sec:
        return "?"
    s = int(round(sec))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def yt_clean(text: str) -> str:
    """YouTube nepovolí v názvu ani popisu znaky < a >."""
    return str(text or "").replace("<", "‹").replace(">", "›")


async def video_duration(ffmpeg: str, video: Path) -> float | None:
    """Délka videa z výpisu `ffmpeg -i` (stačí ffmpeg, ffprobe není potřeba)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-i", str(video), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, creationflags=NO_WINDOW)
        _, err = await proc.communicate()
        for line in err.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("Duration:"):
                h, m, s = line.split(",")[0].split()[1].split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception as ex:
        log.debug("délka videa %s: %s", video.name, ex)
    return None


def video_meta(video_stem: str) -> tuple[datetime | None, int]:
    """Z názvu záznamu RRRR-MM-DD_HH-MM[_dilN] vytáhne začátek nahrávání a číslo dílu."""
    base, part = video_stem, 1
    if "_dil" in video_stem:
        base, _, n = video_stem.rpartition("_dil")
        part = int(n) if n.isdigit() else 1
    try:
        return datetime.strptime(base, "%Y-%m-%d_%H-%M"), part
    except ValueError:
        return None, part


def youtube_title(template: str, streamer: str, start: datetime | None, part: int) -> str:
    values = {"ucet": streamer, "datum": f"{start.day}. {start.month}. {start.year}" if start else "",
              "cas": start.strftime("%H:%M") if start else "", "dil": str(part)}
    try:
        title = template.format_map(values)
    except (KeyError, ValueError, IndexError) as ex:
        log.warning("YOUTUBE_TITLE má chybu (%s) – použit výchozí název", ex)
        title = DEFAULT_YT_TITLE.format_map(values)
    if part > 1 and "{dil}" not in template:
        title += f" (díl {part})"
    return yt_clean(" ".join(title.split()))[:100]


def youtube_description(streamer: str, start: datetime | None, part: int, duration: float | None,
                        top: list[dict], totals: dict, link: str) -> str:
    when = f" z {start.day}. {start.month}. {start.year} {start:%H:%M}" if start else ""
    lines = [f"Záznam TikTok LIVE @{streamer}{when}" + (f", díl {part}" if part > 1 else "")
             + (f" (délka {fmt_duration(duration)})." if duration else ".")]
    if top:
        lines += ["", "🏆 Top dárci vysílání:"]
        for i, r in enumerate(top, 1):
            nick, handle = (r.get("nickname") or "").strip(), r.get("unique_id") or r.get("user_id") or "?"
            who = f"{nick} (@{handle})" if nick and nick != handle else f"@{handle}"
            lines.append(f"{r.get('rank') or i}. {who} – {fmt_int(r.get('diamonds'))} mincí")
        if totals.get("diamonds"):
            lines += ["", f"Celkem {fmt_int(totals['diamonds'])} mincí od {fmt_int(totals.get('donors'))} dárců."]
    if link:
        lines += ["", f"Celý žebříček dárců: {link}"]
    return yt_clean("\n".join(lines))[:4900]


async def prepare_youtube(streamer: str, source: Path, rendered: Path, cfg: RecordConfig,
                          store: "Store | None", stream_id: int | None) -> Path | None:
    """Přesune hotové video s overlayem do YOUTUBE_DIR, uloží vedle něj .txt s názvem a popisem
    a pošle upozornění na Telegram. Když něco selže, video zůstane, kde bylo (nic se neztratí)."""
    start, part = video_meta(source.stem)
    duration = await video_duration(cfg.ffmpeg, rendered)
    size = f"{rendered.stat().st_size / 1e9:.2f} GB".replace(".", ",")
    top, totals = [], {}
    if store is not None and stream_id is not None:
        try:
            top, totals = await store.stream_top(streamer, stream_id)
        except Exception as ex:
            log.warning("@%s: top dárce pro popis videa se nepodařilo načíst: %s", streamer, ex)
    title = youtube_title(cfg.youtube_title, streamer, start, part)
    desc = youtube_description(streamer, start, part, duration, top, totals, cfg.youtube_link)
    try:
        cfg.youtube_dir.mkdir(parents=True, exist_ok=True)
        dest = cfg.youtube_dir / f"{streamer}_{rendered.name}"
        info = dest.with_suffix(".txt")
        # .txt napřed – sync klient ho nahraje dřív než video, takže je u videa hned po jeho objevení
        info.write_text(f"NÁZEV:\n{title}\n\nPOPIS:\n{desc}\n\n---\nSoubor: {dest.name}\n"
                        f"Délka: {fmt_duration(duration)} · {size}\n", encoding="utf-8-sig")
        await asyncio.to_thread(shutil.move, str(rendered), str(dest))
    except Exception as ex:
        log.warning("@%s: přesun videa do složky pro YouTube selhal (%s) – zůstává %s", streamer, ex, rendered)
        notify(f"⚠️ <b>Video je hotové, ale přesun do složky pro YouTube selhal</b>\n"
                     f"📁 <code>{tg_escape(str(rendered))}</code>\n{tg_escape(str(ex)[:300])}")
        return None
    log.info("@%s: video připravené k nahrání na YouTube: %s", streamer, dest)
    when = f" · {start.day}. {start.month}. {start.year} {start:%H:%M}" if start else ""
    notify(f"🎬 <b>Nové video k nahrání na YouTube</b>\n@{tg_escape(streamer)}{when}"
                 f"{' · díl ' + str(part) if part > 1 else ''} · {fmt_duration(duration)} · {size}\n"
                 f"📁 <code>{tg_escape(str(dest))}</code>\n\nNázev (klepnutím zkopíruješ):\n"
                 f"<code>{tg_escape(title)}</code>",
           f"Popis:\n<pre>{tg_escape(desc[:3500])}</pre>")
    return dest


# --------------------------------------------------------------------------
# Telegram – upozornění (volitelné, TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
# --------------------------------------------------------------------------

TELEGRAM_API = "https://api.telegram.org"


def tg_escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    @classmethod
    def from_env(cls) -> "Telegram | None":
        token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
        if token and not chat:
            log.warning("TELEGRAM_BOT_TOKEN je vyplněný, ale chybí TELEGRAM_CHAT_ID – zjistíš ho: "
                        "python tracker.py --telegram-test")
        return cls(token, chat) if token and chat else None

    async def send(self, html: str, retries: int = 3) -> bool:
        """Pošle zprávu (HTML). Při výpadku sítě to zkusí znovu (po 10 s, 1 min, 5 min); selhání nevadí."""
        delays = [10, 60, 300]
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=20) as http:
                    r = await http.post(f"{TELEGRAM_API}/bot{self.token}/sendMessage",
                                        json={"chat_id": self.chat_id, "text": html, "parse_mode": "HTML",
                                              "disable_web_page_preview": True})
                if r.status_code == 200:
                    return True
                err = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in (400, 401, 403, 404):   # špatný token / chat / text – opakování nepomůže
                    log.warning("Telegram zprávu odmítl (%s)", err)
                    return False
            except Exception as ex:
                err = f"{type(ex).__name__}: {ex}"
            if attempt < retries:
                await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
        log.warning("Zprávu na Telegram se nepodařilo poslat: %s", err)
        return False


TELEGRAM: Telegram | None = None


def notify(*messages: str) -> None:
    """Pošle zprávy na Telegram na pozadí (v daném pořadí) – nečeká se na ně, takže opakované pokusy
    při výpadku sítě nezdržují další práci (např. výrobu dalšího videa)."""
    if TELEGRAM is None or not messages:
        return

    async def _send() -> None:
        for m in messages:
            await TELEGRAM.send(m)

    task = asyncio.create_task(_send())
    BACKGROUND.add(task)
    task.add_done_callback(BACKGROUND.discard)



# --------------------------------------------------------------------------
# Týdenní souhrn návštěvnosti webu -> Telegram (jen se secret klíčem a zapnutým Telegramem)
# --------------------------------------------------------------------------

REPORT_HOUR = 8                      # v pondělí od této hodiny (čas PC) se pošle souhrn minulého týdne
CZ_DAYS = ["Po", "Út", "St", "Čt", "Pá", "So", "Ne"]


def fmt_day(d: date) -> str:
    return f"{d.day}. {d.month}."


def visits_message(cur: dict, prev: dict, start: date, end: date) -> str:
    by_day = {date.fromisoformat(x["day"]): x for x in cur.get("per_day", [])}
    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        days.append(f"{CZ_DAYS[d.weekday()]} {by_day[d]['visitors'] if d in by_day else 0}")
    return (f"📊 <b>Návštěvnost webu</b> {fmt_day(start)} – {fmt_day(end)}\n"
            f"Návštěvníků: <b>{cur['visitors']}</b> (noví: {cur['new_visitors']}, minulý týden: {prev['visitors']})\n"
            f"Návštěv celkem: {cur['hits']}\n"
            f"Po dnech: {' · '.join(days)}")


async def weekly_visits_report(store: "Store") -> None:
    """Každé pondělí po REPORT_HOUR pošle souhrn minulého týdne. Když byl sběrač v pondělí vypnutý,
    pošle se při dalším spuštění (stav je v tabulce visit_reports, takže nikdy dvakrát)."""
    if store.token or TELEGRAM is None:      # tokenový sběrač tabulky nečte; bez Telegramu není kam psát
        return
    while True:
        try:
            now = datetime.now()
            monday = now.date() - timedelta(days=now.weekday())
            start, end = monday - timedelta(days=7), monday - timedelta(days=1)
            if now >= datetime.combine(monday, dtime(REPORT_HOUR)):
                r = await store._req("GET", "/visit_reports", params={"week_start": f"eq.{start}", "select": "week_start"})
                if not r.json():
                    async def summary(a: date, b: date) -> dict:
                        rr = await store._req("POST", "/rpc/visits_summary", json={"p_from": str(a), "p_to": str(b)})
                        return rr.json()
                    cur = await summary(start, end)
                    first = cur.get("first_day")
                    # Měření začalo až po konci hlášeného týdne -> nic nehlásit (jen označit jako vyřízené)
                    ok = True
                    if first and date.fromisoformat(first) <= end:
                        prev = await summary(start - timedelta(days=7), end - timedelta(days=7))
                        ok = await TELEGRAM.send(visits_message(cur, prev, start, end))
                    if ok:
                        await store._req("POST", "/visit_reports", params={"on_conflict": "week_start"},
                                         headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                                         json={"week_start": str(start)})
        except Exception as ex:
            log.warning("Týdenní souhrn návštěvnosti se nepovedl (zkusím za hodinu): %s", ex)
        await asyncio.sleep(3600)


async def telegram_test() -> int:
    """--telegram-test: bez TELEGRAM_CHAT_ID vypíše chaty, které botovi napsaly; s ním pošle zkušební zprávu."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("Chybí TELEGRAM_BOT_TOKEN v .env – bota založíš v Telegramu u @BotFather (/newbot).")
        return 1
    chat = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    async with httpx.AsyncClient(timeout=20) as http:
        if not chat:
            r = await http.get(f"{TELEGRAM_API}/bot{token}/getUpdates")
            if r.status_code != 200:
                print(f"Telegram odmítl token (HTTP {r.status_code}): {r.text[:200]}")
                return 1
            chats = {}
            for upd in r.json().get("result", []):
                msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
                c = msg.get("chat") or {}
                if c.get("id") is not None:
                    chats[c["id"]] = c.get("title") or " ".join(
                        x for x in (c.get("first_name"), c.get("last_name")) if x) or c.get("username") or ""
            if not chats:
                print("Bot zatím nemá žádnou zprávu. Otevři ho v Telegramu, pošli mu cokoli (např. /start) "
                      "a spusť test znovu.")
                return 1
            print("Doplň do .env jeden z těchto řádků:")
            for cid, name in chats.items():
                print(f"  TELEGRAM_CHAT_ID={cid}    ({name})")
            return 0
    ok = await Telegram(token, chat).send("✅ Sběrač TikTok Giftboard: Telegram funguje, o nových videích "
                                          "k nahrání ti dám vědět.", retries=0)
    print("Zkušební zpráva odeslána." if ok else "Zprávu se nepodařilo poslat – viz chyba výše.")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Hlídání LIVE
# --------------------------------------------------------------------------

async def watch(streamer: str, store: Store, interval: int, rec_cfg: RecordConfig | None = None) -> None:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import (CommentEvent, ConnectEvent, DisconnectEvent, GiftEvent, LinkMicArmiesEvent,
                                   LinkMicBattleEvent, LiveEndEvent)

    had_error = False       # po chybě (výpadek spojení) jednou zalogovat, že zase hlídá
    close_pending = True    # při startu i po každém LIVE: až účet nevysílá, uzavřít visící vysílání v DB
    while True:
        client = TikTokLiveClient(unique_id="@" + streamer)
        # Známá chyba TikTokLive/TikTokLiveProto: zprávy o bitvách (RivalExtra s hashtagem) nejdou přečíst
        # ('bytes' object has no attribute 'HashtagNamespace'). Dary se netýká → jen do debug logu.
        if hasattr(client, "parse_error_ignorelist"):
            client.parse_error_ignorelist.append("HashtagNamespace")
        state: dict = {"stream_id": None, "rec": None}

        async def finish(*_):
            LIVE.discard(streamer)
            rec, state["rec"] = state["rec"], None
            if rec is not None:
                await rec.stop()
            sid, state["stream_id"] = state["stream_id"], None
            if sid is not None:
                try:
                    await store.close_stream(sid)
                except Exception as ex:
                    log.warning("@%s: nepodařilo se uzavřít vysílání: %s", streamer, ex)

        try:
            if not await client.is_live():
                if had_error:
                    log.info("@%s: spojení s TikTokem zase funguje – nevysílá, hlídám dál", streamer)
                    had_error = False
                if close_pending:
                    try:
                        await store.close_open_streams(streamer)
                        close_pending = False
                    except Exception as ex:
                        log.warning("@%s: nepodařilo se uzavřít staré vysílání: %s", streamer, ex)
                log.debug("@%s nevysílá, další kontrola za %ss", streamer, interval)
                await asyncio.sleep(interval)
                continue

            async def room_start(stream_id: int) -> None:
                try:
                    info = await client.web.fetch_room_info() or {}
                    started = float(info.get("create_time") or info.get("start_time") or 0)
                    if started > 1e12:                       # ms → s
                        started /= 1000
                    if started > 0:
                        await store.set_room_start(stream_id, started)
                except Exception as ex:
                    log.debug("@%s: začátek vysílání se nepodařilo zjistit: %s", streamer, ex)

            @client.on(ConnectEvent)
            async def _on_connect(ev: ConnectEvent):
                state["stream_id"] = await store.open_stream(streamer, str(ev.room_id))
                LIVE.add(streamer)
                task = asyncio.create_task(room_start(state["stream_id"]))
                BACKGROUND.add(task)
                task.add_done_callback(BACKGROUND.discard)
                log.info("@%s je LIVE (room %s) – sbírám dary", streamer, ev.room_id)
                if rec_cfg and state["rec"] is None:
                    state["rec"] = Recorder(streamer, client, rec_cfg, str(ev.room_id),
                                            stream_id=state["stream_id"], store=store)
                    state["rec"].start()

            def rec_event(data: dict, ev=None) -> None:
                rec = state["rec"]
                if rec is not None:
                    st = getattr(getattr(ev, "common", None), "create_time", 0) if ev is not None else 0
                    rec.event({**data, "st": int(st or 0)} if st else data)

            @client.on(CommentEvent)
            async def _on_comment(ev: CommentEvent):
                try:
                    if ev.user is not None:
                        rec_event({"type": "comment", "user": ev.user.unique_id, "nick": ev.user.nickname,
                                   "text": ev.content}, ev)
                except Exception as ex:
                    log.debug("komentář: %s", ex)

            @client.on(LinkMicBattleEvent)
            async def _on_battle(ev: LinkMicBattleEvent):
                try:
                    bs = ev.battle_settings
                    anchors = {}
                    for w in ev.anchors_info or []:
                        u = getattr(w.value, "user", None) if w.value else None
                        if u is not None:
                            anchors[str(w.key)] = {"display_id": u.display_id, "nick": u.nick_name}
                    rec_event({"type": "battle", "battle_id": str(ev.battle_id),
                               "action": getattr(ev.action, "name", str(ev.action)),
                               "start_ms": int(getattr(bs, "start_time_ms", 0) or 0) if bs else 0,
                               "duration": int(getattr(bs, "duration", 0) or 0) if bs else 0,
                               "end_ms": int(getattr(bs, "end_time_ms", 0) or 0) if bs else 0,
                               "anchors": anchors,
                               "scores": {str(w.key): int(w.value.hostscore) for w in ev.armies or [] if w.value},
                               "result": {str(k): {"score": int(v.score), "result": getattr(v.result, "name", str(v.result))}
                                          for k, v in (ev.battle_result or {}).items()},
                               # série výher v řadě, jak ji počítá TikTok („WIN x8“)
                               "combo": {str(k): int(v.combo_count) for k, v in (ev.battle_combo_v2 or {}).items()}}, ev)
                    action = getattr(ev.action, "name", str(ev.action))
                    label = {"OPEN": "Duel ZAČAL", "ACCEPT": "Duel ZAČAL", "FINISH": "Duel SKONČIL",
                             "LINK_MIC_BATTLE_BATTLE_ACTION_CUT_SHORT": "Duel SKONČIL (předčasně)"}.get(action,
                                                                                                  f"Duel: {action}")
                    who = ", ".join("@" + a["display_id"] for a in anchors.values())
                    log.info("@%s: %s%s", streamer, label, f" ({who})" if who else "")
                except Exception as ex:
                    log.warning("@%s: událost bitvy se nepodařilo zpracovat: %s", streamer, ex)

            @client.on(LinkMicArmiesEvent)
            async def _on_armies(ev: LinkMicArmiesEvent):
                try:
                    rec_event({"type": "score", "battle_id": str(ev.battle_id),
                               "scores": {str(k): int(v.hostscore) for k, v in (ev.armies or {}).items()}}, ev)
                except Exception as ex:
                    log.warning("@%s: skóre bitvy se nepodařilo zpracovat: %s", streamer, ex)

            @client.on(GiftEvent)
            async def _on_gift(ev: GiftEvent):
                if state["stream_id"] is None or ev.gift is None or ev.user is None:
                    return
                try:                                        # pro overlay i průběh série (x1, x2, … x12)
                    rec_event({"type": "gift", "uid": str(ev.user.id), "user": ev.user.unique_id,
                               "nick": ev.user.nickname, "gift": ev.gift.name, "gift_id": ev.gift.id,
                               "count": max(int(ev.repeat_count or 1), 1), "unit": int(ev.gift.diamond_count or 0),
                               "streak": bool(ev.streaking)}, ev)
                except Exception as ex:
                    log.debug("dar do událostí: %s", ex)
                # Streakovatelné dary (Rose…) chodí průběžně; počítá se až konec série,
                # který nese celkový repeat_count – jinak by se započítaly vícekrát.
                if ev.streaking:
                    return
                count = max(int(ev.repeat_count or 1), 1)
                msg_id = getattr(ev.common, "msg_id", 0) if ev.common else 0
                diamonds = count * int(ev.gift.diamond_count or 0)
                store.add_gift({
                    "stream_id": state["stream_id"], "streamer": streamer,
                    "user_id": str(ev.user.id), "unique_id": ev.user.unique_id,
                    "nickname": ev.user.nickname, "gift_id": ev.gift.id,
                    "gift_name": ev.gift.name, "count": count, "diamonds": diamonds,
                    "msg_id": str(msg_id) if msg_id else None,
                })
                log.info("@%s ← %s: %sx %s (%s 💎)", streamer, ev.user.unique_id,
                         count, ev.gift.name, diamonds)
                try:
                    store.add_avatar(str(ev.user.id), avatar_url(ev.user))
                    store.add_image("gift", ev.gift.id, gift_image_url(ev.gift))
                except Exception as ex:
                    log.debug("obrázky k daru: %s", ex)

            client.add_listener(LiveEndEvent, finish)
            client.add_listener(DisconnectEvent, finish)

            close_pending = True
            await client.connect(fetch_live_check=False)   # blokuje do konce vysílání
            log.info("@%s: spojení ukončeno", streamer)
            await finish()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            rec = state["rec"]
            if rec is not None and rec.proc is not None and rec.proc.returncode is None:
                rec.proc.terminate()                 # .ts zůstane přehratelný i bez dokončení
            raise
        except Exception as ex:  # TikTok občas změní API / sign server odmítne požadavek
            log.warning("@%s: %s: %s – zkusím znovu za %ss",
                        streamer, type(ex).__name__, ex, interval)
            had_error = True
            try:
                await client.disconnect()
            except Exception:
                pass
            await finish()
            await asyncio.sleep(interval)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

async def run(usernames: list[str], interval: int) -> None:
    url, token = os.getenv("SUPABASE_URL"), (os.getenv("COLLECTOR_TOKEN") or "").strip() or None
    if token:   # sběrač u někoho jiného: veřejný klíč + token, secret klíč se nepoužije
        key = os.getenv("SUPABASE_PUBLISHABLE_KEY")
        if not url or not key:
            log.error("Chybí SUPABASE_URL nebo SUPABASE_PUBLISHABLE_KEY v .env (viz .env.example).")
            raise SystemExit(1)
    else:
        key = os.getenv("SUPABASE_SECRET_KEY")
        if not url or not key or key == "sb_secret_...":
            log.error("Chybí SUPABASE_URL a SUPABASE_SECRET_KEY (nebo COLLECTOR_TOKEN) v .env (viz .env.example).")
            raise SystemExit(1)
    if os.getenv("EULER_API_KEY"):
        from TikTokLive.client.web.web_settings import WebDefaults
        WebDefaults.tiktok_sign_api_key = os.getenv("EULER_API_KEY")

    global TELEGRAM
    TELEGRAM = Telegram.from_env()
    if TELEGRAM:
        log.info("Upozornění na Telegram zapnuto")
    rec_cfg = RecordConfig.from_env()
    if rec_cfg:
        log.info("Nahrávání videa zapnuto → %s (kvalita %s)%s", rec_cfg.out_dir, rec_cfg.quality,
                 f", hotová MP4 se přesunou do {rec_cfg.share_dir}" if rec_cfg.share_dir else "")
        if rec_cfg.youtube_dir:
            log.info("Příprava pro YouTube zapnuta → videa s overlayem + popis do %s", rec_cfg.youtube_dir)
    store = Store(url, key, token)
    streamers = [norm(u) for u in usernames]
    try:
        token_name = await store.register(streamers)
    except RuntimeError as ex:
        if token and ("28000" in str(ex) or "42501" in str(ex)):
            log.error("Token sběrače nefunguje: %s – požádej vlastníka o nový token / povolení účtu.", ex)
            raise SystemExit(1)
        raise
    if token_name:
        log.info("Přihlášen tokenem jako sběrač „%s“", token_name)
    log.info("Hlídám: %s (kontrola každých %ss)", ", ".join("@" + s for s in streamers), interval)
    name = token_name or (os.getenv("COLLECTOR_NAME") or socket.gethostname() or "sběrač").strip()[:60]
    await asyncio.gather(store.writer(), store.image_worker(), store.heartbeat(name, streamers, bool(rec_cfg)),
                         weekly_visits_report(store),
                         *(watch(s, store, interval, rec_cfg) for s in streamers))


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    p = argparse.ArgumentParser(description="TikTok LIVE – sběr darů do Supabase")
    p.add_argument("usernames", nargs="*", help="účty k hlídání (nebo env STREAMERS=ucet1,ucet2)")
    p.add_argument("--interval", type=int, default=int(os.getenv("CHECK_INTERVAL", 60)),
                   help="jak často kontrolovat, jestli je účet LIVE (s)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--telegram-test", action="store_true",
                   help="ověří Telegram bota: vypíše TELEGRAM_CHAT_ID, nebo pošle zkušební zprávu")
    a = p.parse_args()
    if a.telegram_test:
        raise SystemExit(asyncio.run(telegram_test()))

    users = a.usernames or [u for u in os.getenv("STREAMERS", "").split(",") if u.strip()]
    if not users:
        p.error("zadej aspoň jeden účet")
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:                       # pythonw.exe (běh na pozadí) nemá konzoli
        handlers.append(logging.StreamHandler())
    if os.getenv("LOG_FILE"):                        # log do souboru, max 3× 5 MB
        from logging.handlers import RotatingFileHandler
        log_path = Path(os.getenv("LOG_FILE"))
        if not log_path.is_absolute():
            log_path = Path(__file__).with_name(log_path.name)
        handlers.append(RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, handlers=handlers or None,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not a.verbose:
        logging.getLogger("TikTokLive").setLevel(logging.WARNING)
    try:
        asyncio.run(run(users, a.interval))
    except KeyboardInterrupt:
        log.info("Ukončeno.")


if __name__ == "__main__":
    main()
