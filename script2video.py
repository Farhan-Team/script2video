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
    captions: str = "words"           # words (highlight as spoken) | phrases | sentences | none
    caption_style: str = "box"        # box | outline
    font: str = "Arial"
    font_size: int = 0                # 0 = auto
    caption_words: int = 6
    highlight: str = "&H0000D7FF"     # ASS colour (BGR) of the spoken word — gold
    # audio
    music: Optional[Path] = None      # a file, or None
    auto_music: bool = False          # fetch free background music (Openverse, no key)
    music_mood: str = "cinematic ambient"
    music_volume: float = 0.18        # music dips automatically under the voice
    # encoding
    crf: int = 23
    preset: str = "veryfast"
    encoder: str = "auto"             # auto (GPU if available) | gpu | cpu
    workers: int = 0                  # 0 = one per CPU core
    partial_downloads: bool = True    # fetch only the needed seconds of long remote clips
    progress: Callable[[float, str], None] = field(default=lambda f, m: None, repr=False)


@dataclass
class Scene:
    text: str
    tag: Optional[str] = None             # manual [search: ...] query
    queries: list[str] = field(default_factory=list)
    audio: Optional[Path] = None
    speech_dur: float = 0.0
    words: list = field(default_factory=list)      # [start, end, word] from the voice engine
    media: list["Media"] = field(default_factory=list)
    shot_queries: list = field(default_factory=list)  # per-shot search terms


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
    need: float = 0.0          # seconds of this clip the shot needs
    trimmed: bool = False      # downloaded already cut to the needed part


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


class QueryMaker:
    """Stock-search keywords from a small TF-IDF over the script, preferring
    two-word phrases that appear in the text ("sand dunes", "city skyline")."""

    def __init__(self, scenes: list[Scene]):
        docs = [_words(s.text) for s in scenes]
        self.df = Counter()
        for d in docs:
            self.df.update(set(d))
        self.n = max(1, len(docs))
        total = Counter(w for d in docs for w in d)
        self.topic = [w for w, _ in total.most_common(3)]

    def __call__(self, text: str, tag: Optional[str] = None) -> list[str]:
        d = _words(text)
        tf = Counter(d)
        L = max(1, len(d))

        def score(w: str) -> float:   # frequent here + appears early + a bit rare overall
            return tf[w] + 1.2 * (1 - d.index(w) / L) + 0.25 * math.log(1 + self.n / max(1, self.df[w]))

        ranked = sorted(tf, key=lambda w: -score(w))
        # adjacent content-word pairs straight from the text
        pairs = []
        for clause in re.split(r"[,.;:!?()\"—–]+", text.lower()):   # never pair across punctuation
            toks = [re.sub(r"'s$", "", t).strip("-'") for t in re.findall(r"[a-z][a-z'\-]+", clause)]
            for a, b in zip(toks, toks[1:]):
                if a in tf and b in tf and a != b:
                    pairs.append((score(a) + score(b), f"{a} {b}"))
        pairs.sort(key=lambda x: -x[0])
        q: list[str] = []
        if tag:
            q.append(tag)
        q += [p for _, p in pairs[:2]]
        if len(ranked) >= 2:
            q.append(f"{ranked[0]} {ranked[1]}")
        q += ranked[:3]
        seen, out = set(), []
        for item in q:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out

    def fallback(self) -> list[str]:
        return ([self.topic[0]] if self.topic else []) + ["abstract background"]


def split_for_shots(text: str, n: int) -> list[str]:
    """Split a scene's text into n consecutive parts (on sentence edges when possible)
    so each shot is matched to what is being said while it is on screen."""
    if n <= 1:
        return [text]
    sents = [x for x in SENT_RE.split(text) if x.strip()]
    if len(sents) >= n:
        parts, per = [], len(sents) / n
        for k in range(n):
            parts.append(" ".join(sents[round(k * per):round((k + 1) * per)]))
        return [p for p in parts if p] or [text]
    words = text.split()
    per = max(1, math.ceil(len(words) / n))
    return [" ".join(words[k * per:(k + 1) * per]) or text for k in range(n)]


def assign_queries(scenes: list[Scene]) -> QueryMaker:
    qm = QueryMaker(scenes)
    for s in scenes:
        s.queries = qm(s.text, s.tag) + [x for x in qm.fallback() if x not in qm(s.text, s.tag)]
    return qm


# --------------------------------------------------------------------------- text to speech

