import struct
from datetime import UTC, datetime
from io import BytesIO

from isynca.media.capture import (
    APPLE_CREATIONDATE_KEY,
    _iter_boxes,
    _read_keys,
    read_capture_date,
    read_container_date,
    read_exif_date,
)
from isynca.media.types import MediaFile, MediaKind
from tests.media.conftest import apple_meta, box, mvhd

# 3_000_000_000 seconds after the 1904 QuickTime epoch.
MVHD_DATE = datetime(1999, 1, 24, 5, 20, tzinfo=UTC)


def taken(value: datetime | None) -> datetime:
    """Assert a date was found and return it, so attributes can be read."""
    assert value is not None, "expected a capture date"
    return value


def as_media(path, kind):
    stat = path.stat()
    return MediaFile(
        path=path,
        kind=kind,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        source_root=path.parent,
    )


# --- images ------------------------------------------------------------------


def test_reads_datetime_original(make_image):
    path = make_image(original="2023:07:14 12:34:56")
    assert read_exif_date(path) == datetime(2023, 7, 14, 12, 34, 56)


def test_original_wins_over_the_fallbacks(make_image):
    path = make_image(
        original="2023:07:14 12:34:56",
        digitized="2010:01:01 00:00:00",
        plain="2011:01:01 00:00:00",
    )
    assert taken(read_exif_date(path)).year == 2023


def test_falls_back_to_datetime_digitized(make_image):
    path = make_image(digitized="2010:02:03 04:05:06")
    assert read_exif_date(path) == datetime(2010, 2, 3, 4, 5, 6)


def test_falls_back_to_plain_datetime(make_image):
    """Editors routinely drop the original tag while keeping this one."""
    path = make_image(plain="2019:05:06 07:08:09")
    assert read_exif_date(path) == datetime(2019, 5, 6, 7, 8, 9)


def test_image_without_exif_has_no_date(make_image):
    assert read_exif_date(make_image()) is None


def test_heic_is_read(make_image):
    """IPhone photos are HEIC; misreading them would block real photos."""
    path = make_image(name="photo.heic", original="2022:11:12 13:14:15")
    assert read_exif_date(path) == datetime(2022, 11, 12, 13, 14, 15)


def test_malformed_timestamp_is_no_date(make_image):
    assert read_exif_date(make_image(original="not a timestamp")) is None


def test_zeroed_timestamp_is_no_date(make_image):
    """Some cameras write all zeros rather than omitting the tag."""
    assert read_exif_date(make_image(original="0000:00:00 00:00:00")) is None


def test_unreadable_image_has_no_date(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"this is not an image")
    assert read_exif_date(broken) is None


def test_missing_image_has_no_date(tmp_path):
    assert read_exif_date(tmp_path / "absent.jpg") is None


# --- video -------------------------------------------------------------------


def test_reads_mvhd_creation_time(make_video):
    assert read_container_date(make_video()) == MVHD_DATE


def test_reads_64_bit_mvhd(make_video):
    path = make_video(moov=box(b"moov", mvhd(3_000_000_000, version=1)))
    assert read_container_date(path) == MVHD_DATE


def test_zeroed_mvhd_is_no_date(make_video):
    """Muxers often zero this; that is an absent date, not a 1904 video."""
    assert read_container_date(make_video(moov=box(b"moov", mvhd(0)))) is None


def test_apple_creationdate_wins_over_mvhd(make_video):
    path = make_video(
        moov=box(b"moov", mvhd(3_000_000_000) + apple_meta("2024-03-04T05:06:07+0200"))
    )
    found = taken(read_container_date(path))
    offset = found.utcoffset()

    assert found.year == 2024
    assert offset is not None
    assert offset.total_seconds() == 7200, "the recorded UTC offset is kept"


def test_quicktime_meta_layout_is_handled(make_video):
    """A .mov writes `meta` as a plain box, not a FullBox."""
    path = make_video(
        name="clip.mov",
        moov=box(
            b"moov",
            mvhd(3_000_000_000)
            + apple_meta("2020-08-09T10:11:12-0500", full_box=False),
        ),
    )
    assert taken(read_container_date(path)).year == 2020


