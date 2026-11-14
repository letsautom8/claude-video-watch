# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["numpy>=2,<3", "pillow>=11,<14"]
# ///
"""Tests for skills/watch/scripts/watch.py. No network and no speech model: the synthetic test
videos are made with ffmpeg and are silent or carry silence only where nothing is transcribed.
Needs ffmpeg on PATH; the default-output comparison also needs git and this repo's history.

  uv run tests/test_watch.py
"""
import importlib.util
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import watch  # noqa: E402


def make_video(path: Path) -> None:
    """Red 2s, blue 1.9s, then a 0.12s white flash: the last cut lands 3 frames before the
    picture ends. Silent audio runs to 6s, so the container is 2s longer than the picture,
    the shape that made the last shot's frame come back empty."""
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=red:s=180x320:r=25:d=2",
                    "-f", "lavfi", "-i", "color=blue:s=180x320:r=25:d=1.9",
                    "-f", "lavfi", "-i", "color=white:s=180x320:r=25:d=0.12",
                    "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                    "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a",
                    "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


class LastShot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.video = cls.dir / "v.mp4"
        make_video(cls.video)
        cls.info = watch.probe(cls.video)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_end_is_the_picture_not_the_container(self):
        end, fps = watch.video_end(self.info)
        self.assertAlmostEqual(float(self.info["format"]["duration"]), 6.0, delta=0.1)
        self.assertAlmostEqual(end, 4.04, delta=0.05)
        self.assertEqual(fps, 25.0)

    def test_every_shot_gets_a_frame(self):
        end, fps = watch.video_end(self.info)
        cuts = watch.scene_cuts(self.video, 1.0)
        self.assertGreaterEqual(cuts[-1], 3.8, "the synthetic last cut should sit at the end")
        bounds = [0.0] + [c for c in cuts if c < end] + [end]
        shots = [(s, e) for s, e in zip(bounds, bounds[1:]) if e - s > 0.05]
        d = self.dir / "frames-shots"
        d.mkdir()
        frames = watch.shot_frames(self.video, d, shots, 2.0, end, fps)
        self.assertEqual(len(frames), len(shots))
        self.assertTrue(all(p.exists() for _, p in frames))
        self.assertLess(frames[-1][0], end)
        watch.sheet(frames, self.dir / "sheet.jpg")
        self.assertTrue((self.dir / "sheet.jpg").exists())

    def test_old_bound_would_have_missed(self):
        # The container's end (6s) is where the old code took the last shot's middle from.
        self.assertFalse(watch.grab(self.video, 5.0, self.dir / "past-end.jpg"))

    def test_clamp(self):
        self.assertAlmostEqual(watch.clamp(4.03, 4.04, 25), 3.94)
        self.assertEqual(watch.clamp(1.0, 4.04, 25), 1.0)
        self.assertEqual(watch.clamp(-1, 4.04, 25), 0.0)

    def test_missing_frame_is_skipped_on_the_sheet(self):
        ok = self.dir / "ok.jpg"
        self.assertTrue(watch.grab(self.video, 1.0, ok))
        watch.sheet([(1.0, ok), (5.0, self.dir / "missing.jpg")], self.dir / "partial.jpg")
        self.assertTrue((self.dir / "partial.jpg").exists())
        watch.sheet([(5.0, self.dir / "missing.jpg")], self.dir / "empty.jpg")  # no crash, no file
        self.assertFalse((self.dir / "empty.jpg").exists())


REPO = Path(__file__).resolve().parent.parent
# The last watch.py before --study/--motion/--step/--min-gap: default mode must match it.
BASELINE = "769a68c"
QUICK_CUTS = [1.0, 1.2, 1.4, 1.6, 1.8, 2.0]  # five 0.2s shots, then the zoom from 2.0s to 4.0s


