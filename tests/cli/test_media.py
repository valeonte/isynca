import os
import time
from datetime import UTC, datetime

import pytest
from PIL import Image

from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger, UploadStatus
from isynca.media.dating import dated_path, modification_time
from isynca.media.rotate import read_rotation
from tests.media.conftest import box, mvhd, trak


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "sideways.mp4"
    path.write_bytes(box(b"ftyp", b"isom") + box(b"moov", mvhd(3_000_000_000) + trak()))
    return path


def seed_upload(data_dir, path, content_hash, cache=False):
    with Ledger(data_dir / "ledger.db") as ledger:
        if cache:
            stat = path.stat()
            ledger.remember_file(path, stat.st_size, stat.st_mtime_ns, content_hash)
        ledger.record_upload(
            content_hash=content_hash,
            size=path.stat().st_size,
            path=path,
            status=UploadStatus.CONFIRMED,
            uploaded_at=datetime(2025, 6, 7, tzinfo=UTC),
        )


def test_rotates_a_video(invoke, video):
    result = invoke("media", "rotate", "--clockwise", "90", str(video))
    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output
    assert "sideways_rot90.mp4 (0° → 90°)" in result.output
    assert read_rotation(video) == 0
    assert read_rotation(video.with_name("sideways_rot90.mp4")) == 90


def test_dry_run_reports_without_writing(invoke, video):
    result = invoke("media", "rotate", "--clockwise", "180", "--dry-run", str(video))
    assert result.exit_code == 0, result.output
    assert "Would write" in result.output
    assert "sideways_rot180.mp4 (0° → 180°)" in result.output
    assert not video.with_name("sideways_rot180.mp4").exists()


def test_rejects_a_turn_that_is_not_a_quarter(invoke, video):
    result = invoke("media", "rotate", "--clockwise", "45", str(video))
    assert result.exit_code == 2
    assert "90, 180 or 270" in result.output


def test_a_bad_file_fails_the_run_but_not_the_others(invoke, video, tmp_path):
    avi = tmp_path / "old.avi"
    avi.write_bytes(b"RIFF")
    result = invoke("media", "rotate", "--clockwise", "90", str(avi), str(video))
    assert result.exit_code == 1
    assert "Cannot rotate" in result.output
    assert "not an MP4/QuickTime file" in result.output
    assert video.with_name("sideways_rot90.mp4").exists()


def test_warns_when_the_old_version_is_already_in_icloud(invoke, video, data_dir):
    seed_upload(data_dir, video, hash_file(video))
    result = invoke("media", "rotate", "--clockwise", "90", str(video))
    assert result.exit_code == 0, result.output
    assert "uploaded to iCloud Photos on 2025-06-07" in result.output
    assert "delete the old one" in result.output


def test_the_stat_cache_answers_without_hashing(invoke, video, data_dir, monkeypatch):
    seed_upload(data_dir, video, "cached-hash", cache=True)

    def unexpected(_path):
        raise AssertionError("hashed a file the stat cache already knew")

    monkeypatch.setattr("isynca.cli.media.hash_file", unexpected)
    result = invoke("media", "rotate", "--clockwise", "90", str(video))
    assert "uploaded to iCloud Photos" in result.output


def test_an_unreadable_file_while_hashing_is_reported(invoke, video, monkeypatch):
    def fail(_path):
        raise PermissionError("denied")

    monkeypatch.setattr("isynca.cli.media.hash_file", fail)
    result = invoke("media", "rotate", "--clockwise", "90", str(video))
    assert result.exit_code == 1
    assert "denied" in result.output
    assert not video.with_name("sideways_rot90.mp4").exists()


# --- check -------------------------------------------------------------------

XVID_AVI = {
    "format": {"format_name": "avi"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "mpeg4",
            "profile": "Advanced Simple Profile",
            "width": 640,
            "height": 480,
        },
        {"codec_type": "audio", "codec_name": "mp2"},
    ],
}


