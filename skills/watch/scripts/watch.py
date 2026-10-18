# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["faster-whisper>=1.2,<2", "numpy>=2,<3", "pillow>=11,<14"]
# ///
"""Break a video into things Claude can read, so Claude can "watch" it.

Local and free: ffmpeg finds the cuts, grabs frames per shot (every 2s inside
long shots) and the first 3s densely, and measures silences and loudness;
faster-whisper transcribes the speech (any language, auto-detected) with
timestamps. Output: summary.md plus contact-sheet images, so a 1-minute video
costs Claude ~2 images and a page of text to read.

Usage:
  uv run watch.py <video file or URL> [--out DIR] [--sensitivity 1.0] [--max-frames 30] [--model base]
    A URL is downloaded first with yt-dlp (public videos only) into DIR/source/.
    writes DIR (default <video>.watch/, or ./<id>.watch/ for a URL):
      summary.md       metadata, pacing, cut list, transcript, audio notes
      hook.jpg         the first 3s, 6 frames
      sheet-N.jpg      labelled frames, 15 per sheet
      frames/          the individual frames

Needs ffmpeg and ffprobe on PATH, and uv (which also runs yt-dlp for links).
How Claude should use the output is in ../SKILL.md.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Label font for the contact sheets: the first that exists, else Pillow's built-in one.
FONTS = ["/System/Library/Fonts/Supplemental/Arial Black.ttf", "/System/Library/Fonts/Helvetica.ttc",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
         "C:/Windows/Fonts/arialbd.ttf"]
THUMB_W = 270  # 9:16 thumbs → 270×480
PER_SHEET, COLS = 15, 5


def ff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True)


def probe(video: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          str(video)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def scene_cuts(video: Path, sensitivity: float) -> list[float]:
    """Hard cuts: spikes in frame-to-frame change, sampled at 10 fps on 36×64 greyscale.

    ffmpeg's own scene score misses cuts between stills that share a palette (flat cream
    cartoons scored < 0.1), so compare pixels directly and look for outliers instead.
    """
    import numpy as np
    w, h, fps = 36, 64, 10
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={fps},scale={w}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(np.float32)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    if len(diff) == 0:
        return []
    floor = max(float(np.median(diff)) * 6, 4.0 / sensitivity)
    cuts, last = [], -1.0
    for i, d in enumerate(diff):
        t = (i + 1) / fps
        if d > floor and t - last > 0.4:
            cuts.append(t)
            last = t
    return cuts


def silences(video: Path) -> list[tuple[float, float]]:
    log = ff("-i", str(video), "-af", "silencedetect=noise=-35dB:d=0.4", "-vn", "-f", "null", "-").stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    return list(zip(starts, ends))


def loudness(video: Path) -> str:
    log = ff("-i", str(video), "-af", "ebur128", "-vn", "-f", "null", "-").stderr
    # The last "I:" is the final summary; earlier ones are running values.
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", log)
    return f"{m[-1]} LUFS integrated" if m else "n/a"


def grab(video: Path, t: float, out: Path) -> None:
    ff("-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2", str(out))


def label_font(size: int):
    for f in FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def sheet(frames: list[tuple[float, Path]], out: Path, cols: int = COLS) -> None:
    thumbs = [Image.open(p) for _, p in frames]
    w, h = thumbs[0].size
    rows = (len(thumbs) + cols - 1) // cols
    img = Image.new("RGB", (cols * w, rows * (h + 34)), "white")
    d = ImageDraw.Draw(img)
    font = label_font(22)
    for i, ((t, _), th) in enumerate(zip(frames, thumbs)):
        x, y = (i % cols) * w, (i // cols) * (h + 34)
        img.paste(th, (x, y + 34))
        d.text((x + 6, y + 4), f"{t:05.1f}s", font=font, fill="black")
    img.save(out, quality=85)


def transcribe(video: Path, model_name: str) -> tuple[list[dict], str]:
    """Words with timestamps, plus the detected language. Decodes with ffmpeg (PyAV breaks on new av)."""
    from faster_whisper import WhisperModel
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio, word_timestamps=True)
    words = [{"w": w.word.strip(), "start": w.start, "end": w.end} for s in segments for w in s.words or []]
    return words, f"{info.language} ({info.language_probability:.0%})"


def download(url: str, out: Path) -> Path:
    """Fetch a public video with yt-dlp into its own empty folder (downloads are untrusted data)."""
    src = out / "source"
    src.mkdir(parents=True, exist_ok=True)
    subprocess.run(["uvx", "yt-dlp", "--no-playlist", "-f", "bv*[height<=1920]+ba/b", "--merge-output-format", "mp4",
                    "-o", str(src / "video.%(ext)s"), "--write-info-json", url], check=True,
                   stdout=subprocess.DEVNULL)
    return src / "video.mp4"


def fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--out")
    p.add_argument("--sensitivity", type=float, default=1.0, help="cut sensitivity (higher = more cuts)")
    p.add_argument("--max-frames", type=int, default=30)
    p.add_argument("--model", default="base", help="faster-whisper model: tiny, base, small, medium, large-v3")
    a = p.parse_args()
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        sys.exit(f"watch.py needs {' and '.join(missing)} on PATH (macOS: brew install ffmpeg; "
                 "Debian/Ubuntu: apt install ffmpeg; Windows: winget install ffmpeg)")
    source_note = ""
    if re.match(r"https?://", a.video):
        out = Path(a.out) if a.out else Path.cwd() / (re.sub(r"[^A-Za-z0-9]+", "-", a.video.split("//", 1)[1])[:60] + ".watch")
        video = download(a.video, out)
        meta = json.loads(next((out / "source").glob("*.info.json")).read_text())
        source_note = (f"- Source: {a.video} · \"{meta.get('title', '')[:120]}\" by {meta.get('uploader', '?')} · "
                       f"{meta.get('view_count') or '?'} views · uploaded {meta.get('upload_date') or '?'}")
    else:
        video = Path(a.video).resolve()
        out = Path(a.out) if a.out else video.with_name(video.name + ".watch")
    (out / "frames").mkdir(parents=True, exist_ok=True)

    info = probe(video)
    dur = float(info["format"]["duration"])
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])

    # Shots: cut points → one frame from the middle of each shot (capped, evenly thinned).
    bounds = [0.0] + scene_cuts(video, a.sensitivity) + [dur]
    shots = [(s, e) for s, e in zip(bounds, bounds[1:]) if e - s > 0.05]
    pick = shots if len(shots) <= a.max_frames else \
        [shots[round(i * (len(shots) - 1) / (a.max_frames - 1))] for i in range(a.max_frames)]
    # Mid-shot frame, plus one every 2s inside long shots (animated reels often act out a
    # whole scene in one shot: the expressions change even when the camera doesn't cut).
    frames = []
    for i, (s, e) in enumerate(pick):
        n = max(1, int((e - s) // 2))
        for k in range(n):
            t = s + (e - s) * (k + 0.5) / n
            f = out / "frames" / f"shot-{i:03d}-{k}.jpg"
            grab(video, t, f)
            frames.append((t, f))
    for n in range(0, len(frames), PER_SHEET):
        sheet(frames[n:n + PER_SHEET], out / f"sheet-{n // PER_SHEET + 1}.jpg")

    # Hook: the first 3 seconds, every 0.5s.
    hook = []
    for i, t in enumerate([0.1, 0.5, 1.0, 1.5, 2.0, 2.5]):
        if t < dur:
            f = out / "frames" / f"hook-{i}.jpg"
            grab(video, t, f)
            hook.append((t, f))
    sheet(hook, out / "hook.jpg", cols=6)

    # Sound: transcript with timestamps, silences, loudness.
    words, lang = transcribe(video, a.model) if has_audio else ([], "n/a")
    lines, cur = [], []
    for w in words:
        cur.append(w)
        if w["w"][-1:] in ".?!" or len(cur) >= 14:
            lines.append(f"[{fmt(cur[0]['start'])}] " + " ".join(x["w"] for x in cur))
            cur = []
    if cur:
        lines.append(f"[{fmt(cur[0]['start'])}] " + " ".join(x["w"] for x in cur))
    speech_s = sum(w["end"] - w["start"] for w in words)
    sil = silences(video) if has_audio else []

    lens = [e - s for s, e in shots]
    md = [
        f"# Watch: {video.name}", "", *([source_note] if source_note else []),
        f"- Duration {dur:.1f}s · {v['width']}×{v['height']} · {v.get('r_frame_rate')} fps · audio: {'yes' if has_audio else 'no'}",
        f"- Shots: {len(shots)} (sensitivity {a.sensitivity}) · average {sum(lens) / len(lens):.1f}s · "
        f"shortest {min(lens):.1f}s · longest {max(lens):.1f}s · {(len(shots) - 1) / dur * 60:.0f} cuts/min",
        f"- Speech: {len(words)} words · {len(words) / dur * 60:.0f} words/min over the whole video · "
        f"talking {speech_s / dur:.0%} of the time · language {lang}" if words else "- Speech: none detected",
        f"- Loudness: {loudness(video) if has_audio else 'n/a'} · silences ≥0.4s: {len(sil)}", "",
        "## Look at", "", "- `hook.jpg`: first 3s, every 0.5s",
        *[f"- `sheet-{i + 1}.jpg`: shots {i * PER_SHEET + 1}–{min((i + 1) * PER_SHEET, len(frames))}"
          for i in range((len(frames) + PER_SHEET - 1) // PER_SHEET)],
        "", "## Cut list", "", "| # | start | length |", "|---|---|---|",
        *[f"| {i + 1} | {fmt(s)} | {e - s:.1f}s |" for i, (s, e) in enumerate(shots)],
        "", "## Transcript", "", *(lines or ["(no speech)"]),
        "", "## Silences", "", *([f"- {fmt(s)}–{fmt(e)} ({e - s:.1f}s)" for s, e in sil] or ["(none)"]),
    ]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print(f"wrote {out}/summary.md · {len(shots)} shots · {len(frames)} frames on "
          f"{(len(frames) + PER_SHEET - 1) // PER_SHEET} sheet(s) · {len(words)} words")


if __name__ == "__main__":
    main()