async def _edge_save(text: str, voice: str, rate: str, path: Path) -> list:
    """Save the voiceover and return word timings [[start, end, word], ...] in seconds."""
    import edge_tts
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    except TypeError:                       # edge-tts < 7 always sends word boundaries
        comm = edge_tts.Communicate(text, voice, rate=rate)
    words = []
    with open(path, "wb") as fh:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                st = chunk["offset"] / 1e7
                words.append([st, st + chunk["duration"] / 1e7, chunk["text"]])
    return words


def _system_tts(text: str, path: Path) -> None:
    import pyttsx3
    eng = pyttsx3.init()
    eng.save_to_file(text, str(path))
    eng.runAndWait()


def synthesize(scene: Scene, cfg: Config, tts_dir: Path) -> None:
    ext = ".mp3" if cfg.tts == "edge" else ".wav"
    path = tts_dir / f"tts_{sha(scene.text, cfg.voice, cfg.rate, cfg.tts)}{ext}"
    wpath = path.with_suffix(".words.json")
    need_words = cfg.tts == "edge" and not wpath.exists()
    if need_words or not (path.exists() and path.stat().st_size > 0):
        tmp = path.with_name(path.stem + ".part" + ext)
        if cfg.tts == "edge":
            last, words = None, []
            for attempt in range(4):
                try:
                    words = asyncio.run(_edge_save(scene.text, cfg.voice, cfg.rate, tmp))
                    if tmp.exists() and tmp.stat().st_size > 0:
                        break
                except Exception as e:  # network hiccup / throttling
                    last = e
                time.sleep(1.5 * (attempt + 1))
            else:
                raise RuntimeError(f"edge-tts failed ({last}). Check your internet, or use --tts system")
            wpath.write_text(json.dumps(words), "utf-8")
        else:
            _system_tts(scene.text, tmp)
        tmp.replace(path)
    scene.audio = path
    scene.speech_dur = probe_duration(path)
    if wpath.exists():
        try:
            scene.words = json.loads(wpath.read_text("utf-8"))
        except ValueError:
            scene.words = []


def align_words(text: str, bounds: list, speech_dur: float) -> list:
    """Attach the voice engine's word timings to the script's own words (which keep
    their punctuation). Words the engine split or expanded ("70%" → "seventy
    percent") get their times interpolated from their neighbours."""
    toks = text.split()
    norm = lambda x: re.sub(r"[^\w]", "", x.lower())
    times: list = [None] * len(toks)
    j = 0
    for b0, b1, bt in bounds:
        nb = norm(bt)
        if not nb:
            continue
        for k in range(j, min(j + 4, len(toks))):
            nt = norm(toks[k])
            if nt and (nt == nb or nb in nt or nt in nb):
                if times[k] is None:
                    times[k] = [b0, b1]
                else:
                    times[k][1] = b1
                j = k + 1 if (nt == nb or nt.endswith(nb) or nb.endswith(nt)) else k
                break
    known = [i for i, t in enumerate(times) if t]
    if not known:   # no timings at all: spread evenly by length
        tot = sum(len(t) + 2 for t in toks) or 1
        t, out = 0.0, []
        for tok in toks:
            d = speech_dur * (len(tok) + 2) / tot
            out.append([t, t + d, tok])
            t += d
        return out
    i = 0
    while i < len(toks):
        if times[i] is None:
            k = i
            while k < len(toks) and times[k] is None:
                k += 1
            a = times[i - 1][1] if i > 0 else 0.0
            b = times[k][0] if k < len(toks) else max(a + 0.3 * (k - i), speech_dur)
            step = max(0.05, (b - a) / (k - i))
            for m in range(i, k):
                times[m] = [a + step * (m - i), a + step * (m - i + 1)]
            i = k
        else:
            i += 1
    return [[t[0], t[1], tok] for t, tok in zip(times, toks)]


# --------------------------------------------------------------------------- stock media

# Results whose titles look like talks, title cards, maps or charts rarely work as b-roll
JUNK_RE = re.compile(r"\b(lecture|interview|talk|talks|speech|conference|presentation|panel|webinar|podcast|"
                     r"seminar|keynote|press|briefing|debate|tutorial|explained|explainer|news|trailer|"
                     r"intro|title|titles|credits|logo|logos|map|maps|diagram|chart|graph|infographic|"
                     r"animation|animated|simulation|screencast|screenshot|slideshow|slides|episode|"
                     r"documentary|wikipedia|wikimedia|commons|lesson|course|class|meeting|hearing|"
                     r"visualization|visualisation|model|render|cgi|3d|game|gameplay|"
                     r"body ?cam|bodycam|dashcam|dash cam|axon|cctv|surveillance|band|orchestra|concert|"
                     r"choir|astronaut|nasa|iss|spacewalk|ceremony|parade|speaker|testimony|vlog|selfie|"
                     r"timelapse of construction|unboxing|review)\b", re.I)


