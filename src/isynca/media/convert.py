"""Converting media iCloud Photos will not take into something it will.

Video becomes MP4 holding H.264 video and AAC audio, the pair every Apple
device decodes. A stream that is already H.264 or HEVC, AAC or ALAC is copied
across untouched, so a file that only needs a new container -- an MKV or AVI
of H.264 -- is rewrapped in seconds with no loss. Everything else is
re-encoded: x264 at CRF 18 with the slow preset, which is visually lossless
for the camera and phone footage this is meant for, and AAC at 128 kb/s.
Interlaced video is deinterlaced first, odd frame sizes are rounded down to
even ones, which 4:2:0 H.264 requires, and an incomplete colour description
is completed, without which iCloud may refuse the result.

Images become JPEG at quality 95, keeping their EXIF and colour profile.

Every converted file carries a date taken: the original's own if it has one,
or else its modification time. Video gets it as ``creation_time``, which
ffmpeg writes into the movie and track headers; images get it in EXIF.

The original is never modified. The converted file goes beside it as
``<stem>_converted.mp4`` or ``.jpg``, appears under that name only once it is
complete, and keeps the original's modification time.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from isynca.errors import ConvertError, ToolMissingError
from isynca.media.compat import Verdict, judge_video_stream
from isynca.media.dating import modification_time, stamp_exif
from isynca.media.probe import MediaInfo
from isynca.media.types import MediaFile, MediaKind

FFMPEG = "ffmpeg"
CONVERTED_SUFFIX = "_converted"
JPEG_QUALITY = 95

_VIDEO_COPY = frozenset({"h264", "hevc"})
_AUDIO_COPY = frozenset({"aac", "alac"})
"""Codecs whose streams may be copied as they are.

Narrower than what :mod:`isynca.media.compat` calls fine: an MP4 cannot hold
PCM or ProRes the way Apple expects, so those are re-encoded too. A video
stream is copied only if compat also passes its profile.
"""

_X264 = ("-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p")
_AAC = ("-c:a", "aac", "-b:a", "128k")
_EVEN_SIZE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
_HD_HEIGHT = 720

Progress = Callable[[float], None]
"""Called with the fraction of a video converted so far, from 0 to 1."""


@dataclass(frozen=True, slots=True)
class Conversion:
    """One converted file: where it came from, where it went, and its date."""

    source: Path
    output: Path
    taken: datetime
    dated_from_mtime: bool
    """Whether ``taken`` is the original's modification time, for want of a date."""

    copied: tuple[str, ...] = ()
    """The streams copied across untouched rather than re-encoded."""


def converted_path(media: MediaFile) -> Path:
    """Return where the converted copy of ``media`` goes."""
    suffix = ".jpg" if media.kind is MediaKind.IMAGE else ".mp4"
    return media.path.with_name(f"{media.path.stem}{CONVERTED_SUFFIX}{suffix}")


def convert(
    info: MediaInfo,
    *,
    date: datetime | None = None,
    dry_run: bool = False,
    on_progress: Progress | None = None,
) -> Conversion:
    """Write a copy of the file ``info`` describes that iCloud Photos takes.

    Args:
        info: What :func:`isynca.media.probe.probe` found in the file.
        date: The date taken to give the copy, in place of the original's own
            date or its modification time.
        dry_run: Check and report the conversion without running it.
        on_progress: Told how far a video conversion has got, when known.

    Raises:
        ConvertError: The file is unreadable, the output's name is taken, or
            the conversion failed.
        ToolMissingError: ffmpeg is not installed.
    """
    media = info.media
    if info.error is not None:
        raise ConvertError(info.error, retryable=False)
    output = converted_path(media)
    if output.exists():
        raise ConvertError(f"{output.name} already exists", retryable=False)

    try:
        taken = date or info.taken or modification_time(media.path)
        if not dry_run:
            partial = output.with_name(f".{output.name}.partial")
            try:
                if media.kind is MediaKind.IMAGE:
                    _convert_image(media.path, partial, taken)
                else:
                    _run_ffmpeg(
                        _ffmpeg_command(info, partial, taken), info, on_progress
                    )
                shutil.copystat(media.path, partial)
                partial.replace(output)
            finally:
                partial.unlink(missing_ok=True)
    except OSError as exc:
        raise ConvertError(str(exc), retryable=False) from exc

    copied = tuple(
        kind
        for kind, keep in (
            ("video", _copies_video(info)),
            ("audio", info.audio_codec in _AUDIO_COPY),
        )
        if media.kind is MediaKind.VIDEO and keep
    )
    return Conversion(
        source=media.path,
        output=output,
        taken=taken,
        dated_from_mtime=date is None and info.taken is None,
        copied=copied,
    )


