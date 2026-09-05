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
from tests.fakes.icloud import FakeAlbum, FakeAsset, FakeSession


def test_upload_to_root_library(session):
    uploader = PhotosUploader(session)
    outcome = uploader.upload(Path("/videos/a.mp4"))

    assert outcome.status is UploadStatus.CONFIRMED
    assert outcome.asset_id == "asset-1"
    assert outcome.master_id == "master-1"
    assert session.photos_service.uploaded == [("/videos/a.mp4", None)]


def test_none_result_means_uploaded_but_unindexed(session):
    """Pyicloud returns None when CloudKit has not indexed the record yet.

    That is a success: re-sending the bytes next run would be pure waste.
    """
    session.photos_service.upload_results = [None]
    outcome = PhotosUploader(session).upload(Path("/videos/a.mp4"))

    assert outcome.status is UploadStatus.UNVERIFIED
    assert outcome.asset_id is None


def test_existing_album_is_reused(session):
    session.photos_service.album_container.albums["Trip"] = FakeAlbum("Trip")
    uploader = PhotosUploader(session, album="Trip")
    uploader.upload(Path("/videos/a.mp4"))

    assert session.photos_service.uploaded == [("/videos/a.mp4", "Trip")]


def test_missing_album_is_created(session):
    uploader = PhotosUploader(session, album="New")
    uploader.upload(Path("/videos/a.mp4"))

    assert "New" in session.photos_service.album_container.albums
    assert session.photos_service.uploaded == [("/videos/a.mp4", "New")]


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


def test_duplicate_response_is_reported_not_raised(session):
    session.photos_service.upload_results = [
        PyiCloudAPIResponseException("409 duplicate asset")
    ]
    outcome = PhotosUploader(session).upload(Path("/a.mp4"))
    assert outcome.status is UploadStatus.DUPLICATE


def test_already_exists_message_is_a_duplicate(session):
    session.photos_service.upload_results = [
        PyiCloudAPIResponseException("Asset already exists in library")
    ]
    outcome = PhotosUploader(session).upload(Path("/a.mp4"))
    assert outcome.status is UploadStatus.DUPLICATE


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
