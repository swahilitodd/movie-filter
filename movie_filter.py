#!/usr/bin/env python3
"""Movie Filter: find swear words via subtitles, pin them down in the audio,
write a mute (.edl) file, and censor the subtitle.

    python movie_filter.py                    open the window
    python movie_filter.py "D:\\TV" --auto     no window, use recommended subtitles
    python movie_filter.py "D:\\TV" --auto --dry-run   change nothing, just report

Requires ffmpeg/ffprobe on PATH and `pip install -r requirements.txt`
(Python 3.12, 3.13 or 3.14). See README.txt.
"""
from __future__ import annotations

import argparse
import dataclasses
import difflib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi"}
TRASH = "Trash"
DEFAULT_DICT = Path(__file__).resolve().parent / "swear_words.txt"
Log = Callable[[str], None]

# ---------------------------------------------------------------- dictionary


class Dictionary:
    def __init__(self, entries: list[str]):
        if not entries:
            raise ValueError("The swear word dictionary is empty")
        self.patterns = [(e, re.compile(self._to_regex(e), re.I)) for e in entries if not e.startswith("!")]
        self.allowed = [re.compile(self._to_regex(e[1:]), re.I) for e in entries if e.startswith("!")]

    @classmethod
    def load(cls, path: Path) -> "Dictionary":
        entries = []
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#") and line not in entries:
                entries.append(line)
        return cls(entries)

    @staticmethod
    def _to_regex(entry: str) -> str:
        pre = r"\w*" if entry.startswith("*") else ""
        post = r"\w*" if entry.endswith("*") else ""
        words = [re.escape(w) for w in re.split(r"[\s\-]+", entry.strip("*"))]
        apos = r"['\u2019]"
        body = r"[\s\-]+".join(words).replace("'", apos)
        return rf"(?<![\w]){pre}{body}{post}(?![\w])"

    def find(self, text: str) -> list[tuple[int, int, str]]:
        """Return non-overlapping (start, end, entry), longest match wins."""
        safe = [m.span() for rx in self.allowed for m in rx.finditer(text)]
        found = []
        for entry, rx in self.patterns:
            found += [(m.start(), m.end(), entry) for m in rx.finditer(text)
                      if not any(a <= m.start() and m.end() <= b for a, b in safe)]
        found.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
        kept: list[tuple[int, int, str]] = []
        for hit in found:
            if all(hit[1] <= k[0] or hit[0] >= k[1] for k in kept):
                kept.append(hit)
        return sorted(kept)


# ----------------------------------------------------------------- subtitles

