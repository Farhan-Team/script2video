#!/usr/bin/env python3
"""
script2video — turn a text script into a narrated stock-footage video. 100% free.

Pipeline
  1. Split the script into scenes (by paragraph / sentence count).
  2. Voiceover per scene with free Microsoft Edge neural voices (edge-tts),
     falling back to your system's offline voice (pyttsx3).
  3. Stock footage per scene from Pexels / Pixabay videos, then Pexels /
     Pixabay / Unsplash photos (with a slow Ken Burns zoom), or your own
     local footage folder. With no API keys at all it uses Wikimedia Commons
     videos and Openverse photos (free licenses, no key needed).
  4. Burned-in text overlay captions, timed to the narration.
  5. Everything joined with FFmpeg into one MP4 (+ optional background music).

All API keys are free. Re-running the same script resumes where it left off:
voiceovers, downloads and finished scenes are cached in the work folder.

Usage
  python script2video.py my_script.txt -o my_video.mp4
  python script2video.py my_script.txt --voice en-GB-RyanNeural --music bg.mp3
  python script2video.py my_script.txt --vertical          # 1080x1920 Shorts/Reels
  python script2video.py my_script.txt --media-dir ./my_footage
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: pip install -r requirements.txt")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

STOPWORDS = set("""
a about above after again against all almost also am among an and another any are aren't around as at
be became because become becomes been before being below between both but by can can't cannot could
couldn't did didn't do does doesn't doing don't done down during each either else enough even ever every
few for from further get gets getting give given gives go goes going gone got had hadn't has hasn't have
haven't having he he'd he'll he's her here here's hers herself him himself his how how's however i i'd
i'll i'm i've if in into is isn't it it's its itself just keep know let let's like likely made make makes
making many may maybe me might more most much must mustn't my myself need needs never new no nor not now
of off often on once one only onto or other others our ours ourselves out over own per perhaps put quite
rather really right said same say says see seem seems shall shan't she she'd she'll she's should
shouldn't show since so some something sometimes still such take takes than that that's the their theirs
them themselves then there there's these they they'd they'll they're they've thing things think this
those though through thus to today too took toward towards try under until up upon us use used uses using
very via want wants was wasn't way ways we we'd we'll we're we've well were weren't what what's when
when's where where's whether which while who who's whom whose why why's will with within without won't
would wouldn't yet you you'd you'll you're you've your yours yourself yourselves first second third next
last year years day days time times lot lots kind sort part look looks looking people person world
everything anything nothing someone everyone anyone come comes came back even ever actually basically
simply truly called around across along already always among let's okay yes yeah hello welcome video
today's channel subscribe meanwhile however therefore moreover instead indeed
later early earlier soon away together whole entire million millions billion percent
believe believes said says told connected beneath behind high low big small large great long short
""".split())


# --------------------------------------------------------------------------- config

@dataclass
class Config:
    script_text: str
    output: Path = Path("output.mp4")
    workdir: Path = Path("work")
    # voice
    voice: str = "en-US-GuyNeural"
    rate: str = "+0%"
    tts: str = "edge"                 # edge | system
    pause: float = 0.35               # silence after each scene (s)
    # scenes / visuals
    words_per_scene: int = 30
    clip_seconds: float = 6.0         # change the shot about every N seconds
    width: int = 1920
    height: int = 1080
    fps: int = 30
    prefer: str = "video"             # video | photo
    media_dir: Optional[Path] = None
    pexels_key: str = ""
    pixabay_key: str = ""
    unsplash_key: str = ""
    # captions
    captions: str = "phrases"         # phrases | sentences | none
    caption_style: str = "box"        # box | outline
    font: str = "Arial"
    font_size: int = 0                # 0 = auto
    caption_words: int = 7
    # audio
    music: Optional[Path] = None
    music_volume: float = 0.12
    # encoding
    crf: int = 23
    preset: str = "veryfast"
    workers: int = 2
    progress: Callable[[float, str], None] = field(default=lambda f, m: None, repr=False)


@dataclass
class Scene:
    text: str
    tag: Optional[str] = None             # manual [search: ...] query
    queries: list[str] = field(default_factory=list)
    audio: Optional[Path] = None
    speech_dur: float = 0.0
    media: list["Media"] = field(default_factory=list)


@dataclass
class Media:
    kind: str                  # video | image | color
    source: str                # pexels | pixabay | unsplash | local | none
    key: str                   # unique id (to avoid reusing a clip)
    url: str = ""
    path: Optional[Path] = None
    duration: float = 0.0
    credit: str = ""
    page: str = ""
    ping: str = ""             # Unsplash download-tracking endpoint


def log(msg: str) -> None:
    print(msg, flush=True)


def sha(*parts) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


# --------------------------------------------------------------------------- script parsing

TAG_RE = re.compile(r"\[(?:search|visual|keywords?|broll|b-roll)\s*:\s*([^\]]+)\]", re.I)
SENT_RE = re.compile(r"(?<=[.!?؟۔…])[\"')\]]*\s+")


def parse_script(text: str, words_per_scene: int) -> list[Scene]:
    """Paragraphs are never merged; long paragraphs are split on sentence
    boundaries into scenes of roughly `words_per_scene` words."""
    text = text.replace("\r\n", "\n").replace("﻿", "")
    scenes: list[Scene] = []
    for para in re.split(r"\n\s*\n", text):
        tags = TAG_RE.findall(para)
        tag = tags[0].strip() if tags else None
        clean = re.sub(r"\[[^\]]*\]", " ", para)           # drop [tags] / [stage directions]
        clean = re.sub(r"^\s*#+\s*", "", clean, flags=re.M)  # markdown headings
        clean = re.sub(r"[*_`>]+", "", clean)                 # markdown emphasis
        clean = re.sub(r"\s+", " ", clean).strip()
        if not clean:
            continue
        sentences = [s.strip() for s in SENT_RE.split(clean) if s.strip()]
        chunks: list[str] = []
        buf: list[str] = []
        for s in sentences:
            buf.append(s)
            if len(" ".join(buf).split()) >= words_per_scene:
                chunks.append(" ".join(buf))
                buf = []
        if buf:
            rest = " ".join(buf)
            if chunks and len(rest.split()) < max(6, words_per_scene // 3):
                chunks[-1] += " " + rest                       # don't leave a tiny tail scene
            else:
                chunks.append(rest)
        scenes.extend(Scene(c, tag) for c in chunks)
    return scenes


def _words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", text.lower()):
        w = re.sub(r"'s$", "", w).strip("-'")
        if len(w) >= 3 and w not in STOPWORDS and not w.endswith("ly"):
            out.append(w)
    return out


def assign_queries(scenes: list[Scene]) -> None:
    """Pick stock-search keywords per scene with a small TF-IDF."""
    docs = [_words(s.text) for s in scenes]
    df = Counter()
    for d in docs:
        df.update(set(d))
    n = max(1, len(docs))
    total = Counter(w for d in docs for w in d)
    topic = [w for w, _ in total.most_common(3)]
    for s, d in zip(scenes, docs):
        tf = Counter(d)
        L = max(1, len(d))
        # frequent in this scene + appears early (the subject usually comes first),
        # with a light rarity boost so every scene doesn't get the same query
        def score(w: str) -> float:
            return tf[w] + 1.2 * (1 - d.index(w) / L) + 0.25 * math.log(1 + n / df[w])
        ranked = sorted(tf, key=lambda w: -score(w))
        q: list[str] = []
        if s.tag:
            q.append(s.tag)
        if len(ranked) >= 2:
            q.append(f"{ranked[0]} {ranked[1]}")
        q += ranked[:3]
        if topic:
            q.append(topic[0])
        q.append("abstract background")
        seen, s.queries = set(), []
        for item in q:
            if item and item not in seen:
                seen.add(item)
                s.queries.append(item)


# --------------------------------------------------------------------------- text to speech

async def _edge_save(text: str, voice: str, rate: str, path: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, voice, rate=rate).save(str(path))


def _system_tts(text: str, path: Path) -> None:
    import pyttsx3
    eng = pyttsx3.init()
    eng.save_to_file(text, str(path))
    eng.runAndWait()


def synthesize(scene: Scene, cfg: Config, tts_dir: Path) -> None:
    ext = ".mp3" if cfg.tts == "edge" else ".wav"
    path = tts_dir / f"tts_{sha(scene.text, cfg.voice, cfg.rate, cfg.tts)}{ext}"
    if not (path.exists() and path.stat().st_size > 0):
        tmp = path.with_name(path.stem + ".part" + ext)
        if cfg.tts == "edge":
            last = None
            for attempt in range(4):
                try:
                    asyncio.run(_edge_save(scene.text, cfg.voice, cfg.rate, tmp))
                    if tmp.exists() and tmp.stat().st_size > 0:
                        break
                except Exception as e:  # network hiccup / throttling
                    last = e
                time.sleep(1.5 * (attempt + 1))
            else:
                raise RuntimeError(f"edge-tts failed ({last}). Check your internet, or use --tts system")
        else:
            _system_tts(scene.text, tmp)
        tmp.replace(path)
    scene.audio = path
    scene.speech_dur = probe_duration(path)


# --------------------------------------------------------------------------- stock media

class StockFinder:
    def __init__(self, cfg: Config, cache: Path):
        self.cfg = cfg
        self.cache = cache
        self.used: set[str] = set()
        self.http = requests.Session()
        # Wikimedia requires a descriptive User-Agent with a contact URL
        self.http.headers["User-Agent"] = "script2video/1.1 (https://github.com/Farhan-Team/script2video)"
        self.wiki_gate = threading.Semaphore(1)   # Wikimedia rate-limits parallel downloads
        self.local: list[Path] = []
        if cfg.media_dir:
            self.local = sorted(p for p in Path(cfg.media_dir).rglob("*")
                                if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT)
        self.warned: set[str] = set()
        self.down: set[str] = set()

    # ---- provider searches (results cached on disk so re-runs cost no API calls)
    def _cached_get(self, provider: str, url: str, params: dict, headers: dict) -> Optional[dict]:
        f = self.cache / f"search_{provider}_{sha(url, json.dumps(params, sort_keys=True))}.json"
        if f.exists():
            return json.loads(f.read_text("utf-8"))
        if provider in self.down:
            return None
        for attempt in range(3):
            try:
                r = self.http.get(url, params=params, headers=headers, timeout=20)
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                if r.status_code in (401, 403):
                    if provider not in self.warned:
                        self.warned.add(provider)
                        log(f"  ! {provider}: API key rejected (HTTP {r.status_code}) — skipping")
                    return None
                r.raise_for_status()
                data = r.json()
                f.write_text(json.dumps(data), "utf-8")
                return data
            except requests.RequestException as e:
                if attempt == 2:
                    self.down.add(provider)
                    log(f"  ! {provider} unreachable ({type(e).__name__}) — skipping it for this run")
                time.sleep(1)
        return None

    def pexels_videos(self, q: str) -> list[Media]:
        if not self.cfg.pexels_key:
            return []
        portrait = self.cfg.height > self.cfg.width
        d = self._cached_get("pexels_v", "https://api.pexels.com/videos/search",
                             {"query": q, "per_page": 30, "orientation": "portrait" if portrait else "landscape"},
                             {"Authorization": self.cfg.pexels_key})
        out = []
        for v in (d or {}).get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("link") and f.get("width") and f.get("height")
                     and (f.get("file_type") or "video/mp4") == "video/mp4"]
            target = max(self.cfg.width, self.cfg.height)
            good = [f for f in files if 720 <= max(f["width"], f["height"]) <= 2160] or files
            if not good:
                continue
            best = min(good, key=lambda f: abs(max(f["width"], f["height"]) - target))
            out.append(Media("video", "pexels", f"pexels_v{v['id']}", url=best["link"],
                             duration=float(v.get("duration") or 0),
                             credit=f"Video by {v.get('user', {}).get('name', 'Unknown')} on Pexels",
                             page=v.get("url", "")))
        return out

    def pixabay_videos(self, q: str) -> list[Media]:
        if not self.cfg.pixabay_key:
            return []
        d = self._cached_get("pixabay_v", "https://pixabay.com/api/videos/",
                             {"key": self.cfg.pixabay_key, "q": q[:100], "per_page": 30, "safesearch": "true"}, {})
        out = []
        for h in (d or {}).get("hits", []):
            vids = h.get("videos", {})
            pick = None
            for size in ("large", "medium", "small"):
                v = vids.get(size) or {}
                if v.get("url") and (v.get("width") or 0) >= 960:
                    pick = v
                    break
            if pick:
                out.append(Media("video", "pixabay", f"pixabay_v{h['id']}", url=pick["url"],
                                 duration=float(h.get("duration") or 0),
                                 credit=f"Video by {h.get('user', 'Unknown')} on Pixabay",
                                 page=h.get("pageURL", "")))
        return out

    def pexels_photos(self, q: str) -> list[Media]:
        if not self.cfg.pexels_key:
            return []
        portrait = self.cfg.height > self.cfg.width
        d = self._cached_get("pexels_p", "https://api.pexels.com/v1/search",
                             {"query": q, "per_page": 30, "orientation": "portrait" if portrait else "landscape"},
                             {"Authorization": self.cfg.pexels_key})
        return [Media("image", "pexels", f"pexels_p{p['id']}", url=p["src"].get("large2x") or p["src"]["original"],
                      credit=f"Photo by {p.get('photographer', 'Unknown')} on Pexels", page=p.get("url", ""))
                for p in (d or {}).get("photos", []) if p.get("src")]

    def pixabay_photos(self, q: str) -> list[Media]:
        if not self.cfg.pixabay_key:
            return []
        portrait = self.cfg.height > self.cfg.width
        d = self._cached_get("pixabay_p", "https://pixabay.com/api/",
                             {"key": self.cfg.pixabay_key, "q": q[:100], "per_page": 30, "image_type": "photo",
                              "orientation": "vertical" if portrait else "horizontal", "safesearch": "true"}, {})
        return [Media("image", "pixabay", f"pixabay_p{h['id']}", url=h.get("largeImageURL") or h["webformatURL"],
                      credit=f"Image by {h.get('user', 'Unknown')} on Pixabay", page=h.get("pageURL", ""))
                for h in (d or {}).get("hits", [])]

    def unsplash_photos(self, q: str) -> list[Media]:
        if not self.cfg.unsplash_key:
            return []
        portrait = self.cfg.height > self.cfg.width
        d = self._cached_get("unsplash", "https://api.unsplash.com/search/photos",
                             {"query": q, "per_page": 30, "orientation": "portrait" if portrait else "landscape",
                              "content_filter": "high"},
                             {"Authorization": f"Client-ID {self.cfg.unsplash_key}", "Accept-Version": "v1"})
        out = []
        for p in (d or {}).get("results", []):
            base = p["urls"].get("raw") or p["urls"]["full"]
            sep = "&" if "?" in base else "?"
            url = f"{base}{sep}w={max(self.cfg.width, self.cfg.height) * 2}&fm=jpg&q=85&fit=max"
            user = p.get("user", {})
            out.append(Media("image", "unsplash", f"unsplash_{p['id']}", url=url,
                             credit=f"Photo by {user.get('name', 'Unknown')} on Unsplash",
                             page=(p.get("links", {}) or {}).get("html", ""),
                             ping=(p.get("links", {}) or {}).get("download_location", "")))
        return out

    # ---- keyless sources (work with no API key at all)
    def commons_videos(self, q: str) -> list[Media]:
        """Wikimedia Commons free-licensed videos, using its 720p/1080p transcodes."""
        d = self._cached_get("commons_v", "https://commons.wikimedia.org/w/api.php",
                             {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                              "gsrsearch": f"{q} filetype:video", "gsrlimit": 25,
                              "prop": "videoinfo", "viprop": "url|size|derivatives|extmetadata"}, {})
        out = []
        pages = ((d or {}).get("query") or {}).get("pages") or {}
        for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            info = (p.get("videoinfo") or [{}])[0]
            dur = float(info.get("duration") or 0)
            if dur and dur < 3:
                continue
            ders = [x for x in info.get("derivatives", []) if x.get("src") and x.get("height")
                    and "audio" not in (x.get("type") or "")]
            target = min(720, min(self.cfg.width, self.cfg.height))
            ders = [x for x in ders if 480 <= int(x["height"]) <= 1080] or ders
            if not ders:
                continue
            best = min(ders, key=lambda x: abs(int(x["height"]) - target))
            meta = info.get("extmetadata") or {}
            artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()[:80] or "Unknown"
            lic = (meta.get("LicenseShortName") or {}).get("value", "")
            out.append(Media("video", "commons", f"commons_{p.get('pageid')}", url=best["src"], duration=dur,
                             credit=f"Video by {artist} ({lic}) via Wikimedia Commons",
                             page=info.get("descriptionurl", "")))
        return out

    def openverse_photos(self, q: str) -> list[Media]:
        """Openverse (WordPress) free-licensed photos — no key needed."""
        d = self._cached_get("openverse", "https://api.openverse.org/v1/images/",
                             {"q": q, "page_size": 20, "category": "photograph",
                              "aspect_ratio": "tall" if self.cfg.height > self.cfg.width else "wide",
                              "size": "large", "mature": "false"}, {})
        out = []
        for r in (d or {}).get("results", []):
            if not r.get("url"):
                continue
            out.append(Media("image", "openverse", f"openverse_{r.get('id')}", url=r["url"],
                             credit=f"Photo by {r.get('creator') or 'Unknown'} "
                                    f"({(r.get('license') or '').upper()} {r.get('license_version') or ''}) "
                                    f"via {r.get('source') or 'Openverse'}".replace("  ", " "),
                             page=r.get("foreign_landing_url", "")))
        return out

    def local_matches(self, q: str) -> list[Media]:
        words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 2]
        hits = [p for p in self.local if any(w in p.stem.lower() for w in words)]
        return [self._local_media(p) for p in hits]

    def _local_media(self, p: Path) -> Media:
        kind = "video" if p.suffix.lower() in VIDEO_EXT else "image"
        return Media(kind, "local", f"local_{p}", path=p.resolve(),
                     duration=probe_duration(p) if kind == "video" else 0, credit=f"Local file: {p.name}")

    # ---- selection
    def pick(self, scene: Scene, n: int, seg: float) -> list[Media]:
        has_keys = bool(self.cfg.pexels_key or self.cfg.pixabay_key or self.cfg.unsplash_key)
        if self.cfg.prefer == "photo":
            providers = [self.local_matches, self.pexels_photos, self.unsplash_photos, self.pixabay_photos,
                         self.openverse_photos, self.pexels_videos, self.pixabay_videos, self.commons_videos]
        else:
            providers = [self.local_matches, self.pexels_videos, self.pixabay_videos,
                         self.pexels_photos, self.unsplash_photos, self.pixabay_photos,
                         self.commons_videos, self.openverse_photos]
        if has_keys:  # keyless sources are only a fallback when keys exist
            providers = [p for p in providers if p not in (self.commons_videos, self.openverse_photos)] + \
                        [self.commons_videos, self.openverse_photos]
        chosen: list[Media] = []
        for q in scene.queries:
            for prov in providers:
                if len(chosen) >= n:
                    break
                cands = [m for m in prov(q) if m.key not in self.used]
                # clips long enough to cover the shot without looping come first
                cands.sort(key=lambda m: 0 if (m.kind != "video" or m.duration >= seg) else 1)
                for m in cands:
                    if len(chosen) >= n:
                        break
                    chosen.append(m)
                    self.used.add(m.key)
            if len(chosen) >= n:
                break
        # nothing matched: reuse local footage at random, then a plain gradient
        while len(chosen) < n and self.local:
            fresh = [p for p in self.local if f"local_{p}" not in self.used] or self.local
            p = random.Random(sha(scene.text, len(chosen))).choice(fresh)
            self.used.add(f"local_{p}")
            chosen.append(self._local_media(p))
        while len(chosen) < n:
            chosen.append(Media("color", "none", f"color{len(chosen)}"))
        return chosen

    def download(self, m: Media) -> None:
        if m.path or m.kind == "color":
            return
        ext = ".mp4" if m.kind == "video" else ".jpg"
        dest = self.cache / f"{m.source}_{sha(m.url)}{ext}"
        if not (dest.exists() and dest.stat().st_size > 0):
            tmp = dest.with_name(dest.stem + ".part" + ext)
            wiki = "wikimedia.org" in m.url
            gate = self.wiki_gate if wiki else None
            tries = 6 if wiki else 3
            for attempt in range(tries):
                try:
                    if gate:
                        gate.acquire()
                    try:
                        with self.http.get(m.url, stream=True, timeout=90) as r:
                            if r.status_code == 429:
                                wait = float(r.headers.get("Retry-After") or 0) or 4 * (attempt + 1)
                                raise requests.HTTPError(f"429 rate limited, waiting {wait:.0f}s",
                                                         response=r)
                            r.raise_for_status()
                            with open(tmp, "wb") as fh:
                                for chunk in r.iter_content(1 << 16):
                                    fh.write(chunk)
                        if wiki:
                            time.sleep(0.5)          # be polite between Wikimedia downloads
                    finally:
                        if gate:
                            gate.release()
                    tmp.replace(dest)
                    break
                except requests.RequestException as e:
                    if attempt == tries - 1:
                        log(f"  ! download failed ({e}); using a plain background for this shot")
                        m.kind, m.source = "color", "none"
                        return
                    resp = getattr(e, "response", None)
                    if resp is not None and resp.status_code == 429:
                        time.sleep(min(30, float(resp.headers.get("Retry-After") or 0) or 4 * (attempt + 1)))
                    else:
                        time.sleep(2)
            if m.ping and self.cfg.unsplash_key:  # required by Unsplash API guidelines
                try:
                    self.http.get(m.ping, headers={"Authorization": f"Client-ID {self.cfg.unsplash_key}"}, timeout=15)
                except requests.RequestException:
                    pass
        m.path = dest.resolve()
        if m.kind == "video" and not m.duration:
            m.duration = probe_duration(dest)


# --------------------------------------------------------------------------- ffmpeg helpers

def ffmpeg_bin() -> str:
    b = shutil.which("ffmpeg")
    if not b:
        sys.exit("FFmpeg not found. Install it (https://ffmpeg.org/download.html) and make sure it's on PATH.")
    return b


def probe_duration(path: Path) -> float:
    exe = shutil.which("ffprobe") or "ffprobe"
    try:
        out = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)],
                             capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def run(cmd: list[str], cwd: Optional[Path] = None) -> None:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("FFmpeg error:\n" + (p.stderr or "")[-2500:])


# --------------------------------------------------------------------------- captions (ASS)

def ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ")


def caption_chunks(text: str, mode: str, max_words: int) -> list[str]:
    if mode == "sentences":
        return [s for s in SENT_RE.split(text) if s.strip()]
    words, chunks, buf = text.split(), [], []
    for w in words:
        buf.append(w)
        end_punct = w[-1:] in ".!?,;:؟۔…" or w.endswith(("\"", "'"))
        if len(buf) >= max_words or (end_punct and len(buf) >= 3):
            chunks.append(" ".join(buf))
            buf = []
    if buf:
        if chunks and len(buf) <= 2:
            chunks[-1] += " " + " ".join(buf)
        else:
            chunks.append(" ".join(buf))
    return chunks


def write_ass(scene: Scene, cfg: Config, path: Path) -> None:
    W, H = cfg.width, cfg.height
    vertical = H > W
    size = cfg.font_size or (round(W * 0.052) if vertical else round(H * 0.056))
    if cfg.caption_style == "box":
        border, outline, shadow, back = 3, max(8, size // 5), 0, "&H64000000"
        outc = "&H8C000000"
    else:
        border, outline, shadow, back = 1, max(3, size // 14), 2, "&H96000000"
        outc = "&H00000000"
    margin_v = round(H * (0.22 if vertical else 0.08))
    margin_lr = round(W * 0.08)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{cfg.font},{size},&H00FFFFFF,&H000000FF,{outc},{back},-1,0,0,0,100,100,0,0,"
        f"{border},{outline},{shadow},2,{margin_lr},{margin_lr},{margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    chunks = caption_chunks(scene.text, cfg.captions, cfg.caption_words)
    weights = [len(c) + 6 for c in chunks]          # +6 ≈ the small pause at each break
    total_w = sum(weights) or 1
    t = 0.0
    for c, w in zip(chunks, weights):
        d = scene.speech_dur * w / total_w
        lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + d)},Default,,0,0,0,,{ass_escape(c)}")
        t += d
    path.write_text("\n".join(lines) + "\n", "utf-8")


# --------------------------------------------------------------------------- scene rendering

def render_scene(idx: int, scene: Scene, cfg: Config, sdir: Path) -> Path:
    W, H, FPS = cfg.width, cfg.height, cfg.fps
    total = scene.speech_dur + cfg.pause
    sig = sha(scene.text, scene.audio, [str(m.path or m.key) for m in scene.media], W, H, FPS, cfg.captions,
              cfg.caption_style, cfg.font, cfg.font_size, cfg.caption_words, cfg.pause, cfg.crf, cfg.preset)
    out = sdir / f"scene_{idx:04d}_{sig}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    n = len(scene.media)
    seg = total / n
    cmd = [ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error"]
    chains = []
    for k, m in enumerate(scene.media):
        frames = max(1, math.ceil(seg * FPS) + 2)
        if m.kind == "video":
            loop = ["-stream_loop", "-1"] if m.duration and m.duration < seg + 0.2 else []
            start = 0.0
            if m.duration > seg + 2:                      # skip the often-static first second
                start = min(1.0, m.duration - seg - 0.5)
            cmd += loop + ["-ss", f"{start:.2f}", "-t", f"{seg + 0.5:.3f}", "-i", str(m.path)]
            chains.append(f"[{k}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                          f"fps={FPS},setsar=1,trim=duration={seg:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{k}]")
        elif m.kind == "image":
            cmd += ["-i", str(m.path)]
            zin = (idx + k) % 2 == 0
            z = f"1+0.12*on/{frames}" if zin else f"1.12-0.12*on/{frames}"
            # drift slightly left/right for variety
            x = "iw/2-(iw/zoom/2)" if (idx + k) % 3 == 0 else (
                f"(iw-iw/zoom)*on/{frames}" if (idx + k) % 3 == 1 else f"(iw-iw/zoom)*(1-on/{frames})")
            chains.append(f"[{k}:v]scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
                          f"zoompan=z='{z}':x='{x}':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},"
                          f"trim=duration={seg:.3f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{k}]")
        else:
            cmd += ["-f", "lavfi", "-t", f"{seg + 0.5:.3f}", "-i",
                    f"gradients=s={W}x{H}:r={FPS}:c0=0x0f172a:c1=0x1e3a5f:speed=0.01"]
            chains.append(f"[{k}:v]trim=duration={seg:.3f},setpts=PTS-STARTPTS,setsar=1,format=yuv420p[v{k}]")
    cmd += ["-i", str(scene.audio)]
    vlabels = "".join(f"[v{k}]" for k in range(n))
    chains.append(f"{vlabels}concat=n={n}:v=1:a=0[vc]")
    if cfg.captions != "none":
        ass = sdir / f"scene_{idx:04d}_{sig}.ass"
        write_ass(scene, cfg, ass)
        chains.append(f"[vc]subtitles={ass.name}[vout]")   # relative name: avoids path-escaping issues
    else:
        chains.append("[vc]null[vout]")
    chains.append(f"[{n}:a]aresample=44100,aformat=channel_layouts=stereo,apad=whole_dur={total:.3f}[aout]")
    tmp = out.with_name(out.stem + ".part.mp4")
    cmd += ["-filter_complex", ";".join(chains), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf), "-pix_fmt", "yuv420p",
            "-r", str(FPS), "-g", str(FPS * 2),
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-t", f"{total:.3f}", "-movflags", "+faststart", str(tmp.resolve())]
    try:
        run(cmd, cwd=sdir)
    except RuntimeError as e:
        if "gradients" in str(e) and "No such filter" in str(e):     # very old FFmpeg
            raise RuntimeError("Your FFmpeg is too old (needs 4.4+).") from e
        raise
    tmp.replace(out)
    return out


# --------------------------------------------------------------------------- pipeline

def build_video(cfg: Config) -> Path:
    ffmpeg_bin()
    work = Path(cfg.workdir).resolve()
    tts_dir, cache, sdir = work / "voice", work / "media", work / "scenes"
    for d in (tts_dir, cache, sdir):
        d.mkdir(parents=True, exist_ok=True)

    scenes = parse_script(cfg.script_text, cfg.words_per_scene)
    if not scenes:
        raise ValueError("The script is empty.")
    assign_queries(scenes)
    words = sum(len(s.text.split()) for s in scenes)
    log(f"• {len(scenes)} scenes, {words} words (≈{words / 150:.1f} min of narration)")
    if not (cfg.pexels_key or cfg.pixabay_key or cfg.unsplash_key or cfg.media_dir):
        log("  • No API keys: using free keyless sources (Wikimedia Commons videos, Openverse photos). "
            "For better footage add a free Pexels key (pexels.com/api)")

    # 1. voiceover
    cfg.progress(0.02, "Generating voiceover…")
    done = 0
    with ThreadPoolExecutor(max_workers=4 if cfg.tts == "edge" else 1) as ex:
        for _ in ex.map(lambda s: synthesize(s, cfg, tts_dir), scenes):
            done += 1
            cfg.progress(0.02 + 0.23 * done / len(scenes), f"Voiceover {done}/{len(scenes)}")
    runtime = sum(s.speech_dur + cfg.pause for s in scenes)
    log(f"• Voiceover done — video length {runtime / 60:.1f} min")

    # 2. footage
    cfg.progress(0.25, "Finding stock footage…")
    finder = StockFinder(cfg, cache)
    for i, s in enumerate(scenes):
        total = s.speech_dur + cfg.pause
        n = max(1, round(total / cfg.clip_seconds))
        s.media = finder.pick(s, n, total / n)
        cfg.progress(0.25 + 0.10 * (i + 1) / len(scenes), f"Searching footage {i + 1}/{len(scenes)}")
    all_media = [m for s in scenes for m in s.media]
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for _ in ex.map(finder.download, all_media):
            done += 1
            cfg.progress(0.35 + 0.15 * done / len(all_media), f"Downloading media {done}/{len(all_media)}")
    srcs = Counter(m.source for m in all_media)
    log("• Media: " + ", ".join(f"{v} {k}" for k, v in srcs.items()))

    # 3. render scenes
    files: list[Optional[Path]] = [None] * len(scenes)
    done = 0

    def _render(i: int) -> None:
        files[i] = render_scene(i, scenes[i], cfg, sdir)

    with ThreadPoolExecutor(max_workers=max(1, cfg.workers)) as ex:
        for _ in ex.map(_render, range(len(scenes))):
            done += 1
            cfg.progress(0.50 + 0.45 * done / len(scenes), f"Rendering scene {done}/{len(scenes)}")
            if done % 5 == 0 or done == len(scenes):
                log(f"  rendered {done}/{len(scenes)}")

    # 4. join (+ music)
    cfg.progress(0.96, "Joining scenes…")
    out = Path(cfg.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    listfile = work / "concat.txt"
    listfile.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), "utf-8")
    body = work / "joined.mp4" if cfg.music else out
    run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(body)])
    if cfg.music:
        dur = probe_duration(body)
        fade = max(0.0, dur - 3)
        run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(body),
             "-stream_loop", "-1", "-i", str(Path(cfg.music).resolve()),
             "-filter_complex",
             f"[1:a]aresample=44100,aformat=channel_layouts=stereo,volume={cfg.music_volume},"
             f"afade=t=in:d=2,afade=t=out:st={fade:.2f}:d=3[m];"
             f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out)])

    credits = sorted({f"{m.credit} — {m.page}".rstrip(" —") for m in all_media if m.source not in ("none",)})
    out.with_suffix(".credits.txt").write_text("\n".join(credits) + "\n", "utf-8")
    cfg.progress(1.0, "Done")
    log(f"✓ Saved {out}  ({probe_duration(out) / 60:.1f} min)")
    return out


# --------------------------------------------------------------------------- CLI

def load_env(path: Path = Path(".env")) -> None:
    """Tiny .env reader so keys can live next to the script."""
    for p in (path, Path(__file__).with_name(".env")):
        if p.exists():
            for line in p.read_text("utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="Turn a text script into a narrated stock-footage video (free).")
    ap.add_argument("script", type=Path, help="script file (.txt or .md)")
    ap.add_argument("-o", "--output", type=Path, help="output .mp4 (default: <script>.mp4)")
    ap.add_argument("--voice", default="en-US-GuyNeural",
                    help="edge-tts voice, e.g. en-US-JennyNeural, en-GB-RyanNeural, ur-PK-AsadNeural "
                         "(list all: edge-tts --list-voices)")
    ap.add_argument("--rate", default="+0%", help="speech speed, e.g. -10%% or +15%%")
    ap.add_argument("--tts", choices=["edge", "system"], default="edge", help="system = offline voice")
    ap.add_argument("--vertical", action="store_true", help="1080x1920 for Shorts / Reels / TikTok")
    ap.add_argument("--size", help="custom WxH, e.g. 1280x720 (renders faster)")
    ap.add_argument("--prefer", choices=["video", "photo"], default="video", help="stock videos or photos first")
    ap.add_argument("--media-dir", type=Path, help="folder of your own footage/images (matched by file name)")
    ap.add_argument("--clip-seconds", type=float, default=6.0, help="change shot every ~N seconds")
    ap.add_argument("--words-per-scene", type=int, default=30)
    ap.add_argument("--captions", choices=["phrases", "sentences", "none"], default="phrases")
    ap.add_argument("--caption-style", choices=["box", "outline"], default="box")
    ap.add_argument("--caption-words", type=int, default=7, help="max words per caption (phrases mode)")
    ap.add_argument("--font", default="Arial", help="caption font name installed on your system")
    ap.add_argument("--font-size", type=int, default=0)
    ap.add_argument("--music", type=Path, help="background music file (loops, auto-faded)")
    ap.add_argument("--music-volume", type=float, default=0.12)
    ap.add_argument("--pause", type=float, default=0.35, help="silence between scenes (s)")
    ap.add_argument("--workdir", type=Path, help="cache folder (default: <script>_work)")
    ap.add_argument("--workers", type=int, default=2, help="scenes rendered in parallel")
    ap.add_argument("--crf", type=int, default=23, help="quality (lower = better, bigger)")
    ap.add_argument("--preset", default="veryfast")
    a = ap.parse_args()

    if not a.script.exists():
        sys.exit(f"Script not found: {a.script}")
    w, h = 1920, 1080
    if a.vertical:
        w, h = 1080, 1920
    if a.size:
        w, h = (int(x) for x in a.size.lower().split("x"))
    w, h = w - w % 2, h - h % 2
    cfg = Config(
        script_text=a.script.read_text("utf-8"),
        output=a.output or a.script.with_suffix(".mp4"),
        workdir=a.workdir or a.script.with_name(a.script.stem + "_work"),
        voice=a.voice, rate=a.rate, tts=a.tts, pause=a.pause,
        words_per_scene=a.words_per_scene, clip_seconds=a.clip_seconds,
        width=w, height=h, prefer=a.prefer, media_dir=a.media_dir,
        pexels_key=os.getenv("PEXELS_API_KEY", ""), pixabay_key=os.getenv("PIXABAY_API_KEY", ""),
        unsplash_key=os.getenv("UNSPLASH_ACCESS_KEY", ""),
        captions=a.captions, caption_style=a.caption_style, caption_words=a.caption_words,
        font=a.font, font_size=a.font_size, music=a.music, music_volume=a.music_volume,
        crf=a.crf, preset=a.preset, workers=a.workers,
    )
    t0 = time.time()
    build_video(cfg)
    log(f"  took {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