class StockFinder:
    def __init__(self, cfg: Config, cache: Path):
        self.cfg = cfg
        self.cache = cache
        self.used: set[str] = set()
        self.http = requests.Session()
        # Wikimedia requires a descriptive User-Agent with a contact URL
        self.http.headers["User-Agent"] = "script2video/1.1 (https://github.com/Farhan-Team/script2video)"
        self.wiki_gate = threading.Semaphore(2)   # Wikimedia rate-limits parallel downloads
        self.local: list[Path] = []
        if cfg.media_dir:
            self.local = sorted(p for p in Path(cfg.media_dir).rglob("*")
                                if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT)
        self.warned: set[str] = set()
        self.down: set[str] = set()
        self.fails: Counter = Counter()

    # ---- provider searches (results cached on disk so re-runs cost no API calls)
    def _cached_get(self, provider: str, url: str, params: dict, headers: dict) -> Optional[dict]:
        f = self.cache / f"search_{provider}_{sha(url, json.dumps(params, sort_keys=True))}.json"
        if f.exists():
            return json.loads(f.read_text("utf-8"))
        if provider in self.down:
            return None
        for attempt in range(2):
            try:
                r = self.http.get(url, params=params, headers=headers, timeout=10)
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
                self.fails[provider] = 0
                f.write_text(json.dumps(data), "utf-8")
                return data
            except requests.RequestException as e:
                if attempt == 1:
                    # a busy server (e.g. 504) can recover: give up after two failed searches
                    self.fails[provider] += 1
                    if self.fails[provider] >= 2 or isinstance(e, (requests.ConnectionError, requests.exceptions.ProxyError)):
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
    def commons_videos_relaxed(self, q: str) -> list[Media]:
        """Last resort before a plain background: allow 360p+ clips."""
        return self.commons_videos(q, min_side=360)

    def commons_videos(self, q: str, min_side: int = 0) -> list[Media]:
        """Wikimedia Commons free-licensed videos, using its 720p/1080p transcodes."""
        d = self._cached_get("commons_v", "https://commons.wikimedia.org/w/api.php",
                             {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                              "gsrsearch": f"{q} filetype:video", "gsrlimit": 25,
                              "prop": "videoinfo", "viprop": "url|size|derivatives|extmetadata"}, {})
        out = []
        pages = ((d or {}).get("query") or {}).get("pages") or {}
        portrait = self.cfg.height > self.cfg.width
        for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            info = (p.get("videoinfo") or [{}])[0]
            dur = float(info.get("duration") or 0)
            title = re.sub(r"^File:|\.[a-z0-9]+$", "", p.get("title", ""), flags=re.I).replace("_", " ")
            if (dur and dur < 3) or dur > 900 or JUNK_RE.search(title):    # long = usually a talk/film
                continue
            w, h = int(info.get("width") or 0), int(info.get("height") or 0)
            if w and h and ((h > w) != portrait) and abs(w - h) > 0.1 * max(w, h):
                continue                                                      # wrong orientation
            if min(w, h) and min(w, h) < (min_side or min(720, min(self.cfg.width, self.cfg.height))):
                continue                                                      # too low-res: blurry
            ders = [x for x in info.get("derivatives", []) if x.get("src") and x.get("height")
                    and "audio" not in (x.get("type") or "")]
            target = min(self.cfg.width, self.cfg.height)                     # 1080p when available
            lo = min_side or 720
            ders = [x for x in ders if lo <= min(int(x["height"]), int(x.get("width") or 9999)) <= 1080] or ders
            if not ders:
                continue
            best = min(ders, key=lambda x: abs(int(x["height"]) - target))
            meta = info.get("extmetadata") or {}
            artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()[:80] or "Unknown"
            lic = (meta.get("LicenseShortName") or {}).get("value", "")
            out.append(Media("video", "commons", f"commons_{p.get('pageid')}", url=best["src"], duration=dur,  # noqa
                             credit=f"Video by {artist} ({lic}) via Wikimedia Commons",
                             page=info.get("descriptionurl", "")))
        return out

    def commons_photos(self, q: str) -> list[Media]:
        """Wikimedia Commons free-licensed photos and historical images — huge, high-res, no key."""
        d = self._cached_get("commons_p", "https://commons.wikimedia.org/w/api.php",
                             {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
                              "gsrsearch": f"{q} filetype:bitmap", "gsrlimit": 30, "prop": "imageinfo",
                              "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 2560}, {})
        portrait = self.cfg.height > self.cfg.width
        need = min(self.cfg.width, self.cfg.height)
        out = []
        pages = ((d or {}).get("query") or {}).get("pages") or {}
        for p in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            info = (p.get("imageinfo") or [{}])[0]
            title = re.sub(r"^File:|\.[a-z0-9]+$", "", p.get("title", ""), flags=re.I).replace("_", " ")
            w, h = int(info.get("width") or 0), int(info.get("height") or 0)
            if info.get("mime") not in ("image/jpeg", "image/png", "image/webp") or not (w and h):
                continue
            if JUNK_RE.search(title) or re.search(r"\b(coat of arms|flag|seal|signature|plan|icon)\b", title, re.I):
                continue
            if min(w, h) < need * 0.8:
                continue                                                      # too small: blurry
            if (not portrait and h > w * 1.15) or (portrait and w > h * 1.15):
                continue                                                      # wrong orientation
            meta = info.get("extmetadata") or {}
            artist = re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()[:80] or "Unknown"
            lic = (meta.get("LicenseShortName") or {}).get("value", "")
            out.append(Media("image", "commons", f"commons_p{p.get('pageid')}",
                             url=info.get("thumburl") or info["url"],
                             credit=f"Image by {artist} ({lic}) via Wikimedia Commons",
                             page=info.get("descriptionurl", "")))
        return out

    def openverse_photos(self, q: str) -> list[Media]:
        """Openverse (WordPress) free-licensed photos — no key needed."""
        d = self._cached_get("openverse", "https://api.openverse.org/v1/images/",
                             {"q": q, "page_size": 20, "category": "photograph",
                              "aspect_ratio": "tall" if self.cfg.height > self.cfg.width else "wide",
                              "mature": "false"}, {})
        out = []
        for r in (d or {}).get("results", []):
            if not r.get("url") or JUNK_RE.search(r.get("title") or ""):
                continue
            if r.get("width") and r.get("height") and min(r["width"], r["height"]) < 0.6 * min(self.cfg.width, self.cfg.height):
                continue                                                      # too small: blurry
            out.append(Media("image", "openverse", f"openverse_{r.get('id')}", url=r["url"],
                             credit=f"Photo by {r.get('creator') or 'Unknown'} "
                                    f"({(r.get('license') or '').upper()} {r.get('license_version') or ''}) "
                                    f"via {r.get('source') or 'Openverse'}".replace("  ", " "),
                             page=r.get("foreign_landing_url", "")))
        return out

    def find_music(self, mood: str, seed: str) -> Optional[Media]:
        """Free background music from Openverse (Jamendo, ccMixter, Freesound…), no key.
        Only licenses that allow commercial use, so monetised YouTube videos are fine."""
        for q in (mood, f"{mood.split()[0]} instrumental", "ambient instrumental", "cinematic"):
            d = self._cached_get("openverse_a", "https://api.openverse.org/v1/audio/",
                                 {"q": q, "page_size": 20, "category": "music", "license_type": "commercial",
                                  "length": "medium,long", "mature": "false"}, {})
            cands = [r for r in (d or {}).get("results", []) if r.get("url")
                     and (r.get("duration") or 0) >= 60_000
                     and not re.search(r"\b(vocal|vocals|song|lyrics|remix|rap|speech|podcast)\b",
                                       (r.get("title") or "") + " " + " ".join(t.get("name", "") for t in
                                                                               (r.get("tags") or [])), re.I)]
            if cands:
                r = random.Random(seed).choice(cands[:8])
                return Media("audio", "openverse", f"music_{r.get('id')}", url=r["url"],
                             duration=(r.get("duration") or 0) / 1000,
                             credit=f"Music: \"{r.get('title') or 'Untitled'}\" by {r.get('creator') or 'Unknown'} "
                                    f"({(r.get('license') or '').upper()} {r.get('license_version') or ''}) "
                                    f"via {r.get('source') or 'Openverse'}",
                             page=r.get("foreign_landing_url", ""))
        return None

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
        """One clip per shot. Each shot first searches for what is being said while it is on
        screen (scene.shot_queries[k]), then falls back to the scene's overall keywords."""
        has_keys = bool(self.cfg.pexels_key or self.cfg.pixabay_key or self.cfg.unsplash_key)
        if self.cfg.prefer == "photo":
            providers = [self.local_matches, self.pexels_photos, self.unsplash_photos, self.pixabay_photos,
                         self.openverse_photos, self.pexels_videos, self.pixabay_videos, self.commons_videos]
        else:
            providers = [self.local_matches, self.pexels_videos, self.pixabay_videos,
                         self.pexels_photos, self.unsplash_photos, self.pixabay_photos,
                         self.commons_videos, self.openverse_photos]
        if not has_keys:  # keyless: HD Commons video, then sharp HD photos (Ken Burns)
            providers = [p for p in providers if p not in (self.commons_videos, self.openverse_photos)] + \
                        ([self.commons_photos, self.openverse_photos, self.commons_videos]
                         if self.cfg.prefer == "photo" else
                         [self.commons_videos, self.commons_photos, self.openverse_photos])
        if has_keys:  # keyless sources are only a fallback when keys exist
            providers = [p for p in providers if p not in (self.commons_videos, self.openverse_photos)] + \
                        [self.commons_videos, self.commons_photos, self.openverse_photos]
        chosen: list[Media] = []
        for k in range(n):
            own = scene.shot_queries[k] if k < len(scene.shot_queries) else []
            queries = own + [q for q in scene.queries if q not in own]
            got = None
            for q in queries:
                for prov in providers:
                    cands = [m for m in prov(q) if m.key not in self.used]
                    # clips long enough to cover the shot without looping come first
                    cands.sort(key=lambda m: 0 if (m.kind != "video" or m.duration >= seg) else 1)
                    if cands:
                        got = cands[0]
                        break
                if got:
                    break
            if got is None:                          # last resort: softer (360p+) clips beat a blank shot
                for q in queries:
                    cands = [m for m in self.commons_videos_relaxed(q) if m.key not in self.used]
                    if cands:
                        got = cands[0]
                        break
            if got is None and self.local:          # reuse local footage at random
                fresh = [p for p in self.local if f"local_{p}" not in self.used] or self.local
                got = self._local_media(random.Random(sha(scene.text, k)).choice(fresh))
            if got is None:
                got = Media("color", "none", f"color{k}")
            got.need = seg
            self.used.add(got.key)
            chosen.append(got)
        return chosen

    def _fetch_segment(self, m: Media) -> bool:
        """Download only the part of a long remote video that the shot uses (HTTP range
        requests via FFmpeg) instead of the whole file — e.g. 1 MB instead of 20 MB."""
        # Read only the first part of the file, in order (seeking inside remote WebM files
        # produced broken clips). The render step then skips the intro of this piece.
        lead = 1.0 if m.source in ("pexels", "pixabay") else min(12.0, max(1.5, 0.2 * m.duration))
        upto = min(m.duration, lead + m.need + 1.5)
        dest = self.cache / f"{m.source}_{sha(m.url, round(upto, 1))}_head.mkv"
        if not (dest.exists() and dest.stat().st_size > 0):
            tmp = dest.with_name(dest.stem + ".part.mkv")
            gate = self.wiki_gate if "wikimedia.org" in m.url else None
            for attempt in range(4):
                if gate:
                    gate.acquire()
                try:
                    p = subprocess.run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error",
                                        "-user_agent", self.http.headers["User-Agent"],
                                        "-i", m.url, "-t", f"{upto:.2f}",
                                        "-map", "0:v:0", "-c", "copy", str(tmp)],
                                       capture_output=True, text=True, timeout=180)
                except subprocess.TimeoutExpired:
                    p = None
                finally:
                    if gate:
                        gate.release()
                if p is not None and p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    tmp.replace(dest)
                    break
                err = (p.stderr if p is not None else "timeout") or ""
                time.sleep(4 * (attempt + 1) if "429" in err else 1)
            else:
                return False
        d = probe_duration(dest)
        if d < min(1.0, m.need * 0.5) or not decodes(dest):
            dest.unlink(missing_ok=True)
            return False
        m.path, m.duration = dest.resolve(), d      # render skips the intro as usual
        return True

    def download(self, m: Media) -> None:
        if m.path or m.kind == "color":
            return
        if m.kind == "video" and m.need and m.duration > m.need + 8 and self.cfg.partial_downloads:
            if self._fetch_segment(m):
                return
        ext = {"video": ".mp4", "image": ".jpg"}.get(m.kind) or (Path(m.url.split("?")[0]).suffix or ".mp3")
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


