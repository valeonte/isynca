from pathlib import Path

import pytest

from isynca.media.compat import ISO_BMFF, Verdict, assess, container_name
from isynca.media.probe import MediaInfo
from isynca.media.types import MediaFile, MediaKind


def info(name="clip.mp4", kind=MediaKind.VIDEO, **fields) -> MediaInfo:
    media = MediaFile(
        path=Path("/media") / name,
        kind=kind,
        size=1,
        mtime_ns=0,
        source_root=Path("/media"),
    )
    return MediaInfo(media=media, **fields)


def video(container=ISO_BMFF, codec="h264", audio="aac", **fields) -> MediaInfo:
    return info(container=container, video_codec=codec, audio_codec=audio, **fields)


def image(fmt, name="photo.jpg") -> MediaInfo:
    return info(name=name, kind=MediaKind.IMAGE, container=fmt)


def test_an_unreadable_file_carries_its_error():
    result = assess(info(error="Invalid data"))
    assert result.verdict is Verdict.UNREADABLE
    assert result.reasons == ("Invalid data",)


# --- images ------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "MPO", "HEIF", "PNG", "GIF", "TIFF", "WEBP"])
def test_common_image_formats_are_ok(fmt):
    assert assess(image(fmt)).verdict is Verdict.OK


def test_avif_is_unsure():
    result = assess(image("AVIF"))
    assert result.verdict is Verdict.UNSURE
    assert "recent Apple devices" in result.reasons[0]


def test_raw_is_unsure_whatever_pillow_makes_of_it():
    result = assess(image("TIFF", name="IMG_1.DNG"))
    assert result.verdict is Verdict.UNSURE
    assert "RAW" in result.reasons[0]


@pytest.mark.parametrize(("fmt", "named"), [("BMP", "BMP"), (None, "unknown")])
def test_other_images_need_converting(fmt, named):
    result = assess(image(fmt))
    assert result.verdict is Verdict.CONVERT
    assert result.reasons == (f"{named} images are not taken",)


# --- video -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("codec", "audio"),
    [
        ("h264", "aac"),
        ("hevc", None),
        ("prores", "pcm_s16le"),
        ("h264", "mp3"),
        ("hevc", "alac"),
    ],
)
def test_apple_friendly_video_is_ok(codec, audio):
    result = assess(video(codec=codec, audio=audio))
    assert result.verdict is Verdict.OK
    assert result.reasons == ()


def test_a_video_with_no_video_stream_is_unreadable():
    result = assess(video(codec=None))
    assert result.verdict is Verdict.UNREADABLE
    assert "no video stream" in result.reasons[0]


def test_a_still_image_named_as_a_video_is_unreadable():
    result = assess(video(container="jpeg_pipe", codec="mjpeg", audio=None))
    assert result.verdict is Verdict.UNREADABLE
    assert "still image, not a video" in result.reasons[0]


def test_the_example_xvid_avi_needs_converting_for_three_reasons():
    result = assess(
        video(
            container="avi",
            codec="mpeg4",
            video_profile="Advanced Simple Profile",
            audio="mp2",
        )
    )
    assert result.verdict is Verdict.CONVERT
    assert len(result.reasons) == 3
    assert result.reasons[0] == "AVI is not taken; iCloud wants MP4 or MOV"
    assert "XviD/DivX" in result.reasons[1]
    assert result.reasons[2] == "mp2 audio does not play on Apple devices"


def test_good_streams_in_a_bad_container_only_need_rewrapping():
    result = assess(video(container="matroska,webm"))
    assert result.verdict is Verdict.CONVERT
    assert result.reasons == (
        "Matroska/WebM is not taken; iCloud wants MP4 or MOV; its streams can "
        "move into an MP4 as they are, without re-encoding",
    )


def test_blackberry_3gp_needs_converting():
    """MPEG-4 Simple Profile with AMR, as BlackBerry 3GPs iCloud refused."""
    result = assess(
        video(codec="mpeg4", video_profile="Simple Profile", audio="amr_nb")
    )
    assert result.verdict is Verdict.CONVERT
    assert result.reasons == (
        "iCloud refuses MPEG-4 Part 2 video, as early phones recorded it",
        "iCloud refuses AMR phone audio",
    )


def test_mpeg4_of_unknown_profile_needs_converting():
    result = assess(video(codec="mpeg4"))
    assert result.verdict is Verdict.CONVERT
    assert "unknown profile" in result.reasons[0]


