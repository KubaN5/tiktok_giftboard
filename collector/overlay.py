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
COMMENTS = 5             # kolik komentářů je vidět (nejvýš – musí se vejít pod okna bitvy)
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
    for deco, repl in DECOR.items():          # ozdobné „srdíčko“ z cizího písma → ♥ (Arial ho má)
        s = s.replace(deco, repl)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


# Znaky z exotických písem, které se v přezdívkách používají jako ozdoba (Arial je nemá → libass je nevykreslí
# a zahltí výpis varováním „fontselect: failed to find any fallback with glyph …“).
DECOR = {"ꨄ": "♥", "ᥫ᭡": "♥", "ᥫ": "♥", "᭡": ""}


def run_ffmpeg(cmd: list[str], cwd: str) -> int:
    """Spustí ffmpeg a jeho výpis propustí dál, jen opakovaná varování libass o chybějícím znaku v písmu
    („fontselect: failed to find any fallback …“, chodí u každého snímku) vypíše jen jednou."""
    proc = subprocess.Popen(cmd, cwd=cwd, stderr=subprocess.PIPE)
    out = sys.stderr.buffer if hasattr(sys.stderr, "buffer") else None
    seen: set[bytes] = set()
    buf = b""
    while True:
        chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, "read1") else proc.stderr.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if b"fontselect: failed to find any fallback" in line:
                key = line.split(b"glyph", 1)[-1].strip()
                if key in seen:
                    continue
                seen.add(key)
                line += "  (znak se nevykreslí; další stejná varování skrývám)".encode()
            if out:
                out.write(line + b"\n"); out.flush()
        if b"\r" in buf and out:                # průběžný stav (frame=… time=…) se přepisuje na jednom řádku
            head, buf = buf.rsplit(b"\r", 1)
            out.write(head + b"\r"); out.flush()
    if buf and out:
        out.write(buf); out.flush()
    return proc.wait()


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
    alt = stem[:-5] if stem.endswith("_dil1") else None   # 1. díl vícedílného záznamu sběrač přejmenuje na _dil1
    for e in parts:
        if e.get("file") in (stem, alt):
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


# ---------------------------------------------------------------- tvary a odhad šířky textu
NARROW, WIDE = set("ijlI.,:;!|'()[]{} f t"), set("mwMW@%")


def text_w(s: str, fs: float, bold: bool = False) -> float:
    """Hrubý odhad šířky textu v Arialu (bez fontu nejde změřit přesně) – stačí na velikost bubliny."""
    w = 0.0
    for ch in s:
        if ch in NARROW:
            w += 0.30
        elif ch in WIDE:
            w += 0.86
        elif ch.isupper() or ch.isdigit():
            w += 0.67
        else:
            w += 0.55
    return w * fs * (1.08 if bold else 1.0)


