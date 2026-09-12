import errno
from pathlib import Path

import pytest
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudException,
    PyiCloudServiceNotActivatedException,
)
from requests import RequestException

from isynca.errors import AlbumNotFoundError, UploadError
from isynca.icloud.photos import PhotosUploader
from isynca.ledger.store import UploadStatus
from tests.fakes.icloud import (
    FakeAlbum,
    FakeAsset,
    FakeRegistration,
    FakeSession,
    cloudkit_error,
)


def test_upload_to_root_library(session):
    uploader = PhotosUploader(session)
    outcome = uploader.upload(Path("/videos/a.mp4"))

    assert outcome.status is UploadStatus.CONFIRMED
    assert outcome.asset_id == "asset-1"
    assert outcome.master_id == "master-1"
    assert session.photos_service.uploaded == [("/videos/a.mp4", None)]


def test_upload_registers_into_the_primary_zone(session):
    """The bytes go to the account's own library, not a shared one."""
    PhotosUploader(session).upload(Path("/videos/a.mp4"))

    assert session.photos_service.direct_client.zones == ["PrimarySync"]


def test_existing_album_is_reused(session):
    album = FakeAlbum("Trip")
    session.photos_service.album_container.albums["Trip"] = album
    uploader = PhotosUploader(session, album="Trip")
    outcome = uploader.upload(Path("/videos/a.mp4"))

    assert session.photos_service.uploaded == [("/videos/a.mp4", None)]
    assert album.added == [outcome.asset_id]


def test_missing_album_is_created(session):
    uploader = PhotosUploader(session, album="New")
    outcome = uploader.upload(Path("/videos/a.mp4"))

    created = session.photos_service.album_container.albums["New"]
    assert created.added == [outcome.asset_id]


def test_album_membership_failure_is_reported(session):
    """The bytes are in iCloud; only the album relation failed."""
    session.photos_service.album_container.albums["Trip"] = FakeAlbum(
        "Trip", add_error=cloudkit_error("relation rejected")
    )
    with pytest.raises(UploadError, match="could not be added to 'Trip'"):
        PhotosUploader(session, album="Trip").upload(Path("/videos/a.mp4"))


def test_album_is_resolved_once_across_uploads(session):
    calls = []
    container = session.photos_service.album_container
    original_find = container.find

    def counting_find(name):
        calls.append(name)
        return original_find(name)

    container.find = counting_find
    uploader = PhotosUploader(session, album="Trip")
    uploader.upload(Path("/a.mp4"))
    uploader.upload(Path("/b.mp4"))

    assert calls == ["Trip"]


def test_no_album_resolution_when_none_configured(session):
    assert PhotosUploader(session).ensure_album() is None


def test_album_creation_returning_none_is_fatal(session):
    session.photos_service.create_returns_none = True
    with pytest.raises(AlbumNotFoundError, match="could not be created"):
        PhotosUploader(session, album="Nope").upload(Path("/a.mp4"))


def test_album_lookup_error_is_fatal(session):
    session.photos_service.album_container.find_error = (
        PyiCloudServiceNotActivatedException("photos off")
    )
    with pytest.raises(AlbumNotFoundError, match="Could not resolve album"):
        PhotosUploader(session, album="Trip").upload(Path("/a.mp4"))


def test_duplicate_registration_is_reported_not_raised(session):
    """Apple flags a duplicate outright, so nothing has to read the message."""
    session.photos_service.upload_results = [
        FakeRegistration(cplMaster="m1", cplAsset="a1", duplicate=True)
    ]
    outcome = PhotosUploader(session).upload(Path("/a.mp4"))

    assert outcome.status is UploadStatus.DUPLICATE
    assert (outcome.master_id, outcome.asset_id) == ("m1", "a1")


def test_registration_without_record_names_is_unverified(session):
    session.photos_service.upload_results = [FakeRegistration()]
    outcome = PhotosUploader(session).upload(Path("/a.mp4"))

    assert outcome.status is UploadStatus.UNVERIFIED
    assert outcome.asset_id is None


def test_cloudkit_error_becomes_upload_error(session):
    session.photos_service.upload_results = [cloudkit_error("putAsset rejected it")]
    with pytest.raises(UploadError, match=r"Upload of /a\.mp4 failed"):
        PhotosUploader(session).upload(Path("/a.mp4"))


