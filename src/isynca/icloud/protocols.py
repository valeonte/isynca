"""Structural types describing the slice of pyicloud that isynca uses.

This module is the seam that keeps the test suite off the network. Everything
above it depends on these protocols rather than on pyicloud itself, so the
planner, runner and CLI are exercised against an in-memory fake. Only the thin
adapters in :mod:`isynca.icloud.session` and :mod:`isynca.icloud.photos` touch
the real library.

The shapes mirror pyicloud 2.7: ``PhotosService.upload`` returns the created
asset, or ``None`` when the upload succeeded but CloudKit had not indexed the
record before the hydration timeout elapsed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PhotoAssetLike(Protocol):
    """An asset record returned by a successful upload."""

    @property
    def id(self) -> str:
        """Return the asset's record name."""
        ...

    @property
    def master_id(self) -> str:
        """Return the master record name."""
        ...

    @property
    def filename(self) -> str:
        """Return the asset's file name as stored by iCloud."""
        ...


@runtime_checkable
class AlbumLike(Protocol):
    """A photo album."""

    @property
    def name(self) -> str:
        """Return the album's display name."""
        ...


@runtime_checkable
class AlbumContainerLike(Protocol):
    """The album collection exposed by the photos service."""

    def find(self, name: str) -> AlbumLike | None:
        """Return the album matching ``name``, or ``None``."""
        ...


@runtime_checkable
class PhotosServiceLike(Protocol):
    """The photos service surface isynca depends on."""

    @property
    def albums(self) -> AlbumContainerLike:
        """Return the account's albums."""
        ...

    def create_album(self, name: str) -> AlbumLike | None:
        """Create an album and return it."""
        ...

    def upload(self, path: str, *, album: str | None = None) -> PhotoAssetLike | None:
        """Upload one file, optionally into the album named ``album``.

        The album is addressed by name rather than by object: pyicloud accepts
        either, and a name keeps this protocol assignable from the real
        service, whose parameter is typed ``str | BasePhotoAlbum | None``.
        """
        ...


@runtime_checkable
class ICloudSessionLike(Protocol):
    """The account-level surface isynca depends on."""

    @property
    def photos(self) -> PhotosServiceLike:
        """Return the photos service."""
        ...

    @property
    def requires_2fa(self) -> bool:
        """Return whether interactive two-factor authentication is pending."""
        ...

    @property
    def is_trusted_session(self) -> bool:
        """Return whether the stored session is trusted by Apple."""
        ...

    def validate_2fa_code(self, code: str) -> bool:
        """Submit a two-factor code; return whether Apple accepted it."""
        ...

    def trust_session(self) -> bool:
        """Ask Apple to trust this session; return whether it did."""
        ...