TIME_RE = re.compile(
    r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")
TAG_RE = re.compile(r"<[^>]*>|\{[^}]*\}")


@dataclass
class Cue:
    start: float
    end: float
    raw_start: int  # character span of the text inside the file
    raw_end: int
    text: str  # tags removed


@dataclass
class Subtitle:
    path: Path
    text: str
    encoding: str
    cues: list[Cue]


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def read_srt(path: Path) -> Subtitle:
    data = Path(path).read_bytes()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    elif data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = data.decode(encoding, errors="replace")
    return Subtitle(Path(path), text, encoding, _parse_cues(text))


def _parse_cues(text: str) -> list[Cue]:
    cues: list[Cue] = []
    current = None
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        m = TIME_RE.search(line)
        if m:
            current = [_secs(*m.groups()[:4]), _secs(*m.groups()[4:]), pos + len(line), pos + len(line)]
            cues.append(current)
        elif not stripped:
            current = None
        elif current is not None:
            current[3] = pos + len(line)
        pos += len(line)
    return [Cue(a, b, s, e, TAG_RE.sub("", text[s:e]).replace("\r", "").strip()) for a, b, s, e in cues]


def censor_text(text: str, cues: list[Cue], words: Dictionary) -> str:
    """Star out letters of each match, leaving tags and line breaks in place."""
    out, last = [], 0
    for cue in cues:
        out.append(text[last:cue.raw_start])
        for piece in re.split(r"(<[^>]*>|\{[^}]*\})", text[cue.raw_start:cue.raw_end]):
            if TAG_RE.fullmatch(piece):
                out.append(piece)
                continue
            chars = list(piece)
            for start, end, _ in words.find(piece):
                for i in range(start, end):
                    if chars[i].isalnum():
                        chars[i] = "*"
            out.append("".join(chars))
        last = cue.raw_end
    out.append(text[last:])
    return "".join(out)


# ------------------------------------------------------- choosing a subtitle

OTHER_LANGS = {
    "es", "spa", "spanish", "fr", "fre", "fra", "french", "de", "ger", "deu", "german", "it", "ita",
    "italian", "pt", "por", "portuguese", "ru", "rus", "russian", "ja", "jpn", "japanese", "ko",
    "kor", "korean", "zh", "chi", "zho", "chinese", "ar", "ara", "arabic", "nl", "dut", "nld",
    "dutch", "sv", "swe", "swedish", "no", "nor", "norwegian", "da", "dan", "danish", "fi", "fin",
    "finnish", "pl", "pol", "polish", "tr", "tur", "turkish", "he", "heb", "hebrew", "cs", "cze",
    "czech", "el", "gre", "greek", "hu", "hun", "hungarian", "th", "tha", "thai", "vi", "vie",
    "vietnamese", "uk", "ukr", "ukrainian", "ro", "rum", "romanian", "id", "ind", "indonesian",
}
ENGLISH = {"en", "eng", "english"}
SDH = {"sdh", "cc", "hi", "hearing", "impaired", "closedcaption", "closedcaptions", "captions"}
DESCRIPTIVE = {"descriptive", "description", "desc", "ad", "audiodescription", "dvs", "described"}
FORCED = {"forced", "foreign", "signs", "songs", "commentary", "comment"}
SOUND_RE = re.compile(r"^\s*[\[(].*[\])]\s*$|[\u266a\u266b#]|^\s*-?\s*[A-Z][A-Z .']+:", re.M)


@dataclass
class Candidate:
    path: Path
    kind: str
    cues: int
    sound_pct: int
    swears: int
    score: float
    preview: str


@dataclass
class Episode:
    video: Path
    candidates: list[Candidate]
    chosen: Optional[Path] = None
    review: bool = False
    skip: bool = False
    status: str = "Waiting"


def sidecars(video: Path) -> list[Path]:
    stem = video.stem.lower()
    return sorted(
        p for p in video.parent.iterdir()
        if p.is_file() and p.suffix.lower() == ".srt"
        and (p.stem.lower() == stem or p.name.lower().startswith(stem + "."))
    )


def classify(video: Path, path: Path, words: Dictionary, max_cues: int = 0) -> Candidate:
    suffix = path.stem[len(video.stem):].lower()
    tokens = set(t for t in re.split(r"[\W_]+", suffix) if t)
    sub = read_srt(path)
    texts = [c.text for c in sub.cues if c.text]
    sound = sum(1 for t in texts if SOUND_RE.search(t))
    pct = round(100 * sound / len(texts)) if texts else 0
    swears = sum(len(words.find(t)) for t in texts)

    score, kind = 100.0, "Standard"
    if tokens & DESCRIPTIVE:
        kind, score = "Descriptive", score - 80
    elif tokens & FORCED:
        kind, score = "Forced / partial", score - 80
    elif tokens & SDH:
        kind, score = "CC / SDH", score - 40
    elif pct >= 15:
        kind, score = "Looks like CC", score - 25
    if tokens & OTHER_LANGS and not tokens & ENGLISH:
        kind, score = "Other language", score - 200
    elif not tokens & ENGLISH:
        score -= 5
    if not texts:
        score -= 300
    score -= pct * 0.5
    return Candidate(path, kind, len(texts), pct, swears, score, "\n".join(texts[:400]))


def scan(root: Path, words: Dictionary, force: bool, log: Log) -> list[Episode]:
    episodes = []
    videos = sorted(
        p for p in Path(root).rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
        and TRASH.lower() not in (x.lower() for x in p.relative_to(root).parts)
    )
    for video in videos:
        if video.with_suffix(".edl").exists() and not force:
            log(f"Already done, skipping: {video.name}")
            continue
        try:
            candidates = [classify(video, p, words) for p in sidecars(video)]
        except OSError as exc:
            log(f"Could not read subtitles for {video.name}: {exc}")
            continue
        most = max((c.cues for c in candidates), default=0)
        for c in candidates:  # a much shorter file than its siblings is likely partial
            if c.cues < most * 0.5:
                c.score -= 30
        candidates.sort(key=lambda c: -c.score)
        ep = Episode(video, candidates)
        if not candidates:
            ep.skip, ep.status = True, "No subtitle file"
            log(f"No subtitle file found, skipping: {video.name}")
        else:
            best = candidates[0]
            if best.score > 0:
                ep.chosen = best.path
            gap = best.score - candidates[1].score if len(candidates) > 1 else 99
            ep.review = best.score <= 0 or gap < 15
            if ep.review:
                ep.status = "Please check"
        episodes.append(ep)
    return episodes


# --------------------------------------------------------------- audio tools


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=flags)