def decodes(path: Path) -> bool:
    """True if FFmpeg can decode the first frames (catches truncated/broken downloads)."""
    p = subprocess.run([ffmpeg_bin(), "-v", "error", "-i", str(path), "-frames:v", "5", "-f", "null", "-"],
                       capture_output=True, text=True)
    return p.returncode == 0 and "Invalid" not in p.stderr and "error" not in p.stderr.lower()


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
        end_punct = w[-1:] in ".!?,;:؟۔…،" or w.endswith(("\"", "'"))
        if len(buf) >= max_words or (end_punct and len(buf) >= 3):
            chunks.append(" ".join(buf))
            buf = []
    if buf:
        if chunks and len(buf) <= 2:
            chunks[-1] += " " + " ".join(buf)
        else:
            chunks.append(" ".join(buf))
    return chunks


URDU_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")


def caption_font(text: str, cfg: Config) -> tuple[str, float]:
    """Urdu/Arabic-script text gets a Nastaliq font (if installed); returns (font, size factor)."""
    if URDU_RE.search(text) and cfg.font == "Arial":
        try:
            out = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
        except OSError:
            out = ""
        # DejaVu Sans renders Urdu reliably with every libass build; Nastaliq variable fonts can show
        # empty boxes on some builds (seen on Colab), so they are only used if DejaVu is missing.
        for fam in ("DejaVu Sans", "Noto Sans Arabic", "Noto Naskh Arabic", "Jameel Noori Nastaleeq"):
            if fam in out:
                return fam, (0.85 if "Nastaliq" in fam or "Nastaleeq" in fam else 1.0)
    return cfg.font, 1.0


