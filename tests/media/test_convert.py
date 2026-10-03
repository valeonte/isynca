import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from PIL import Image

from isynca.errors import ConvertError, ToolMissingError
from isynca.media import convert as convert_module
from isynca.media.capture import read_container_date, read_exif_date
from isynca.media.convert import (
    _ffmpeg_command,
    _run_ffmpeg,
    convert,
    converted_path,
)
from isynca.media.probe import MediaInfo, probe
from isynca.media.types import MediaFile, MediaKind

ATHENS = timezone(timedelta(hours=3))


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
def avi(tmp_path):
    path = tmp_path / "SSL12779.AVI"
    path.write_bytes(b"RIFF")
    os.utime(path, (1_121_853_364, 1_121_853_364))  # 2005-07-20 09:56:04 UTC
    return path


def xvid(path, **fields) -> MediaInfo:
    values: dict[str, Any] = {
        "container": "avi",
        "video_codec": "mpeg4",
        "video_profile": "Advanced Simple Profile",
        "audio_codec": "mp2",
        "duration": 10.0,
    }
    values.update(fields)
    return MediaInfo(media=as_media(path), **values)


def option(command, flag):
    return command[command.index(flag) + 1]


# --- naming ------------------------------------------------------------------


def test_names_the_converted_copy(tmp_path):
    video = MediaFile(tmp_path / "a.AVI", MediaKind.VIDEO, 1, 0, tmp_path)
    image = MediaFile(tmp_path / "b.bmp", MediaKind.IMAGE, 1, 0, tmp_path)
    assert converted_path(video) == tmp_path / "a_converted.mp4"
    assert converted_path(image) == tmp_path / "b_converted.jpg"


# --- the ffmpeg command ------------------------------------------------------


def test_reencodes_what_apple_cannot_play(avi):
    command = _ffmpeg_command(xvid(avi), avi.with_name("out"), datetime.now(UTC))
    assert option(command, "-c:v") == "libx264"
    assert option(command, "-crf") == "18"
    assert option(command, "-pix_fmt") == "yuv420p"
    assert option(command, "-vf") == "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    assert option(command, "-c:a") == "aac"
    assert option(command, "-f") == "mp4"
    assert command[command.index("-i") + 1] == str(avi)
    assert command[-1] == str(avi.with_name("out"))


def test_deinterlaces_interlaced_video(avi):
    info = xvid(avi, video_codec="mpeg2video", interlaced=True)
    command = _ffmpeg_command(info, avi, datetime.now(UTC))
    assert option(command, "-vf").startswith("yadif,")


def test_copies_streams_apple_already_plays(avi):
    info = xvid(avi, video_codec="h264", video_profile="High", audio_codec="aac")
    command = _ffmpeg_command(info, avi, datetime.now(UTC))
    assert option(command, "-c:v") == "copy"
    assert option(command, "-c:a") == "copy"
    assert "-vf" not in command
    assert "-tag:v" not in command


def test_copied_hevc_gets_the_tag_apple_wants(avi):
    info = xvid(avi, video_codec="hevc", video_profile="Main 10", audio_codec="alac")
    command = _ffmpeg_command(info, avi, datetime.now(UTC))
    assert option(command, "-tag:v") == "hvc1"


def test_reencodes_h264_in_a_profile_apple_cannot_play(avi):
    info = xvid(avi, video_codec="h264", video_profile="High 4:4:4 Predictive")
    command = _ffmpeg_command(info, avi, datetime.now(UTC))
    assert option(command, "-c:v") == "libx264"


def test_writes_the_date_as_utc_creation_time(avi):
    command = _ffmpeg_command(
        xvid(avi), avi, datetime(2009, 7, 20, 15, 30, tzinfo=ATHENS)
    )
    assert "creation_time=2009-07-20T12:30:00Z" in command