def rrect(w: float, h: float, r: float) -> str:
    """Zaoblený obdélník jako ASS kresba."""
    w, h = round(w), round(h)
    r = max(0, min(round(r), w // 2, h // 2))
    if r == 0:
        return rect(w, h)
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")


AVATAR_COLORS = ["&H7A5CFF&", "&HE0A33D&", "&H5BC86B&", "&HD86FD6&", "&H3FB3FF&", "&HB07BFF&", "&H6BD4F5&"]


# ---------------------------------------------------------------- stavba titulků
class Ass:
    def __init__(self, w: int, h: int):
        self.w, self.h, self.lines = w, h, []
        u = h / 1280 if h / w >= 1.4 else w / 720       # měřítko vůči 720×1280
        self.u = u
        o, s = max(1, round(2 * u)), max(1, round(1 * u))
        self.header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Shape,{FONT},10,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Text,{FONT},{round(24*u)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Shadowed,{FONT},{round(24*u)},&H00FFFFFF,&H00FFFFFF,&H50000000,&H80000000,0,0,0,0,100,100,0,0,1,{o},{s},7,0,0,0,1
Style: Big,{FONT},{round(44*u)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{round(3*u)},{round(2*u)},5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def add(self, start: float, end: float, style: str, text: str, layer: int = 0) -> None:
        if end - start >= 0.02:
            self.lines.append((start, f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}"))

    def shape(self, start, end, x, y, drawing, color, alpha="&H00&", layer=1, extra=""):
        self.add(start, end, "Shape", rf"{{\an7\pos({round(x)},{round(y)})\p1\bord0\shad0\c{color}\1a{alpha}{extra}}}{drawing}",
                 layer)

    def dump(self) -> str:
        return self.header + "\n".join(l for _, l in sorted(self.lines, key=lambda x: x[0])) + "\n"


def build_gifts(ass: Ass, gifts: list[dict], vt) -> None:
    """Dary seskupí do sérií (stejný dárce + dar); každá série = bublina jako v TikToku s rostoucím xN."""
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
        c["last"] = e["t"]
        if not e.get("streak"):
            open_.pop(key, None)                        # série skončila

    u, h = ass.u, ass.h
    ph, pad, av = round(62 * u), round(5 * u), round(52 * u)
    fs_name, fs_sub, fs_cnt = round(23 * u), round(20 * u), round(48 * u)
    lane_free = [-1e9] * TOAST_LANES
    lane_y = [round(h * 0.575 - i * (ph + 12 * u)) for i in range(TOAST_LANES)]
    x = round(14 * u)
    for c in sorted(combos, key=lambda c: c["steps"][0][0]):
        start, end = vt(c["steps"][0][0]), vt(c["last"]) + TOAST_HOLD
        lane = next((i for i in range(TOAST_LANES) if lane_free[i] <= start), None)
        if lane is None:
            lane = min(range(TOAST_LANES), key=lambda i: lane_free[i])
        lane_free[lane] = end
        e, y = c["ev"], lane_y[lane]
        who, gift = name_of(e), clean(e.get("gift"), 22) or "dárek"
        sub = f"poslal(a) {gift}"
        pw = pad + av + round(10 * u) + max(text_w(who, fs_name, True), text_w(sub, fs_sub)) + round(20 * u)
        col = AVATAR_COLORS[sum(map(ord, str(e.get("uid") or who))) % len(AVATAR_COLORS)]
        fade = r"\fad(180,300)"
        slide = rf"\move({x - 40},{y},{x},{y},0,180)"
        # bublina, avatar s iniciálou, jméno a dar
        ass.add(start, end, "Shape", rf"{{\an7{slide}\p1\bord0\shad0\c&H000000&\1a&H70&{fade}}}{rrect(pw, ph, ph / 2)}", 3)
        ass.add(start, end, "Shape", rf"{{\an7\move({x - 40 + pad},{y + pad},{x + pad},{y + pad},0,180)\p1\bord0\shad0"
                                     rf"\c{col}{fade}}}{rrect(av, av, av / 2)}", 4)
        ass.add(start, end, "Text", rf"{{\an5\move({x - 40 + pad + av // 2},{y + ph // 2},{x + pad + av // 2},{y + ph // 2},0,180)"
                                    rf"\b1\fs{round(26*u)}{fade}}}{initial(who)}", 5)
        tx = x + pad + av + round(10 * u)
        ass.add(start, end, "Text", rf"{{\an1\move({tx - 40},{y + ph // 2 + 1},{tx},{y + ph // 2 + 1},0,180)\b1\fs{fs_name}"
                                    rf"{fade}}}{who}", 5)
        ass.add(start, end, "Text", rf"{{\an7\move({tx - 40},{y + ph // 2 + round(3*u)},{tx},{y + ph // 2 + round(3*u)},0,180)"
                                    rf"\fs{fs_sub}\c&HE6E6E6&{fade}}}{sub}", 5)
        # xN vedle bubliny – při každém zvýšení „poskočí“
        cx = round(x + pw + 10 * u)
        steps = c["steps"]
        for i, (t, count) in enumerate(steps):
            s = vt(t)
            nxt = vt(steps[i + 1][0]) if i + 1 < len(steps) else end
            f = (r"\fad(120,0)" if i == 0 else "") + (r"\fad(0,300)" if i == len(steps) - 1 and i > 0 else "")
            if i == 0 and len(steps) == 1:
                f = r"\fad(120,300)"
            pop = r"\fscx150\fscy150\t(0,160,\fscx100\fscy100)"
            ass.add(s, nxt, "Shadowed", rf"{{\an4\pos({cx},{y + ph // 2})\b1\i1\fs{fs_cnt}\bord{round(3*u)}\3c&H552CFE&"
                                        rf"\shad0{pop}{f}}}x{count}", 6)
        total = int(e.get("unit") or 0) * steps[-1][1]
        if total >= BIG_GIFT:
            text = (rf"{{\pos({ass.w // 2},{round(h * 0.36)})\fad(200,400)\fscx60\fscy60\t(0,250,\fscx110\fscy110)"
                    rf"\t(250,450,\fscx100\fscy100)}}{who}\N{{\c{GOLD}}}{gift} x{steps[-1][1]}"
                    rf"{{\fs{round(26*u)}\c&HFFFFFF&}}\N{total} mincí")
            ass.add(start, start + 4.0, "Big", text, layer=7)


def wrap(text: str, fs: float, max_w: float, max_lines: int = 2) -> list[str]:
    """Zalomí text podle odhadnuté šířky; co se nevejde, zkrátí se třemi tečkami."""
    words, lines, cur = text.split(" "), [], ""
    for wd in words:
        cand = (cur + " " + wd).strip()
        if text_w(cand, fs) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = wd
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    out = []
    for ln in lines:                                   # jedno extrémně dlouhé slovo
        while text_w(ln, fs) > max_w and len(ln) > 4:
            ln = ln[:-2].rstrip("…") + "…"
        out.append(ln)
    return out


def initial(name: str) -> str:
    return next((ch for ch in name if ch.isalnum()), "?").upper()


def build_comments(ass: Ass, comments: list[dict], vt, video_len: float,
                   layout: tuple[int, int, int, int] | None = None) -> None:
    """Komentáře jako v aplikaci: bublina s iniciálou, jméno šedě, text bíle; nové přijíždí zespodu."""
    if not comments:
        return
    u, w, h = ass.u, ass.w, ass.h
    # komentáře jen pod okny bitvy (nebo do spodní třetiny), ať nepřekrývají obličeje a dary
    min_y = max(round(h * 0.60), layout[1] + layout[3] + round(10 * u)) if layout else round(h * 0.62)
    top = max(min_y - round(30 * u), round(h * 0.55))   # jemný tmavý přechod dole
    for k in range(10):
        y = top + k * (h - top) // 10
        ass.shape(0, video_len + 60, 0, y, rect(w, h - y), "&H000000&", "&HEE&", layer=0)

    fs_n, fs_t, av, pad, gap = round(19 * u), round(23 * u), round(34 * u), round(8 * u), round(6 * u)
    line_h = round(fs_t * 1.18)
    max_text_w = w * 0.66
    x0, bottom = round(12 * u), round(h * 0.975)

    def row(c: dict) -> dict:
        who = name_of(c)
        lines = wrap(clean(c.get("text"), 120) or "(emoji)", fs_t, max_text_w)
        bh = pad + round(fs_n * 1.15) + line_h * len(lines) + pad
        bw = pad + av + round(9 * u) + 1.08 * max(text_w(who, fs_n, True), max(text_w(l, fs_t) for l in lines)) + pad + round(8 * u)
        col = AVATAR_COLORS[sum(map(ord, str(c.get("user") or who))) % len(AVATAR_COLORS)]
        return {"who": who, "lines": lines, "h": bh, "w": bw, "col": col}

    rows = [row(c) for c in comments]
    alphas = ["&H00&", "&H00&", "&H00&", "&H30&", "&H70&", "&HA0&"]  # nejstarší nahoře blednou
    for i, e in enumerate(comments):
        start = vt(e["t"])
        end = vt(comments[i + 1]["t"]) if i + 1 < len(comments) else start + COMMENT_TTL
        end = min(end, start + COMMENT_TTL)
        idx = [j for j in range(max(0, i - COMMENTS + 1), i + 1) if e["t"] - comments[j]["t"] <= COMMENT_TTL]
        shift = rows[i]["h"] + gap                      # o kolik se ostatní posunou nahoru
        y = bottom
        for depth, j in enumerate(reversed(idx)):       # odspodu: nejnovější první
            r = rows[j]
            if y - r["h"] < min_y:
                break                                   # nevejde se – starší komentáře už se neukážou
            y -= r["h"]
            fa = alphas[min(depth, len(alphas) - 1)]
            ta = {"&H00&": "&H00&", "&H30&": "&H40&", "&H70&": "&H80&", "&HA0&": "&HB0&"}[fa]
            if depth == 0:
                mv = lambda yy: rf"\move({x0},{yy + 20},{x0},{yy},0,180)\fad(150,0)"
            else:
                mv = lambda yy, s=shift: rf"\move({x0},{yy + s},{x0},{yy},0,180)"
            # bublina
            box_a = "&H50&" if fa == "&H00&" else ("&H78&" if fa == "&H30&" else "&HB0&")   # šedá – vidět i na černé
            ass.add(start, end, "Shape", rf"{{\an7{mv(y)}\p1\bord0\shad0\c&H2A2A2A&\1a{box_a}}}{rrect(r['w'], r['h'], round(14*u))}", 2)
            # avatar s iniciálou
            ay = y + (r["h"] - av) // 2
            ass.add(start, end, "Shape", rf"{{\an7\move({x0 + pad},{ay + (20 if depth == 0 else shift)},{x0 + pad},{ay},0,180)"
                                         rf"\p1\bord0\shad0\c{r['col']}\1a{fa}}}{rrect(av, av, av / 2)}", 3)
            ass.add(start, end, "Text", rf"{{\an5\move({x0 + pad + av // 2},{ay + av // 2 + (20 if depth == 0 else shift)},"
                                        rf"{x0 + pad + av // 2},{ay + av // 2},0,180)\b1\fs{round(18*u)}\1a{ta}}}"
                                        rf"{initial(r['who'])}", 4)
            # jméno a text
            tx = x0 + pad + av + round(9 * u)
            dy = 20 if depth == 0 else shift
            ass.add(start, end, "Text", rf"{{\an7\move({tx},{y + pad + dy},{tx},{y + pad},0,180)\b1\fs{fs_n}\c&HBEBEBE&\1a{ta}}}"
                                        rf"{r['who']}", 4)
            ass.add(start, end, "Text", rf"{{\an7\move({tx},{y + pad + round(fs_n*1.15) + dy},{tx},{y + pad + round(fs_n*1.15)},0,180)"
                                        rf"\fs{fs_t}\1a{ta}}}" + r"\N".join(r["lines"]), 4)
            y -= gap


def our_side(events: list[dict], streamer: str) -> str | None:
    """ID hostitelky v bitvě: z události 'owner', podle display_id, nebo podle toho, čí skóre roste po našich darech."""
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


def parse_battles(events: list[dict]) -> list[dict]:
    """Bitvy z událostí: {id, start, end_at, finish, scores:[(t,{aid:sc})], result} (časy = hodiny PC)."""
    st_offsets = [e["t"] - e["st"] / 1000 for e in events if e.get("st")]
    clock = statistics.median(st_offsets) if st_offsets else 0.0       # rozdíl hodin PC vs. TikTok
    battles: list[dict] = []
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
                if e.get("start_ms") and action in ("OPEN", "ACCEPT") and clock:
                    start = min(e["t"], e["start_ms"] / 1000 + clock)
                cur = {"id": bid, "start": start, "end_at": None, "finish": None, "scores": [], "result": {},
                       "combo": {}, "combo_end": {}}
                battles.append(cur)
            if e.get("combo"):                          # série výher od TikToku: při startu / po konci
                cur["combo_end" if action in finish_actions else "combo"] = dict(e["combo"])
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
                cur = {"id": bid, "start": e["t"], "end_at": None, "finish": None, "scores": [], "result": {},
                       "combo": {}, "combo_end": {}}
                battles.append(cur)
            cur["scores"].append((e["t"], dict(e["scores"])))
    return battles


def detect_layout(ffmpeg: str, video: Path, times: list[float], w: int, h: int) -> tuple[int, int, int, int] | None:
    """Kde jsou v obraze okna bitvy (černé pruhy nad/pod nimi) – ffmpeg cropdetect v několika okamžicích bitvy."""
    boxes = []
    for t in times[:4]:
        r = subprocess.run([ffmpeg, "-hide_banner", "-ss", f"{max(t, 0):.2f}", "-i", str(video), "-t", "2",
                            "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"],
                           capture_output=True, text=True, errors="replace")
        found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        if found:
            cw, ch, cx, cy = map(int, found[-1])
            if ch < h * 0.92 and cw > w * 0.6 and ch > h * 0.2:
                boxes.append((cx, cy, cw, ch))
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return x0, y0, x1 - x0, y1 - y0


def battle_window(b: dict, nxt: dict | None, video_end: float) -> float:
    if b["finish"]:
        stop = b["finish"] + RESULT_HOLD
    elif b["end_at"]:
        stop = b["end_at"] + RESULT_HOLD
    else:
        stop = (b["scores"][-1][0] if b["scores"] else b["start"]) + 60
    return min(stop, video_end, nxt["start"] if nxt is not None else stop)


def build_battles(ass: Ass, battles: list[dict], ours: str | None, vt, video_end: float,
                  layout: tuple[int, int, int, int] | None) -> None:
    u, w, h = ass.u, ass.w, ass.h
    bh = round(30 * u)
    if layout:                                           # pruh přímo nad okny bitvy jako v aplikaci
        bx, bw = layout[0], layout[2]
        by = max(layout[1] - bh, 0)
        panel_top = by + bh
    else:
        bx, bw, by = 0, w, round(h * 0.035)
        panel_top = by + bh
    fs = round(24 * u)
    PINK_C, BLUE_C = "&H552CFE&", "&HF6B34F&"            # #FE2C55, #4FB3F6
    for i, b in enumerate(battles):
        stop = battle_window(b, battles[i + 1] if i + 1 < len(battles) else None, video_end)
        timeline: list[tuple[float, int, int]] = [(b["start"], 0, 0)]
        for t, sc in b["scores"]:
            me = ours if ours in sc else (next(iter(sc)) if sc else None)
            timeline.append((t, sc.get(me, 0) if me else 0, sum(v for k, v in sc.items() if k != me)))
        if b["result"]:
            me = ours if ours in b["result"] else next(iter(b["result"]))
            timeline.append((b["finish"], b["result"].get(me, 0), sum(v for k, v in b["result"].items() if k != me)))
        # pruh se skóre
        for j, (t, mine, other) in enumerate(timeline):
            s = vt(t)
            e = vt(timeline[j + 1][0]) if j + 1 < len(timeline) else vt(stop)
            if e <= s:
                continue
            frac = 0.5 if mine + other == 0 else min(max(mine / (mine + other), 0.08), 0.92)
            lw = round(bw * frac)
            ass.shape(s, e, bx, by, rect(lw, bh), PINK_C)
            ass.shape(s, e, bx + lw, by, rect(bw - lw, bh), BLUE_C)
            ass.shape(s, e, bx + lw - round(2 * u), by, rect(round(4 * u), bh), "&HFFFFFF&", "&H50&", layer=2)
            ass.add(s, e, "Shadowed", rf"{{\an4\pos({bx + round(10*u)},{by + bh // 2})\b1\fs{fs}\bord{max(1, round(1.5*u))}}}"
                                      + num(mine), layer=3)
            ass.add(s, e, "Shadowed", rf"{{\an6\pos({bx + bw - round(10*u)},{by + bh // 2})\b1\fs{fs}\bord{max(1, round(1.5*u))}}}"
                                      + num(other), layer=3)
        # pilulka s odpočtem (během bitvy) / „Vítězné kolo“ (po konci)
        ph, cx = round(34 * u), bx + bw // 2

        def pill(s0: float, s1: float, label: str) -> None:
            pw = text_w(label, round(22 * u), True) + round(30 * u)
            ass.shape(s0, s1, cx - pw / 2, panel_top, rrect(pw, ph, ph / 2), "&H000000&", "&H60&", layer=3)
            ass.add(s0, s1, "Text", rf"{{\an5\pos({cx},{panel_top + ph // 2})\b1\fs{round(22*u)}}}{label}", layer=4)

        end_at = min(x for x in (b["end_at"], b["finish"]) if x) if (b["end_at"] or b["finish"]) else None
        if b["end_at"]:
            t = b["start"]
            while t < end_at:
                left = max(int(end_at - t + 0.999), 0)
                pill(vt(t), vt(min(t + 1, end_at)), f"{left // 60:02d}:{left % 60:02d}")
                t += 1
        # odznaky v rozích oken: během duelu série výher („WIN x8“), po konci WIN xN / LOSE
        fsb, bdh = round(22 * u), round(32 * u)
        by2 = (layout[1] + round(10 * u)) if layout else panel_top + ph + round(8 * u)

        def badge(s0: float, s1: float, side: int, label: str, col: str) -> None:
            if s1 <= s0:
                return
            bwid = text_w(label, fsb, True) + round(24 * u)
            xx = bx + round(10 * u) if side == 0 else bx + bw - round(10 * u) - bwid
            ass.shape(s0, s1, xx, by2, rrect(bwid, bdh, bdh / 2), "&H202020&", "&H50&", layer=3, extra=r"\fad(200,200)")
            ass.add(s0, s1, "Text", rf"{{\an5\pos({round(xx + bwid / 2)},{by2 + bdh // 2})\b1\i1\fs{fsb}\c{col}\fad(200,200)}}{label}", 4)

        def sides(d: dict) -> tuple[int, int]:
            if not d:
                return 0, 0
            me = ours if ours in d else next(iter(d))
            return int(d.get(me, 0)), int(max((v for k, v in d.items() if k != me), default=0))

        streak = sides(b["combo"])
        if not b["combo"]:                              # TikTok sérii neposlal → spočítej z předchozích duelů v záznamu
            k = 0
            for prev in reversed(battles[:i]):
                if prev.get("won") is True:
                    k += 1
                else:
                    break
            streak = (k, 0)
        battle_end = b["finish"] or stop
        for side in (0, 1):
            if streak[side] > 0:
                badge(vt(b["start"]), vt(battle_end), side, f"WIN x{streak[side]}", GOLD)
        if b["finish"]:
            pill(vt(b["finish"]), vt(stop), "Vítězné kolo")
            final = timeline[-1]
            if final[1] == final[2]:
                labels = ("DRAW", "DRAW")
            else:
                labels = ("WIN", "LOSE") if final[1] > final[2] else ("LOSE", "WIN")
            b["won"] = labels[0] == "WIN"
            after = sides(b["combo_end"]) if b["combo_end"] else (
                (streak[0] + 1 if labels[0] == "WIN" else 0), (streak[1] + 1 if labels[1] == "WIN" else 0))
            for side, lab in enumerate(labels):
                text = f"WIN x{after[side]}" if lab == "WIN" and after[side] > 1 else lab
                badge(vt(b["finish"]), vt(stop), side, text, GOLD if lab == "WIN" else "&HDDDDDD&")
            # velký nápis uprostřed na pár sekund (jako animace v aplikaci)
            big = "WIN" if labels[0] == "WIN" else ("DRAW" if labels[0] == "DRAW" else "LOSE")
            ymid = (layout[1] + layout[3] // 2) if layout else h // 2
            ass.add(vt(b["finish"]), vt(b["finish"]) + 3.0, "Big",
                    rf"{{\pos({cx},{ymid})\fs{round(72*u)}\i1\c{GOLD if big == 'WIN' else '&HDDDDDD&'}"
                    rf"\fscx40\fscy40\t(0,250,\fscx115\fscy115)\t(250,450,\fscx100\fscy100)\fad(100,400)}}{big}", 8)


def build_ass(events: list[dict], t0: float, delay: float, w: int, h: int, duration: float,
              offset: float = 0.0, layout: tuple[int, int, int, int] | None = None) -> str:
    streamer = next((e.get("streamer") for e in events if e.get("type") == "meta"), "") or ""
    ass = Ass(w, h)

    def vt(t: float) -> float:
        return t - t0 + delay - offset

    video_end = t0 + duration if duration else max((e["t"] for e in events), default=t0)
    build_gifts(ass, [e for e in events if e.get("type") == "gift"], vt)
    build_comments(ass, [e for e in events if e.get("type") == "comment"], vt, vt(video_end), layout)
    build_battles(ass, parse_battles(events), our_side(events, streamer), vt, video_end, layout)
    return ass.dump()


def battle_sample_times(events: list[dict], t0: float, delay: float, duration: float) -> list[float]:
    """Okamžiky (v sekundách videa) uprostřed bitev – pro zjištění, kde jsou okna bitvy."""
    out = []
    for b in parse_battles(events):
        end = b["finish"] or b["end_at"] or (b["scores"][-1][0] if b["scores"] else b["start"])
        for frac in (0.3, 0.7):
            v = b["start"] + (end - b["start"]) * frac - t0 + delay
            if 0 <= v <= max(duration - 2, 0):
                out.append(v)
    return out


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
    p.add_argument("--size", help="pevný rozměr výstupu ŠxV (např. 720x1280) – sběrač tak sjednotí díly vysílání, "
                                  "aby šly spojit bez překódování; menší obraz se zmenší/doplní okraji")
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
    times = battle_sample_times(events, t0, a.delay, duration)
    layout = detect_layout(ffmpeg, video, times, w, h) if times else None
    if a.size:                                           # sjednocený rozměr (díly jednoho vysílání)
        try:
            tw, th = (int(x) for x in a.size.lower().split("x"))
        except ValueError:
            sys.exit(f"--size musí být ve tvaru ŠxV, např. 720x1280 (je: {a.size})")
        k = min(tw / w, th / h)
        ox, oy = (tw - round(w * k)) // 2, (th - round(h * k)) // 2
        if layout:
            layout = (round(layout[0] * k) + ox, round(layout[1] * k) + oy, round(layout[2] * k), round(layout[3] * k))
        w, h = tw, th
    ass_text = build_ass(events, t0, a.delay, w, h, duration, offset=offset, layout=layout)
    counts = {k: sum(1 for e in events if e.get("type") == k) for k in ("gift", "comment", "battle", "score")}
    print(f"Události: {counts['gift']} darů, {counts['comment']} komentářů, "
          f"{counts['battle']} zpráv o bitvě, {counts['score']} změn skóre; video {w}×{h}, {duration/60:.1f} min"
          + (f"; okna bitvy {layout[2]}×{layout[3]} od y={layout[1]}" if layout else ""))

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
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-stats", "-y"]   # warning: ať je vidět i příčina chyby
        if offset:
            cmd += ["-ss", f"{offset:.3f}"]
        # Stream z TikToku umí uprostřed vysílání změnit parametry (např. zvuk mono → stereo nebo jiná vzorkovací
        # frekvence při bitvě / co-hostu, výjimečně i rozměr obrazu). Kopírování zvuku do MP4 pak novější ffmpeg
        # odmítne („Error muxing a packet … Not yet implemented“) → zvuk se znovu kóduje do jednotného AAC
        # a obraz se drží v pevném rozměru (při změně se zmenší a doplní okraji, nedeformuje se).
        # Časové značky: záznam z TikToku umí uprostřed „resetovat hodiny“ (skok o dny, snímky bez pts, zvuk mírně
        # pozpátku) – přehrávač to přejde, ale kodér/muxer MP4 skončí chybou („Packet duration … out of range“).
        # Obraz: čas běží dál o skutečný rozdíl mezi snímky, nesmyslný skok (záporný, > 5 s, chybějící pts)
        # nebo podezřele malý (< 5 ms) se nahradí jedním snímkem (1/30 s). Zvuk: čas podle počtu vzorků (souvislý, bez skoků).
        # První snímek se pozná podle N, ne podle isnan(PREV_INPTS): po snímku bez pts je PREV_INPTS taky NaN
        # a výraz by se „resetoval“ zpět na původní (rozbitý) čas → ffmpeg zahodil desítky sekund obrazu.
        fix_pts = ("setpts='if(eq(N,0),PTS-STARTPTS,"
                   "if(gt(PTS-PREV_INPTS,0.005/TB)*lt(PTS-PREV_INPTS,5/TB),PREV_OUTPTS+PTS-PREV_INPTS,PREV_OUTPTS+1/(30*TB)))'")
        # fps=30: setpts mění jen čas snímku, ne jeho délku – a novější ffmpeg nesmyslnou délku z poškozeného snímku
        # předá kodéru („Application provided duration … invalid“). Filtr fps skládá výstup jen podle času
        # a každému snímku nastaví délku přesně 1/30 s (zároveň srovná proměnlivou snímkovou frekvenci TikToku).
        vf = (f"{fix_pts},fps=30,scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
              "setsar=1,ass=overlay.ass")
        cmd += ["-i", str(video), "-vf", vf]
        if a.frame is not None:
            cmd += ["-ss", f"{lead:.3f}", "-frames:v", "1", "-update", "1", str(out)]
        else:
            if a.duration:
                cmd += ["-t", f"{a.duration:.3f}"]
            # -bf 0: bez B-snímků je pts == dts. Záznam z TikToku má občas skok v časových značkách; s B-snímky pak
            # rozdíl pts/dts přeteče a muxer MP4 skončí chybou „pts/dts pair unsupported“ → „Not yet implemented“.
            cmd += ["-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf), "-bf", "0", "-pix_fmt", "yuv420p",
                    "-af", "aresample=48000,asetpts=N/SR/TB", "-c:a", "aac", "-b:a", "160k", "-ac", "2",
                    "-movflags", "+faststart", str(out)]
        print("Vyrábím:", out)
        code = run_ffmpeg(cmd, tmp)
        if code != 0:
            sys.exit(f"ffmpeg skončil s chybou {code}")
    print("Hotovo:", out)


if __name__ == "__main__":
    main()