def make_study_video(path: Path) -> None:
    """No audio (so nothing is transcribed): a 1s static test card, five 0.2s flat-colour shots of
    distinct brightness, a 2s slow zoom (1%/frame) into colour bars, then 1s of black."""
    parts = ["rgbtestsrc=s=180x320:r=25:d=1",
             *[f"color={c}:s=180x320:r=25:d=0.2" for c in ("white", "black", "yellow", "blue", "gray")],
             "smptebars=s=360x640:r=25:d=2", "color=black:s=180x320:r=25:d=1"]
    inputs = [x for p in parts for x in ("-f", "lavfi", "-i", p)]
    chain = ";".join(
        f"[{i}]zoompan=z='1+0.01*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=180x320:fps=25,setsar=1[p{i}]"
        if i == 6 else f"[{i}]setsar=1[p{i}]" for i in range(len(parts)))
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                    chain + ";" + "".join(f"[p{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def make_quick_cut_video(path: Path) -> None:
    """No audio: 1.5s static test card, one 0.2s white flash, then 1.5s of static colour bars:
    a genuine quick cut between two long shots."""
    parts = ["rgbtestsrc=s=180x320:r=25:d=1.5", "color=white:s=180x320:r=25:d=0.2",
             "smptebars=s=180x320:r=25:d=1.5"]
    inputs = [x for p in parts for x in ("-f", "lavfi", "-i", p)]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                    "".join(f"[{i}]setsar=1[p{i}];" for i in range(3)) + "[p0][p1][p2]concat=n=3:v=1:a=0[v]",
                    "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def run_watch(script: Path, video: Path, out: Path, *flags: str) -> str:
    subprocess.run([sys.executable, str(script), str(video), "--out", str(out), *flags],
                   check=True, capture_output=True, text=True)
    return (out / "summary.md").read_text()


def shot_rows(summary: str) -> dict[int, str]:
    rows = {}
    for line in summary.splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if line.startswith("| ") and cells[0].isdigit():
            rows[int(cells[0])] = line
    return rows


class Study(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.video = cls.dir / "study.mp4"
        make_study_video(cls.video)
        cls.study = run_watch(SCRIPTS / "watch.py", cls.video, cls.dir / "study", "--study")
        cls.motion = run_watch(SCRIPTS / "watch.py", cls.video, cls.dir / "motion", "--motion")
        cls.quick = cls.dir / "quick.mp4"
        make_quick_cut_video(cls.quick)
        cls.quick_study = run_watch(SCRIPTS / "watch.py", cls.quick, cls.dir / "quick", "--study")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_sampling_rate(self):
        self.assertEqual(watch.cut_fps(0.4), 10)
        self.assertEqual(watch.cut_fps(0.2), 10)
        self.assertEqual(watch.cut_fps(0.15), 20)
        self.assertEqual(watch.cut_fps(0.1), 20)

    def test_quick_cuts_found_only_with_a_small_gap(self):
        fine = watch.scene_cuts(self.video, 1.0, 0.15)
        for c in QUICK_CUTS:
            self.assertTrue(any(abs(c - x) < 0.06 for x in fine), f"missed the cut at {c}s: {fine}")
        coarse = watch.scene_cuts(self.video, 1.0)
        self.assertLess(sum(1.0 <= x <= 2.05 for x in coarse), len(QUICK_CUTS), "default should still drop them")

    def test_burst_of_quick_cuts_is_one_row(self):
        # The detector finds all five 0.2s cuts (above); --study shows them as one row.
        self.assertIn("- Mode: study · min-gap 0.15s (cuts sampled at 20 fps) · step 0.5s · max-frames 120", self.study)
        self.assertIn("fast move / transition rows: 1 (merged from 5 fragments)", self.study)
        rows = shot_rows(self.study)
        self.assertEqual(len(rows), 4, self.study)  # card, burst, zoom, black
        self.assertIn("fast move / transition (5 fragments)", rows[2])
        self.assertRegex(rows[2], r"\| 0:01\.0 \| 1\.0\ds \| \d+\.\d strong")
        self.assertEqual([n for n, r in rows.items() if "fast move" in r], [2])

    def test_burst_has_one_in_and_one_out_frame(self):
        frames = sorted((self.dir / "study" / "frames").glob("shot-001-*.jpg"))
        self.assertEqual(len(frames), 2)
        self.assertGreater(watch.motion_score(frames[0], frames[-1]), watch.STRONG)

    def test_lone_quick_cut_is_kept(self):
        rows = shot_rows(self.quick_study)
        self.assertEqual(len(rows), 3, self.quick_study)
        self.assertIn("| 0.20s |", rows[2])
        self.assertFalse(any("fast move" in r for r in rows.values()), rows)
        self.assertIn("fast move / transition rows: 0 (merged from 0 fragments)", self.quick_study)

    def test_study_grades_the_zoom(self):
        rows = shot_rows(self.study)
        self.assertRegex(rows[3], r"\d strong \|$")  # the slow zoom
        self.assertRegex(rows[1], r"\d still \|$")  # the locked-off test card
        self.assertRegex(rows[4], r"\d still \|$")  # black tail

    def test_motion_alone_grades_the_zoom(self):
        self.assertIn("- Mode: custom · min-gap 0.4s (cuts sampled at 10 fps)", self.motion)
        # At the default 0.4s gap the quick cuts merge with their neighbours, so those merged
        # "shots" score high too (a high figure also exposes a missed cut); the locked-off
        # test card (row 1) and the black tail (last) must stay still, the shot holding the zoom must move.
        rows = shot_rows(self.motion)
        zoom = [r for r in rows.values() if any(f"| 0:0{t} |" in r for t in ("1.6", "2.0"))]
        self.assertTrue(zoom and all(" strong" in r for r in zoom), rows)
        self.assertIn(" still |", rows[1])
        self.assertIn(" still |", rows[max(rows)])

    def test_grade_boundaries(self):
        # Measured on real clips: a still 0, a gentle image-to-video drift 9.7, a held 5% push 17.6.
        self.assertEqual(watch.grade(0.0), "still")
        self.assertEqual(watch.grade(2.99), "still")
        self.assertEqual(watch.grade(3.0), "slight")
        self.assertEqual(watch.grade(9.7), "slight")
        self.assertEqual(watch.grade(12.0), "slight")
        self.assertEqual(watch.grade(12.01), "strong")
        self.assertEqual(watch.grade(17.6), "strong")

    def test_merge_bursts(self):
        long1, long2 = (0.0, 2.0), (3.0, 5.0)
        burst = [(2.0 + 0.2 * k, 2.2 + 0.2 * k) for k in range(5)]
        self.assertEqual(watch.merge_bursts([long1, *burst, long2]), ([long1, (2.0, burst[-1][1]), long2], {1: 5}))
        lone = [long1, (2.0, 2.2), (2.2, 4.0)]
        self.assertEqual(watch.merge_bursts(lone), (lone, {}))
        # a shot of exactly 0.5s is not a fragment, so it splits two runs of one into lone cuts
        split = [long1, (2.0, 2.2), (2.2, 2.7), (2.7, 2.9), (2.9, 5.0)]
        self.assertEqual(watch.merge_bursts(split), (split, {}))
        self.assertEqual(watch.merge_bursts([]), ([], {}))

    def test_in_mid_out_frames(self):
        frames = sorted((self.dir / "study" / "frames").glob("shot-002-*.jpg"))  # the third shot, the zoom
        self.assertGreaterEqual(len(frames), 3)
        self.assertGreater(watch.motion_score(frames[0], frames[-1]), watch.STRONG)
        static = sorted((self.dir / "study" / "frames").glob("shot-000-*.jpg"))
        self.assertLess(watch.motion_score(static[0], static[-1]), 2.0)


class DefaultUnchanged(unittest.TestCase):
    """With no new flag the output must be what the baseline wrote: same files, byte-identical
    summary and sheets."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        src = subprocess.run(["git", "-C", str(REPO), "show", f"{BASELINE}:skills/watch/scripts/watch.py"],
                             capture_output=True, text=True)
        if src.returncode:
            raise unittest.SkipTest(f"baseline {BASELINE} not in git: {src.stderr.strip()}")
        cls.old_path = cls.dir / "old_watch.py"
        cls.old_path.write_text(src.stdout)
        spec = importlib.util.spec_from_file_location("old_watch", cls.old_path)
        cls.old = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.old)
        cls.videos = [cls.dir / "study.mp4"]
        make_study_video(cls.videos[0])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_whole_output(self):
        v = self.videos[0]  # no audio, so no transcription is needed
        old_out, new_out = self.dir / "out-old", self.dir / "out-new"
        run_watch(self.old_path, v, old_out)
        run_watch(SCRIPTS / "watch.py", v, new_out)
        old_files = sorted(p.relative_to(old_out) for p in old_out.rglob("*") if p.is_file())
        self.assertEqual(sorted(p.relative_to(new_out) for p in new_out.rglob("*") if p.is_file()), old_files)
        for rel in old_files:
            self.assertEqual((new_out / rel).read_bytes(), (old_out / rel).read_bytes(), str(rel))


def can_encode_av1() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libsvtav1" in out


class ReservedColourAV1(unittest.TestCase):
    """AV1 files (some Facebook reels) tagged with reserved colour primaries made every ffmpeg frame grab fail
    with exit 211 ("Unsupported input" in swscale); watch.py must still cut and grab them."""

    @classmethod
    def setUpClass(cls):
        if not can_encode_av1():
            raise unittest.SkipTest("ffmpeg has no libsvtav1 encoder")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.video = cls.dir / "av1.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "color=red:s=64x112:r=10:d=1",
                        "-f", "lavfi", "-i", "color=blue:s=64x112:r=10:d=1",
                        "-filter_complex", "[0][1]concat=n=2:v=1:a=0[v]", "-map", "[v]",
                        "-c:v", "libsvtav1", "-preset", "12", "-pix_fmt", "yuv420p",
                        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                        "-bsf:v", "av1_metadata=color_primaries=3", str(cls.video)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fixture_has_reserved_primaries(self):
        v = next(s for s in watch.probe(self.video)["streams"] if s["codec_type"] == "video")
        self.assertEqual((v["codec_name"], v["color_primaries"]), ("av1", "reserved"))
        self.assertEqual(watch.input_fix(self.video), ("-bsf:v", "av1_metadata=color_primaries=2"))

    def test_grab_and_cuts(self):
        self.assertTrue(watch.grab(self.video, 0.5, self.dir / "f.jpg"))
        cuts = watch.scene_cuts(self.video, 1.0)
        self.assertEqual(len(cuts), 1)
        self.assertAlmostEqual(cuts[0], 1.0, delta=0.15)

    def test_whole_run(self):
        self.assertIn("Shots: 2", run_watch(SCRIPTS / "watch.py", self.video, self.dir / "out"))

    def test_normal_file_untouched(self):
        plain = self.dir / "plain.mp4"
        make_video(plain)
        self.assertEqual(watch.input_fix(plain), ())


class Vimeo(unittest.TestCase):
    P = "https://player.vimeo.com/video/"

    def test_plain_id(self):
        self.assertEqual(watch.vimeo("https://vimeo.com/123456789"), (self.P + "123456789", "https://vimeo.com/"))
        self.assertEqual(watch.vimeo("https://www.vimeo.com/123456789/"), (self.P + "123456789", "https://vimeo.com/"))

    def test_hash_kept(self):
        self.assertEqual(watch.vimeo("https://vimeo.com/123456789/abc123def0")[0], self.P + "123456789?h=abc123def0")
        self.assertEqual(watch.vimeo("https://vimeo.com/123456789?h=abc123def0&share=copy")[0],
                         self.P + "123456789?h=abc123def0")

    def test_channel_path(self):
        self.assertEqual(watch.vimeo("https://vimeo.com/channels/staffpicks/123456789")[0], self.P + "123456789")

    def test_page_referer_wins(self):
        page = "https://courses.example.com/lesson-1"
        self.assertEqual(watch.vimeo("https://vimeo.com/42", page), (self.P + "42", page))

    def test_player_url_gets_referer(self):
        url = self.P + "42?h=ff00"
        self.assertEqual(watch.vimeo(url), (url, "https://vimeo.com/"))

    def test_not_vimeo_untouched(self):
        url = "https://www.youtube.com/watch?v=abc"
        self.assertEqual(watch.vimeo(url), (url, None))
        self.assertEqual(watch.vimeo("https://vimeo.com/about")[0], "https://vimeo.com/about")


if __name__ == "__main__":
    unittest.main(verbosity=2)