# Urdu letters some fonts (e.g. DejaVu Sans) lack -> look-alike Arabic letters they do have
URDU_FALLBACK = str.maketrans({"\u06C1": "\u0647", "\u06C2": "\u06C0", "\u06C3": "\u0629"})


def write_ass(scene: Scene, cfg: Config, path: Path) -> None:
    W, H = cfg.width, cfg.height
    vertical = H > W
    font, factor = caption_font(scene.text, cfg)
    if font == "DejaVu Sans":
        scene = Scene(scene.text.translate(URDU_FALLBACK), scene.tag, scene.queries, scene.audio,
                      scene.speech_dur, [[a, b, w.translate(URDU_FALLBACK)] for a, b, w in scene.words])
    size = cfg.font_size or round((W * 0.052 if vertical else H * 0.056) * factor)
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
        f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,{outc},{back},-1,0,0,0,100,100,0,0,"
        f"{border},{outline},{shadow},2,{margin_lr},{margin_lr},{margin_v},1",
        # same layout without a box: drawn on top of the boxed line for word highlighting
        f"Style: Hi,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&HFF000000,-1,0,0,0,100,100,0,0,"
        f"1,0,0,2,{margin_lr},{margin_lr},{margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if cfg.captions == "words":
        lines += karaoke_events(scene, cfg)
        path.write_text("\n".join(lines) + "\n", "utf-8")
        return
    chunks = caption_chunks(scene.text, cfg.captions, cfg.caption_words)
    weights = [len(c) + 6 for c in chunks]          # +6 ≈ the small pause at each break
    total_w = sum(weights) or 1
    t = 0.0
    for c, w in zip(chunks, weights):
        d = scene.speech_dur * w / total_w
        lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(t + d)},Default,,0,0,0,,{ass_escape(c)}")
        t += d
    path.write_text("\n".join(lines) + "\n", "utf-8")