def _copies_video(info: MediaInfo) -> bool:
    """Return whether the video stream can go into the MP4 untouched."""
    return info.video_codec in _VIDEO_COPY and judge_video_stream(info)[0] is Verdict.OK


def _colour_options(info: MediaInfo) -> list[str]:
    """Return options giving a re-encode a complete colour description.

    An encoder copies whatever description the source had, and a partial one
    can get the file refused by iCloud (see
    :func:`isynca.media.compat.has_rejected_colours`). A complete description
    is kept as it is. Anything less is filled in with the standard for the
    frame size -- BT.601 below 720 lines, BT.709 from there up -- which is
    what players assume of untagged video anyway, so no colour changes. A
    bt470bg matrix becomes smpte170m: the same BT.601 coefficients, under
    the name that matches the primaries and transfer written beside it.
    """
    if info.color_matrix and info.color_primaries and info.color_transfer:
        return []
    hd = (info.height or 0) >= _HD_HEIGHT
    standard = (
        "bt709"
        if info.color_matrix == "bt709" or (info.color_matrix is None and hd)
        else "smpte170m"
    )
    matrix = (
        info.color_matrix if info.color_matrix not in (None, "bt470bg") else standard
    )
    return [
        "-colorspace",
        matrix,
        "-color_primaries",
        info.color_primaries or standard,
        "-color_trc",
        info.color_transfer or standard,
    ]


def _convert_image(source: Path, target: Path, taken: datetime) -> None:
    """Re-encode an image as JPEG, keeping its EXIF and stamping the date."""
    try:
        with Image.open(source) as image:
            exif = image.getexif()
            icc_profile = image.info.get("icc_profile")
            pixels = image.convert("RGB")
    except UnidentifiedImageError as exc:
        raise ConvertError("is not a readable image", retryable=False) from exc
    stamp_exif(exif, taken)
    pixels.save(
        target, "JPEG", quality=JPEG_QUALITY, exif=exif, icc_profile=icc_profile
    )


def _ffmpeg_command(info: MediaInfo, target: Path, taken: datetime) -> list[str]:
    """Return the ffmpeg command converting ``info``'s video into ``target``."""
    command = [
        FFMPEG,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(info.media.path),
        # The first video stream, and the first audio stream if there is one;
        # subtitles and data tracks have nowhere to go in an iCloud asset.
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]
    if _copies_video(info):
        command += ["-c:v", "copy"]
        if info.video_codec == "hevc":
            # Apple plays HEVC only under the hvc1 tag, not ffmpeg's hev1.
            command += ["-tag:v", "hvc1"]
    else:
        filters = ["yadif"] if info.interlaced else []
        command += [*_X264, "-vf", ",".join([*filters, _EVEN_SIZE])]
        command += _colour_options(info)
    command += ["-c:a", "copy"] if info.audio_codec in _AUDIO_COPY else list(_AAC)
    command += [
        "-map_metadata",
        "0",
        # A naive date is wall-clock time, which astimezone reads as local.
        "-metadata",
        f"creation_time={taken.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        "-f",
        "mp4",
        str(target),
    ]
    return command


def _run_ffmpeg(
    command: list[str], info: MediaInfo, on_progress: Progress | None
) -> None:
    """Run ffmpeg, reporting progress from its ``-progress`` output.

    An interrupt kills ffmpeg rather than leaving it writing in the
    background; the caller removes whatever it had written.
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ToolMissingError(
            "ffmpeg is not installed (e.g. 'sudo apt install ffmpeg')"
        ) from exc

    with process:
        try:
            for line in process.stdout or ():
                key, _, value = line.strip().partition("=")
                if (
                    on_progress is not None
                    and key == "out_time_us"
                    and value.isdigit()
                    and info.duration
                ):
                    on_progress(min(int(value) / 1e6 / info.duration, 1.0))
            errors = process.stderr.read() if process.stderr else ""
            code = process.wait()
        except BaseException:
            process.kill()
            raise

    if code != 0:
        lines = errors.strip().splitlines()
        raise ConvertError(
            lines[-1] if lines else f"ffmpeg exited with {code}", retryable=False
        )
