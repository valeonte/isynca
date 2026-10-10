import os
import struct
from pathlib import Path

import pytest

from isynca.errors import RotationError
from isynca.media.rotate import read_rotation, rotate_video, rotated_path
from tests.media.conftest import box, mvhd, tkhd, trak

ONE = 1 << 16
CW90 = (0, ONE, -ONE, 0)
MIRRORED = (-ONE, 0, 0, ONE)


def movie(*traks: bytes) -> bytes:
    return box(b"moov", mvhd(3_000_000_000) + b"".join(traks))


def matrix_of(path, track: int = 0, version: int = 0) -> tuple[int, ...]:
    """Return the nine matrix cells of the ``track``-th tkhd in ``path``."""
    data = path.read_bytes()
    start = 0
    for _ in range(track + 1):
        start = data.index(b"tkhd", start) + 4
    offset = start + (52 if version == 1 else 40)
    return struct.unpack(">9i", data[offset : offset + 36])


# --- reading -----------------------------------------------------------------


def test_an_unrotated_video_reads_as_zero(make_video):
    assert read_rotation(make_video(moov=movie(trak()))) == 0


def test_reads_an_existing_quarter_turn(make_video):
    path = make_video(moov=movie(trak(header=tkhd(CW90))))
    assert read_rotation(path) == 90


def test_audio_tracks_are_ignored(make_video):
    path = make_video(moov=movie(trak(b"soun"), trak(header=tkhd(CW90))))
    assert read_rotation(path) == 90


# --- rotating ----------------------------------------------------------------


def test_names_the_copy_after_the_turn(tmp_path):
    assert rotated_path(tmp_path / "WP_1.MP4", 90) == tmp_path / "WP_1_rot90.MP4"


@pytest.mark.parametrize(
    ("clockwise", "cells", "translation"),
    [
        (90, (0, ONE, -ONE, 0), (720 << 16, 0)),
        (180, (-ONE, 0, 0, -ONE), (1280 << 16, 720 << 16)),
        (270, (0, -ONE, ONE, 0), (0, 1280 << 16)),
    ],
)
def test_writes_apples_matrix_for_each_turn(make_video, clockwise, cells, translation):
    path = make_video(moov=movie(trak()))
    change = rotate_video(path, clockwise)

    assert change.output == rotated_path(path, clockwise)
    assert (change.before, change.after) == (0, clockwise)
    a, b, u, c, d, v, tx, ty, w = matrix_of(change.output)
    assert (a, b, c, d) == cells
    assert (tx, ty) == translation
    assert (u, v, w) == (0, 0, 1 << 30)


def test_leaves_the_original_untouched(make_video):
    path = make_video(moov=movie(trak()))
    original = path.read_bytes()
    rotate_video(path, 90)
    assert path.read_bytes() == original
    assert read_rotation(path) == 0


def test_the_copy_differs_only_in_the_matrix(make_video):
    """Same size, same capture date: nothing but the 36 matrix bytes moves."""
    path = make_video(moov=movie(trak()))
    original = path.read_bytes()
    rotated = rotate_video(path, 90).output.read_bytes()

    assert len(rotated) == len(original)
    changed = [
        i for i, (x, y) in enumerate(zip(original, rotated, strict=True)) if x != y
    ]
    matrix_start = original.index(b"tkhd") + 4 + 40
    assert changed
    assert all(matrix_start <= i < matrix_start + 36 for i in changed)


def test_the_copy_keeps_the_original_modification_time(make_video):
    path = make_video(moov=movie(trak()))
    os.utime(path, ns=(1_000_000_000, 1_353_087_563_000_000_000))
    output = rotate_video(path, 90).output
    assert output.stat().st_mtime_ns == path.stat().st_mtime_ns


def test_turns_add_to_an_existing_rotation(make_video):
    path = make_video(moov=movie(trak(header=tkhd(CW90))))
    change = rotate_video(path, 270)
    assert (change.before, change.after) == (90, 0)
    assert read_rotation(change.output) == 0