@pytest.fixture
def ffprobe_reports(monkeypatch):
    """Answer ffprobe from a table keyed by file name."""

    def install(reports):
        monkeypatch.setattr(
            "isynca.media.probe.run_ffprobe", lambda path: reports[path.name]
        )

    return install


def test_check_reports_the_example_avi(invoke, tmp_path, ffprobe_reports):
    avi = tmp_path / "SSL12779.AVI"
    avi.write_bytes(b"RIFF")
    ffprobe_reports({"SSL12779.AVI": XVID_AVI})

    result = invoke("media", "check", str(avi))
    assert result.exit_code == 0, result.output
    assert f"convert    {avi}" in result.output
    assert (
        "AVI · mpeg4 (Advanced Simple Profile) · mp2 · 640x480 · no date taken"
        in result.output
    )
    assert "- AVI is not taken; iCloud wants MP4 or MOV" in result.output
    assert "1 file(s): 0 ok, 0 unsure, 1 to convert, 0 unreadable." in result.output
    assert "1 file(s) carry no date taken" in result.output


def test_check_walks_a_folder_of_mixed_media(
    invoke, tmp_path, make_image, ffprobe_reports
):
    make_image("album/photo.jpg", original="2023:07:14 12:34:56")
    (tmp_path / "album" / "portrait.mov").write_bytes(b"x")
    (tmp_path / "album" / "broken.mov").write_bytes(b"x")
    ffprobe_reports(
        {
            "portrait.mov": {
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "tags": {"creation_time": "2024-05-06T07:08:09Z"},
                },
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "width": 1920,
                        "height": 1080,
                        "side_data_list": [{"rotation": -90}],
                    }
                ],
            },
            "broken.mov": "Invalid data found when processing input",
        }
    )

    result = invoke("media", "check", str(tmp_path / "album"))
    assert result.exit_code == 0, result.output
    assert "JPEG · 16x16 · taken 2023-07-14 12:34" in result.output
    assert (
        "MOV · hevc · no audio · 1920x1080 turned 90° · taken 2024-05-06 07:08"
        in result.output
    )
    assert "unreadable" in result.output
    assert "- Invalid data found when processing input" in result.output
    assert "3 file(s): 2 ok, 0 unsure, 0 to convert, 1 unreadable." in result.output
    assert "carry no date taken" not in result.output


def test_check_leaves_out_a_size_it_does_not_know(invoke, tmp_path, ffprobe_reports):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    ffprobe_reports(
        {
            "clip.mp4": {
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
                "streams": [{"codec_type": "video", "codec_name": "h264"}],
            }
        }
    )
    result = invoke("media", "check", str(clip))
    assert "MP4 · h264 · no audio · no date taken" in result.output


def test_check_with_nothing_to_check(invoke, tmp_path):
    result = invoke("media", "check", str(tmp_path))
    assert result.exit_code == 0
    assert "No media found." in result.output


def test_check_honours_the_kind_switches(invoke, tmp_path, make_image):
    make_image("photo.jpg")
    result = invoke("media", "check", "--no-images", str(tmp_path))
    assert "No media found." in result.output


# --- fix-date ----------------------------------------------------------------


def exif_taken(path):
    with Image.open(path) as image:
        return image.getexif().get_ifd(0x8769).get(0x9003)


def test_fix_date_stamps_undated_files_from_their_modification_time(
    invoke, tmp_path, make_image
):
    undated = make_image("album/scan.jpg")
    os.utime(undated, (1_248_093_005, 1_248_093_005))
    make_image("album/phone.jpg", original="2023:07:14 12:34:56")
    (tmp_path / "album" / "old.avi").write_bytes(b"RIFF")

    result = invoke("media", "fix-date", str(tmp_path / "album"))
    assert result.exit_code == 1, result.output
    expected = modification_time(undated)
    assert (
        f"Wrote {dated_path(undated)} (taken {expected:%Y-%m-%d %H:%M %z}, "
        f"from its modification time)" in result.output
    )
    assert exif_taken(dated_path(undated)) == f"{expected:%Y:%m:%d %H:%M:%S}"
    assert "Skipped" in result.output
    assert "already taken 2023-07-14 12:34" in result.output
    assert not (tmp_path / "album" / "phone_dated.jpg").exists()
    assert "Cannot date" in result.output
    assert "has to be converted first" in result.output