def test_rejection_reports_what_the_status_means(session):
    """A bare status number says nothing; Apple's own block says plenty."""
    session.photos_service.upload_results = [
        cloudkit_error(
            "Photos putAsset rejected a.mp4 with status 415",
            payload={
                "response": {
                    "status": 415,
                    "isRetryable": False,
                    "errorMessage": "unsupported asset type",
                }
            },
        )
    ]
    with pytest.raises(UploadError) as raised:
        PhotosUploader(session).upload(Path("/a.mp4"))

    message = str(raised.value)
    assert "Unsupported Media Type" in message
    assert "container, format, or codec" in message
    assert "Apple said: unsupported asset type" in message
    assert "retrying will not help" in message
    assert raised.value.retryable is False


@pytest.mark.parametrize(
    ("response", "retryable"),
    [
        ({"status": 415}, False),
        ({"status": 413}, False),
        ({"status": 408}, True),
        ({"status": 429}, True),
        ({"status": 500}, True),
        ({"status": 503, "isRetryable": False}, False),
        ({"status": None}, True),
        ({}, True),
    ],
    ids=[
        "unsupported",
        "too-large",
        "timeout",
        "rate-limited",
        "server-fault",
        "apple-says-no",
        "no-status",
        "empty",
    ],
)
def test_retryability_follows_apple(session, response, retryable):
    """A verdict on the file is settled; a bad moment is not."""
    session.photos_service.upload_results = [
        cloudkit_error("rejected", payload={"response": response})
    ]
    with pytest.raises(UploadError) as raised:
        PhotosUploader(session).upload(Path("/a.mp4"))

    assert raised.value.retryable is retryable


def test_an_error_without_a_failure_block_stays_retryable(session):
    session.photos_service.upload_results = [cloudkit_error("connection reset")]
    with pytest.raises(UploadError) as raised:
        PhotosUploader(session).upload(Path("/a.mp4"))

    assert raised.value.retryable is True


def test_rejection_names_a_status_without_a_hint(session):
    session.photos_service.upload_results = [
        cloudkit_error("rejected", payload={"response": {"status": 400}})
    ]
    with pytest.raises(UploadError, match=r"\(Bad Request; retrying will not help\)"):
        PhotosUploader(session).upload(Path("/a.mp4"))


def test_rejection_with_an_unknown_status_still_reads(session):
    session.photos_service.upload_results = [
        cloudkit_error("rejected", payload={"response": {"status": 599}})
    ]
    with pytest.raises(UploadError, match="unrecognised status 599"):
        PhotosUploader(session).upload(Path("/a.mp4"))


@pytest.mark.parametrize(
    "payload",
    [None, "plain text body", {"response": None}, {"response": {}}],
    ids=["missing", "text", "no-block", "empty-block"],
)
def test_payloads_without_a_failure_block_leave_the_message_alone(session, payload):
    session.photos_service.upload_results = [cloudkit_error("rejected", payload)]
    with pytest.raises(UploadError) as raised:
        PhotosUploader(session).upload(Path("/a.mp4"))

    assert str(raised.value) == "Upload of /a.mp4 failed: rejected"


def test_album_failure_also_explains_the_status(session):
    session.photos_service.album_container.albums["Trip"] = FakeAlbum(
        "Trip",
        add_error=cloudkit_error(
            "relation rejected", payload={"response": {"status": 429}}
        ),
    )
    with pytest.raises(UploadError, match="rate limiting") as raised:
        PhotosUploader(session, album="Trip").upload(Path("/a.mp4"))

    assert raised.value.retryable is True


@pytest.fixture
def slow_session(session):
    """A session with no CloudKit client, forcing pyicloud's waiting upload."""
    session.photos_service.has_direct_client = False
    return session


def test_upload_falls_back_to_the_waiting_call(slow_session):
    outcome = PhotosUploader(slow_session).upload(Path("/videos/a.mp4"))

    assert outcome.status is UploadStatus.CONFIRMED
    assert slow_session.photos_service.uploaded == [("/videos/a.mp4", None)]


def test_fallback_passes_the_album_by_name(slow_session):
    """The waiting call resolves the album itself, so it is given the name."""
    PhotosUploader(slow_session, album="Trip").upload(Path("/videos/a.mp4"))

    assert slow_session.photos_service.uploaded == [("/videos/a.mp4", "Trip")]


def test_fallback_none_result_is_unverified(slow_session):
    slow_session.photos_service.upload_results = [None]
    outcome = PhotosUploader(slow_session).upload(Path("/a.mp4"))

    assert outcome.status is UploadStatus.UNVERIFIED
    assert outcome.asset_id is None


def test_duplicate_response_is_reported_not_raised(slow_session):
    """Without the duplicate flag, the error message is the only signal."""
    slow_session.photos_service.upload_results = [
        PyiCloudAPIResponseException("409 duplicate asset")
    ]
    outcome = PhotosUploader(slow_session).upload(Path("/a.mp4"))
    assert outcome.status is UploadStatus.DUPLICATE