def probe(video: Path) -> tuple[Optional[int], float]:
    """Return (index among audio streams of the main English track, duration)."""
    res = _run(["ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=index,codec_type:stream_tags=language,title:stream_disposition=default,comment,visual_impaired,hearing_impaired",
                "-of", "json", str(video)])
    if res.returncode:
        raise RuntimeError(f"ffprobe failed: {res.stderr.strip()[:300]}")
    info = json.loads(res.stdout)
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    best, best_score = None, -1e9
    for i, s in enumerate(audio):
        tags = {k.lower(): str(v).lower() for k, v in s.get("tags", {}).items()}
        disp = s.get("disposition", {})
        score = -i * 0.01
        if tags.get("language") in ("en", "eng"):
            score += 10
        elif tags.get("language") not in (None, "", "und"):
            score -= 10
        if disp.get("default"):
            score += 3
        title = tags.get("title", "")
        if disp.get("comment") or disp.get("visual_impaired") or re.search(r"comment|descri", title):
            score -= 20
        if score > best_score:
            best, best_score = i, score
    return best, float(info.get("format", {}).get("duration") or 0)


def extract_clip(video: Path, track: int, start: float, length: float, out: Path) -> None:
    res = _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(video),
                "-t", f"{length:.3f}", "-map", f"0:a:{track}", "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(out)])
    if res.returncode or not out.exists():
        raise RuntimeError(f"ffmpeg failed: {res.stderr.strip()[:300]}")


def load_wav(path: Path):
    """Read the 16 kHz mono 16-bit WAV that extract_clip writes, as float32 samples."""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 16000:
            raise RuntimeError("Unexpected audio format from ffmpeg")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


@dataclass
class Word:
    text: str
    start: float
    end: float
    prob: float = 1.0


class WhisperRecognizer:
    def __init__(self, model: str, device: str, log: Log):
        from faster_whisper import WhisperModel
        log(f"Loading speech model '{model}' (the first run downloads it)...")
        self.model = WhisperModel(model, device=device,
                                  compute_type="int8" if device == "cpu" else "auto")

    def transcribe(self, wav: Path) -> list[Word]:
        # The clip is handed over as samples rather than a file name, so the
        # recognizer never opens it with PyAV (whose options keep changing).
        segments, _ = self.model.transcribe(
            load_wav(wav), language="en", word_timestamps=True, beam_size=5,
            condition_on_previous_text=False, vad_filter=False)
        return [Word(w.word.strip(), w.start, w.end, w.probability)
                for seg in segments for w in (seg.words or [])]


def make_recognizer(model: str, device: str, log: Log):
    return WhisperRecognizer(model, device, log)


# ------------------------------------------------------- locating the swears


@dataclass
class Options:
    model: str = "medium.en"
    device: str = "cpu"
    action: int = 1  # EDL: 1 = mute, 0 = skip
    pad_before: float = 0.15
    pad_after: float = 0.10
    merge_gap: float = 0.25
    dry_run: bool = False
    force: bool = False


@dataclass
class Hit:
    start: float
    end: float
    word: str
    source: str  # audio | estimate | heard-only
    time_text: str = ""


@dataclass
class SubHit:
    cue: Cue
    char_start: int
    char_end: int
    entry: str


