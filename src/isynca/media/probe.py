"""Finding out what a media file really is: container, codecs, size and date.

Video is read with ffprobe. No Python library covers every container an old
collection holds -- AVI, MPEG program streams, ASF, 3GP, Matroska -- and
ffprobe does, reporting codec, profile and rotation the same way for each.
Images are read with Pillow, which isynca already depends on.

The date taken comes from :mod:`isynca.media.capture` first, since that reads
the Apple-specific atoms ffprobe does not surface. For containers it cannot
parse, such as AVI, ffprobe's ``creation_time`` tag is the fallback.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from isynca.errors import ToolMissingError
from isynca.logging import get_logger
from isynca.media.capture import read_capture_date
from isynca.media.types import MediaFile, MediaKind

LOGGER = get_logger("probe")

FFPROBE = "ffprobe"
FFPROBE_TIMEOUT = 60
"""Seconds before giving up on one file; a sane file answers in well under one."""

_DATE_TAGS = ("creation_time", "date")
_INTERLACED = frozenset({"tt", "bb", "tb", "bt"})
"""ffprobe's field orders for interlaced video; "progressive" is the other."""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """What one media file turned out to hold.

    Every field but ``media`` is ``None`` when it could not be determined;
    ``error`` then says why the file could not be read at all.
    """

    media: MediaFile
    container: str | None = None
    """ffprobe's format name for video, Pillow's format for images."""

    video_codec: str | None = None
    video_profile: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    rotation: int = 0
    """Clockwise degrees the video is displayed turned by."""

    interlaced: bool = False
    color_matrix: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    """The video's colour description, as ffprobe names it; ``None`` if unset."""

    duration: float | None = None
    """Length of a video in seconds."""

    taken: datetime | None = None
    error: str | None = None


def probe(media: MediaFile) -> MediaInfo:
    """Return what ``media`` holds.

    Raises:
        ToolMissingError: ``media`` is a video and ffprobe is not installed.
    """
    if media.kind is MediaKind.IMAGE:
        return _probe_image(media)
    return _probe_video(media)


def _probe_image(media: MediaFile) -> MediaInfo:
    """Read an image's format and size with Pillow."""
    try:
        with Image.open(media.path) as image:
            container, (width, height) = image.format, image.size
    except (UnidentifiedImageError, OSError) as exc:
        return MediaInfo(media=media, error=str(exc))
    return MediaInfo(
        media=media,
        container=container,
        width=width,
        height=height,
        taken=read_capture_date(media),
    )


def _probe_video(media: MediaFile) -> MediaInfo:
    """Read a video's container and streams with ffprobe."""
    report = run_ffprobe(media.path)
    if isinstance(report, str):
        return MediaInfo(media=media, error=report)

    fmt = report.get("format", {})
    streams = report.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            # Cover art in an MP4 is a one-frame "video" stream; it says
            # nothing about whether the actual video will play.
            and not s.get("disposition", {}).get("attached_pic")
        ),
        {},
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    return MediaInfo(
        media=media,
        container=fmt.get("format_name"),
        video_codec=video.get("codec_name"),
        video_profile=video.get("profile"),
        audio_codec=audio.get("codec_name"),
        width=video.get("width"),
        height=video.get("height"),
        rotation=_rotation(video),
        interlaced=video.get("field_order") in _INTERLACED,
        color_matrix=_colour(video.get("color_space")),
        color_primaries=_colour(video.get("color_primaries")),
        color_transfer=_colour(video.get("color_transfer")),
        duration=_duration(fmt.get("duration")),
        taken=read_capture_date(media) or _tag_date(fmt.get("tags", {})),
    )


def run_ffprobe(path: Path) -> dict[str, Any] | str:
    """Return ffprobe's JSON report on ``path``, or why it could not be read.

    Raises:
        ToolMissingError: ffprobe is not installed.
    """
    command = [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolMissingError(
            "ffprobe is not installed; it comes with ffmpeg "
            "(e.g. 'sudo apt install ffmpeg')"
        ) from exc
    except subprocess.TimeoutExpired:
        return f"ffprobe gave no answer within {FFPROBE_TIMEOUT}s"

    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        if not lines:
            return f"ffprobe exited with {result.returncode}"
        # ffprobe leads with the path, which the caller already shows.
        return lines[-1].removeprefix(f"{path}: ")
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "ffprobe returned an unreadable report"
    return report if isinstance(report, dict) else "ffprobe returned no report"


def _rotation(stream: dict[str, Any]) -> int:
    """Return the clockwise display rotation of a video stream, in degrees.

    ffprobe reports the display matrix's angle anticlockwise, so an upright
    portrait iPhone video reads as -90 there.
    """
    for side_data in stream.get("side_data_list", []):
        if "rotation" in side_data:
            return round(-float(side_data["rotation"])) % 360
    return 0


def _colour(value: object) -> str | None:
    """Return one colour-description field, or ``None`` if ffprobe has none."""
    return None if value in (None, "unknown", "reserved") else str(value)


def _duration(value: object) -> float | None:
    """Return ffprobe's duration string as seconds, or ``None``."""
    try:
        seconds = float(str(value))
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _tag_date(tags: dict[str, str]) -> datetime | None:
    """Return the first date ffprobe found among a container's tags."""
    for key in _DATE_TAGS:
        value = tags.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            LOGGER.debug("Unparseable %s tag %r", key, value)
    return None