def test_fix_date_dry_run_writes_nothing(invoke, tmp_path, make_image):
    path = make_image("scan.jpg")
    result = invoke("media", "fix-date", "--dry-run", str(path))
    assert result.exit_code == 0, result.output
    assert "Would write" in result.output
    assert not dated_path(path).exists()


def test_fix_date_writes_a_given_date_over_an_existing_one(
    invoke, tmp_path, make_image
):
    path = make_image("wrong.jpg", original="2001:01:01 00:00:00")
    result = invoke("media", "fix-date", "--date", "2009-07-20T15:30+03:00", str(path))
    assert result.exit_code == 0, result.output
    assert (
        "(taken 2009-07-20 15:30 +0300, from the given date; was 2001-01-01 00:00)"
        in result.output
    )
    assert exif_taken(dated_path(path)) == "2009:07:20 15:30:00"


def test_fix_date_reads_an_unzoned_date_as_local_wall_time(
    invoke, tmp_path, make_image
):
    path = make_image("scan.jpg")
    result = invoke("media", "fix-date", "--date", "2009-07-20T15:30", str(path))
    assert result.exit_code == 0, result.output
    assert exif_taken(dated_path(path)) == "2009:07:20 15:30:00"


def test_fix_date_refuses_a_date_for_more_than_one_file(invoke, tmp_path, make_image):
    make_image("a.jpg")
    result = invoke("media", "fix-date", "--date", "2009-07-20", str(tmp_path))
    assert result.exit_code == 2
    assert "takes exactly one file" in result.output


def test_fix_date_refuses_an_unreadable_date(invoke, tmp_path, make_image):
    path = make_image("a.jpg")
    result = invoke("media", "fix-date", "--date", "last summer", str(path))
    assert result.exit_code == 2
    assert "expected a date like" in result.output


def test_fix_date_with_nothing_to_date(invoke, tmp_path):
    result = invoke("media", "fix-date", str(tmp_path))
    assert result.exit_code == 0
    assert "No media found." in result.output


def test_fix_date_warns_when_the_original_is_already_in_icloud(
    invoke, tmp_path, make_image, data_dir
):
    path = make_image("scan.jpg")
    seed_upload(data_dir, path, hash_file(path))
    result = invoke("media", "fix-date", str(path))
    assert result.exit_code == 0, result.output
    assert "The original was uploaded to iCloud Photos on 2025-06-07" in result.output


# --- fix ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """Show dates in UTC, so local-time output is the same on every machine."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