def _heard_hits(words: list[Word], dictionary: Dictionary) -> list[tuple[Word, Word, str, str]]:
    """Dictionary matches in recognized words: (first, last, entry, text)."""
    joined, spans, pos = [], [], 0
    for w in words:
        clean = w.text.strip(" .,!?;:\"()[]-\u2014\u2026")
        joined.append(clean)
        spans.append((pos, pos + len(clean)))
        pos += len(clean) + 1
    text = " ".join(joined)
    hits = []
    def word_at(a: int, b: int):
        inside = [i for i, s in enumerate(spans) if s[0] < b and s[1] > a]
        return (words[inside[0]], words[inside[-1]]) if inside else None
    for a, b, entry in dictionary.find(text):
        got = word_at(a, b)
        if got:
            hits.append((got[0], got[1], entry, text[a:b]))
    # Whisper sometimes writes "f***" or "s**t" itself
    for m in re.finditer(r"(?<!\S)\w?\*+\w*(?!\S)", text):
        got = word_at(m.start(), m.end())
        if got:
            hits.append((got[0], got[1], "*", m.group()))
    hits.sort(key=lambda h: h[0].start)
    return hits


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w']+", text.lower().replace("\u2019", "'"))


def _estimate(hit: SubHit, words: list[Word], clip_start: float) -> tuple[float, float]:
    """The word wasn't recognized (often misheard). Find it from its neighbours."""
    cue = hit.cue
    said = _tokens(cue.text)
    index = len(_tokens(cue.text[:hit.char_start]))
    count = max(1, len(_tokens(cue.text[hit.char_start:hit.char_end])))
    heard = [t[0] if (t := _tokens(w.text)) else "" for w in words]
    matcher = difflib.SequenceMatcher(None, said, heard, autojunk=False)
    before = after = None
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            if block.a + k < index:
                before = words[block.b + k]
            elif block.a + k >= index + count and after is None:
                after = words[block.b + k]
    if before and after and 0 <= after.start - before.end <= 3.0:
        # Keep a little of each neighbour, because recognized word edges are approximate.
        return clip_start + before.end - 0.05, clip_start + after.start + 0.05
    if before:
        return clip_start + before.end - 0.05, clip_start + before.end + 1.0
    if after:
        return clip_start + after.start - 1.0, clip_start + after.start + 0.05
    length = cue.end - cue.start
    if length <= 4.0:
        return cue.start, cue.end
    mid = cue.start + length * ((hit.char_start + hit.char_end) / 2) / max(len(cue.text), 1)
    return max(cue.start, mid - 1.25), min(cue.end, mid + 1.25)


def locate(video: Path, cues: list[Cue], dictionary: Dictionary, recognizer, opts: Options,
           log: Log) -> list[Hit]:
    sub_hits = [SubHit(c, a, b, e) for c in cues for a, b, e in dictionary.find(c.text)]
    if not sub_hits:
        return []
    track, duration = probe(video)
    if track is None:
        raise RuntimeError("The video has no audio track")

    # Lines close together are listened to as a single clip.
    groups: list[list[SubHit]] = []
    for h in sub_hits:
        if groups and h.cue.start - groups[-1][-1].cue.end < 2.0:
            groups[-1].append(h)
        else:
            groups.append([h])

    hits: list[Hit] = []
    with tempfile.TemporaryDirectory(prefix="moviefilter") as tmp:
        wav = Path(tmp) / "clip.wav"
        for n, group in enumerate(groups, 1):
            log(f"  Listening {n}/{len(groups)} at {_clock(group[0].cue.start)}")
            start = max(0.0, group[0].cue.start - 2.0)
            end = group[-1].cue.end + 2.0
            if duration:
                end = min(end, duration)
            extract_clip(video, track, start, max(end - start, 0.1), wav)
            words = recognizer.transcribe(wav)
            hits += _pair(group, _heard_hits(words, dictionary), words, start)
    return sorted(hits, key=lambda h: h.start)