@pytest.mark.parametrize("codec", ["h263", "dvvideo", "av1"])
def test_patchily_supported_video_is_unsure(codec):
    assert assess(video(codec=codec)).verdict is Verdict.UNSURE


def test_8_bit_pcm_audio_is_unsure():
    assert assess(video(audio="pcm_u8")).verdict is Verdict.UNSURE


@pytest.mark.parametrize("audio", ["amr_nb", "amr_wb"])
def test_amr_audio_needs_converting(audio):
    result = assess(video(audio=audio))
    assert result.verdict is Verdict.CONVERT
    assert result.reasons == ("iCloud refuses AMR phone audio",)


def test_unknown_codecs_need_converting():
    result = assess(video(codec="wmv3", audio="wmav2"))
    assert result.verdict is Verdict.CONVERT
    assert result.reasons == (
        "wmv3 video does not play on Apple devices",
        "wmav2 audio does not play on Apple devices",
    )


def test_motion_jpeg_needs_converting():
    """An Olympus MJPEG MOV was refused by iCloud; none was ever accepted."""
    result = assess(video(codec="mjpeg", audio="pcm_u8"))
    assert result.verdict is Verdict.CONVERT
    assert result.reasons[0] == (
        "iCloud refuses Motion JPEG video, as early digital cameras recorded it"
    )
    assert "8-bit PCM" in result.reasons[1]


def test_the_worst_finding_decides_and_every_reason_is_kept():
    result = assess(video(codec="h263", audio="wmav2"))
    assert result.verdict is Verdict.CONVERT
    assert len(result.reasons) == 2


# --- naming ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "container", "expected"),
    [
        ("a.3gp", ISO_BMFF, "3GP"),
        ("a.MOV", ISO_BMFF, "MOV"),
        ("a.qt", ISO_BMFF, "MP4/MOV"),
        ("a.avi", "avi", "AVI"),
        ("a.mov", "png_pipe", "PNG image"),
        ("a.nut", "nut", "nut"),
        ("a.avi", None, "unknown"),
    ],
)
def test_container_names(name, container, expected):
    assert container_name(info(name=name, container=container)) == expected


# --- profiles ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("codec", "profile"),
    [
        ("h264", "Constrained Baseline"),
        ("h264", "Main"),
        ("h264", "High"),
        ("hevc", "Main"),
        ("hevc", "Main 10"),
    ],
)
def test_apple_profiles_are_ok(codec, profile):
    assert assess(video(codec=codec, video_profile=profile)).verdict is Verdict.OK


@pytest.mark.parametrize(
    ("codec", "profile", "reason"),
    [
        ("h264", "High 10", "H.264 video in High 10 plays on some Apple devices only"),
        ("hevc", "Rext", "HEVC video in Rext plays on some Apple devices only"),
    ],
)
def test_uncommon_profiles_are_unsure(codec, profile, reason):
    result = assess(video(codec=codec, video_profile=profile))
    assert result.verdict is Verdict.UNSURE
    assert result.reasons == (reason,)


@pytest.mark.parametrize("profile", ["High 4:4:4 Predictive", "High 4:2:2"])
def test_editing_profiles_of_h264_need_converting(profile):
    result = assess(video(video_profile=profile))
    assert result.verdict is Verdict.CONVERT
    assert result.reasons == (
        f"H.264 video in {profile} does not play on Apple devices",
    )


# --- colour ------------------------------------------------------------------


def test_the_motion_jpeg_colour_description_needs_converting():
    """A bt470bg matrix with nothing else is what iCloud refused with 415."""
    result = assess(video(video_profile="High", color_matrix="bt470bg"))
    assert result.verdict is Verdict.CONVERT
    assert "bt470bg matrix but no primaries or transfer" in result.reasons[0]


@pytest.mark.parametrize(
    "colours",
    [
        {},
        {"color_matrix": "smpte170m"},
        {"color_matrix": "bt470bg", "color_primaries": "bt470bg"},
        {"color_matrix": "bt470bg", "color_transfer": "smpte170m"},
        {
            "color_matrix": "bt709",
            "color_primaries": "bt709",
            "color_transfer": "bt709",
        },
    ],
)
def test_colour_descriptions_seen_among_accepted_uploads_are_ok(colours):
    assert assess(video(video_profile="High", **colours)).verdict is Verdict.OK
