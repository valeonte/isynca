from datetime import UTC, datetime

import pytest

from isynca.ledger.hashing import hash_file
from isynca.ledger.store import Ledger, UploadStatus
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
