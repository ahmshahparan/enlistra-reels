"""Analysis agent: turn a film into per-second signals the selector can score.

Signals:
  motion[t]   mean absolute frame difference at second t (0..255), from a tiny
              grayscale downscale so a 25-minute film analyzes in seconds.
  cuts        list of timestamps where ffmpeg's scene detector fired.
  transcript  optional list of {start, end, text} from faster-whisper, only when
              the film has dialogue and the package is installed.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

log = logging.getLogger("analyze")
W, H, FPS = 48, 27, 4  # analysis resolution


@dataclass
class Analysis:
    duration: float
    motion: list[float]              # one value per second
    cuts: list[float]
    transcript: list[dict] = field(default_factory=list)
    has_dialogue: bool = False

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self)))

    @classmethod
    def load(cls, path: Path) -> "Analysis":
        return cls(**json.loads(path.read_text()))


def probe_duration(video: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)], text=True)
    return float(out.strip())


def motion_series(video: Path) -> list[float]:
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-vf",
           f"fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.check_output(cmd)
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H * W).astype(np.int16)
    if len(frames) < 2:
        return [0.0]
    diffs = np.abs(np.diff(frames, axis=0)).mean(axis=1)      # per frame
    diffs = np.concatenate([[0.0], diffs])
    n_sec = int(np.ceil(len(diffs) / FPS))
    per_sec = [float(diffs[i * FPS:(i + 1) * FPS].mean()) for i in range(n_sec)]
    return per_sec


def scene_cuts(video: Path, threshold: float = 0.35) -> list[float]:
    cmd = ["ffmpeg", "-v", "info", "-i", str(video), "-vf",
           f"scale={W*4}:-2,select='gt(scene,{threshold})',showinfo",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return [float(m) for m in re.findall(r"pts_time:([\d.]+)", proc.stderr)]


def transcribe(video: Path) -> list[dict]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        log.info("faster-whisper not installed; skipping transcript")
        return []
    model = WhisperModel("base", compute_type="int8")
    segments, _ = model.transcribe(str(video), vad_filter=True)
    return [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]


def has_speech(video: Path) -> bool:
    """Cheap check: does the audio track carry speech-band energy? Silent films
    usually have only a music score or nothing, so we skip transcription for them."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-t", "120", "-af",
           "highpass=f=300,lowpass=f=3400,volumedetect", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"mean_volume: (-?[\d.]+) dB", proc.stderr)
    return bool(m) and float(m.group(1)) > -35.0


def analyze(video: Path, out_json: Path, want_transcript: bool = True) -> Analysis:
    if out_json.exists():
        return Analysis.load(out_json)
    log.info("analyzing %s", video.name)
    dur = probe_duration(video)
    motion = motion_series(video)
    cuts = scene_cuts(video)
    speech = has_speech(video)
    transcript = transcribe(video) if (want_transcript and speech) else []
    a = Analysis(duration=dur, motion=motion, cuts=cuts,
                 transcript=transcript, has_dialogue=bool(transcript))
    a.save(out_json)
    log.info("duration %.0fs, %d cuts, dialogue=%s", dur, len(cuts), a.has_dialogue)
    return a
