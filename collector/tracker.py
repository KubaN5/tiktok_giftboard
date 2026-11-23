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
    COLLECTOR_NAME        volitelné – jméno sběrače na webu (výchozí název počítače)

Použití:
    python tracker.py @ucet1 [@ucet2 ...] [--interval 60]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("collector")
HEARTBEAT_EVERY = 30   # s; web považuje sběrač za offline po 2 min bez zprávy
LIVE: set[str] = set()        # účty, jejichž LIVE právě sbíráme
RECORDING: set[str] = set()   # účty, jejichž LIVE právě nahráváme


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def add_gift(self, row: dict) -> None:
        row.setdefault("ts", now_iso())
        self.queue.put_nowait(row)

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


class RecordConfig:
    def __init__(self, ffmpeg: str, out_dir: Path, quality: str, share_dir: Path | None = None,
                 overlay: bool = False):
        self.ffmpeg, self.out_dir, self.quality, self.share_dir = ffmpeg, out_dir, quality, share_dir
        self.overlay = overlay

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
        overlay = os.getenv("RECORD_OVERLAY", "").strip().lower() in ("1", "true", "yes", "ano")
        return cls(ffmpeg, out, quality, share, overlay)


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

    def __init__(self, streamer: str, client, cfg: RecordConfig, room_id: str = ""):
        self.streamer, self.client, self.cfg = streamer, client, cfg
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
            try:
                info = await self.client.web.fetch_room_info()
            except Exception as ex:
                log.warning("@%s: nahrávání – nepodařilo se načíst info o vysílání: %s", self.streamer, ex)
                info = None
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
            await asyncio.sleep(5)
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
            except Exception as ex:
                log.warning("@%s: dokončení nahrávky selhalo: %s", self.streamer, ex)

    async def _finalize(self) -> None:
        done: list[Path] = []
        for part in self.parts:
            try:
                mp4 = await self._finalize_part(part)
                if mp4:
                    done.append(mp4)
            except Exception as ex:                       # např. soubor zamčený antivirem / synchronizací
                log.warning("@%s: dokončení %s selhalo: %s", self.streamer, part.name, ex)
        events = self.events_path
        if done and events.exists() and self.cfg.share_dir:   # kopie událostí k videu (ruční overlay.py)
            try:
                dest = self.cfg.share_dir / self.streamer / events.name
                await asyncio.to_thread(shutil.copyfile, str(events), str(dest))
            except Exception as ex:
                log.warning("@%s: kopie událostí do sdílené složky selhala: %s", self.streamer, ex)
        if done and self.cfg.overlay and events.exists():
            task = asyncio.create_task(render_overlays(self.streamer, done, events, self.cfg))
            OVERLAY_TASKS.add(task)
            task.add_done_callback(OVERLAY_TASKS.discard)

    async def _finalize_part(self, part: Path) -> Path | None:
        if not part.exists() or part.stat().st_size < 100_000:
            part.unlink(missing_ok=True)
            return None
        mp4 = part.with_suffix(".mp4")
        proc = await asyncio.create_subprocess_exec(
            self.cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(part),
            "-c", "copy", "-movflags", "+faststart", str(mp4),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, creationflags=NO_WINDOW)
        if await proc.wait() == 0 and mp4.exists() and mp4.stat().st_size > 0:
            part.unlink(missing_ok=True)
            size = mp4.stat().st_size / 1e6
            if self.cfg.share_dir:                        # do sdílené složky až hotový soubor
                dest = self.cfg.share_dir / self.streamer / mp4.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.move, str(mp4), str(dest))
                mp4 = dest
            log.info("@%s: záznam uložen: %s (%.1f MB)", self.streamer, mp4, size)
            return mp4
        mp4.unlink(missing_ok=True)
        log.warning("@%s: převod na MP4 se nepovedl, záznam zůstává jako %s", self.streamer, part)
        return None


OVERLAY_TASKS: set[asyncio.Task] = set()
BACKGROUND: set[asyncio.Task] = set()
OVERLAY_LOCK = asyncio.Lock()
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if sys.platform == "win32" else 0


async def render_overlays(streamer: str, videos: list[Path], events: Path, cfg: RecordConfig) -> None:
    """Na pozadí (a se sníženou prioritou) vyrobí *_overlay.mp4 přes overlay.py – jedno video po druhém."""
    script = Path(__file__).resolve().with_name("overlay.py")
    async with OVERLAY_LOCK:
        for video in videos:
            out = cfg.out_dir / streamer / f"{video.stem}_overlay.mp4"
            log.info("@%s: vyrábím video s dary a komentáři → %s (může trvat dlouho)", streamer, out.name)
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(script), str(video), "--events", str(events), "--out", str(out),
                    "--ffmpeg", cfg.ffmpeg, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                    creationflags=NO_WINDOW | BELOW_NORMAL)
                _, err = await proc.communicate()
                if proc.returncode != 0 or not out.exists():
                    log.warning("@%s: overlay se nepovedl: %s", streamer, err.decode(errors="replace")[-500:])
                    continue
                if cfg.share_dir:
                    dest = cfg.share_dir / streamer / out.name
                    await asyncio.to_thread(shutil.move, str(out), str(dest))
                    out = dest
                log.info("@%s: video s overlayem hotové: %s", streamer, out)
            except Exception as ex:
                log.warning("@%s: overlay %s selhal: %s", streamer, video.name, ex)


# --------------------------------------------------------------------------
# Hlídání LIVE
# --------------------------------------------------------------------------

async def watch(streamer: str, store: Store, interval: int, rec_cfg: RecordConfig | None = None) -> None:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import (CommentEvent, ConnectEvent, DisconnectEvent, GiftEvent, LinkMicArmiesEvent,
                                   LinkMicBattleEvent, LiveEndEvent)

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
                    state["rec"] = Recorder(streamer, client, rec_cfg, str(ev.room_id))
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
                                          for k, v in (ev.battle_result or {}).items()}}, ev)
                    log.info("@%s: bitva %s (%s)", streamer, getattr(ev.action, "name", ev.action),
                             ", ".join(a["display_id"] for a in anchors.values()))
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

    rec_cfg = RecordConfig.from_env()
    if rec_cfg:
        log.info("Nahrávání videa zapnuto → %s (kvalita %s)%s", rec_cfg.out_dir, rec_cfg.quality,
                 f", hotová MP4 se přesunou do {rec_cfg.share_dir}" if rec_cfg.share_dir else "")
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
    await asyncio.gather(store.writer(), store.heartbeat(name, streamers, bool(rec_cfg)),
                         *(watch(s, store, interval, rec_cfg) for s in streamers))


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    p = argparse.ArgumentParser(description="TikTok LIVE – sběr darů do Supabase")
    p.add_argument("usernames", nargs="*", help="účty k hlídání (nebo env STREAMERS=ucet1,ucet2)")
    p.add_argument("--interval", type=int, default=int(os.getenv("CHECK_INTERVAL", 60)),
                   help="jak často kontrolovat, jestli je účet LIVE (s)")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

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