def _pair(group: list[SubHit], heard, words: list[Word], clip_start: float) -> list[Hit]:
    """Match subtitle hits to heard hits in order."""
    def same(a: str, b: str) -> bool:
        return a == b or a == "*" or b == "*" or a.strip("*")[:4] == b.strip("*")[:4]

    # Order-preserving alignment (longest common subsequence).
    n, m = len(group), len(heard)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            table[i][j] = (table[i + 1][j + 1] + 1 if same(group[i].entry, heard[j][2])
                           else max(table[i + 1][j], table[i][j + 1]))
    out: list[Hit] = []
    i = j = 0
    used = set()
    while i < n and j < m:
        if same(group[i].entry, heard[j][2]) and table[i][j] == table[i + 1][j + 1] + 1:
            first, last, _, text = heard[j]
            out.append(Hit(clip_start + first.start, clip_start + last.end, text, "audio"))
            used.add(j)
            i, j = i + 1, j + 1
            group[i - 1] = None  # type: ignore[call-overload]
        elif table[i + 1][j] >= table[i][j + 1]:
            i += 1
        else:
            j += 1
    for h in group:
        if h is not None:
            a, b = _estimate(h, words, clip_start)
            out.append(Hit(a, b, h.cue.text[h.char_start:h.char_end], "estimate"))
    for j, (first, last, _, text) in enumerate(heard):
        if j not in used and min(first.prob, last.prob) >= 0.5:
            out.append(Hit(clip_start + first.start, clip_start + last.end, text, "heard-only"))
    return out


def _clock(t: float) -> str:
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"


def build_edl(hits: list[Hit], opts: Options, duration: float = 0) -> str:
    spans: list[list[float]] = []
    for h in sorted(hits, key=lambda x: x.start):
        a = max(0.0, h.start - opts.pad_before)
        b = h.end + opts.pad_after
        if duration:
            b = min(b, duration)
        if spans and a - spans[-1][1] <= opts.merge_gap:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return "".join(f"{a:.3f}\t{b:.3f}\t{opts.action}\n" for a, b in spans)


# --------------------------------------------------------------- one episode


def _free_name(folder: Path, name: str) -> Path:
    target = folder / name
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        target = folder / f"{stem}.{n}{suffix}"
        n += 1
    return target


def process(ep: Episode, dictionary: Dictionary, recognizer, opts: Options, log: Log) -> str:
    video, chosen = ep.video, ep.chosen
    assert chosen is not None
    log(f"{video.name}: using {chosen.name}")
    sub = read_srt(chosen)
    hits = locate(video, sub.cues, dictionary, recognizer, opts, log)
    duration = probe(video)[1] if hits else 0
    edl = build_edl(hits, opts, duration)
    censored = censor_text(sub.text, sub.cues, dictionary)
    counts = {s: sum(h.source == s for h in hits) for s in ("audio", "estimate", "heard-only")}
    summary = (f"{len(hits)} found: {counts['audio']} pinned in audio, {counts['estimate']} estimated "
               f"(check these), {counts['heard-only']} heard but not in subtitle")
    log(f"  {summary}")
    report = [f"{_clock(h.start)}  {h.source:<10}  {h.word}" for h in hits]
    if opts.dry_run:
        for line in report:
            log("    " + line)
        return summary + " (preview only, no files changed)"

    # Nothing has changed up to here, so a failure above leaves the folder untouched.
    edl_path = video.with_suffix(".edl")
    tmp_edl = edl_path.with_name(edl_path.name + ".tmp")
    tmp_sub = chosen.with_name(chosen.name + ".tmp")
    tmp_edl.write_text(edl, encoding="ascii", newline="\n")
    tmp_sub.write_bytes(censored.encode(sub.encoding, errors="replace"))

    trash = video.parent / TRASH
    trash.mkdir(exist_ok=True)
    shutil.move(str(chosen), str(_free_name(trash, f"{chosen.stem}.original.srt")))
    os.replace(tmp_sub, chosen)
    for other in sidecars(video):
        if other != chosen:
            shutil.move(str(other), str(_free_name(trash, other.name)))
    report_file = _free_name(trash, f"{video.stem}.filter-report.txt")
    report_file.write_text(summary + "\n\n" + "\n".join(report) + "\n", encoding="utf-8")
    os.replace(tmp_edl, edl_path)  # last, because its presence marks the episode as done
    return summary


