"""Uploading media into iCloud Photos.

Apple's upload protocol registers an asset before CloudKit can find it:
``putAsset`` returns the ``CPLMaster``/``CPLAsset`` record names the moment the
bytes are stored, but a lookup of those names answers NOT_FOUND for another
14-20 seconds. pyicloud's ``PhotosService.upload`` sits out that gap so it can
hand back a fully hydrated asset, which prices every upload at roughly a
quarter of a minute no matter how small the file.

isynca only ever wanted the two record names, and ``putAsset`` has already
supplied them, so the registration is taken as the result and CloudKit is left
to index in its own time. Hence :attr:`UploadStatus.CONFIRMED` here means
"iCloud stored the bytes and named the records", not "the asset is queryable".

The waiting call remains as a fallback for a session that exposes no CloudKit
client. There its ``None`` return is a success too: the bytes were accepted but
not indexed in time, and re-sending them on the next run would buy nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import cast

from pyicloud.common.cloudkit.client import CloudKitApiError
from pyicloud.exceptions import PyiCloudAPIResponseException, PyiCloudException
from pyicloud.services.photos_cloudkit import PRIMARY_ZONE

from isynca.errors import AlbumNotFoundError, UploadError
from isynca.icloud.protocols import (
    AlbumFilingLike,
    DirectUploadLike,
    ICloudSessionLike,
    PhotosServiceLike,
    UploadRegistrationLike,
)
from isynca.ledger.store import UploadStatus
from isynca.logging import get_logger

LOGGER = get_logger("photos")

_DUPLICATE_MARKERS = ("duplicate", "already exists")

_RETRYABLE_CLIENT_STATUSES = frozenset(
    {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
)
"""The 4xx statuses that describe a moment rather than the file itself."""

_STATUS_HINTS: dict[int, str] = {
    HTTPStatus.UNAUTHORIZED: "the iCloud session is no longer accepted",
    HTTPStatus.FORBIDDEN: "iCloud refused this account access to the library",
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: "the file is bigger than iCloud accepts",
    HTTPStatus.UNSUPPORTED_MEDIA_TYPE: (
        "iCloud Photos will not take this file's container, format, or codec"
    ),
    HTTPStatus.TOO_MANY_REQUESTS: "iCloud is rate limiting this account",
    HTTPStatus.INSUFFICIENT_STORAGE: "the iCloud storage plan is full",
}
"""What the statuses Apple returns for a rejected file mean in practice."""

PRIMARY_ZONE_NAME = str(PRIMARY_ZONE["zoneName"])
"""The CloudKit zone holding the account's own library."""


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    """The result of uploading one file."""

    status: UploadStatus
    master_id: str | None = None
    asset_id: str | None = None


@dataclass(frozen=True, slots=True)
class _RegisteredAsset:
    """The asset view an album needs, built from a registration.

    Album membership is a CloudKit relation keyed by the asset's record name,
    which ``putAsset`` returned, so there is nothing here worth waiting for
    the hydrated asset to learn.
    """

    id: str
    master_id: str

    @property
    def asset_id(self) -> str:
        """Return the asset record name."""
        return self.id


class PhotosUploader:
    """Uploads local files into a photo library, optionally into an album."""

    def __init__(self, session: ICloudSessionLike, *, album: str | None = None) -> None:
        self._session = session
        self._album_name = album
        self._album: AlbumFilingLike | None = None
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
        # pyicloud declares add_photo only on its concrete album class, and
        # an album from find() is typed as the abstract base; the cast records
        # that the call is real even though the declared type cannot show it.
        self._album = cast(AlbumFilingLike, album)
        return self._album_name

    def upload(self, path: Path) -> UploadOutcome:
        """Upload one file and classify the result.

        Raises:
            UploadError: iCloud rejected the file or the transfer failed.
        """
        album = self.ensure_album()
        client = _direct_client(self.service)
        if client is not None:
            return self._register(client, path)
        return self._upload_and_wait(path, album)

    def _register(self, client: DirectUploadLike, path: Path) -> UploadOutcome:
        """Store the bytes and return as soon as Apple has named the records."""
        try:
            result = client.upload_file(str(path), zone_name=PRIMARY_ZONE_NAME)
        except CloudKitApiError as exc:
            raise UploadError(
                f"Upload of {path} failed: {_describe(exc)}",
                retryable=_is_retryable(exc),
            ) from exc
        except PyiCloudException as exc:
            raise UploadError(f"Upload of {path} failed: {exc}") from exc
        except OSError as exc:
            raise UploadError(f"Could not read {path}: {exc}") from exc

        return self._classify(result, path)

    def _classify(self, result: UploadRegistrationLike, path: Path) -> UploadOutcome:
        """Turn a registration into an outcome, filing it into the album."""
        status = UploadStatus.CONFIRMED
        if result.is_duplicate:
            LOGGER.debug("iCloud already holds %s", path.name)
            status = UploadStatus.DUPLICATE

        master_id, asset_id = result.cplMaster, result.cplAsset
        if not master_id or not asset_id:
            # Apple took the bytes but named no records, so there is nothing
            # to file into an album and nothing to prove for the ledger.
            LOGGER.debug("%s was stored without record names", path.name)
            return UploadOutcome(status=UploadStatus.UNVERIFIED)

        self._add_to_album(_RegisteredAsset(id=asset_id, master_id=master_id), path)
        return UploadOutcome(status=status, master_id=master_id, asset_id=asset_id)

    def _add_to_album(self, asset: _RegisteredAsset, path: Path) -> None:
        """File a registered asset into the configured album, if there is one.

        Raises:
            UploadError: The relation could not be created. The bytes are in
                iCloud either way, so a retry re-registers as a duplicate and
                tries the album again rather than re-uploading for nothing.
        """
        if self._album is None:
            return
        try:
            self._album.add_photo(asset)
        except (CloudKitApiError, PyiCloudException) as exc:
            raise UploadError(
                f"{path} reached iCloud but could not be added to "
                f"{self._album_name!r}: {_describe(exc)}",
                retryable=_is_retryable(exc),
            ) from exc

    def _upload_and_wait(self, path: Path, album: str | None) -> UploadOutcome:
        """Upload through pyicloud's public call, which waits for indexing."""
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