def test_reads_a_naive_date_as_local_time(avi):
    naive = datetime(2009, 7, 20, 15, 30)  # noqa: DTZ001
    command = _ffmpeg_command(xvid(avi), avi, naive)
    expected = naive.astimezone(UTC)
    assert f"creation_time={expected:%Y-%m-%dT%H:%M:%SZ}" in command


# --- running ffmpeg ----------------------------------------------------------


def test_reports_progress_against_the_duration(avi, ffmpeg):
    ffmpeg(lines=["frame=1\n", "out_time_us=N/A\n", "out_time_us=5000000\n",
                  "out_time_us=12000000\n", "progress=end\n"])  # fmt: skip
    seen = []
    _run_ffmpeg(["ffmpeg", str(avi.with_name("t"))], xvid(avi), seen.append)
    assert seen == [0.5, 1.0]


def test_progress_needs_a_duration_and_a_listener(avi, ffmpeg):
    ffmpeg(lines=["out_time_us=5000000\n"])
    seen = []
    _run_ffmpeg(
        ["ffmpeg", str(avi.with_name("t"))], xvid(avi, duration=None), seen.append
    )
    _run_ffmpeg(["ffmpeg", str(avi.with_name("t"))], xvid(avi), None)
    assert seen == []


def test_a_failure_reports_ffmpegs_last_line(avi, ffmpeg):
    ffmpeg(stderr="noise\nUnknown encoder 'libx264'\n", code=1)
    with pytest.raises(ConvertError, match="Unknown encoder 'libx264'"):
        _run_ffmpeg(["ffmpeg", str(avi.with_name("t"))], xvid(avi), None)


def test_a_silent_failure_reports_the_exit_code(avi, ffmpeg):
    ffmpeg(code=8)
    with pytest.raises(ConvertError, match="ffmpeg exited with 8"):
        _run_ffmpeg(["ffmpeg", str(avi.with_name("t"))], xvid(avi), None)