def test_unparseable_apple_date_falls_back_to_mvhd(make_video):
    path = make_video(moov=box(b"moov", mvhd(3_000_000_000) + apple_meta("not a date")))
    assert read_container_date(path) == MVHD_DATE


def test_meta_without_the_creationdate_key_falls_back(make_video):
    meta = box(b"meta", b"\0\0\0\0" + box(b"hdlr", b"\0" * 24))
    path = make_video(moov=box(b"moov", mvhd(3_000_000_000) + meta))
    assert read_container_date(path) == MVHD_DATE


def test_video_without_moov_has_no_date(make_video):
    assert read_container_date(make_video(moov=box(b"mdat", b"\0" * 16))) is None


def test_video_without_mvhd_has_no_date(make_video):
    assert read_container_date(make_video(moov=box(b"moov", b""))) is None


def test_truncated_file_has_no_date(tmp_path):
    path = tmp_path / "truncated.mp4"
    path.write_bytes(b"\x00\x00\x00")
    assert read_container_date(path) is None


def test_missing_video_has_no_date(tmp_path):
    assert read_container_date(tmp_path / "absent.mp4") is None


def test_oversized_box_is_rejected(tmp_path):
    """A size field larger than the file must not be trusted."""
    path = tmp_path / "lying.mp4"
    path.write_bytes(b"\xff\xff\xff\xffmoov" + b"\0" * 8)
    assert read_container_date(path) is None


# --- dispatch ----------------------------------------------------------------


def test_dispatches_images_to_exif(make_image):
    path = make_image(original="2023:07:14 12:34:56")
    assert taken(read_capture_date(as_media(path, MediaKind.IMAGE))).year == 2023


def test_dispatches_video_to_the_container(make_video):
    assert read_capture_date(as_media(make_video(), MediaKind.VIDEO)) == MVHD_DATE


# --- malformed input ---------------------------------------------------------
#
# A corrupt or hostile file must yield "no date", never an exception and never
# a huge read. These exercise the parser's guards directly, since a
# well-formed file can never reach them.


def boxes(handle, start, end):
    return list(_iter_boxes(handle, start, end))


def test_iter_boxes_stops_at_a_truncated_header():
    """A declared range longer than the actual data must not over-read."""
    handle = BytesIO(b"\x00\x00")
    assert boxes(handle, 0, 64) == []


def test_iter_boxes_reads_a_64_bit_size():
    payload = b"body"
    blob = (
        struct.pack(">I", 1) + b"free" + struct.pack(">Q", 16 + len(payload)) + payload
    )
    found = boxes(BytesIO(blob), 0, len(blob))
    assert [b[0] for b in found] == [b"free"]


def test_iter_boxes_stops_on_a_truncated_64_bit_size():
    blob = struct.pack(">I", 1) + b"free" + b"\x00\x00"
    assert boxes(BytesIO(blob), 0, len(blob)) == []


def test_iter_boxes_treats_zero_size_as_to_end():
    blob = struct.pack(">I", 0) + b"free" + b"payload"
    found = boxes(BytesIO(blob), 0, len(blob))
    assert [b[0] for b in found] == [b"free"]


def test_iter_boxes_rejects_a_size_smaller_than_its_header():
    blob = struct.pack(">I", 4) + b"free"
    assert boxes(BytesIO(blob), 0, len(blob)) == []


def test_read_keys_handles_a_truncated_count():
    blob = b"\x00\x00\x00\x00\x00"
    assert _read_keys(BytesIO(blob), 0, len(blob)) == []


def test_read_keys_stops_when_entries_run_past_the_box():
    """An entry count larger than the data must not be trusted."""
    blob = b"\x00\x00\x00\x00" + struct.pack(">I", 99)
    assert _read_keys(BytesIO(blob), 0, len(blob)) == []