def run_all(episodes: list[Episode], dictionary: Dictionary, opts: Options, log: Log,
            status: Callable[[Episode], None] = lambda e: None) -> int:
    todo = [e for e in episodes if e.chosen and not e.skip]
    if not todo:
        log("Nothing to process.")
        return 0
    recognizer = None
    failures = 0
    for i, ep in enumerate(todo, 1):
        ep.status = "Working"
        status(ep)
        log(f"[{i}/{len(todo)}]")
        try:
            if recognizer is None:
                recognizer = make_recognizer(opts.model, opts.device, log)
            log("  " + process(ep, dictionary, recognizer, opts, log))
            ep.status = "Previewed" if opts.dry_run else "Done"
        except ImportError:
            log("The speech recognizer is not installed. Delete the .venv folder and run the launcher again.")
            ep.status = "Failed"
            failures += 1
            status(ep)
            break
        except Exception as exc:  # keep going with the other episodes
            failures += 1
            ep.status = "Failed"
            log(f"  FAILED {ep.video.name}: {exc}")
            log(traceback.format_exc(limit=2))
        status(ep)
    log(f"Finished. {len(todo) - failures} succeeded, {failures} failed.")
    return failures


# ----------------------------------------------------------------------- GUI


def run_gui(root: Optional[Path], dict_path: Path, opts: Options) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    win = tk.Tk()
    win.title("Movie Filter")
    win.geometry("1180x760")
    win.minsize(900, 560)
    dictionary = Dictionary.load(dict_path)
    state: dict = {"episodes": [], "busy": False}
    messages: "queue.Queue" = queue.Queue()

    top = ttk.Frame(win, padding=8)
    top.pack(fill="x")
    folder = tk.StringVar(value=str(root) if root else "")
    ttk.Label(top, text="Folder").pack(side="left")
    ttk.Entry(top, textvariable=folder).pack(side="left", fill="x", expand=True, padx=6)

    panes = ttk.PanedWindow(win, orient="horizontal")
    panes.pack(fill="both", expand=True, padx=8)

    left = ttk.Frame(panes)
    ttk.Label(left, text="1. Episodes (\"Please check\" needs your eye)").pack(anchor="w")
    ep_tree = ttk.Treeview(left, columns=("status", "subtitle"), selectmode="browse")
    ep_tree.heading("#0", text="Video")
    ep_tree.heading("status", text="Status")
    ep_tree.heading("subtitle", text="Subtitle")
    ep_tree.column("#0", width=210)
    ep_tree.column("status", width=125, stretch=False)
    ep_tree.column("subtitle", width=190)
    ep_tree.tag_configure("review", background="#fff1c9", foreground="#3a2c00")
    ep_tree.pack(fill="both", expand=True)
    panes.add(left, weight=2)

    right = ttk.Frame(panes)
    ttk.Label(right, text="2. Subtitle files (double-click to use one)").pack(anchor="w")
    sub_tree = ttk.Treeview(right, columns=("use", "kind", "cues", "sound", "swears"), height=6,
                            selectmode="browse")
    for col, title, width in (("use", "Use", 45), ("kind", "Type", 120), ("cues", "Lines", 55),
                              ("sound", "Sound cues", 95), ("swears", "Swears", 60)):
        sub_tree.heading(col, text=title)
        sub_tree.column(col, width=width, stretch=False, anchor="w")
    sub_tree.heading("#0", text="File")
    sub_tree.column("#0", width=250)
    sub_tree.pack(fill="x")
    ttk.Label(right, text="Preview (swear words highlighted)").pack(anchor="w", pady=(8, 0))
    preview = tk.Text(right, height=10, wrap="word", state="disabled")
    preview.tag_configure("swear", background="#ffc9c9", foreground="#5c0000")
    preview.pack(fill="both", expand=True)
    panes.add(right, weight=3)

    controls = ttk.Frame(win, padding=8)
    controls.pack(fill="x")
    action = tk.StringVar(value="Skip" if opts.action == 0 else "Mute")
    model = tk.StringVar(value=opts.model)
    dry = tk.BooleanVar(value=opts.dry_run)
    skip_var = tk.BooleanVar()
    ttk.Label(controls, text="At swear words:").pack(side="left")
    ttk.Combobox(controls, textvariable=action, values=("Mute", "Skip"), width=6,
                 state="readonly").pack(side="left", padx=(4, 12))
    acc_label = ttk.Label(controls, text="Accuracy (?):")
    acc_label.pack(side="left")
    acc_box = ttk.Combobox(controls, textvariable=model, values=MODEL_CHOICES,
                           width=10, state="readonly")
    acc_box.pack(side="left", padx=(4, 12))
    Tooltip(acc_label, ACCURACY_HELP)
    Tooltip(acc_box, ACCURACY_HELP)
    ttk.Checkbutton(controls, text="Preview only (change no files)", variable=dry).pack(side="left")
    skip_btn = ttk.Checkbutton(controls, text="Leave this episode out", variable=skip_var)
    skip_btn.pack(side="left", padx=12)
    start_btn = ttk.Button(controls, text="Start")
    start_btn.pack(side="right")

    log_box = tk.Text(win, height=9, state="disabled", wrap="word")
    log_box.pack(fill="x", padx=8, pady=(0, 8))

    def log(msg: str) -> None:
        messages.put(("log", msg))

    def current() -> Optional[Episode]:
        sel = ep_tree.selection()
        return state["episodes"][int(sel[0])] if sel else None

    def refresh_row(i: int) -> None:
        ep = state["episodes"][i]
        label = "(left out)" if ep.skip else (ep.chosen.name if ep.chosen else "(none chosen)")
        ep_tree.item(str(i), values=(ep.status, label),
                     tags=("review",) if ep.status == "Please check" else ())

    def show_preview(cand: Optional[Candidate]) -> None:
        preview.configure(state="normal")
        preview.delete("1.0", "end")
        if cand:
            preview.insert("1.0", cand.preview)
            for lineno, line in enumerate(cand.preview.split("\n"), 1):
                for a, b, _ in dictionary.find(line):
                    preview.tag_add("swear", f"{lineno}.{a}", f"{lineno}.{b}")
        preview.configure(state="disabled")

    def show_episode(_=None) -> None:
        ep = current()
        sub_tree.delete(*sub_tree.get_children())
        show_preview(None)
        if not ep:
            return
        skip_var.set(ep.skip)
        for n, c in enumerate(ep.candidates):
            used = "\u2713" if c.path == ep.chosen else ""
            sub_tree.insert("", "end", iid=str(n), text=c.path.name,
                            values=(used, c.kind, c.cues, f"{c.sound_pct}%", c.swears))
        if ep.candidates:
            pick = next((n for n, c in enumerate(ep.candidates) if c.path == ep.chosen), 0)
            sub_tree.selection_set(str(pick))

    def on_sub_select(_=None) -> None:
        ep, sel = current(), sub_tree.selection()
        show_preview(ep.candidates[int(sel[0])] if ep and sel else None)

    def on_use(_=None) -> None:
        ep, sel = current(), sub_tree.selection()
        if ep and sel:
            ep.chosen = ep.candidates[int(sel[0])].path
            if ep.status == "Please check":
                ep.status = "Waiting"
            refresh_row(int(ep_tree.selection()[0]))
            keep = sel[0]
            show_episode()
            sub_tree.selection_set(keep)

    def on_skip() -> None:
        ep = current()
        if ep and ep.candidates:
            ep.skip = skip_var.get()
            refresh_row(int(ep_tree.selection()[0]))

    def load() -> None:
        path = folder.get().strip()
        if not path or not Path(path).is_dir():
            return
        ep_tree.delete(*ep_tree.get_children())
        state["episodes"] = scan(Path(path), dictionary, opts.force, log)
        for i, ep in enumerate(state["episodes"]):
            ep_tree.insert("", "end", iid=str(i), text=ep.video.name)
            refresh_row(i)
        first = next((i for i, e in enumerate(state["episodes"]) if e.status == "Please check"), 0)
        if state["episodes"]:
            ep_tree.selection_set(str(first))
        log(f"Found {len(state['episodes'])} video(s) to process.")

    def browse() -> None:
        chosen = filedialog.askdirectory(title="Choose your movies folder")
        if chosen:
            folder.set(chosen)
            load()

    def start() -> None:
        eps = state["episodes"]
        unchecked = [e for e in eps if e.status == "Please check" and not e.skip]
        if unchecked and not messagebox.askyesno(
                "Movie Filter", f"{len(unchecked)} episode(s) still say 'Please check'. "
                "Use the highlighted subtitle for them anyway?"):
            return
        run_opts = dataclasses.replace(opts, model=model.get(), dry_run=dry.get(),
                                       action=0 if action.get() == "Skip" else 1)
        start_btn.configure(state="disabled")
        def work() -> None:
            try:
                run_all(eps, dictionary, run_opts, log, lambda e: messages.put(("row", eps.index(e))))
            finally:
                messages.put(("done", None))
        threading.Thread(target=work, daemon=True).start()

    def pump() -> None:
        while True:
            try:
                kind, value = messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                log_box.configure(state="normal")
                log_box.insert("end", value + "\n")
                log_box.see("end")
                log_box.configure(state="disabled")
            elif kind == "row":
                refresh_row(value)
            else:
                start_btn.configure(state="normal")
        win.after(100, pump)

    ttk.Button(top, text="Browse...", command=browse).pack(side="left")
    ttk.Button(top, text="Rescan", command=load).pack(side="left", padx=(6, 0))
    ep_tree.bind("<<TreeviewSelect>>", show_episode)
    sub_tree.bind("<<TreeviewSelect>>", on_sub_select)
    sub_tree.bind("<Double-1>", on_use)
    skip_btn.configure(command=on_skip)
    start_btn.configure(command=start)
    win.after(100, pump)
    if root:
        win.after(50, load)
    win.mainloop()


