import json
import shutil
import subprocess
from datetime import UTC, datetime

import pytest

from isynca.errors import ToolMissingError
from isynca.media import probe as probe_module
from isynca.media.probe import probe, run_ffprobe
from isynca.media.types import MediaFile, MediaKind


def as_media(path, kind=MediaKind.VIDEO):
    stat = path.stat()
    return MediaFile(
        path=path,
        kind=kind,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        source_root=path.parent,
    )


@pytest.fixture
def junk_video(tmp_path):
    """A video-named file no capture-date reader can make sense of."""
    path = tmp_path / "clip.avi"
    path.write_bytes(b"RIFF junk")
    return path


@pytest.fixture
def ffprobe_says(monkeypatch):
    """Make run_ffprobe return ``report`` without running anything."""

    def install(report):
        monkeypatch.setattr(probe_module, "run_ffprobe", lambda _path: report)

    return install


# --- images ------------------------------------------------------------------


def test_reads_an_image_with_pillow(make_image):
    path = make_image(original="2023:07:14 12:34:56")
    info = probe(as_media(path, MediaKind.IMAGE))
    assert (info.container, info.width, info.height) == ("JPEG", 16, 16)
    assert info.taken == datetime(2023, 7, 14, 12, 34, 56)
    assert info.error is None


def test_an_unreadable_image_carries_the_error(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image")
    info = probe(as_media(path, MediaKind.IMAGE))
    assert info.error is not None
    assert info.container is None


# --- video -------------------------------------------------------------------


def test_reads_streams_from_the_ffprobe_report(junk_video, ffprobe_says):
    ffprobe_says(
        {
            "format": {"format_name": "avi"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mpeg4",
                    "profile": "Advanced Simple Profile",
                    "width": 640,
                    "height": 480,
                    "field_order": "progressive",
                },
                {"codec_type": "audio", "codec_name": "mp2"},
            ],
        }
    )
    info = probe(as_media(junk_video))
    assert (info.interlaced, info.duration) == (False, None)
    assert info.container == "avi"
    assert (info.video_codec, info.video_profile) == (
        "mpeg4",
        "Advanced Simple Profile",
    )
    assert info.audio_codec == "mp2"
    assert (info.width, info.height, info.rotation) == (640, 480, 0)
    assert info.taken is None


def test_skips_cover_art_and_reads_rotation(junk_video, ffprobe_says):
    ffprobe_says(
        {
            "format": {},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "disposition": {"attached_pic": 1},
                },
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "side_data_list": [
                        {"side_data_type": "Other"},
                        {"side_data_type": "Display Matrix", "rotation": -90},
                    ],
                },
            ],
        }
    )
    info = probe(as_media(junk_video))
    assert info.video_codec == "h264"
    assert info.rotation == 90
    assert info.audio_codec is None


def test_a_video_with_no_streams_reads_as_empty(junk_video, ffprobe_says):
    ffprobe_says({})
    info = probe(as_media(junk_video))
    assert (info.container, info.video_codec, info.error) == (None, None, None)


def test_falls_back_to_the_container_date_tag(junk_video, ffprobe_says):
    ffprobe_says(
        {
            "format": {
                "tags": {"creation_time": "", "date": "2005-07-20T10:11:12Z"},
            }
        }
    )
    info = probe(as_media(junk_video))
    assert info.taken == datetime(2005, 7, 20, 10, 11, 12, tzinfo=UTC)


def test_an_unparseable_date_tag_is_no_date(junk_video, ffprobe_says):
    ffprobe_says({"format": {"tags": {"creation_time": "last tuesday"}}})
    assert probe(as_media(junk_video)).taken is None


def test_the_capture_reader_wins_over_the_tag(make_video, ffprobe_says):
    ffprobe_says({"format": {"tags": {"creation_time": "2020-01-01T00:00:00Z"}}})
    info = probe(as_media(make_video()))
    assert info.taken == datetime(1999, 1, 24, 5, 20, tzinfo=UTC)