def karaoke_events(scene: Scene, cfg: Config) -> list[str]:
    """Short phrases on screen; the word being spoken lights up (TikTok / YouTube style).
    Exact timings come from the voice engine; without them, timing is estimated."""
    words = align_words(scene.text, scene.words, scene.speech_dur)
    groups, buf = [], []
    for w in words:
        buf.append(w)
        if len(buf) >= cfg.caption_words or (w[2][-1:] in ".!?,;:؟۔…،" and len(buf) >= 3):
            groups.append(buf)
            buf = []
    if buf:
        if groups and len(buf) <= 2:
            groups[-1] += buf
        else:
            groups.append(buf)
    hl = cfg.highlight
    boxed = cfg.caption_style == "box"
    rtl = bool(URDU_RE.search(scene.text))   # colour tags break right-to-left shaping: phrase-only
    if rtl:
        boxed = True
    events = []
    for gi, g in enumerate(groups):
        g_end = groups[gi + 1][0][0] if gi + 1 < len(groups) else max(g[-1][1], scene.speech_dur)
        if boxed:   # one continuous box per phrase (colour changes would otherwise split the box)
            plain = " ".join(ass_escape(t) for _, _, t in g)
            events.append(f"Dialogue: 0,{ass_time(g[0][0])},{ass_time(g_end)},Default,,0,0,0,,{plain}")
        if rtl:
            continue
        for i, (st, en, _) in enumerate(g):
            start = g[0][0] if i == 0 else st
            end = g[i + 1][0] if i + 1 < len(g) else g_end
            if end - start < 0.02:
                continue
            txt = " ".join((f"{{\\1c{hl}&}}{ass_escape(t)}{{\\r}}" if j == i else ass_escape(t))
                           for j, (_, _, t) in enumerate(g))
            events.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},{'Hi' if boxed else 'Default'},,0,0,0,,{txt}")
    return events