def test_four_quarter_turns_come_back_to_the_original_bytes(make_video):
    path = make_video(moov=movie(trak(), trak(b"soun")))
    current = path
    for _ in range(4):
        current = rotate_video(current, 90).output
    assert current.name == "clip_rot90_rot90_rot90_rot90.mp4"
    assert current.read_bytes() == path.read_bytes()


def test_rotates_a_version_1_track_header(make_video):
    path = make_video(moov=movie(trak(header=tkhd(version=1))))
    output = rotate_video(path, 90).output
    assert matrix_of(output, version=1)[:5] == (0, ONE, 0, -ONE, 0)


def test_rotates_every_video_track(make_video):
    path = make_video(moov=movie(trak(), trak(header=tkhd(CW90))))
    output = rotate_video(path, 90).output
    assert matrix_of(output, 0)[:5] == (0, ONE, 0, -ONE, 0)
    assert matrix_of(output, 1)[:5] == (-ONE, 0, 0, 0, -ONE)


def test_dry_run_writes_nothing(make_video):
    path = make_video(moov=movie(trak()))
    change = rotate_video(path, 90, dry_run=True)
    assert (change.before, change.after) == (0, 90)
    assert sorted(p.name for p in path.parent.iterdir()) == ["clip.mp4"]


def test_refuses_anything_but_a_quarter_turn(make_video):
    with pytest.raises(ValueError, match="Rotation must be"):
        rotate_video(make_video(moov=movie(trak())), 45)


# --- refusals ----------------------------------------------------------------


def refusal(path) -> RotationError:
    with pytest.raises(RotationError) as info:
        rotate_video(path, 90)
    assert info.value.retryable is False
    return info.value


def test_a_missing_file_is_a_rotation_error(tmp_path):
    assert "No such file" in str(refusal(tmp_path / "gone.mp4"))


def test_refuses_a_file_that_is_not_mp4(tmp_path):
    path = tmp_path / "clip.avi"
    path.write_bytes(b"RIFF\0\0\0\0AVI LIST")
    assert "not an MP4/QuickTime file" in str(refusal(path))


def test_refuses_a_movie_with_no_video_track(make_video):
    headerless = box(b"trak", tkhd())
    no_handler = box(b"trak", tkhd() + box(b"mdia", b""))
    path = make_video(moov=movie(trak(b"soun"), headerless, no_handler))
    assert "no video track" in str(refusal(path))


def test_refuses_a_video_track_with_no_header(make_video):
    path = make_video(moov=movie(box(b"trak", trak()[8:].replace(tkhd(), b""))))
    assert "no header" in str(refusal(path))


def test_refuses_a_truncated_track_header(make_video):
    short = box(b"tkhd", tkhd()[8:40])
    path = make_video(moov=movie(trak(header=short)))
    assert "truncated" in str(refusal(path))


def test_refuses_a_mirrored_video_and_writes_nothing(make_video):
    path = make_video(moov=movie(trak(), trak(header=tkhd(MIRRORED))))
    assert "mirrored" in str(refusal(path))
    assert sorted(p.name for p in path.parent.iterdir()) == ["clip.mp4"]


def test_never_overwrites_an_existing_copy(make_video):
    path = make_video(moov=movie(trak()))
    rotated_path(path, 90).write_bytes(b"keep me")
    assert "already exists" in str(refusal(path))
    assert rotated_path(path, 90).read_bytes() == b"keep me"


def test_a_failed_write_leaves_no_copy_behind(make_video, monkeypatch):
    path = make_video(moov=movie(trak()))

    def half_copy(_source, target):
        Path(target).write_bytes(b"half")
        raise OSError("disk full")

    monkeypatch.setattr("isynca.media.rotate.shutil.copyfile", half_copy)
    assert "disk full" in str(refusal(path))
    assert sorted(p.name for p in path.parent.iterdir()) == ["clip.mp4"]