def test_an_ffprobe_failure_becomes_the_error(junk_video, ffprobe_says):
    ffprobe_says("Invalid data found when processing input")
    info = probe(as_media(junk_video))
    assert info.error == "Invalid data found when processing input"


# --- running ffprobe ---------------------------------------------------------


@pytest.fixture
def subprocess_gives(monkeypatch):
    """Replace subprocess.run with one returning, or raising, ``outcome``."""
    calls = []

    def install(outcome):
        def fake_run(command, **_kwargs):
            calls.append(command)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        monkeypatch.setattr(probe_module.subprocess, "run", fake_run)
        return calls

    return install


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_runs_ffprobe_on_the_path_and_parses_json(tmp_path, subprocess_gives):
    calls = subprocess_gives(completed(stdout=json.dumps({"format": {}})))
    assert run_ffprobe(tmp_path / "a b.avi") == {"format": {}}
    assert calls[0][0] == "ffprobe"
    assert calls[0][-1] == str(tmp_path / "a b.avi")


def test_a_missing_ffprobe_is_fatal(tmp_path, subprocess_gives):
    subprocess_gives(FileNotFoundError("ffprobe"))
    with pytest.raises(ToolMissingError, match="ffmpeg"):
        run_ffprobe(tmp_path / "a.avi")


def test_a_hung_ffprobe_is_reported(tmp_path, subprocess_gives):
    subprocess_gives(subprocess.TimeoutExpired("ffprobe", 60))
    assert "no answer" in str(run_ffprobe(tmp_path / "a.avi"))


def test_a_failure_reports_the_last_line_without_the_path(tmp_path, subprocess_gives):
    path = tmp_path / "a.mov"
    subprocess_gives(
        completed(1, stderr=f"noise\n{path}: Invalid data found when processing\n")
    )
    assert run_ffprobe(path) == "Invalid data found when processing"


def test_a_silent_failure_reports_the_exit_code(tmp_path, subprocess_gives):
    subprocess_gives(completed(1))
    assert run_ffprobe(tmp_path / "a.avi") == "ffprobe exited with 1"


def test_unparseable_output_is_reported(tmp_path, subprocess_gives):
    subprocess_gives(completed(stdout="{not json"))
    assert run_ffprobe(tmp_path / "a.avi") == "ffprobe returned an unreadable report"


def test_a_report_that_is_not_an_object_is_reported(tmp_path, subprocess_gives):
    subprocess_gives(completed(stdout="[]"))
    assert run_ffprobe(tmp_path / "a.avi") == "ffprobe returned no report"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg and ffprobe installed",
)
def test_against_the_real_ffprobe(tmp_path):
    """Pins the report shape the parser relies on to what ffprobe emits."""
    path = tmp_path / "real.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", "testsrc=size=64x48:duration=0.2",
            "-c:v", "mpeg4", "-metadata", "creation_time=2012-11-16T17:39:23Z",
            str(path),
        ],
        check=True,
    )  # fmt: skip
    info = probe(as_media(path))
    assert info.container == "mov,mp4,m4a,3gp,3g2,mj2"
    assert (info.video_codec, info.video_profile) == ("mpeg4", "Simple Profile")
    assert (info.width, info.height, info.audio_codec) == (64, 48, None)
    assert info.taken == datetime(2012, 11, 16, 17, 39, 23, tzinfo=UTC)


def test_reads_duration_and_interlacing(junk_video, ffprobe_says):
    ffprobe_says(
        {
            "format": {"format_name": "mpeg", "duration": "104.233333"},
            "streams": [
                {"codec_type": "video", "codec_name": "mpeg2video", "field_order": "tt"}
            ],
        }
    )
    info = probe(as_media(junk_video))
    assert info.interlaced
    assert info.duration == pytest.approx(104.233333)


@pytest.mark.parametrize("value", ["N/A", "0", "-1"])
def test_an_unusable_duration_is_none(junk_video, ffprobe_says, value):
    ffprobe_says({"format": {"duration": value}})
    assert probe(as_media(junk_video)).duration is None