MODEL_CHOICES = ("base.en", "small.en", "medium.en", "large-v3")
ACCURACY_HELP = (
    "Which speech model listens for the swear words.\n\n"
    "medium.en (recommended): the best balance. Much better than the smaller\n"
    "  models at fast, quiet or mumbled words and words over music.\n"
    "large-v3: the most accurate and the slowest. Worth trying if the report\n"
    "  shows many 'estimated' times.\n"
    "small.en / base.en: faster, but miss more words, which gives longer\n"
    "  estimated mutes.\n\n"
    "Only the few seconds around each swear are listened to, so even the\n"
    "larger models stay practical. Each model downloads the first time you use it.")


class Tooltip:
    """Shows text while the mouse is over a widget."""

    def __init__(self, widget, text: str):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def show(self, _event=None) -> None:
        import tkinter as tk
        if self.tip:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() - 10
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        tk.Label(self.tip, text=self.text, justify="left", background="#ffffe0", foreground="#000000",
                 relief="solid", borderwidth=1, padx=6, pady=4).pack()
        self.tip.update_idletasks()
        self.tip.wm_geometry(f"+{x}+{max(0, y - self.tip.winfo_height())}")

    def hide(self, _event=None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ----------------------------------------------------------------------- CLI


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="Create mute files and censor subtitles for videos.")
    ap.add_argument("folder", nargs="?", type=Path)
    ap.add_argument("--auto", action="store_true", help="no window; use the recommended subtitle")
    ap.add_argument("--dry-run", action="store_true", help="report only; change no files")
    ap.add_argument("--skip", action="store_true", help="write skip entries instead of mutes")
    ap.add_argument("--force", action="store_true", help="also redo videos that already have an .edl")
    ap.add_argument("--model", default="medium.en",
                    help="accuracy: base.en, small.en, medium.en (default), large-v3")
    ap.add_argument("--device", default="cpu", help="cpu or cuda")
    ap.add_argument("--pad", type=float, default=0.15, help="seconds added before a word (0.10 is added after)")
    ap.add_argument("--dictionary", type=Path, default=DEFAULT_DICT)
    args = ap.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"{tool} was not found. On Windows run: winget install Gyan.FFmpeg")
            return 2
    opts = Options(model=args.model, device=args.device, action=0 if args.skip else 1,
                   pad_before=args.pad, dry_run=args.dry_run, force=args.force)
    if not args.auto:
        run_gui(args.folder, args.dictionary, opts)
        return 0
    if not args.folder or not args.folder.is_dir():
        print("--auto needs a folder")
        return 2
    dictionary = Dictionary.load(args.dictionary)
    episodes = scan(args.folder, dictionary, args.force, print)
    for ep in episodes:
        if ep.review and ep.chosen:
            print(f"Unsure about {ep.video.name}; using {ep.chosen.name}")
        elif ep.review:
            print(f"No usable subtitle for {ep.video.name}; skipping")
    return 1 if run_all(episodes, dictionary, opts, print) else 0


if __name__ == "__main__":
    sys.exit(main())
