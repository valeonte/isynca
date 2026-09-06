"""In-memory stand-ins for the pyicloud surface isynca depends on.

These implement the protocols in :mod:`isynca.icloud.protocols`, so every
layer above the adapters is exercised without a socket in sight.

Both upload paths are modelled, because isynca uses both: registrations
through :class:`FakeDirectClient` normally, and the waiting
:meth:`FakePhotosService.upload` when an account exposes no CloudKit client.
Whichever path runs, the file lands in ``FakePhotosService.uploaded``, so a
test asserting nothing was re-sent still means it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyicloud.common.cloudkit.client import CloudKitApiError
from pyicloud.exceptions import PyiCloudAPIResponseException

from isynca.icloud.protocols import PhotoAssetLike
from tests.fakes.drive import FakeDriveService


@dataclass
class FakeAsset:
    """A minimal asset record."""

    id: str
    master_id: str
    filename: str

    @property
    def asset_id(self) -> str:
        """Return the asset record name."""
        return self.id


@dataclass
class FakeRegistration:
    """A ``putAsset`` result, using Apple's own field names."""

    cplMaster: str | None = None  # noqa: N815 - Apple's field name
    cplAsset: str | None = None  # noqa: N815 - Apple's field name
    duplicate: bool = False

    @property
    def is_duplicate(self) -> bool:
        """Return whether iCloud reported it already held the content."""
        return self.duplicate


@dataclass
class FakeAlbum:
    """A minimal album."""

    name: str
    added: list[str] = field(default_factory=list)
    add_error: Exception | None = None

    def add_photo(self, photo: PhotoAssetLike) -> bool:
        """Record which asset was filed into this album."""
        if self.add_error is not None:
            raise self.add_error
        self.added.append(photo.asset_id)
        return True


@dataclass
class FakeAlbumContainer:
    """An album collection backed by a dict."""

    albums: dict[str, FakeAlbum] = field(default_factory=dict)
    find_error: Exception | None = None

    def find(self, name: str) -> FakeAlbum | None:
        """Return the named album, or ``None``."""
        if self.find_error is not None:
            raise self.find_error
        return self.albums.get(name)


@dataclass
class FakeDirectClient:
    """The CloudKit client, registering uploads without waiting on indexing.

    Results are scripted on the service rather than here, so a test can drive
    either upload path with the same ``upload_results`` list. A scripted
    ``None`` -- pyicloud's "accepted but not indexed" -- becomes a
    registration with no record names, which is this path's equivalent.
    """

    service: FakePhotosService
    zones: list[str] = field(default_factory=list)

    def upload_file(self, path: str, *, zone_name: str) -> FakeRegistration:
        """Register an upload and return the next scripted result."""
        self.zones.append(zone_name)
        result = self.service.next_result(path, album=None)
        if result is None:
            return FakeRegistration()
        if isinstance(result, FakeRegistration):
            return result
        return FakeRegistration(cplMaster=result.master_id, cplAsset=result.id)


@dataclass
class FakePhotosService:
    """A photos service that records uploads instead of performing them."""

    album_container: FakeAlbumContainer = field(default_factory=FakeAlbumContainer)
    uploaded: list[tuple[str, str | None]] = field(default_factory=list)
    upload_results: list[FakeAsset | FakeRegistration | Exception | None] = field(
        default_factory=list
    )
    create_returns_none: bool = False
    create_error: Exception | None = None
    counter: int = 0
    has_direct_client: bool = True

    def __post_init__(self) -> None:
        """Give the service its own client, which reads results back off it."""
        self.direct_client = FakeDirectClient(self)

    @property
    def albums(self) -> FakeAlbumContainer:
        """Return the album container."""
        return self.album_container

    @property
    def private_client(self) -> FakeDirectClient | None:
        """Return the CloudKit client, or ``None`` to force the waiting path."""
        return self.direct_client if self.has_direct_client else None

    def create_album(self, name: str) -> FakeAlbum | None:
        """Create and register an album."""
        if self.create_error is not None:
            raise self.create_error
        if self.create_returns_none:
            return None
        album = FakeAlbum(name=name)
        self.album_container.albums[name] = album
        return album

    def next_result(
        self, path: str, *, album: str | None
    ) -> FakeAsset | FakeRegistration | None:
        """Record an upload and return the next scripted result.

        ``upload_results`` is consumed in order; each entry is either an
        exception to raise, ``None`` to simulate an un-indexed upload, or a
        result to return. An empty list means "always succeed".
        """
        self.uploaded.append((path, album))
        self.counter += 1

        if self.upload_results:
            result = self.upload_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        return FakeAsset(
            id=f"asset-{self.counter}",
            master_id=f"master-{self.counter}",
            filename=path.rsplit("/", 1)[-1],
        )

    def upload(self, path: str, *, album: str | None = None) -> FakeAsset | None:
        """Upload the pyicloud way, waiting for an asset that may never come."""
        result = self.next_result(path, album=album)
        if isinstance(result, FakeRegistration):  # pragma: no cover - path unused
            return None
        return result


@dataclass
class FakeSession:
    """An account session exposing fake photos and drive services."""

    photos_service: FakePhotosService = field(default_factory=FakePhotosService)
    drive_service: FakeDriveService = field(default_factory=FakeDriveService)
    requires_2fa: bool = False
    is_trusted_session: bool = True
    code_valid: bool = True
    trust_result: bool = True
    validated_codes: list[str] = field(default_factory=list)
    trust_calls: int = 0
    drive_error: Exception | None = None

    @property
    def photos(self) -> FakePhotosService:
        """Return the photos service."""
        return self.photos_service

    @property
    def drive(self) -> FakeDriveService:
        """Return the drive service."""
        if self.drive_error is not None:
            raise self.drive_error
        return self.drive_service

    def validate_2fa_code(self, code: str) -> bool:
        """Record and validate a 2FA code."""
        self.validated_codes.append(code)
        if self.code_valid:
            self.requires_2fa = False
        return self.code_valid

    def trust_session(self) -> bool:
        """Record a trust request."""
        self.trust_calls += 1
        if self.trust_result:
            self.is_trusted_session = True
        return self.trust_result


def api_error(message: str) -> PyiCloudAPIResponseException:
    """Build a pyicloud API error carrying ``message``."""
    return PyiCloudAPIResponseException(message)


def cloudkit_error(message: str, payload: object | None = None) -> CloudKitApiError:
    """Build a CloudKit error carrying ``message`` and Apple's ``payload``."""
    return CloudKitApiError(message, payload=payload)
