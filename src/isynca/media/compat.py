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
* **Colour.** A bt470bg matrix with no primaries or transfer -- what old
  Motion JPEG cameras write -- makes iCloud refuse the file outright, so
  H.264 or HEVC carrying it has to be re-encoded.
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
_PROFILES_OK: dict[str, frozenset[str]] = {
    "h264": frozenset({"Baseline", "Constrained Baseline", "Main", "High"}),
    "hevc": frozenset({"Main", "Main 10"}),
}
"""The profiles every Apple device decodes; ffprobe names them this way.

The rest -- 10-bit, 4:2:2 and 4:4:4 H.264, HEVC range extensions -- come from
editing software and screen recorders, and play on some machines only.
"""
_H264_UNSURE_PROFILES = frozenset({"High 10"})
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

    findings = [judge_video_stream(info), judge_audio_stream(info.audio_codec)]
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


def judge_video_stream(info: MediaInfo) -> tuple[Verdict, str]:
    """Return the verdict on a file's video stream, and why."""
    codec = info.video_codec
    if codec in _PROFILES_OK and has_rejected_colours(info):
        return (
            Verdict.CONVERT,
            "its colour description names a bt470bg matrix but no primaries or "
            "transfer, which iCloud refuses to transcode",
        )
    if codec is not None and (codec in _PROFILES_OK or codec == "mpeg4"):
        return _judge_profile(codec, info.video_profile)
    if codec in _VIDEO_OK:
        return Verdict.OK, ""
    if codec in _VIDEO_UNSURE:
        return Verdict.UNSURE, _VIDEO_UNSURE[codec]
    return Verdict.CONVERT, f"{codec} video does not play on Apple devices"


def has_rejected_colours(info: MediaInfo) -> bool:
    """Return whether the video's colour description is one iCloud rejects.

    Old cameras recording Motion JPEG mark the matrix as bt470bg and leave
    primaries and transfer unset, and a re-encode inherits that. iCloud
    answers such a file with 415 "unsupported for transcoding"; the same
    video with a complete description, or none at all, goes through. Other
    half-filled descriptions are seen among accepted uploads, so only this
    one is singled out.
    """
    return (
        info.color_matrix == "bt470bg"
        and info.color_primaries is None
        and info.color_transfer is None
    )


def _judge_profile(codec: str, profile: str | None) -> tuple[Verdict, str]:
    """Judge the codecs whose playability depends on the profile."""
    if codec == "mpeg4":
        if profile == _MPEG4_SIMPLE:
            return (
                Verdict.UNSURE,
                "MPEG-4 Part 2 video plays on Apple devices only in Simple "
                "Profile, and not on all of them",
            )
        return (
            Verdict.CONVERT,
            f"MPEG-4 Part 2 video in {profile or 'unknown profile'} (XviD/DivX "
            f"style) does not play on Apple devices",
        )
    # An unreported profile gets the benefit of the doubt: H.264 and HEVC
    # straight from a camera or phone are virtually always a safe one.
    if profile is None or profile in _PROFILES_OK[codec]:
        return Verdict.OK, ""
    name = "H.264" if codec == "h264" else "HEVC"
    if codec == "hevc" or profile in _H264_UNSURE_PROFILES:
        return (
            Verdict.UNSURE,
            f"{name} video in {profile} plays on some Apple devices only",
        )
    return Verdict.CONVERT, f"{name} video in {profile} does not play on Apple devices"


def judge_audio_stream(codec: str | None) -> tuple[Verdict, str]:
    """Return the verdict on an audio stream's codec, and why; silence is fine."""
    if codec is None or codec in _AUDIO_OK or codec.startswith("pcm_s"):
        return Verdict.OK, ""
    if codec in _AUDIO_UNSURE:
        return Verdict.UNSURE, _AUDIO_UNSURE[codec]
    return Verdict.CONVERT, f"{codec} audio does not play on Apple devices"
