from pathlib import Path

import pytest
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudException,
    PyiCloudServiceNotActivatedException,
)

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


def test_service_property_exposes_photos(session):
    assert PhotosUploader(session).service is session.photos_service


def test_asset_fields_are_carried_through(session):
    session.photos_service.upload_results = [FakeAsset("a9", "m9", "clip.mp4")]
    outcome = PhotosUploader(FakeSession(photos_service=session.photos_service)).upload(
        Path("/clip.mp4")
    )
    assert (outcome.master_id, outcome.asset_id) == ("m9", "a9")
