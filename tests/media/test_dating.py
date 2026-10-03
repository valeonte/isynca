import os
import struct
from datetime import UTC, datetime, timedelta, timezone

import pytest
from PIL import Image

from isynca.errors import DateError
from isynca.media import dating
from isynca.media.capture import read_container_date, read_exif_date
from isynca.media.dating import (
    _splice_exif,
    dated_path,
    modification_time,
    write_date,
)
from isynca.media.types import MediaFile, MediaKind
from tests.media.conftest import apple_meta, box, mvhd, tkhd

ATHENS = timezone(timedelta(hours=3))
WHEN = datetime(2009, 7, 20, 15, 30, 5, tzinfo=ATHENS)
QUICKTIME_SECONDS = int((WHEN - datetime(1904, 1, 1, tzinfo=UTC)).total_seconds())


def as_media(path, kind=MediaKind.IMAGE):
    stat = path.stat()
    return MediaFile(
        path=path,
        kind=kind,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        source_root=path.parent,
    )


def refusal(media, when=WHEN) -> DateError:
    with pytest.raises(DateError) as info:
        write_date(media, when)
    assert info.value.retryable is False
    return info.value


def only_original(path):
    return sorted(p.name for p in path.parent.iterdir()) == [path.name]


def image_data(data: bytes) -> bytes:
    """Return everything from the start-of-scan marker on: the pixels."""
    return data[data.index(b"\xff\xda") :]


# --- naming and dates --------------------------------------------------------


def test_names_the_copy(tmp_path):
    assert dated_path(tmp_path / "SSL1.AVI") == tmp_path / "SSL1_dated.AVI"


def test_modification_time_is_local_and_aware(tmp_path):
    path = tmp_path / "a.jpg"
    path.write_bytes(b"x")
    os.utime(path, (1_248_093_005, 1_248_093_005))
    when = modification_time(path)
    assert when.tzinfo is not None
    assert when.timestamp() == 1_248_093_005


def test_refuses_a_date_without_a_timezone(make_image):
    with pytest.raises(ValueError, match="timezone"):
        write_date(as_media(make_image()), datetime(2009, 7, 20))  # noqa: DTZ001


# --- JPEG --------------------------------------------------------------------


def test_stamps_a_jpeg_with_the_wall_clock_time_and_offset(make_image):
    path = make_image()
    fix = write_date(as_media(path), WHEN)

    assert fix.output == dated_path(path)
    assert (fix.before, fix.after) == (None, WHEN)
    assert read_exif_date(fix.output) == datetime(2009, 7, 20, 15, 30, 5)  # noqa: DTZ001
    with Image.open(fix.output) as image:
        sub = image.getexif().get_ifd(0x8769)
    assert sub[0x9004] == "2009:07:20 15:30:05"
    assert sub[0x9011] == sub[0x9012] == "+03:00"


def test_the_jpeg_pixels_and_the_original_are_untouched(make_image):
    path = make_image()
    original = path.read_bytes()
    output = write_date(as_media(path), WHEN).output
    assert path.read_bytes() == original
    assert image_data(output.read_bytes()) == image_data(original)


def test_the_copy_keeps_the_original_modification_time(make_image):
    path = make_image()
    os.utime(path, ns=(1_000_000_000, 1_248_093_005_000_000_000))
    output = write_date(as_media(path), WHEN).output
    assert output.stat().st_mtime_ns == path.stat().st_mtime_ns


def test_replaces_an_existing_date_and_keeps_other_tags(tmp_path):
    path = tmp_path / "old.jpg"
    image = Image.new("RGB", (8, 8))
    exif = image.getexif()
    exif[0x010F] = "Nokia"
    exif.get_ifd(0x8769)[0x9003] = "2001:01:01 00:00:00"
    image.save(path, exif=exif)

    fix = write_date(as_media(path), WHEN)
    assert fix.before == datetime(2001, 1, 1)  # noqa: DTZ001
    data = fix.output.read_bytes()
    assert data.count(b"Exif\x00\x00") == 1
    with Image.open(fix.output) as dated:
        assert dated.getexif()[0x010F] == "Nokia"
        assert dated.getexif().get_ifd(0x8769)[0x9003] == "2009:07:20 15:30:05"


def test_keeps_a_jfif_segment_first(make_image):
    data = write_date(as_media(make_image()), WHEN).output.read_bytes()
    assert data[2:4] == b"\xff\xe0"
    jfif_end = 4 + int.from_bytes(data[4:6], "big")
    assert data[jfif_end : jfif_end + 2] == b"\xff\xe1"


def test_puts_exif_straight_after_the_start_without_jfif(make_image):
    path = make_image()
    data = path.read_bytes()
    jfif_end = 4 + int.from_bytes(data[4:6], "big")
    path.write_bytes(data[:2] + data[jfif_end:])

    output = write_date(as_media(path), WHEN).output.read_bytes()
    assert output[2:4] == b"\xff\xe1"
    assert output[6:12] == b"Exif\x00\x00"


def test_splice_appends_when_the_header_is_only_jfif():
    jfif = b"\xff\xe0\x00\x04ab"
    spliced = _splice_exif(b"\xff\xd8" + jfif + b"\xff\xdascan", b"NEW")
    assert spliced == b"\xff\xd8" + jfif + b"NEW" + b"\xff\xdascan"


def test_splice_refuses_what_is_not_a_jpeg():
    with pytest.raises(DateError, match="not a JPEG"):
        _splice_exif(b"\x89PNG", b"NEW")