ISO_BMFF = "mov,mp4,m4a,3gp,3g2,mj2"
MJPEG_MOV = {
    "format": {"format_name": ISO_BMFF, "duration": "4.0"},
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "mjpeg",
            "width": 320,
            "height": 240,
            "color_space": "bt470bg",
        },
        {"codec_type": "audio", "codec_name": "pcm_u8"},
    ],
}
H263_3GP = {
    "format": {"format_name": ISO_BMFF, "duration": "4.0"},
    "streams": [
        {"codec_type": "video", "codec_name": "h263", "width": 176, "height": 144},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}


@pytest.fixture
def fix_folder(tmp_path, make_image, ffprobe_reports, ffmpeg):
    """A folder of one file for each thing fix can do, dated 2005-07-20."""
    folder = tmp_path / "media"
    make_image("media/undated.jpg")
    make_image("media/dated.jpg", original="2023:07:14 12:34:56")
    Image.new("RGB", (8, 8)).save(folder / "old.bmp")
    (folder / "SSL12779.AVI").write_bytes(b"RIFF")
    (folder / "broken.mov").write_bytes(b"x")
    for path in folder.iterdir():
        os.utime(path, (1_121_853_364, 1_121_853_364))  # 2005-07-20 09:56:04
    ffprobe_reports(
        {
            "SSL12779.AVI": XVID_AVI
            | {"format": {"format_name": "avi", "duration": "10"}},
            "broken.mov": "Invalid data found when processing input",
        }
    )
    ffmpeg(lines=["out_time_us=5000000\n", "progress=end\n"])
    return folder


def test_fix_converts_dates_and_skips_as_each_file_needs(invoke, fix_folder):
    result = invoke("media", "fix", str(fix_folder))
    assert result.exit_code == 1, result.output
    out = result.output

    assert (
        f"Converted {fix_folder / 'SSL12779.AVI'} → SSL12779_converted.mp4 "
        f"(taken 2005-07-20 09:56 +0000, from its modification time)" in out
    )
    assert (fix_folder / "SSL12779_converted.mp4").exists()
    assert f"Converted {fix_folder / 'old.bmp'} → old_converted.jpg" in out
    assert exif_taken(fix_folder / "old_converted.jpg") == "2005:07:20 09:56:04"
    assert f"Wrote {fix_folder / 'undated_dated.jpg'}" in out
    assert "already taken 2023-07-14 12:34" in out
    assert f"Cannot fix {fix_folder / 'broken.mov'}" in out
    assert "Invalid data found when processing input" in out


def test_fix_dry_run_writes_nothing(invoke, fix_folder):
    before = sorted(p.name for p in fix_folder.iterdir())
    result = invoke("media", "fix", "--dry-run", str(fix_folder))
    assert "Would convert" in result.output
    assert "Would write" in result.output
    assert sorted(p.name for p in fix_folder.iterdir()) == before


def test_fix_reruns_skip_what_is_done(invoke, fix_folder):
    invoke("media", "fix", str(fix_folder))
    result = invoke("media", "fix", str(fix_folder))
    out = result.output
    assert "Converted" not in out
    assert "Wrote" not in out
    assert "SSL12779_converted.mp4 already exists" in out
    assert "undated_dated.jpg already exists" in out
    assert (
        f"Skipped {fix_folder / 'old_converted.jpg'}: written by an earlier fix" in out
    )


def test_fix_only_dates_unsure_files_unless_asked(
    invoke, tmp_path, ffprobe_reports, ffmpeg
):
    clip = tmp_path / "phone.3gp"
    clip.write_bytes(box(b"ftyp", b"3gp4") + box(b"moov", mvhd(0) + trak()))
    ffprobe_reports({"phone.3gp": H263_3GP})
    ffmpeg()

    result = invoke("media", "fix", str(clip))
    assert result.exit_code == 0, result.output
    assert f"Wrote {tmp_path / 'phone_dated.3gp'}" in result.output
    assert "Not converted, though it may not play everywhere" in result.output
    assert "H.263 video" in result.output

    result = invoke("media", "fix", "--convert-unsure", str(clip))
    assert result.exit_code == 0, result.output
    assert "Converted" in result.output
    assert (tmp_path / "phone_converted.mp4").exists()


def test_fix_converts_motion_jpeg_without_being_asked(
    invoke, tmp_path, ffprobe_reports, ffmpeg
):
    clip = tmp_path / "P7020700.MOV"
    clip.write_bytes(box(b"ftyp", b"qt  ") + box(b"moov", mvhd(0) + trak()))
    ffprobe_reports({"P7020700.MOV": MJPEG_MOV})
    instances = ffmpeg()

    result = invoke("media", "fix", str(clip))
    assert result.exit_code == 0, result.output
    assert f"Converted {clip} → P7020700_converted.mp4" in result.output
    command = instances[0].command
    assert command[command.index("-colorspace") + 1] == "smpte170m"


def test_fix_reports_copied_streams(invoke, tmp_path, ffprobe_reports, ffmpeg):
    mkv = tmp_path / "clip.mkv"
    mkv.write_bytes(b"x")
    ffprobe_reports(
        {
            "clip.mkv": {
                "format": {
                    "format_name": "matroska,webm",
                    "tags": {"creation_time": "2019-04-05T06:07:08Z"},
                },
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "profile": "High"},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }
        }
    )
    ffmpeg()
    result = invoke("media", "fix", str(mkv))
    assert (
        "(taken 2019-04-05 06:07, from its own date; video and audio copied as is)"
        in result.output
    )