# --------------------------------------------------------------------------- encoder (GPU / CPU)

_ENC_LOCK = threading.Lock()
_ENC_CACHE: dict = {}


def video_encoder_args(cfg: Config) -> list[str]:
    """NVIDIA GPU encoding (NVENC) when available — e.g. Colab's free T4 — else x264 on the CPU."""
    with _ENC_LOCK:
        key = (cfg.encoder, cfg.crf, cfg.preset)
        if key in _ENC_CACHE:
            return _ENC_CACHE[key]
        cap = "5M" if max(cfg.width, cfg.height) >= 1920 else "3M"
        args = ["-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf), "-maxrate", cap, "-bufsize", "10M"]
        if cfg.encoder in ("auto", "gpu"):
            for preset in ("p4", "fast"):
                trial = ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", str(cfg.crf + 1),
                         "-b:v", "0", "-maxrate", cap, "-bufsize", "10M", "-spatial-aq", "1"] if preset == "p4" else \
                        ["-c:v", "h264_nvenc", "-preset", preset, "-rc", "vbr", "-cq", str(cfg.crf + 1)]
                test = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                                       "-i", "color=s=640x360:r=30:d=0.5", *trial, "-pix_fmt", "yuv420p",
                                       "-f", "null", "-"], capture_output=True, text=True)
                if test.returncode == 0:
                    args = trial
                    break
            else:
                if cfg.encoder == "gpu":
                    log("  ! GPU encoder (NVENC) not available here — using the CPU")
        log("• Encoder: " + ("GPU (NVENC)" if "h264_nvenc" in args else "CPU (x264)"))
        _ENC_CACHE[key] = args
        return args


# --------------------------------------------------------------------------- scene rendering

