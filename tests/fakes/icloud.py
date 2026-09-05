"""In-memory stand-ins for the pyicloud surface isynca depends on.

These implement the protocols in :mod:`isynca.icloud.protocols`, so every
layer above the adapters is exercised without a socket in sight.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyicloud.exceptions import PyiCloudAPIResponseException


@dataclass
class FakeAsset:
    """A minimal asset record."""

    id: str
    master_id: str
    filename: str


@dataclass
class FakeAlbum:
    """A minimal album."""

    name: str


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
class FakePhotosService:
    """A photos service that records uploads instead of performing them."""

    album_container: FakeAlbumContainer = field(default_factory=FakeAlbumContainer)
    uploaded: list[tuple[str, str | None]] = field(default_factory=list)
    upload_results: list[FakeAsset | Exception | None] = field(default_factory=list)
    create_returns_none: bool = False
    create_error: Exception | None = None
    counter: int = 0

    @property
    def albums(self) -> FakeAlbumContainer:
        """Return the album container."""
        return self.album_container

    def create_album(self, name: str) -> FakeAlbum | None:
        """Create and register an album."""
        if self.create_error is not None:
            raise self.create_error
        if self.create_returns_none:
            return None
        album = FakeAlbum(name=name)
        self.album_container.albums[name] = album
        return album

    def upload(self, path: str, *, album: str | None = None) -> FakeAsset | None:
        """Record an upload and return the next scripted result.

        ``upload_results`` is consumed in order; each entry is either an
        exception to raise, ``None`` to simulate an un-indexed upload, or an
        asset to return. An empty list means "always succeed".
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


@dataclass
class FakeSession:
    """An account session exposing a fake photos service."""

    photos_service: FakePhotosService = field(default_factory=FakePhotosService)
    requires_2fa: bool = False
    is_trusted_session: bool = True
    code_valid: bool = True
    trust_result: bool = True
    validated_codes: list[str] = field(default_factory=list)
    trust_calls: int = 0

    @property
    def photos(self) -> FakePhotosService:
        """Return the photos service."""
        return self.photos_service

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