def test_read_keys_stops_on_an_undersized_entry():
    blob = (
        b"\x00\x00\x00\x00"
        + struct.pack(">I", 1)
        + struct.pack(">I", 2)
        + b"mdta"
        + b"key"
    )
    assert _read_keys(BytesIO(blob), 0, len(blob)) == []


def make_meta(keys_payload: bytes, ilst_payload: bytes) -> bytes:
    body = b"\x00\x00\x00\x00" + box(b"keys", keys_payload) + box(b"ilst", ilst_payload)
    return box(b"meta", body)


def keys_payload(*names: bytes) -> bytes:
    entries = b"".join(struct.pack(">I", len(n) + 8) + b"mdta" + n for n in names)
    return b"\x00\x00\x00\x00" + struct.pack(">I", len(names)) + entries


def video_with_meta(make_video, meta: bytes, name: str = "clip.mp4"):
    return make_video(name=name, moov=box(b"moov", mvhd(3_000_000_000) + meta))


def test_other_keys_are_ignored(make_video):
    meta = make_meta(keys_payload(b"com.example.other"), b"")
    assert read_container_date(video_with_meta(make_video, meta)) == MVHD_DATE


def test_ilst_entry_for_another_key_is_skipped(make_video):
    """A non-matching index must be stepped over, not misread."""
    data = box(b"data", struct.pack(">II", 1, 0) + b"2024-03-04T05:06:07+0000")
    ilst = box(struct.pack(">I", 2), data)
    meta = make_meta(keys_payload(APPLE_CREATIONDATE_KEY), ilst)
    assert read_container_date(video_with_meta(make_video, meta)) == MVHD_DATE


def test_ilst_entry_without_a_data_box(make_video):
    ilst = box(struct.pack(">I", 1), box(b"nope", b"x"))
    meta = make_meta(keys_payload(APPLE_CREATIONDATE_KEY), ilst)
    assert read_container_date(video_with_meta(make_video, meta)) == MVHD_DATE


def test_empty_ilst_falls_back(make_video):
    meta = make_meta(keys_payload(APPLE_CREATIONDATE_KEY), b"")
    assert read_container_date(video_with_meta(make_video, meta)) == MVHD_DATE


def test_oversized_keys_box_is_refused(make_video, monkeypatch):
    """A huge declared metadata box must never be read into memory."""
    monkeypatch.setattr("isynca.media.capture._MAX_METADATA_BOX", 4)
    meta = make_meta(keys_payload(APPLE_CREATIONDATE_KEY), b"")
    assert read_container_date(video_with_meta(make_video, meta)) == MVHD_DATE


def test_oversized_data_box_is_refused(make_video, monkeypatch):
    """The value guard is separate from the keys guard, so size it past both.

    The cap sits above the keys box (48 bytes here) and below the padded data
    box, so the keys table parses and the value itself is what gets refused.
    """
    padded = b"2024-03-04T05:06:07+0000" + b" " * 200
    data = box(b"data", struct.pack(">II", 1, 0) + padded)
    ilst = box(struct.pack(">I", 1), data)
    meta = make_meta(keys_payload(APPLE_CREATIONDATE_KEY), ilst)
    path = video_with_meta(make_video, meta)
    monkeypatch.setattr("isynca.media.capture._MAX_METADATA_BOX", 100)
    assert read_container_date(path) == MVHD_DATE


def test_truncated_mvhd_body(make_video):
    path = make_video(moov=box(b"moov", box(b"mvhd", b"\x00\x00")))
    assert read_container_date(path) is None


def test_mvhd_without_a_creation_time(make_video):
    path = make_video(moov=box(b"moov", box(b"mvhd", b"\x00\x00\x00\x00\x00")))
    assert read_container_date(path) is None


def test_absurd_mvhd_creation_time(make_video):
    """A corrupt field can hold a value no datetime can represent."""
    path = make_video(moov=box(b"moov", mvhd(2**64 - 1, version=1)))
    assert read_container_date(path) is None