def test_an_interrupt_kills_ffmpeg(avi, ffmpeg):
    instances = ffmpeg(lines=["frame=1\n"], raise_on=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _run_ffmpeg(["ffmpeg", str(avi.with_name("t"))], xvid(avi), None)
    assert instances[0].killed


def test_a_missing_ffmpeg_is_fatal(avi, monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(convert_module.subprocess, "Popen", missing)
    with pytest.raises(ToolMissingError, match="ffmpeg is not installed"):
        _run_ffmpeg(["ffmpeg"], xvid(avi), None)


# --- converting --------------------------------------------------------------


def test_converts_a_video_beside_the_original(avi, ffmpeg):
    instances = ffmpeg()
    result = convert(xvid(avi))

    assert result.output == avi.with_name("SSL12779_converted.mp4")
    assert result.output.read_bytes() == b"converted"
    assert result.output.stat().st_mtime_ns == avi.stat().st_mtime_ns
    assert avi.read_bytes() == b"RIFF"
    assert result.dated_from_mtime
    assert result.taken.timestamp() == 1_121_853_364
    assert result.copied == ()
    assert instances[0].command[-1].endswith(".SSL12779_converted.mp4.partial")
    assert sorted(p.name for p in avi.parent.iterdir()) == [
        "SSL12779.AVI",
        "SSL12779_converted.mp4",
    ]


def test_keeps_the_videos_own_date(avi, ffmpeg):
    ffmpeg()
    own = datetime(2005, 7, 20, 10, 0, tzinfo=UTC)
    result = convert(xvid(avi, taken=own))
    assert (result.taken, result.dated_from_mtime) == (own, False)


def test_reports_the_streams_it_copied(avi, ffmpeg):
    ffmpeg()
    result = convert(xvid(avi, video_codec="h264", video_profile="High"))
    assert result.copied == ("video",)


def test_dry_run_writes_nothing(avi, ffmpeg):
    instances = ffmpeg()
    result = convert(xvid(avi), dry_run=True)
    assert result.output == converted_path(as_media(avi))
    assert instances == []
    assert sorted(p.name for p in avi.parent.iterdir()) == ["SSL12779.AVI"]


def test_refuses_an_unreadable_file(avi):
    with pytest.raises(ConvertError, match="Invalid data"):
        convert(xvid(avi, error="Invalid data"))


def test_never_overwrites_an_existing_copy(avi):
    converted_path(as_media(avi)).write_bytes(b"keep me")
    with pytest.raises(ConvertError, match="already exists"):
        convert(xvid(avi))
    assert converted_path(as_media(avi)).read_bytes() == b"keep me"


def test_a_failed_conversion_leaves_nothing_behind(avi, ffmpeg):
    ffmpeg(stderr="Invalid data found\n", code=1)
    with pytest.raises(ConvertError, match="Invalid data found"):
        convert(xvid(avi))
    assert sorted(p.name for p in avi.parent.iterdir()) == ["SSL12779.AVI"]


def test_an_os_error_becomes_a_convert_error(avi):
    info = xvid(avi)
    avi.unlink()
    with pytest.raises(ConvertError, match="No such file"):
        convert(info)


# --- images ------------------------------------------------------------------


def test_converts_an_image_to_a_dated_jpeg(tmp_path):
    path = tmp_path / "scan.bmp"
    Image.new("RGB", (31, 17), (0, 90, 200)).save(path)
    os.utime(path, (1_248_093_005, 1_248_093_005))
    info = MediaInfo(media=as_media(path, MediaKind.IMAGE), container="BMP")

    result = convert(info)
    assert result.output == tmp_path / "scan_converted.jpg"
    with Image.open(result.output) as image:
        assert (image.format, image.size) == ("JPEG", (31, 17))
        offset = image.getexif().get_ifd(0x8769).get(0x9011)
    local = datetime.fromtimestamp(1_248_093_005, tz=UTC).astimezone()
    assert read_exif_date(result.output) == local.replace(tzinfo=None)
    assert offset is not None
    assert result.copied == ()


def test_an_images_own_date_is_kept_without_an_invented_offset(tmp_path):
    path = tmp_path / "scan.png"
    Image.new("RGBA", (8, 8)).save(path)
    own = datetime(2003, 2, 1, 10, 0)  # noqa: DTZ001
    info = MediaInfo(media=as_media(path, MediaKind.IMAGE), container="PNG", taken=own)

    output = convert(info).output
    assert read_exif_date(output) == own
    with Image.open(output) as image:
        assert 0x9011 not in image.getexif().get_ifd(0x8769)


def test_refuses_an_unreadable_image(tmp_path):
    path = tmp_path / "broken.bmp"
    path.write_bytes(b"not an image")
    info = MediaInfo(media=as_media(path, MediaKind.IMAGE), container="BMP")
    with pytest.raises(ConvertError, match="not a readable image"):
        convert(info)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["broken.bmp"]


# --- the real thing ----------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg and ffprobe installed",
)
def test_against_the_real_ffmpeg(tmp_path):
    """Converts a genuine XviD/MP2 AVI, the most common case, end to end."""
    path = tmp_path / "old.avi"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi",
            "-i", "testsrc=size=65x49:duration=0.5",
            "-f", "lavfi", "-i", "sine=duration=0.5",
            "-c:v", "mpeg4", "-vtag", "XVID", "-c:a", "mp2", str(path),
        ],
        check=True,
    )  # fmt: skip
    os.utime(path, (1_121_853_364, 1_121_853_364))

    result = convert(probe(as_media(path)))
    converted = probe(as_media(result.output))
    assert converted.container == "mov,mp4,m4a,3gp,3g2,mj2"
    assert (converted.video_codec, converted.audio_codec) == ("h264", "aac")
    assert (converted.width, converted.height) == (64, 48)
    assert read_container_date(result.output) == datetime(
        2005, 7, 20, 9, 56, 4, tzinfo=UTC
    )
