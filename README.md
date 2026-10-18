# video-watch: let Claude watch a video

Claude can read images and text, but it can't play a video. This Claude Code plugin
bridges the gap: give it a link or a file and it turns the video into **labelled frames
plus a timestamped transcript**, then Claude tells you what the video is, who made it,
what it's for, and answers your question about it.

Everything runs on your machine with ffmpeg and faster-whisper. There's no API to pay
for and nothing is uploaded. A one-minute video costs Claude about two images and a page
of text to read.

How it was built, and why it samples frames the way it does:
[letsautom8.dev/posts/claude-watches-video](https://letsautom8.dev/posts/claude-watches-video/).

## Install

In Claude Code:

```
/plugin marketplace add letsautom8/claude-video-watch
/plugin install video-watch@letsautom8
```

You also need:

- **ffmpeg** (with ffprobe): `brew install ffmpeg`, `apt install ffmpeg` or `winget install ffmpeg`
- **uv**: [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/). It installs
  the Python dependencies on first run and runs yt-dlp for links.

## Use

Share a video and ask about it:

> what is this? https://www.youtube.com/watch?v=…
>
> is this ad legit? ~/Downloads/offer.mp4
>
> how did they structure the hook in this reel?

The skill also answers to `/video-watch:watch <link or file>`.

The first run takes a minute or two while uv fetches faster-whisper and the speech
model (about 150 MB from Hugging Face). After that, a one-minute video takes about a
minute.

## What it makes

```
watch-<name>/
  summary.md     source, uploader, duration, cut list, speech rate, language,
                 loudness, silences and the full transcript with timestamps
  hook.jpg       the first 3 seconds, every 0.5 s
  sheet-N.jpg    labelled frames: one per shot, plus one every 2 s in long shots
  frames/        the individual frames
  source/        the downloaded video (links only)
```

You can run the script without Claude too:

```sh
uv run skills/watch/scripts/watch.py <file or URL> --out ./watch-test \
  [--max-frames 30] [--sensitivity 1.0] [--model base]
```

| Option | Default | What it does |
|---|---|---|
| `--max-frames` | 30 | Most shots to sample. Use 40 or so for videos over 10 minutes. |
| `--sensitivity` | 1.0 | Cut detection. Higher finds more cuts. |
| `--model` | base | faster-whisper model: `tiny`, `base`, `small`, `medium`, `large-v3`. Bigger is slower and more accurate. |

## Limits

- **Links:** yt-dlp handles YouTube, TikTok, Instagram, X and most public video pages.
  Facebook breaks whenever Facebook changes its pages, until yt-dlp catches up. Private,
  login-only and DRM videos fail. In all of these cases, give Claude the file instead
  (a download or a screen recording).
  YouTube works best with [deno](https://deno.com) installed (yt-dlp warns without it).
- **What it can't perceive:** tone of voice, music and sound effects aren't transcribed,
  and frames miss motion between samples. The skill tells Claude to say so instead of
  guessing.
- **Transcripts make mistakes**, especially with the default `base` model, accents or
  music. Use `--model small` when the words matter.

## Safety and fair use

- Downloaded videos and their transcripts are treated as untrusted data. Spoken or
  on-screen text that tells Claude to do something is reported, not obeyed.
- Downloads go to a temp folder for private study. Don't commit or re-share them, and
  check the platform's terms: downloading can break them.

## License

MIT. Made by [Marlon Cruz](https://letsautom8.dev).