def _direct_client(service: PhotosServiceLike) -> DirectUploadLike | None:
    """Return the client that can register an upload, or ``None`` if there is none.

    pyicloud's legacy photos service carries no ``private_client`` at all, so
    the attribute is asked for rather than assumed. A missing one is not an
    error: it only means this account falls back to the waiting upload.
    """
    client = getattr(service, "private_client", None)
    if client is None or not callable(getattr(client, "upload_file", None)):
        LOGGER.debug("No CloudKit upload client; falling back to the slow upload")
        return None
    return client


def _describe(exc: Exception) -> str:
    """Return the error text with Apple's own explanation appended.

    pyicloud reports a rejected file as a bare status number and hangs the
    per-file response block off a :class:`CloudKitApiError` as ``payload``.
    That block holds what the number means, any message Apple wrote, and
    whether a retry could ever succeed -- the parts that say what to do about
    the file. An error carrying no such block is returned as it stands.
    """
    detail = "; ".join(_failure_details(getattr(exc, "payload", None)))
    return f"{exc} ({detail})" if detail else str(exc)


def _is_retryable(exc: Exception) -> bool:
    """Return whether trying ``exc``'s file again could plausibly work.

    Apple answers that question outright with ``isRetryable``, but omits the
    flag on some rejections, so the status stands in for it: a 4xx is a
    verdict on the request itself -- an unsupported codec, a file too large, a
    session iCloud will not accept -- and re-sending the same bytes only earns
    the same verdict. The exceptions are the two 4xx that describe a moment
    rather than the file. Anything with no failure block, a 5xx included,
    keeps the benefit of the doubt.
    """
    response = _failure_block(getattr(exc, "payload", None))
    return True if response is None else _block_is_retryable(response)


def _block_is_retryable(response: Mapping[str, object]) -> bool:
    """Return whether a per-file response block leaves room for another try."""
    if response.get("isRetryable") is False:
        return False
    status = response.get("status")
    if not isinstance(status, int):
        return True
    return (
        not HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
        or status in _RETRYABLE_CLIENT_STATUSES
    )


def _failure_block(payload: object) -> Mapping[str, object] | None:
    """Return the per-file response block of a ``putAsset`` failure payload.

    Only that payload has this shape; the other CloudKit calls attach a raw
    body, which carries none of the fields read here.
    """
    if not isinstance(payload, Mapping):
        return None
    response = payload.get("response")
    return response if isinstance(response, Mapping) else None


def _failure_details(payload: object) -> list[str]:
    """Return the readable parts of a ``putAsset`` failure payload."""
    response = _failure_block(payload)
    if response is None:
        return []

    details: list[str] = []
    status = response.get("status")
    if isinstance(status, int):
        details.append(_status_text(status))
    message = response.get("errorMessage")
    if message:
        details.append(f"Apple said: {message}")
    if not _block_is_retryable(response):
        details.append("retrying will not help")
    return details


def _status_text(status: int) -> str:
    """Name an HTTP status, adding what it tends to mean for an upload."""
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        return f"unrecognised status {status}"
    hint = _STATUS_HINTS.get(status)
    return f"{phrase}: {hint}" if hint else phrase


def _looks_like_duplicate(exc: Exception) -> bool:
    """Return whether an API error means iCloud already holds the content.

    Only the fallback path needs this. A registration reports the duplicate
    flag outright, where here the message is the sole signal pyicloud leaves.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _DUPLICATE_MARKERS)
