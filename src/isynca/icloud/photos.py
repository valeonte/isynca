"""Uploading media into iCloud Photos.

The interesting part is mapping pyicloud's return value onto a status. Its
``upload`` returns the created :class:`PhotoAsset`, or ``None`` when the bytes
were accepted but CloudKit had not indexed the record before the hydration
timeout. ``None`` is therefore a success, not a failure: treating it as one
would re-send the whole file on the next run for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pyicloud.exceptions import PyiCloudAPIResponseException, PyiCloudException

from isynca.errors import AlbumNotFoundError, UploadError
from isynca.icloud.protocols import AlbumLike, ICloudSessionLike, PhotosServiceLike
from isynca.ledger.store import UploadStatus
from isynca.logging import get_logger

LOGGER = get_logger("photos")

_DUPLICATE_MARKERS = ("duplicate", "already exists")


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """The result of uploading one file."""

    status: UploadStatus
    master_id: str | None = None
    asset_id: str | None = None


class PhotosUploader:
    """Uploads local files into a photo library, optionally into an album."""

    def __init__(self, session: ICloudSessionLike, *, album: str | None = None) -> None:
        self._session = session
        self._album_name = album
        self._album: AlbumLike | None = None
        self._album_resolved = False

    @property
    def service(self) -> PhotosServiceLike:
        """Return the underlying photos service."""
        return self._session.photos

    def ensure_album(self) -> str | None:
        """Ensure the configured album exists and return its name.

        pyicloud's ``upload`` resolves an album by name but raises if no album
        matches, so the create-if-missing step happens here. Resolution is
        deferred to the first upload and cached: a dry run never touches the
        network, and a real run pays for it once rather than per file.
        """
        if self._album_resolved:
            return self._album_name
        self._album_resolved = True

        if self._album_name is None:
            return None

        try:
            album = self.service.albums.find(self._album_name)
            if album is None:
                LOGGER.info("Creating album %r", self._album_name)
                album = self.service.create_album(self._album_name)
        except PyiCloudException as exc:
            raise AlbumNotFoundError(
                f"Could not resolve album {self._album_name!r}: {exc}"
            ) from exc

        if album is None:
            raise AlbumNotFoundError(
                f"Album {self._album_name!r} does not exist and could not be created"
            )
        self._album = album
        return self._album_name

    def upload(self, path: Path) -> UploadOutcome:
        """Upload one file and classify the result.

        Raises:
            UploadError: iCloud rejected the file or the transfer failed.
        """
        album = self.ensure_album()
        try:
            asset = self.service.upload(str(path), album=album)
        except PyiCloudAPIResponseException as exc:
            if _looks_like_duplicate(exc):
                LOGGER.debug("iCloud already holds %s", path.name)
                return UploadOutcome(status=UploadStatus.DUPLICATE)
            raise UploadError(f"Upload of {path} failed: {exc}") from exc
        except PyiCloudException as exc:
            raise UploadError(f"Upload of {path} failed: {exc}") from exc
        except OSError as exc:
            raise UploadError(f"Could not read {path}: {exc}") from exc

        if asset is None:
            LOGGER.debug("%s uploaded but was not indexed in time", path.name)
            return UploadOutcome(status=UploadStatus.UNVERIFIED)

        return UploadOutcome(
            status=UploadStatus.CONFIRMED,
            master_id=asset.master_id,
            asset_id=asset.id,
        )


def _looks_like_duplicate(exc: Exception) -> bool:
    """Return whether an API error means iCloud already holds the content.

    pyicloud's public wrapper does not surface the ``putAsset`` duplicate flag,
    so the message is the only signal available at this layer.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _DUPLICATE_MARKERS)