def render_scene(idx: int, scene: Scene, cfg: Config, sdir: Path) -> Path:
    W, H, FPS = cfg.width, cfg.height, cfg.fps
    total = scene.speech_dur + cfg.pause
    sig = sha(scene.text, scene.audio, [str(m.path or m.key) for m in scene.media], W, H, FPS, cfg.captions,
              cfg.caption_style, cfg.font, cfg.font_size, cfg.caption_words, cfg.pause, cfg.crf, cfg.preset,
              video_encoder_args(cfg), cfg.highlight, json.dumps(scene.words))
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
            if m.duration > seg + 2 and not m.trimmed:
                # stock clips: skip the often-static first second; Commons/archive clips often open
                # with a title card, so start ~20% in (different offsets keep reused clips fresh)
                lead = 1.0 if m.source in ("pexels", "pixabay", "local") else min(12.0, max(1.5, 0.2 * m.duration))
                start = min(lead, m.duration - seg - 0.5)
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
            *video_encoder_args(cfg), "-pix_fmt", "yuv420p",
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
    qm = assign_queries(scenes)
    t_start = time.time()
    stage = {"t": t_start}

    def lap(name: str) -> None:
        now = time.time()
        log(f"  ⏱ {name}: {now - stage['t']:.0f}s")
        stage["t"] = now

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
    lap("voiceover")

    # 2. footage
    cfg.progress(0.25, "Finding stock footage…")
    finder = StockFinder(cfg, cache)
    for i, s in enumerate(scenes):
        total = s.speech_dur + cfg.pause
        n = max(1, round(total / cfg.clip_seconds))
        s.shot_queries = [qm(part) for part in split_for_shots(s.text, n)]
        s.media = finder.pick(s, n, total / n)
        cfg.progress(0.25 + 0.10 * (i + 1) / len(scenes), f"Searching footage {i + 1}/{len(scenes)}")
    all_media = [m for s in scenes for m in s.media]
    music_media = None
    if cfg.auto_music and not cfg.music:
        music_media = finder.find_music(cfg.music_mood, sha(cfg.script_text))
        if music_media:
            all_media.append(music_media)
        else:
            log("  ! No free background music found — continuing without music")
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for _ in ex.map(finder.download, all_media):
            done += 1
            cfg.progress(0.35 + 0.15 * done / len(all_media), f"Downloading media {done}/{len(all_media)}")
    srcs = Counter(m.source for m in all_media if m.kind != "audio")
    log("• Media: " + ", ".join(f"{v} {k}" for k, v in srcs.items()))
    music_path = Path(cfg.music) if cfg.music else (
        music_media.path if music_media and music_media.kind == "audio" and music_media.path else None)
    if music_media and music_path:
        log(f"• {music_media.credit}")
    lap("footage")

    # 3. render scenes
    files: list[Optional[Path]] = [None] * len(scenes)
    done = 0

    def _render(i: int) -> None:
        files[i] = render_scene(i, scenes[i], cfg, sdir)

    video_encoder_args(cfg)
    workers = cfg.workers or max(2, os.cpu_count() or 2)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(_render, range(len(scenes))):
            done += 1
            cfg.progress(0.50 + 0.45 * done / len(scenes), f"Rendering scene {done}/{len(scenes)}")
            if done % 5 == 0 or done == len(scenes):
                log(f"  rendered {done}/{len(scenes)}")

    lap("rendering")

    # 4. join (+ music)
    cfg.progress(0.96, "Joining scenes…")
    out = Path(cfg.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    listfile = work / "concat.txt"
    listfile.write_text("".join(f"file '{f.as_posix()}'\n" for f in files), "utf-8")
    body = work / "joined.mp4" if music_path else out
    run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", "-movflags", "+faststart", str(body)])
    if music_path:
        dur = probe_duration(body)
        fade = max(0.0, dur - 3)
        # music "ducks" (gets quieter) automatically whenever the voice is speaking
        run([ffmpeg_bin(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(body),
             "-stream_loop", "-1", "-i", str(Path(music_path).resolve()),
             "-filter_complex",
             f"[0:a]asplit=2[voice][key];"
             f"[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,volume={cfg.music_volume},"
             f"afade=t=in:d=2,afade=t=out:st={fade:.2f}:d=3[m];"
             f"[key]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[k];"
             f"[m][k]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=500[duck];"
             f"[voice][duck]amix=inputs=2:duration=first:normalize=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-t", f"{dur:.3f}", "-movflags", "+faststart", str(out)])

    credits = sorted({f"{m.credit} — {m.page}".rstrip(" —") for m in all_media if m.source not in ("none",)})
    out.with_suffix(".credits.txt").write_text("\n".join(credits) + "\n", "utf-8")
    lap("joining")
    cfg.progress(1.0, "Done")
    log(f"✓ Saved {out}  ({probe_duration(out) / 60:.1f} min) in {(time.time() - t_start) / 60:.1f} min")
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
    ap.add_argument("--captions", choices=["words", "phrases", "sentences", "none"], default="words",
                    help="words = highlight each word as it is spoken (default)")
    ap.add_argument("--caption-style", choices=["box", "outline"], default="box")
    ap.add_argument("--caption-words", type=int, default=6, help="max words on screen at once")
    ap.add_argument("--font", default="Arial", help="caption font name installed on your system")
    ap.add_argument("--font-size", type=int, default=0)
    ap.add_argument("--music", help="background music file, or 'auto' for free music (loops, ducks under voice)")
    ap.add_argument("--music-mood", default="cinematic ambient", help="for --music auto, e.g. 'upbeat', 'calm piano'")
    ap.add_argument("--music-volume", type=float, default=0.18)
    ap.add_argument("--pause", type=float, default=0.35, help="silence between scenes (s)")
    ap.add_argument("--workdir", type=Path, help="cache folder (default: <script>_work)")
    ap.add_argument("--workers", type=int, default=0, help="scenes rendered in parallel (0 = one per CPU core)")
    ap.add_argument("--encoder", choices=["auto", "gpu", "cpu"], default="auto",
                    help="auto = NVIDIA GPU if available (e.g. Colab T4), else CPU")
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
        font=a.font, font_size=a.font_size,
        music=Path(a.music) if a.music and a.music.lower() != "auto" else None,
        auto_music=bool(a.music and a.music.lower() == "auto"), music_mood=a.music_mood,
        music_volume=a.music_volume,
        crf=a.crf, preset=a.preset, workers=a.workers, encoder=a.encoder,
    )
    t0 = time.time()
    build_video(cfg)
    log(f"  took {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