def test_already_exists_message_is_a_duplicate(slow_session):
    slow_session.photos_service.upload_results = [
        PyiCloudAPIResponseException("Asset already exists in library")
    ]
    outcome = PhotosUploader(slow_session).upload(Path("/a.mp4"))
    assert outcome.status is UploadStatus.DUPLICATE


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (PyiCloudAPIResponseException("500 server exploded"), "failed"),
        (PyiCloudException("session died"), "failed"),
        (OSError("disk vanished"), "Could not read"),
    ],
)
def test_fallback_errors_become_upload_errors(slow_session, error, message):
    slow_session.photos_service.upload_results = [error]
    with pytest.raises(UploadError, match=message):
        PhotosUploader(slow_session).upload(Path("/a.mp4"))


def test_api_error_becomes_upload_error(session):
    session.photos_service.upload_results = [
        PyiCloudAPIResponseException("500 server exploded")
    ]
    with pytest.raises(UploadError, match=r"Upload of /a\.mp4 failed"):
        PhotosUploader(session).upload(Path("/a.mp4"))


def test_generic_pyicloud_error_becomes_upload_error(session):
    """A non-API pyicloud error still becomes a per-file UploadError.

    PyiCloudServiceNotActivatedException subclasses PyiCloudAPIResponseException,
    so a plain PyiCloudException is what actually exercises the second branch.
    """
    session.photos_service.upload_results = [PyiCloudException("session died")]
    with pytest.raises(UploadError, match="failed"):
        PhotosUploader(session).upload(Path("/a.mp4"))


def test_os_error_becomes_upload_error(session):
    session.photos_service.upload_results = [OSError("disk vanished")]
    with pytest.raises(UploadError, match="Could not read"):
        PhotosUploader(session).upload(Path("/a.mp4"))


def test_a_dropped_connection_is_flagged_as_transport(session):
    """The network went, so nothing was decided about this file."""
    session.photos_service.upload_results = [
        OSError(errno.EHOSTUNREACH, "No route to host")
    ]
    with pytest.raises(UploadError, match="Lost the connection") as raised:
        PhotosUploader(session).upload(Path("/a.mp4"))

    assert raised.value.transport
    assert raised.value.retryable


def test_a_dropped_connection_on_the_fallback_path_is_flagged_too(slow_session):
    slow_session.photos_service.upload_results = [ConnectionResetError("peer hung up")]
    with pytest.raises(UploadError, match="Lost the connection") as raised:
        PhotosUploader(slow_session).upload(Path("/a.mp4"))

    assert raised.value.transport


def test_a_dropped_connection_filing_into_an_album_is_flagged(session):
    session.photos_service.album_container.albums["Trip"] = FakeAlbum(
        "Trip", add_error=OSError(errno.ECONNRESET, "Connection reset by peer")
    )
    with pytest.raises(UploadError, match=r"filing /a\.mp4 into 'Trip'") as raised:
        PhotosUploader(session, album="Trip").upload(Path("/a.mp4"))

    assert raised.value.transport


def test_a_dropped_connection_resolving_an_album_is_not_a_missing_album(session):
    """A lookup the network ate says nothing about whether the album exists."""
    session.photos_service.album_container.find_error = RequestException("dropped")
    with pytest.raises(UploadError, match="resolving album 'Trip'") as raised:
        PhotosUploader(session, album="Trip").ensure_album()

    assert raised.value.transport


def test_an_album_the_network_hid_is_resolved_on_the_next_try(session):
    """Caching that failure would file every later upload nowhere."""
    container = session.photos_service.album_container
    container.albums["Trip"] = FakeAlbum("Trip")
    container.find_error = RequestException("dropped")
    uploader = PhotosUploader(session, album="Trip")
    with pytest.raises(UploadError):
        uploader.upload(Path("/a.mp4"))

    container.find_error = None
    outcome = uploader.upload(Path("/a.mp4"))

    assert container.albums["Trip"].added == [outcome.asset_id]


def test_service_property_exposes_photos(session):
    assert PhotosUploader(session).service is session.photos_service


def test_asset_fields_are_carried_through(session):
    session.photos_service.upload_results = [FakeAsset("a9", "m9", "clip.mp4")]
    outcome = PhotosUploader(FakeSession(photos_service=session.photos_service)).upload(
        Path("/clip.mp4")
    )
    assert (outcome.master_id, outcome.asset_id) == ("m9", "a9")
