#!/usr/bin/env python3
"""
Překryvná vrstva do záznamu LIVE
--------------------------------
Video z TikToku obsahuje jen obraz a zvuk – skóre bitvy, dary ani komentáře v něm nejsou (kreslí je až
aplikace). Sběrač si je ale během nahrávání ukládá do <záznam>.events.jsonl. Tenhle skript z nich udělá
titulky ve formátu ASS a vypálí je do kopie videa (<video>_overlay.mp4):

  * nahoře pruh bitvy se skóre obou stran a odpočtem, po konci VÝHRA / PROHRA,
  * vlevo dole oznámení darů (průběh série x1 → x12), velké dary i uprostřed,
  * pod nimi posledních pár komentářů.

Použití (ve složce collector):
    .venv\\Scripts\\python.exe overlay.py "cesta\\k\\video.mp4"            celé video
    .venv\\Scripts\\python.exe overlay.py video.mp4 --start 600 --duration 60   jen ukázka (rychlé)
    .venv\\Scripts\\python.exe overlay.py video.mp4 --frame 615               jeden snímek jako PNG
    --delay 2      posune vše o 2 s později (záporné = dřív), když dary „předbíhají“ obraz
    --events X     soubor událostí, když neleží vedle videa
    --from-db      starší záznam bez souboru událostí: jen dary, stažené ze Supabase (secret klíč z .env)
                   + --start-time "2026-11-23 16:55:43" (přesný začátek videa; jinak se bere minuta z názvu)
Nebo přetáhni MP4 myší na overlay.bat.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- vzhled (barvy ASS = &HAABBGGRR)
FONT = os.getenv("OVERLAY_FONT", "Arial")
PINK, BLUE = "&H5A3DFF&", "&HF2C433&"           # strana hostitelky / soupeře (#FF3D5A, #33C4F2)
GOLD, GREY = "&H4AD5FF&", "&HCFCFCF&"
TOAST_LANES = 3          # kolik oznámení darů může být vidět najednou
TOAST_HOLD = 3.0         # s – jak dlouho oznámení zůstane po posledním daru série
COMBO_GAP = 8.0          # s – pauza, po které se stejný dar počítá jako nová série
BIG_GIFT = 99            # mincí – od kolika se dar ukáže i velký uprostřed
COMMENTS = 6             # kolik komentářů je vidět
COMMENT_TTL = 45.0       # s – po jaké době komentář zmizí
RESULT_HOLD = 180.0      # s – jak dlouho po konci bitvy zůstane výsledek (vítězné kolo)


# ---------------------------------------------------------------- pomocné funkce
def clean(text: str | None, limit: int = 40) -> str:
    """Přezdívky bývají ze „𝐟𝐚𝐧𝐜𝐲“ písmen a emoji – převeď na běžná písmena, emoji vyhoď (font je nemá)."""
    s = unicodedata.normalize("NFKC", text or "")
    out = []
    for ch in s:
        cat = unicodedata.category(ch)
        if ord(ch) > 0xFFFF or cat in ("So", "Cs", "Co", "Cn", "Cf", "Mn") or 0xFE00 <= ord(ch) <= 0xFE0F:
            continue
        out.append(" " if cat.startswith("Z") or ch in "\r\n\t" else ch)
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    s = s.replace("\\", "/").replace("{", "(").replace("}", ")")
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def name_of(ev: dict) -> str:
    return clean(ev.get("nick")) or clean(ev.get("user")) or "?"


def ass_time(sec: float) -> str:
    sec = max(sec, 0.0)
    cs = int(round(sec * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def num(n: int) -> str:
    return f"{n:,}".replace(",", " ")                    # 1 820 jako na TikToku


def rect(w: float, h: float) -> str:
    w, h = round(w), round(h)
    return f"m 0 0 l {w} 0 {w} {h} 0 {h}"


def find_ffmpeg(explicit: str | None) -> str:
    cand = explicit or os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
    if not cand or not Path(cand).exists():
        sys.exit("ffmpeg nenalezen – nainstaluj ho (winget install ffmpeg) nebo nastav FFMPEG_PATH.")
    return cand


def probe(ffmpeg: str, video: Path) -> tuple[int, int, float]:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix))
    if not Path(ffprobe).exists():
        ffprobe = shutil.which("ffprobe") or ffprobe
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", str(video)],
                         capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    st = info["streams"][0]
    return int(st["width"]), int(st["height"]), float(info.get("format", {}).get("duration") or 0)


def find_events(video: Path) -> Path | None:
    stem = re.sub(r"(_overlay)?$", "", video.stem)
    stem = re.sub(r"_dil\d+$", "", stem)
    here = Path(__file__).resolve().parent
    for folder in (video.parent, here / "recordings" / video.parent.name, here / "recordings"):
        p = folder / f"{stem}.events.jsonl"
        if p.exists():
            return p
    return None


def load_events(path: Path) -> list[dict]:
    evs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                evs.append(json.loads(line))
            except json.JSONDecodeError:
                pass                                   # useknutý poslední řádek po pádu PC
    return sorted(evs, key=lambda e: e.get("t", 0))


def part_start(events: list[dict], video: Path) -> float:
    """Čas (epoch) začátku dílu videa = čas 0 ve videu."""
    stem = re.sub(r"_overlay$", "", video.stem)
    parts = [e for e in events if e.get("type") == "part"]
    for e in parts:
        if e.get("file") == stem:
            return float(e["t"])
    if len(parts) == 1:
        return float(parts[0]["t"])
    m = re.match(r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2})", stem)   # nouzově podle názvu souboru (přesnost na minutu)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M").timestamp()
    sys.exit("Nevím, kdy video začalo (chybí záznam 'part' v událostech). Použij --start-time.")


def gifts_from_db(streamer: str, t0: float, duration: float) -> list[dict]:
    """Dary ze Supabase pro časový úsek videa (pro záznamy pořízené před ukládáním událostí)."""
    import httpx
    from datetime import timezone
    env = Path(__file__).resolve().with_name(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key or key == "sb_secret_...":
        sys.exit("--from-db potřebuje SUPABASE_URL a SUPABASE_SECRET_KEY v collector/.env")
    headers = {"apikey": key}
    if key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {key}"
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    r = httpx.get(url.rstrip("/") + "/rest/v1/gifts", headers=headers, timeout=30, params=[
        ("select", "ts,user_id,unique_id,nickname,gift_name,gift_id,count,diamonds"),
        ("streamer", f"eq.{streamer}"), ("ts", f"gte.{iso(t0 - 5)}"), ("ts", f"lte.{iso(t0 + duration + 5)}"),
        ("order", "ts"), ("limit", "20000")])
    r.raise_for_status()
    out = []
    for g in r.json():
        cnt = max(int(g["count"] or 1), 1)
        out.append({"t": datetime.fromisoformat(g["ts"].replace("Z", "+00:00")).timestamp(), "type": "gift",
                    "uid": g["user_id"], "user": g["unique_id"], "nick": g["nickname"], "gift": g["gift_name"],
                    "gift_id": g["gift_id"], "count": cnt, "unit": int(g["diamonds"] or 0) // cnt, "streak": False})
    return out


# ---------------------------------------------------------------- stavba titulků
class Ass:
    def __init__(self, w: int, h: int):
        self.w, self.h, self.lines = w, h, []
        u = h / 1280 if h / w >= 1.4 else w / 720       # měřítko vůči 720×1280
        self.u = u
        fs = round(30 * u)
        self.header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Toast,{FONT},{fs},&H00FFFFFF,&H00FFFFFF,&H70000000,&H00000000,0,0,0,0,100,100,0,0,3,{round(9*u)},0,1,0,0,0,1
Style: Big,{FONT},{round(46*u)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{round(3*u)},{round(2*u)},8,0,0,0,1
Style: Comment,{FONT},{round(27*u)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,{max(1, round(2*u))},{max(1, round(1*u))},1,0,0,0,1
Style: Shape,{FONT},10,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Score,{FONT},{round(30*u)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{round(2*u)},0,5,0,0,0,1
Style: Timer,{FONT},{round(28*u)},&H00FFFFFF,&H00FFFFFF,&H90000000,&H00000000,1,0,0,0,100,100,0,0,3,{round(7*u)},0,8,0,0,0,1
Style: Result,{FONT},{round(40*u)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,1,0,0,100,100,0,0,1,{round(3*u)},{round(2*u)},5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def add(self, start: float, end: float, style: str, text: str, layer: int = 0) -> None:
        if end - start >= 0.02:
            self.lines.append((start, f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}"))

    def dump(self) -> str:
        return self.header + "\n".join(l for _, l in sorted(self.lines, key=lambda x: x[0])) + "\n"


def build_gifts(ass: Ass, gifts: list[dict], vt) -> None:
    """Dary seskupí do sérií (stejný dárce + dar), každá série = jedno oznámení s rostoucím xN."""
    combos: list[dict] = []
    open_: dict[tuple, dict] = {}
    for e in gifts:
        key = (e.get("uid") or e.get("user"), e.get("gift_id") or e.get("gift"))
        c = open_.get(key)
        if c is None or e["t"] - c["steps"][-1][0] > COMBO_GAP:
            c = {"ev": e, "steps": []}
            combos.append(c)
            open_[key] = c
        count = int(e.get("count") or 1)
        if not c["steps"] or count != c["steps"][-1][1]:
            c["steps"].append((e["t"], count))
        else:
            c["steps"][-1] = (c["steps"][-1][0], count)
        c["last"] = e["t"]
        if not e.get("streak"):
            open_.pop(key, None)                        # série skončila

    u, h = ass.u, ass.h
    lane_free = [-1e9] * TOAST_LANES
    lane_y = [h * 0.705 - i * 86 * u for i in range(TOAST_LANES)]
    x = round(20 * u)
    for c in sorted(combos, key=lambda c: c["steps"][0][0]):
        start, end = vt(c["steps"][0][0]), vt(c["last"]) + TOAST_HOLD
        lane = next((i for i in range(TOAST_LANES) if lane_free[i] <= start), None)
        if lane is None:
            lane = min(range(TOAST_LANES), key=lambda i: lane_free[i])
        lane_free[lane] = end
        e = c["ev"]
        who, gift = name_of(e), clean(e.get("gift"), 24) or "dárek"
        steps = c["steps"]
        for i, (t, count) in enumerate(steps):
            s = vt(t)
            nxt = vt(steps[i + 1][0]) if i + 1 < len(steps) else end
            fade = r"\fad(150,0)" if i == 0 else ""
            if i == len(steps) - 1:
                fade = r"\fad(150,300)" if i == 0 else r"\fad(0,300)"
            pop = rf"\t(0,120,\fscx125\fscy125)\t(120,240,\fscx100\fscy100)" if i > 0 else ""
            text = (rf"{{\pos({x},{round(lane_y[lane])}){fade}}}{{\b1}}{who}{{\b0}}\N"
                    rf"{{\fs{round(24*u)}\c{GREY}}}poslal(a) {gift}  "
                    rf"{{\fs{round(40*u)}\b1\i1\c{GOLD}{pop}}}x{count}")
            ass.add(s, nxt, "Toast", text, layer=3)
        total = int(e.get("unit") or 0) * steps[-1][1]
        if total >= BIG_GIFT:
            text = (rf"{{\pos({ass.w // 2},{round(h * 0.13)})\fad(200,400)\t(0,250,\fscx115\fscy115)"
                    rf"\t(250,500,\fscx100\fscy100)}}{who}\N{{\c{GOLD}}}{gift} x{steps[-1][1]}"
                    rf"{{\fs{round(28*u)}\c&HFFFFFF&}}\N{total} mincí")
            ass.add(start, start + 4.0, "Big", text, layer=5)


def build_comments(ass: Ass, comments: list[dict], vt) -> None:
    u, h = ass.u, ass.h
    x, y = round(20 * u), round(h * 0.975)
    for i, e in enumerate(comments):
        start = vt(e["t"])
        end = vt(comments[i + 1]["t"]) if i + 1 < len(comments) else start + COMMENT_TTL
        end = min(end, start + COMMENT_TTL)
        shown = [c for c in comments[max(0, i - COMMENTS + 1): i + 1] if e["t"] - c["t"] <= COMMENT_TTL]
        lines = [rf"{{\b1\c{GREY}}}{name_of(c)}{{\b0\c&HFFFFFF&}}  {clean(c.get('text'), 60) or '(emoji)'}"
                 for c in shown]
        ass.add(start, end, "Comment", rf"{{\pos({x},{y})}}" + r"\N".join(lines), layer=2)


def our_side(events: list[dict], streamer: str) -> str | None:
    """ID hostitelky v bitvě: z události 'owner', podle display_id soupeřů, nebo podle toho, čí skóre roste po našich darech."""
    for e in events:
        if e.get("type") == "owner" and e.get("id"):
            return str(e["id"])
    for e in events:
        if e.get("type") == "battle":
            for aid, a in (e.get("anchors") or {}).items():
                if (a.get("display_id") or "").lower() == streamer.lower():
                    return aid
    votes: dict[str, int] = {}
    gifts = [e["t"] for e in events if e.get("type") == "gift" and not e.get("streak")]
    prev: dict[str, int] = {}
    for e in events:
        if e.get("type") != "score":
            continue
        for aid, sc in (e.get("scores") or {}).items():
            if sc > prev.get(aid, 0) and any(0 <= e["t"] - g <= 3 for g in gifts):
                votes[aid] = votes.get(aid, 0) + 1
            prev[aid] = sc
    return max(votes, key=votes.get) if votes else None


def build_battles(ass: Ass, events: list[dict], vt, streamer: str, video_end: float) -> None:
    ours = our_side(events, streamer)
    st_offsets = [e["t"] - e["st"] / 1000 for e in events if e.get("st")]
    clock = statistics.median(st_offsets) if st_offsets else 0.0       # rozdíl hodin PC vs. TikTok

    battles: list[dict] = []          # {id, start, end_at, finish, scores:[(t,{aid:sc})], result}
    cur: dict | None = None
    finish_actions = ("FINISH", "LINK_MIC_BATTLE_BATTLE_ACTION_CUT_SHORT")
    for e in events:
        typ, bid = e.get("type"), e.get("battle_id")
        if typ == "battle":
            action = e.get("action", "")
            if action not in ("OPEN", "ACCEPT") + finish_actions:
                continue                                # pozvánky, odmítnutí apod. pruh nezobrazí
            if cur is None or cur["id"] != bid:
                start = e["t"]
                if e.get("start_ms") and action in ("OPEN", "ACCEPT"):
                    start = min(e["t"], e["start_ms"] / 1000 + clock) if clock else e["t"]
                cur = {"id": bid, "start": start, "end_at": None, "finish": None, "scores": [], "result": {}}
                battles.append(cur)
            end_ms = e.get("end_ms") or ((e.get("start_ms") or 0) + 1000 * (e.get("duration") or 0))
            if e.get("start_ms") and end_ms and cur["end_at"] is None:
                cur["end_at"] = end_ms / 1000 + clock
            if e.get("scores"):
                cur["scores"].append((e["t"], dict(e["scores"])))
            if action in finish_actions:
                cur["finish"] = e["t"]
                cur["result"] = {k: v.get("score", 0) for k, v in (e.get("result") or {}).items()}
        elif typ == "score" and e.get("scores"):
            if cur is None or cur["id"] != bid:
                cur = {"id": bid, "start": e["t"], "end_at": None, "finish": None, "scores": [], "result": {}}
                battles.append(cur)
            cur["scores"].append((e["t"], dict(e["scores"])))

    u, w = ass.u, ass.w
    x0, y0, bw, bh = round(w * 0.03), round(ass.h * 0.035), round(w * 0.94), round(42 * u)
    for i, b in enumerate(battles):
        nxt = battles[i + 1]["start"] if i + 1 < len(battles) else None
        if b["finish"]:
            stop = b["finish"] + RESULT_HOLD
        elif b["end_at"]:
            stop = b["end_at"] + RESULT_HOLD
        else:
            stop = (b["scores"][-1][0] if b["scores"] else b["start"]) + 60
        stop = min(stop, video_end, nxt if nxt is not None else stop)
        # průběh skóre: (čas, naše, soupeř)
        timeline: list[tuple[float, int, int]] = [(b["start"], 0, 0)]
        for t, sc in b["scores"]:
            me = ours if ours in sc else (next(iter(sc)) if sc else None)
            mine = sc.get(me, 0) if me else 0
            other = sum(v for k, v in sc.items() if k != me)
            timeline.append((t, mine, other))
        if b["result"]:
            me = ours if ours in b["result"] else next(iter(b["result"]))
            timeline.append((b["finish"], b["result"].get(me, 0), sum(v for k, v in b["result"].items() if k != me)))
        for j, (t, mine, other) in enumerate(timeline):
            s = vt(t)
            e = vt(timeline[j + 1][0]) if j + 1 < len(timeline) else vt(stop)
            if e <= s:
                continue
            frac = 0.5 if mine + other == 0 else min(max(mine / (mine + other), 0.07), 0.93)
            lw = round(bw * frac)
            ass.add(s, e, "Shape", rf"{{\pos({x0},{y0})\p1\c{PINK}\1a&H10&}}{rect(lw, bh)}", layer=1)
            ass.add(s, e, "Shape", rf"{{\pos({x0 + lw},{y0})\p1\c{BLUE}\1a&H10&}}{rect(bw - lw, bh)}", layer=1)
            ass.add(s, e, "Score", rf"{{\an4\pos({x0 + round(12*u)},{y0 + bh // 2})}}" + num(mine), layer=2)
            ass.add(s, e, "Score", rf"{{\an6\pos({x0 + bw - round(12*u)},{y0 + bh // 2})}}" + num(other), layer=2)
        # odpočet
        if b["end_at"]:
            t = b["start"]
            end_at = min(b["end_at"], b["finish"] or b["end_at"])
            while t < end_at:
                left = max(int(end_at - t + 0.999), 0)
                ass.add(vt(t), vt(min(t + 1, end_at)), "Timer",
                        rf"{{\pos({w // 2},{y0 + bh + round(8*u)})}}{left // 60:02d}:{left % 60:02d}", layer=2)
                t += 1
        # výsledek
        if b["finish"]:
            final = timeline[-1]
            if final[1] == final[2]:
                labels = ("REMÍZA", "REMÍZA")
            else:
                labels = ("VÝHRA", "PROHRA") if final[1] > final[2] else ("PROHRA", "VÝHRA")
            yy = y0 + bh + round(70 * u)
            for k, (lab, xx) in enumerate(zip(labels, (x0 + bw // 4, x0 + 3 * bw // 4))):
                col = GOLD if lab == "VÝHRA" else GREY
                ass.add(vt(b["finish"]), vt(stop), "Result",
                        rf"{{\pos({xx},{yy})\c{col}\fad(200,300)}}{lab}", layer=2)


def build_ass(events: list[dict], t0: float, delay: float, w: int, h: int, duration: float,
              offset: float = 0.0) -> str:
    streamer = next((e.get("streamer") for e in events if e.get("type") == "meta"), "") or ""
    ass = Ass(w, h)

    def vt(t: float) -> float:
        return t - t0 + delay - offset

    video_end = t0 + duration if duration else max((e["t"] for e in events), default=t0)
    build_gifts(ass, [e for e in events if e.get("type") == "gift"], vt)
    build_comments(ass, [e for e in events if e.get("type") == "comment"], vt)
    build_battles(ass, events, vt, streamer, video_end)
    return ass.dump()


# ---------------------------------------------------------------- CLI
def main() -> None:
    p = argparse.ArgumentParser(description="Vypálí do záznamu LIVE dary, komentáře a bitvy.")
    p.add_argument("video", type=Path)
    p.add_argument("--events", type=Path, help="soubor .events.jsonl (výchozí: vedle videa)")
    p.add_argument("--out", type=Path, help="výstup (výchozí: <video>_overlay.mp4)")
    p.add_argument("--delay", type=float, default=float(os.getenv("OVERLAY_DELAY", 0)),
                   help="posun událostí v s (+ později, - dřív)")
    p.add_argument("--start", type=float, default=0.0, help="ukázka: od které sekundy videa")
    p.add_argument("--duration", type=float, default=0.0, help="ukázka: kolik sekund")
    p.add_argument("--frame", type=float, help="uložit jen jeden snímek v dané sekundě (PNG)")
    p.add_argument("--start-time", help="ruční začátek videa, např. '2026-11-23 16:55:43'")
    p.add_argument("--preset", default=os.getenv("OVERLAY_PRESET", "veryfast"), help="x264 preset (rychlost)")
    p.add_argument("--crf", default=os.getenv("OVERLAY_CRF", "23"))
    p.add_argument("--ass-only", action="store_true", help="jen vytvořit soubor titulků .ass")
    p.add_argument("--from-db", action="store_true", help="bez souboru událostí: dary ze Supabase")
    p.add_argument("--streamer", help="účet (pro --from-db; výchozí = název složky s videem)")
    p.add_argument("--ffmpeg")
    a = p.parse_args()

    video = a.video.resolve()
    if not video.exists():
        sys.exit(f"Video nenalezeno: {video}")
    ffmpeg = find_ffmpeg(a.ffmpeg)
    w, h, duration = probe(ffmpeg, video)
    events_path = a.events or find_events(video)
    if a.from_db:
        streamer = (a.streamer or video.parent.name).lstrip("@").lower()
        events = [{"t": 0, "type": "meta", "streamer": streamer}]
        t0 = datetime.strptime(a.start_time, "%Y-%m-%d %H:%M:%S").timestamp() if a.start_time \
            else part_start([], video)
        events += gifts_from_db(streamer, t0, duration)
    else:
        if not events_path or not events_path.exists():
            sys.exit("Nenašel jsem soubor událostí (.events.jsonl) vedle videa – zadej ho přes --events,\n"
                     "nebo u starších záznamů použij --from-db (jen dary).")
        events = load_events(events_path)
        t0 = datetime.strptime(a.start_time, "%Y-%m-%d %H:%M:%S").timestamp() if a.start_time \
            else part_start(events, video)

    lead = min(2.0, a.frame) if a.frame is not None else 0.0
    offset = (a.frame - lead) if a.frame is not None else a.start
    ass_text = build_ass(events, t0, a.delay, w, h, duration, offset=offset)
    counts = {k: sum(1 for e in events if e.get("type") == k) for k in ("gift", "comment", "battle", "score")}
    print(f"Události: {counts['gift']} darů, {counts['comment']} komentářů, "
          f"{counts['battle']} zpráv o bitvě, {counts['score']} změn skóre; video {w}×{h}, {duration/60:.1f} min")

    suffix = "_overlay" if a.frame is None else f"_overlay_{int(a.frame)}s"
    out = (a.out or video.with_name(video.stem + suffix + (".png" if a.frame is not None else ".mp4"))).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        ass_file = Path(tmp) / "overlay.ass"            # krátké jméno bez diakritiky → bez escapování ve filtru
        ass_file.write_text(ass_text, encoding="utf-8")
        if a.ass_only:
            target = out.with_suffix(".ass")
            shutil.copyfile(ass_file, target)
            print(f"Titulky: {target}")
            return
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-stats", "-y"]
        if offset:
            cmd += ["-ss", f"{offset:.3f}"]
        cmd += ["-i", str(video), "-vf", "ass=overlay.ass"]
        if a.frame is not None:
            cmd += ["-ss", f"{lead:.3f}", "-frames:v", "1", "-update", "1", str(out)]
        else:
            if a.duration:
                cmd += ["-t", f"{a.duration:.3f}"]
            cmd += ["-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf), "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", str(out)]
        print("Vyrábím:", out)
        r = subprocess.run(cmd, cwd=tmp)
        if r.returncode != 0:
            sys.exit(f"ffmpeg skončil s chybou {r.returncode}")
    print("Hotovo:", out)


if __name__ == "__main__":
    main()
