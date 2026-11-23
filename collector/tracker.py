#!/usr/bin/env python3
"""
Sběrač darů z TikTok LIVE -> Supabase
-------------------------------------
Hlídá zadané účty. Jakmile některý začne vysílat, připojí se k LIVE a každý dar
(kdo, co, kolik diamantů) zapíše do Supabase. Žebříček pak zobrazuje web na Netlify.

Konfigurace přes proměnné prostředí (nebo soubor .env vedle skriptu):
    SUPABASE_URL          https://xxxx.supabase.co
    SUPABASE_SECRET_KEY   secret (sb_secret_...) nebo legacy service_role klíč
    EULER_API_KEY         volitelné – klíč pro podepisovací server TikTokLive
    LOG_FILE              volitelné – zapisovat log i do souboru (např. collector.log)

Použití:
    python tracker.py @ucet1 [@ucet2 ...] [--interval 60]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("collector")


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
    """Zápisy do Supabase. Dary jdou přes frontu, takže výpadek sítě nic neztratí."""

    def __init__(self, url: str, key: str):
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

    async def ensure_streamer(self, name: str) -> None:
        await self._req("POST", "/streamers", params={"on_conflict": "name"},
                        headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                        json={"name": name})

    async def open_stream(self, streamer: str, room_id: str) -> int:
        """Založí vysílání; při znovupřipojení do stejné místnosti vrátí existující."""
        r = await self._req("POST", "/streams", params={"on_conflict": "streamer,room_id", "select": "id"},
                            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
                            json={"streamer": streamer, "room_id": room_id, "ended_at": None})
        return int(r.json()[0]["id"])

    async def close_stream(self, stream_id: int) -> None:
        await self._req("PATCH", "/streams",
                        params={"id": f"eq.{stream_id}", "ended_at": "is.null"},
                        headers={"Prefer": "return=minimal"},
                        json={"ended_at": now_iso()})

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
                    await self._req("POST", "/gifts", params={"on_conflict": "msg_id"},
                                    headers={"Prefer": "resolution=ignore-duplicates,return=minimal"},
                                    json=batch)
                    break
                except Exception as ex:
                    log.warning("Zápis %s darů selhal (%s) – zkusím za %ss", len(batch), ex, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)


# --------------------------------------------------------------------------
# Hlídání LIVE
# --------------------------------------------------------------------------

async def watch(streamer: str, store: Store, interval: int) -> None:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import ConnectEvent, DisconnectEvent, GiftEvent, LiveEndEvent

    while True:
        client = TikTokLiveClient(unique_id="@" + streamer)
        state: dict = {"stream_id": None}

        async def finish(*_):
            sid, state["stream_id"] = state["stream_id"], None
            if sid is not None:
                try:
                    await store.close_stream(sid)
                except Exception as ex:
                    log.warning("@%s: nepodařilo se uzavřít vysílání: %s", streamer, ex)

        try:
            if not await client.is_live():
                log.debug("@%s nevysílá, další kontrola za %ss", streamer, interval)
                await asyncio.sleep(interval)
                continue

            @client.on(ConnectEvent)
            async def _on_connect(ev: ConnectEvent):
                state["stream_id"] = await store.open_stream(streamer, str(ev.room_id))
                log.info("@%s je LIVE (room %s) – sbírám dary", streamer, ev.room_id)

            @client.on(GiftEvent)
            async def _on_gift(ev: GiftEvent):
                if state["stream_id"] is None or ev.gift is None or ev.user is None:
                    return
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

            await client.connect(fetch_live_check=False)   # blokuje do konce vysílání
            log.info("@%s: spojení ukončeno", streamer)
            await finish()
            await asyncio.sleep(10)
        except asyncio.CancelledError:
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
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key:
        log.error("Chybí SUPABASE_URL nebo SUPABASE_SECRET_KEY v .env (viz .env.example).")
        raise SystemExit(1)
    if os.getenv("EULER_API_KEY"):
        from TikTokLive.client.web.web_settings import WebDefaults
        WebDefaults.tiktok_sign_api_key = os.getenv("EULER_API_KEY")

    store = Store(url, key)
    streamers = [norm(u) for u in usernames]
    for s in streamers:
        await store.ensure_streamer(s)
    log.info("Hlídám: %s (kontrola každých %ss)", ", ".join("@" + s for s in streamers), interval)
    await asyncio.gather(store.writer(), *(watch(s, store, interval) for s in streamers))


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