def test_splice_refuses_a_truncated_header():
    with pytest.raises(DateError, match="truncated"):
        _splice_exif(b"\xff\xd8\xff\xe1\xff\xffshort", b"NEW")


def test_refuses_an_image_that_is_not_jpeg(make_image):
    path = make_image("scan.png")
    assert "only JPEG images" in str(refusal(as_media(path)))
    assert only_original(path)


def test_refuses_an_unreadable_image(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image")
    assert "not a readable image" in str(refusal(as_media(path)))


def test_refuses_exif_too_large_for_a_segment(make_image, monkeypatch):
    monkeypatch.setattr(Image.Exif, "tobytes", lambda self: b"x" * 70_000)
    assert "too large" in str(refusal(as_media(make_image())))


def test_a_missing_file_is_a_date_error(make_image):
    path = make_image()
    media = as_media(path)
    path.unlink()
    assert "No such file" in str(refusal(media))


# --- MP4 / MOV ---------------------------------------------------------------


def mdhd(version=0) -> bytes:
    head = bytes([version, 0, 0, 0])
    if version == 1:
        return box(b"mdhd", head + struct.pack(">QQIQ", 0, 0, 1000, 0) + b"\0" * 4)
    return box(b"mdhd", head + struct.pack(">IIII", 0, 0, 1000, 0) + b"\0" * 4)


def full_trak(header=None, media_header=None) -> bytes:
    hdlr = box(b"hdlr", b"\0" * 8 + b"vide" + b"\0" * 12)
    mdia = box(b"mdia", (mdhd() if media_header is None else media_header) + hdlr)
    return box(b"trak", (tkhd() if header is None else header) + mdia)


def undated_movie(*traks, version=0) -> bytes:
    return box(b"moov", mvhd(0, version=version) + b"".join(traks))


def test_stamps_every_movie_and_track_header(make_video):
    path = make_video(moov=undated_movie(full_trak(), full_trak(tkhd(version=1))))
    fix = write_date(as_media(path, MediaKind.VIDEO), WHEN)

    assert fix.before is None
    assert read_container_date(fix.output) == WHEN.astimezone(UTC)
    data = fix.output.read_bytes()
    short = QUICKTIME_SECONDS.to_bytes(4, "big") * 2
    wide = QUICKTIME_SECONDS.to_bytes(8, "big") * 2
    # mvhd, two mdhd and one version-0 tkhd take the short form.
    assert data.count(short) == 4
    assert data.count(wide) == 1


def test_only_the_time_fields_change(make_video):
    path = make_video(moov=undated_movie(full_trak()))
    original = path.read_bytes()
    dated = write_date(as_media(path, MediaKind.VIDEO), WHEN).output.read_bytes()
    assert len(dated) == len(original)
    assert path.read_bytes() == original


def test_stamps_a_version_1_movie_header(make_video):
    path = make_video(moov=undated_movie(version=1))
    output = write_date(as_media(path, MediaKind.VIDEO), WHEN).output
    assert read_container_date(output) == WHEN.astimezone(UTC)


def test_skips_tracks_missing_headers_and_other_boxes(make_video):
    bare = box(b"trak", b"")
    no_mdhd = box(b"trak", tkhd() + box(b"mdia", b""))
    user_data = box(b"udta", b"")
    path = make_video(moov=undated_movie(bare, no_mdhd, user_data))
    output = write_date(as_media(path, MediaKind.VIDEO), WHEN).output
    assert read_container_date(output) == WHEN.astimezone(UTC)


def test_refuses_a_video_that_is_not_mp4(tmp_path):
    path = tmp_path / "SSL12779.AVI"
    path.write_bytes(b"RIFF\0\0\0\0AVI LIST")
    error = refusal(as_media(path, MediaKind.VIDEO))
    assert "has to be converted first" in str(error)
    assert only_original(path)


def test_refuses_a_video_with_apples_own_date(make_video):
    path = make_video(moov=box(b"moov", mvhd(0) + apple_meta("2020-01-01T00:00:00Z")))
    assert "Apple's own capture date" in str(refusal(as_media(path, MediaKind.VIDEO)))


def test_refuses_a_date_after_2040_in_a_version_0_header(make_video):
    path = make_video(moov=undated_movie())
    error = refusal(as_media(path, MediaKind.VIDEO), datetime(2041, 1, 1, tzinfo=UTC))
    assert "from 1904 to 2040" in str(error)


def test_refuses_a_date_before_1904(make_video):
    path = make_video(moov=undated_movie(version=1))
    error = refusal(as_media(path, MediaKind.VIDEO), datetime(1900, 1, 1, tzinfo=UTC))
    assert str(error) == "this video can only hold dates from 1904"


# --- writing -----------------------------------------------------------------


def test_dry_run_writes_nothing(make_image):
    path = make_image()
    fix = write_date(as_media(path), WHEN, dry_run=True)
    assert fix.output == dated_path(path)
    assert only_original(path)


def test_never_overwrites_an_existing_copy(make_image):
    path = make_image()
    dated_path(path).write_bytes(b"keep me")
    assert "already exists" in str(refusal(as_media(path)))
    assert dated_path(path).read_bytes() == b"keep me"


def test_a_failed_write_leaves_no_copy_behind(make_image, monkeypatch):
    path = make_image()

    def fail(_source, _target):
        raise OSError("disk full")

    monkeypatch.setattr(dating.shutil, "copystat", fail)
    assert "disk full" in str(refusal(as_media(path)))
    assert only_original(path)
