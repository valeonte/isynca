"""Guessing whether iCloud Photos will take a file, and play it once it has.

Apple publishes no definitive list, and an upload that is accepted is not the
same as one that plays: CloudKit will store bytes it cannot make a thumbnail
or a stream from. So the question asked here is the stricter one -- will the
file play on an iPhone and on iCloud.com -- answered from what AVFoundation is
known to decode:

* **Containers.** MP4 and MOV (and 3GP, which is the same box format). AVI,
  MPEG program streams, ASF/WMV and Matroska are not taken at all.
* **Video.** H.264 and HEVC are safe, as is ProRes. MPEG-4 Part 2 plays only
  in its Simple Profile, which rules out the XviD/DivX flavour. Motion JPEG,
  H.263, DV and AV1 play on some devices and not others.
* **Audio.** AAC, ALAC, MP3, AC-3 and signed PCM are safe. AMR and 8-bit PCM
  from early phones and cameras are doubtful; anything else is not decoded.
* **Images.** JPEG, HEIF, PNG, GIF, TIFF and WebP. AVIF needs a recent device
  and RAW support depends on the camera model.

Every verdict carries its reasons, so an unsure file can be settled by
uploading that one and looking at it, rather than by trusting the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from isynca.media.probe import MediaInfo
from isynca.media.types import MediaKind


class Verdict(StrEnum):
    """How iCloud Photos is expected to treat a file, best first."""

    OK = "ok"
    UNSURE = "unsure"
    CONVERT = "convert"
    UNREADABLE = "unreadable"


_RANK = {verdict: rank for rank, verdict in enumerate(Verdict)}


@dataclass(frozen=True, slots=True)
class Assessment:
    """A verdict and the reasons behind it; an OK file has none."""

    verdict: Verdict
    reasons: tuple[str, ...] = ()


ISO_BMFF = "mov,mp4,m4a,3gp,3g2,mj2"
"""ffprobe's single format name for every MP4, MOV, M4V and 3GP."""

CONTAINER_NAMES: dict[str, str] = {
    ISO_BMFF: "MP4/MOV",
    "avi": "AVI",
    "mpeg": "MPEG-PS",
    "mpegts": "MPEG-TS",
    "asf": "ASF/WMV",
    "matroska,webm": "Matroska/WebM",
    "flv": "Flash video",
}
"""Readable names for ffprobe's format names."""

_ISO_BMFF_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".3gp", ".3g2"})
_IMAGE_PIPE = "_pipe"
"""ffprobe's format names for a lone still image end in this."""

_VIDEO_OK = frozenset({"h264", "hevc", "prores"})
_VIDEO_UNSURE: dict[str, str] = {
    "mjpeg": "Motion JPEG video plays on a Mac but not reliably elsewhere",
    "h263": "H.263 video from early phones plays on some Apple devices only",
    "dvvideo": "DV video plays on a Mac but not reliably elsewhere",
    "av1": "AV1 video plays only on recent Apple devices",
}
_MPEG4_SIMPLE = "Simple Profile"

_AUDIO_OK = frozenset({"aac", "alac", "mp3", "ac3", "eac3"})
_AUDIO_UNSURE: dict[str, str] = {
    "amr_nb": "AMR phone audio plays on some Apple devices only",
    "amr_wb": "AMR phone audio plays on some Apple devices only",
    "pcm_u8": "8-bit PCM audio from early cameras may play silent or not at all",
}

_IMAGE_OK = frozenset({"JPEG", "MPO", "HEIF", "PNG", "GIF", "TIFF", "WEBP"})
_IMAGE_UNSURE: dict[str, str] = {
    "AVIF": "AVIF images display only on recent Apple devices",
}
_RAW_SUFFIXES = frozenset({".dng"})


def assess(info: MediaInfo) -> Assessment:
    """Return how iCloud Photos is expected to treat the file ``info`` describes."""
    if info.error is not None:
        return Assessment(Verdict.UNREADABLE, (info.error,))
    if info.media.kind is MediaKind.IMAGE:
        return _assess_image(info)
    return _assess_video(info)


