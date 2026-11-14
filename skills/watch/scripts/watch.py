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
                [--referer URL] [--study | --motion --step 0.5 --min-gap 0.15]
    --study is the cinematography preset (fast cuts, in/mid/out frames, graded motion per shot);
    with none of --study/--motion/--step/--min-gap you get the plain one-frame-per-shot view.
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
import functools
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
# --motion grades the in/out difference: under STILL is a locked-off shot (compression noise sits
# around 0-2), up to STRONG is a slight move (a gentle image-to-video drift scored 9.7), above it a
# strong one (a held 5% push scored 17.6, fast camera arcs on drama reels 20-40); see tests/test_watch.py.
STILL, STRONG = 3.0, 12.0
# --motion: back-to-back shots shorter than this are one fast move or transition, not real cuts
# (a fast camera arc or a dip to black splits into 0.2s fragments).
FRAGMENT = 0.5


def ff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", *args], capture_output=True, text=True)


def probe(video: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                          str(video)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


# ffprobe field -> av1_metadata option. Some platforms (Facebook reels, for one) serve AV1 tagged
# with "reserved" colour primaries; ffmpeg 8's scaler refuses them ("Unsupported input", exit 211),
# so every frame grab failed. Retagging those fields "unspecified" (2) on input lets them decode.
RESERVED_TAGS = {"color_primaries": "color_primaries", "color_transfer": "transfer_characteristics",
                 "color_space": "matrix_coefficients"}


@functools.cache
def input_fix(video: Path) -> tuple[str, ...]:
    """Input options that make an AV1 file with reserved colour tags decodable; () for anything
    else, so normal files run exactly the same ffmpeg command as before."""
    try:
        streams = probe(video)["streams"]
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return ()
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    bad = [opt for field, opt in RESERVED_TAGS.items() if v.get(field) == "reserved"]
    if v.get("codec_name") != "av1" or not bad:
        return ()
    return ("-bsf:v", "av1_metadata=" + ":".join(f"{opt}=2" for opt in bad))


def cut_fps(min_gap: float) -> int:
    """Sampling rate for cut detection. 10 fps (0.1s apart) resolves cuts down to ~0.2s apart;
    closer than that two cuts can fall between neighbouring samples, so sample at 20 fps."""
    return 20 if min_gap < 0.2 else 10


def scene_cuts(video: Path, sensitivity: float, min_gap: float = 0.4) -> list[float]:
    """Hard cuts: spikes in frame-to-frame change, sampled at 10 fps (20 fps when min_gap < 0.2)
    on 36×64 greyscale; a cut within min_gap seconds of the last one is ignored.

    ffmpeg's own scene score misses cuts between stills that share a palette (flat cream
    cartoons scored < 0.1), so compare pixels directly and look for outliers instead.
    """
    import numpy as np
    w, h, fps = 36, 64, cut_fps(min_gap)
    raw = subprocess.run(["ffmpeg", "-v", "error", *input_fix(video), "-i", str(video), "-vf", f"fps={fps},scale={w}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(np.float32)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    if len(diff) == 0:
        return []
    floor = max(float(np.median(diff)) * 6, 4.0 / sensitivity)
    cuts, last = [], -1.0
    for i, d in enumerate(diff):
        t = (i + 1) / fps
        if d > floor and t - last > min_gap:
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


def video_end(info: dict) -> tuple[float, float]:
    """End of the picture and its frame rate. The container's duration can run past the last
    video frame (audio longer than the picture), and ffmpeg writes nothing when asked for a
    frame there, so frame times are bounded by the video stream."""
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    dur = float(info["format"]["duration"])
    end = min(dur, float(v["duration"])) if v.get("duration") not in (None, "N/A") else dur
    try:
        num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate")).partition("/")
        fps = float(num) / float(den or 1)
    except (AttributeError, ValueError, ZeroDivisionError):
        fps = 0.0
    return end, fps if fps > 0 else 25.0


def clamp(t: float, end: float, fps: float) -> float:
    """Keep a grab time on a decodable frame: at most one frame (and at least 0.1s) before the end."""
    return max(0.0, min(t, end - max(1 / fps, 0.1)))


def grab(video: Path, t: float, out: Path) -> bool:
    """Write one frame at t; False (with a warning) if ffmpeg wrote nothing, e.g. past the last frame."""
    ff("-y", "-ss", f"{t:.3f}", *input_fix(video), "-i", str(video), "-frames:v", "1", "-vf", f"scale={THUMB_W}:-2", str(out))
    if out.exists():
        return True
    print(f"warning: no frame at {t:.2f}s, skipped")
    return False


def label_font(size: int):
    for f in FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def sheet(frames: list[tuple], out: Path, cols: int = COLS) -> None:
    """Frames are (t, path) or (t, path, label); a label ("#n in") is drawn after the time,
    which then shows hundredths so fast cuts read apart. Without labels the sheet is as before."""
    frames = [f for f in frames if f[1].exists()]
    if not frames:
        return
    thumbs = [Image.open(f[1]) for f in frames]
    w, h = thumbs[0].size
    rows = (len(thumbs) + cols - 1) // cols
    img = Image.new("RGB", (cols * w, rows * (h + 34)), "white")
    d = ImageDraw.Draw(img)
    font = label_font(22)
    for i, (f, th) in enumerate(zip(frames, thumbs)):
        x, y = (i % cols) * w, (i // cols) * (h + 34)
        img.paste(th, (x, y + 34))
        text = f"{f[0]:05.2f}s {f[2]}" if len(f) > 2 else f"{f[0]:05.1f}s"
        d.text((x + 6, y + 4), text, font=font, fill="black")
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


def vimeo(url: str, referer: str | None = None) -> tuple[str, str | None]:
    """Plain vimeo.com pages fail in yt-dlp; the player embed with a referer works.
    Rewrites vimeo.com/<id> and vimeo.com/<id>/<hash> (or ?h=<hash>) to player.vimeo.com/video/<id>,
    keeping the hash as h=. Returns the URL to fetch and the referer to send (None if not Vimeo)."""
    m = re.match(r"https?://(?:www\.)?vimeo\.com/(?:.*/)?(\d+)(?:/([0-9a-f]+))?/?(?:[?#].*)?$", url)
    if m:
        h = m.group(2) or (re.search(r"[?&]h=([0-9a-f]+)", url) or [None, None])[1]
        return f"https://player.vimeo.com/video/{m.group(1)}" + (f"?h={h}" if h else ""), referer or "https://vimeo.com/"
    if re.match(r"https?://player\.vimeo\.com/", url):
        return url, referer or "https://vimeo.com/"
    return url, referer


def download(url: str, out: Path, referer: str | None = None) -> Path:
    """Fetch a public video with yt-dlp into its own empty folder (downloads are untrusted data)."""
    src = out / "source"
    src.mkdir(parents=True, exist_ok=True)
    url, referer = vimeo(url, referer)
    subprocess.run(["uvx", "yt-dlp", "--no-playlist", *(["--referer", referer] if referer else []),
                    "-f", "bv*[height<=1920]+ba/b", "--merge-output-format", "mp4",
                    "-o", str(src / "video.%(ext)s"), "--write-info-json", url], check=True,
                   stdout=subprocess.DEVNULL)
    return src / "video.mp4"


def shot_frames(video: Path, frames_dir: Path, shots: list[tuple[float, float]], step: float,
                end: float, fps: float) -> list[tuple[float, Path]]:
    """Mid-shot frame plus one every `step` inside long shots; times clamped to the last frame,
    frames ffmpeg still couldn't write are skipped."""
    frames = []
    for i, (s, e) in enumerate(shots):
        n = max(1, int((e - s) // step))
        for k in range(n):
            t = clamp(s + (e - s) * (k + 0.5) / n, end, fps)
            f = frames_dir / f"shot-{i:03d}-{k}.jpg"
            if grab(video, t, f):
                frames.append((t, f))
    return frames


def merge_bursts(shots: list[tuple[float, float]], short: float = FRAGMENT) -> tuple[list[tuple[float, float]], dict[int, int]]:
    """--motion: merge each run of two or more back-to-back shots shorter than `short` into one
    shot, and return the shots plus {index of a merged shot: how many fragments it holds}.

    A fast camera arc or a dip to black trips the cut detector every sample, so it comes out as a
    string of 0.2s "shots". A lone short shot between longer ones is a genuine
    quick cut and stays. Lengths are compared with a little slack so a shot of exactly `short`
    (as sampled) counts as long."""
    out, bursts, i = [], {}, 0
    is_short = [e - s < short - 1e-6 for s, e in shots]
    while i < len(shots):
        j = i
        while j < len(shots) and is_short[j]:
            j += 1
        if j - i >= 2:
            bursts[len(out)] = j - i
            out.append((shots[i][0], shots[j - 1][1]))
            i = j
        else:
            out.append(shots[i])
            i += 1
    return out, bursts


def motion_frames(video: Path, frames_dir: Path, shots: list[tuple[float, float]], ids: list[int], step: float,
                  end: float, fps: float, sample_fps: int,
                  bursts: dict[int, int] | None = None) -> tuple[list[tuple[float, Path, str]], dict[int, float]]:
    """--motion: first, middle and last frame of each shot (plus evenly spaced ones every `step`
    between them), labelled "#n in / mid / out", and a motion figure per shot from in vs out.
    A merged fast move / transition (`bursts`) gets just its in and out frame.

    A cut is only known to within one detection sample, so "in" sits one video frame after the
    detected cut and "out" one sample plus one frame before the next; shots too short for that
    get just their mid frame."""
    frames, moves, bursts = [], {}, bursts or {}
    for sid, (s, e) in zip(ids, shots):
        t_in, t_out = clamp(s + 1 / fps, end, fps), clamp(e - 1 / sample_fps - 1 / fps, end, fps)
        if t_out - t_in < 2 / fps:
            times = [clamp((s + e) / 2, end, fps)]
        elif sid in bursts:
            times = [t_in, t_out]
        else:
            n = max(3, int((t_out - t_in) // step) + 1)
            n += 1 - n % 2  # odd, so one frame is the exact middle
            times = [t_in + (t_out - t_in) * k / (n - 1) for k in range(n)]
        got = {}
        for k, t in enumerate(times):
            label = "mid" if len(times) == 1 or k == len(times) // 2 else \
                "in" if k == 0 else "out" if k == len(times) - 1 else ""
            f = frames_dir / f"shot-{sid:03d}-{k:02d}.jpg"
            if grab(video, t, f):
                frames.append((t, f, f"#{sid + 1} {label}".rstrip()))
                got[k] = f
        if 0 in got and len(times) - 1 in got and len(times) > 1:
            moves[sid] = motion_score(got[0], got[len(times) - 1])
    return frames, moves


def motion_score(a: Path, b: Path) -> float:
    """Mean absolute difference (0–255) between two frames at 36×64 greyscale: ~0 for a locked-off
    shot, higher for a pan, zoom, push or a lot of movement in frame."""
    x, y = (np.asarray(Image.open(p).convert("L").resize((36, 64)), np.float32) for p in (a, b))
    return float(np.abs(x - y).mean())


def grade(m: float) -> str:
    """still under STILL, slight from STILL to STRONG, strong above STRONG."""
    return "still" if m < STILL else "slight" if m <= STRONG else "strong"


def motion_cell(i: int, moves: dict[int, float], sampled: set[int], bursts: dict[int, int] | None = None) -> str:
    burst = f" · fast move / transition ({bursts[i]} fragments)" if bursts and i in bursts else ""
    if i in moves:
        return f" {moves[i]:.1f} {grade(moves[i])}{burst} |"
    return (" –" if i in sampled else " – (not sampled)") + burst + " |"


def fmt(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("video")
    p.add_argument("--out")
    p.add_argument("--sensitivity", type=float, default=1.0, help="cut sensitivity (higher = more cuts)")
    p.add_argument("--max-frames", type=int, help="most shots to sample (default 30; 120 with --study)")
    p.add_argument("--step", type=float, help="seconds between frames inside a shot "
                   "(default 2; 0.5 with --study)")
    p.add_argument("--min-gap", type=float, help="shortest gap between two detected cuts, ≥0.1s "
                   "(default 0.4; 0.15 with --study; under 0.2 samples at 20 fps)")
    p.add_argument("--motion", action="store_true", help="in/mid/out frames per shot and a motion figure, "
                   "so pans, zooms and pushes show")
    p.add_argument("--study", action="store_true", help="cinematography preset: --motion --step 0.5 "
                   "--min-gap 0.15 --max-frames 120 (explicit flags win)")
    p.add_argument("--model", default="base", help="faster-whisper model: tiny, base, small, medium, large-v3")
    p.add_argument("--referer", help="page the video is embedded on (Vimeo embeds; default https://vimeo.com/)")
    a = p.parse_args()
    # Default mode (none of these flags) gives the plain one-frame-per-shot output.
    custom = a.study or a.motion or a.step is not None or a.min_gap is not None
    if a.study:
        a.motion = True
        a.step = 0.5 if a.step is None else a.step
        a.min_gap = 0.15 if a.min_gap is None else a.min_gap
        a.max_frames = 120 if a.max_frames is None else a.max_frames
    a.max_frames = 30 if a.max_frames is None else a.max_frames
    a.min_gap = 0.4 if a.min_gap is None else a.min_gap
    if a.min_gap < 0.1:
        p.error("--min-gap must be at least 0.1")
    if a.step is not None and a.step <= 0:
        p.error("--step must be positive")
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        sys.exit(f"watch.py needs {' and '.join(missing)} on PATH (macOS: brew install ffmpeg; "
                 "Debian/Ubuntu: apt install ffmpeg; Windows: winget install ffmpeg)")
    source_note = ""
    if re.match(r"https?://", a.video):
        out = Path(a.out) if a.out else Path.cwd() / (re.sub(r"[^A-Za-z0-9]+", "-", a.video.split("//", 1)[1])[:60] + ".watch")
        video = download(a.video, out, a.referer)
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
    vend, fps = video_end(info)

    # Shots: cut points → one frame from the middle of each shot (capped, evenly thinned).
    bounds = [0.0] + [c for c in scene_cuts(video, a.sensitivity, a.min_gap) if c < vend] + [vend]
    shots = [(s, e) for s, e in zip(bounds, bounds[1:]) if e - s > 0.05]
    bursts: dict[int, int] = {}
    if a.motion:  # after cut detection, so default mode never sees it
        shots, bursts = merge_bursts(shots)
    ids = list(range(len(shots))) if len(shots) <= a.max_frames else \
        [round(i * (len(shots) - 1) / (a.max_frames - 1)) for i in range(a.max_frames)]
    pick = [shots[i] for i in ids]
    # Mid-shot frame, plus one every 2s inside long shots (animated reels often act out a
    # whole scene in one shot: the expressions change even when the camera doesn't cut).
    step = a.step if a.step is not None else 2.0
    moves: dict[int, float] = {}
    if a.motion:
        frames, moves = motion_frames(video, out / "frames", pick, ids, step, vend, fps, cut_fps(a.min_gap), bursts)
    else:
        frames = shot_frames(video, out / "frames", pick, step, vend, fps)
    for n in range(0, len(frames), PER_SHEET):
        sheet(frames[n:n + PER_SHEET], out / f"sheet-{n // PER_SHEET + 1}.jpg")

    # Hook: the first 3 seconds, every 0.5s.
    hook = []
    for i, t in enumerate([0.1, 0.5, 1.0, 1.5, 2.0, 2.5]):
        if t < vend:
            f = out / "frames" / f"hook-{i}.jpg"
            t = clamp(t, vend, fps)
            if grab(video, t, f):
                hook.append((t, f))
    if hook:
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
    cut_list = ["## Cut list", "", "| # | start | length |", "|---|---|---|",
                *[f"| {i + 1} | {fmt(s)} | {e - s:.1f}s |" for i, (s, e) in enumerate(shots)]]
    mode = []
    if custom:
        name = "study" if a.study else "custom"
        mode = [f"- Mode: {name} · min-gap {a.min_gap}s (cuts sampled at {cut_fps(a.min_gap)} fps) · "
                f"step {step:g}s · max-frames {a.max_frames} · motion {'on' if a.motion else 'off'} · "
                f"shots under 0.5s: {sum(x < 0.5 for x in lens)}"
                + (f" · fast move / transition rows: {len(bursts)} (merged from {sum(bursts.values())} fragments)"
                   f" · motion still/slight/strong: "
                   + "/".join(str(sum(grade(m) == g for m in moves.values())) for g in ("still", "slight", "strong"))
                   if a.motion else "")]
        sampled = set(ids)
        cut_list = ["## Shots", "",
                    *(["Motion: mean change between the shot's in and out frames (0–255 at 36×64 grey), graded "
                       f"still (under {STILL:g}), slight ({STILL:g}–{STRONG:g}: a gentle drift or small push) or strong "
                       f"(over {STRONG:g}: a real pan, zoom, push, busy action, or a cut the detector missed: check the sheet). "
                       f"Back-to-back shots under {FRAGMENT:g}s are merged into one **fast move / transition** row "
                       "(a fast camera arc, whip or dip to black), with an in and an out frame; a lone quick cut stays its own row. "
                       "\"–\": shot not sampled or too short.", ""] if a.motion else []),
                    "| # | start | length |" + (" motion |" if a.motion else ""),
                    "|---|---|---|" + ("---|" if a.motion else ""),
                    *[f"| {i + 1} | {fmt(s)} | {e - s:.2f}s |" + (motion_cell(i, moves, sampled, bursts) if a.motion else "")
                      for i, (s, e) in enumerate(shots)]]
    md = [
        f"# Watch: {video.name}", "", *([source_note] if source_note else []),
        f"- Duration {dur:.1f}s · {v['width']}×{v['height']} · {v.get('r_frame_rate')} fps · audio: {'yes' if has_audio else 'no'}",
        f"- Shots: {len(shots)} (sensitivity {a.sensitivity}) · average {sum(lens) / len(lens):.1f}s · "
        f"shortest {min(lens):.1f}s · longest {max(lens):.1f}s · {(len(shots) - 1) / dur * 60:.0f} cuts/min",
        *mode,
        f"- Speech: {len(words)} words · {len(words) / dur * 60:.0f} words/min over the whole video · "
        f"talking {speech_s / dur:.0%} of the time · language {lang}" if words else "- Speech: none detected",
        f"- Loudness: {loudness(video) if has_audio else 'n/a'} · silences ≥0.4s: {len(sil)}", "",
        "## Look at", "", "- `hook.jpg`: first 3s, every 0.5s",
        *[f"- `sheet-{i + 1}.jpg`: {'frames' if custom else 'shots'} {i * PER_SHEET + 1}–{min((i + 1) * PER_SHEET, len(frames))}"
          for i in range((len(frames) + PER_SHEET - 1) // PER_SHEET)],
        "", *cut_list,
        "", "## Transcript", "", *(lines or ["(no speech)"]),
        "", "## Silences", "", *([f"- {fmt(s)}–{fmt(e)} ({e - s:.1f}s)" for s, e in sil] or ["(none)"]),
    ]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print(f"wrote {out}/summary.md · {len(shots)} shots · {len(frames)} frames on "
          f"{(len(frames) + PER_SHEET - 1) // PER_SHEET} sheet(s) · {len(words)} words")


if __name__ == "__main__":
    main()