def test_fix_warns_when_the_original_is_already_in_icloud(invoke, fix_folder, data_dir):
    avi = fix_folder / "SSL12779.AVI"
    seed_upload(data_dir, avi, hash_file(avi))
    result = invoke("media", "fix", str(avi))
    assert result.exit_code == 0, result.output
    assert "Uploading the converted copy adds it as a new item" in result.output


def test_fix_with_nothing_to_fix(invoke, tmp_path):
    result = invoke("media", "fix", str(tmp_path))
    assert result.exit_code == 0
    assert "No media found." in result.output


def test_fix_gives_a_converted_file_the_given_date(invoke, fix_folder):
    avi = fix_folder / "SSL12779.AVI"
    result = invoke("media", "fix", "--date", "2009-07-20T15:30+03:00", str(avi))
    assert result.exit_code == 0, result.output
    assert (
        f"Converted {avi} → SSL12779_converted.mp4 (taken 2009-07-20 15:30 +0300, "
        f"from the given date)" in result.output
    )


def test_fix_gives_a_dated_file_the_given_date_over_its_own(
    invoke, tmp_path, make_image
):
    path = make_image("phone.jpg", original="2023:07:14 12:34:56")
    result = invoke("media", "fix", "--date", "2009-07-20T15:30+03:00", str(path))
    assert result.exit_code == 0, result.output
    assert "from the given date; was 2023-07-14 12:34" in result.output
    assert exif_taken(dated_path(path)) == "2009:07:20 15:30:00"


def test_fix_with_a_date_does_not_skip_a_file_an_earlier_fix_wrote(
    invoke, tmp_path, make_image
):
    path = make_image("scan_dated.jpg", original="2023:07:14 12:34:56")
    result = invoke("media", "fix", "--date", "2009-07-20T15:30", str(path))
    assert result.exit_code == 0, result.output
    assert dated_path(path).name == "scan_dated_dated.jpg"
    assert dated_path(path).exists()


def test_fix_with_a_date_reports_an_existing_output_as_an_error(invoke, fix_folder):
    avi = fix_folder / "SSL12779.AVI"
    (fix_folder / "SSL12779_converted.mp4").write_bytes(b"earlier")
    result = invoke("media", "fix", "--date", "2009-07-20T15:30", str(avi))
    assert result.exit_code == 1
    assert "Cannot fix" in result.output
    assert "SSL12779_converted.mp4 already exists" in result.output
    assert (fix_folder / "SSL12779_converted.mp4").read_bytes() == b"earlier"


@pytest.mark.parametrize("many", ["folder", "two files"])
def test_fix_refuses_a_date_for_more_than_one_file(invoke, fix_folder, many):
    targets = (
        [str(fix_folder)]
        if many == "folder"
        else [str(fix_folder / "undated.jpg"), str(fix_folder / "dated.jpg")]
    )
    before = sorted(p.name for p in fix_folder.iterdir())
    result = invoke("media", "fix", "--date", "2009-07-20T15:30", *targets)
    assert result.exit_code == 2
    assert "takes exactly one file" in result.output
    assert sorted(p.name for p in fix_folder.iterdir()) == before