def container_name(info: MediaInfo) -> str:
    """Return a readable name for the file's container or image format."""
    if info.container is None:
        return "unknown"
    # ffprobe cannot tell the ISO box formats apart, but the name can.
    suffix = info.media.path.suffix.lower()
    if info.container == ISO_BMFF and suffix in _ISO_BMFF_SUFFIXES:
        return suffix[1:].upper()
    if info.container.endswith(_IMAGE_PIPE):
        return f"{info.container.removesuffix(_IMAGE_PIPE).upper()} image"
    return CONTAINER_NAMES.get(info.container, info.container)


def _assess_image(info: MediaInfo) -> Assessment:
    """Judge an image by the format Pillow found, not by its extension."""
    if info.media.path.suffix.lower() in _RAW_SUFFIXES:
        return Assessment(
            Verdict.UNSURE, ("RAW images are supported only for some cameras",)
        )
    fmt = info.container or "unknown"
    if fmt in _IMAGE_OK:
        return Assessment(Verdict.OK)
    if fmt in _IMAGE_UNSURE:
        return Assessment(Verdict.UNSURE, (_IMAGE_UNSURE[fmt],))
    return Assessment(Verdict.CONVERT, (f"{fmt} images are not taken",))


def _assess_video(info: MediaInfo) -> Assessment:
    """Judge a video's container and streams, keeping the worst verdict."""
    if info.video_codec is None:
        return Assessment(
            Verdict.UNREADABLE, ("no video stream found; the file may be damaged",)
        )
    if info.container is not None and info.container.endswith(_IMAGE_PIPE):
        # A video name on a lone still frame is usually a thumbnail that was
        # saved over, or instead of, the real video. Converting it would only
        # produce a one-frame "video"; the original has to be found instead.
        return Assessment(
            Verdict.UNREADABLE,
            (
                "this is a still image, not a video; perhaps a thumbnail "
                "saved under the video's name",
            ),
        )

    findings = [_judge_video_codec(info), _judge_audio_codec(info.audio_codec)]
    streams_fine = all(verdict is Verdict.OK for verdict, _ in findings)
    if info.container != ISO_BMFF:
        remedy = (
            "; its streams can move into an MP4 as they are, without re-encoding"
            if streams_fine
            else ""
        )
        findings.insert(
            0,
            (
                Verdict.CONVERT,
                f"{container_name(info)} is not taken; iCloud wants MP4 or MOV"
                + remedy,
            ),
        )

    verdict = max((v for v, _ in findings), key=_RANK.__getitem__)
    reasons = tuple(reason for v, reason in findings if v is not Verdict.OK)
    return Assessment(verdict, reasons)


def _judge_video_codec(info: MediaInfo) -> tuple[Verdict, str]:
    """Return the verdict on a video codec, and why."""
    codec = info.video_codec
    if codec in _VIDEO_OK:
        return Verdict.OK, ""
    if codec in _VIDEO_UNSURE:
        return Verdict.UNSURE, _VIDEO_UNSURE[codec]
    if codec == "mpeg4":
        profile = info.video_profile or "unknown profile"
        if profile == _MPEG4_SIMPLE:
            return (
                Verdict.UNSURE,
                "MPEG-4 Part 2 video plays on Apple devices only in Simple "
                "Profile, and not on all of them",
            )
        return (
            Verdict.CONVERT,
            f"MPEG-4 Part 2 video in {profile} (XviD/DivX style) does not play "
            f"on Apple devices",
        )
    return Verdict.CONVERT, f"{codec} video does not play on Apple devices"


def _judge_audio_codec(codec: str | None) -> tuple[Verdict, str]:
    """Return the verdict on an audio codec, and why; silence is fine."""
    if codec is None or codec in _AUDIO_OK or codec.startswith("pcm_s"):
        return Verdict.OK, ""
    if codec in _AUDIO_UNSURE:
        return Verdict.UNSURE, _AUDIO_UNSURE[codec]
    return Verdict.CONVERT, f"{codec} audio does not play on Apple devices"