# --- dates from names --------------------------------------------------------

CAPTURE = "%y-%m-%d_%H-%M.%S"


def test_fix_date_reads_the_date_from_the_name(invoke, tmp_path, make_image):
    path = make_image("scan.06-06-30_20-47.00.jpg")
    result = invoke(
        "media", "fix-date", "--date-from-name", CAPTURE,
        "--timezone", "Europe/Athens", str(path),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "(taken 2006-06-30 20:47 +0300, from its name)" in result.output
    output = dated_path(path)
    assert exif_taken(output) == "2006:06:30 20:47:00"
    with Image.open(output) as image:
        assert image.getexif().get_ifd(0x8769)[0x9011] == "+03:00"


def test_fix_date_reports_a_name_that_does_not_match(invoke, tmp_path, make_image):
    path = make_image("holiday.jpg")
    result = invoke("media", "fix-date", "--date-from-name", CAPTURE, str(path))
    assert result.exit_code == 1
    assert "its name does not match the pattern" in result.output
    assert not dated_path(path).exists()


@pytest.fixture
def capture_avi(tmp_path, ffprobe_reports):
    """An undated XviD AVI named the way the capture software names them."""
    path = tmp_path / "capture3.06-06-30_20-47.00.avi"
    path.write_bytes(b"RIFF")
    os.utime(path, (1_315_213_923, 1_315_213_923))  # the wrong 2011 mtime
    ffprobe_reports({path.name: XVID_AVI})
    return path


def test_fix_converts_with_the_date_from_the_name(invoke, capture_avi, ffmpeg):
    instances = ffmpeg()
    result = invoke(
        "media", "fix", "--date-from-name", CAPTURE,
        "--timezone", "Europe/Athens", str(capture_avi),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "(taken 2006-06-30 20:47 +0300, from its name)" in result.output
    assert "creation_time=2006-06-30T17:47:00Z" in instances[0].command


def test_fix_keeps_a_files_own_date_over_its_name(
    invoke, tmp_path, ffprobe_reports, ffmpeg
):
    mkv = tmp_path / "clip.06-06-30_20-47.00.mkv"
    mkv.write_bytes(b"x")
    ffprobe_reports(
        {
            mkv.name: {
                "format": {
                    "format_name": "matroska,webm",
                    "tags": {"creation_time": "2019-04-05T06:07:08Z"},
                },
                "streams": [{"codec_type": "video", "codec_name": "vp9"}],
            }
        }
    )
    ffmpeg()
    result = invoke("media", "fix", "--date-from-name", CAPTURE, str(mkv))
    assert result.exit_code == 0, result.output
    assert "(taken 2019-04-05 06:07, from its own date)" in result.output


def test_fix_reports_a_name_that_does_not_match(
    invoke, tmp_path, ffprobe_reports, ffmpeg
):
    avi = tmp_path / "holiday.avi"
    avi.write_bytes(b"RIFF")
    ffprobe_reports({avi.name: XVID_AVI})
    instances = ffmpeg()
    result = invoke("media", "fix", "--date-from-name", CAPTURE, str(avi))
    assert result.exit_code == 1
    assert f"Cannot fix {avi}" in result.output
    assert "its name does not match the pattern" in result.output
    assert instances == []


def test_a_timezone_applies_to_a_date_without_an_offset(invoke, capture_avi, ffmpeg):
    instances = ffmpeg()
    result = invoke(
        "media", "fix", "--date", "2006-06-30T20:47",
        "--timezone", "Europe/Athens", str(capture_avi),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "creation_time=2006-06-30T17:47:00Z" in instances[0].command


@pytest.mark.parametrize(
    ("args", "complaint"),
    [
        (["--date", "2006-06-30", "--date-from-name", CAPTURE], "cannot be combined"),
        (["--timezone", "Europe/Athens"], "only applies with --date or"),
        (["--date-from-name", CAPTURE, "--timezone", "Mars/Olympus"], "neither an"),
        (["--date-from-name", "%H-%M"], "needs at least a year"),
    ],
)
@pytest.mark.parametrize("command", ["fix", "fix-date"])
def test_refuses_unusable_date_options(invoke, capture_avi, command, args, complaint):
    result = invoke("media", command, *args, str(capture_avi))
    assert result.exit_code == 2
    assert complaint in result.output


# --- shifting dates ----------------------------------------------------------


@pytest.fixture
def london(monkeypatch):
    """Run in UK time, where July and December differ by an hour."""
    monkeypatch.setenv("TZ", "Europe/London")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_fix_shifts_the_modification_time_keeping_its_time_of_day(
    invoke, tmp_path, ffprobe_reports, ffmpeg, london
):
    """The Kriti case: July 2005 on the camera, December 2008 in truth."""
    avi = tmp_path / "SSL12724.AVI"
    avi.write_bytes(b"RIFF")
    os.utime(avi, (1_120_406_790, 1_120_406_790))  # 2005-07-03 17:06:30 BST
    ffprobe_reports({avi.name: XVID_AVI})
    instances = ffmpeg()

    result = invoke("media", "fix", "--shift-date-days", "1267", str(avi))
    assert result.exit_code == 0, result.output
    assert (
        "(taken 2008-12-21 17:06 +0000, from its modification time, "
        "moved +1267 days)" in result.output
    )
    assert "creation_time=2008-12-21T17:06:30Z" in instances[0].command


def test_fix_shifts_a_files_own_date(invoke, tmp_path, ffprobe_reports, ffmpeg):
    mkv = tmp_path / "clip.mkv"
    mkv.write_bytes(b"x")
    ffprobe_reports(
        {
            mkv.name: {
                "format": {
                    "format_name": "matroska,webm",
                    "tags": {"creation_time": "2019-04-05T06:07:08Z"},
                },
                "streams": [{"codec_type": "video", "codec_name": "vp9"}],
            }
        }
    )
    instances = ffmpeg()
    result = invoke("media", "fix", "--shift-date-days", "-10", str(mkv))
    assert result.exit_code == 0, result.output
    assert (
        "(taken 2019-03-26 06:07, from its own date, moved -10 days)" in result.output
    )
    assert "creation_time=2019-03-26T06:07:08Z" in instances[0].command


def test_fix_date_shifts_a_date_read_from_the_name(invoke, tmp_path, make_image):
    path = make_image("scan.06-06-30_20-47.00.jpg")
    result = invoke(
        "media", "fix-date", "--date-from-name", CAPTURE,
        "--timezone", "Europe/Athens", "--shift-date-days", "180", str(path),
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    # Late December is winter in Athens: same 20:47, an hour less offset.
    assert "(taken 2006-12-27 20:47 +0200, from its name, moved +180 days)" in (
        result.output
    )
    assert exif_taken(dated_path(path)) == "2006:12:27 20:47:00"


def test_fix_date_does_not_shift_a_file_that_keeps_its_date(
    invoke, tmp_path, make_image
):
    path = make_image("phone.jpg", original="2023:07:14 12:34:56")
    result = invoke("media", "fix-date", "--shift-date-days", "5", str(path))
    assert result.exit_code == 0, result.output
    assert "already taken 2023-07-14 12:34" in result.output
    assert not dated_path(path).exists()


@pytest.mark.parametrize("command", ["fix", "fix-date"])
def test_a_shift_cannot_be_combined_with_a_date(invoke, capture_avi, command):
    result = invoke(
        "media", command, "--date", "2008-12-21T17:06",
        "--shift-date-days", "1267", str(capture_avi),
    )  # fmt: skip
    assert result.exit_code == 2
    assert "cannot be combined with --date" in result.output
