"""Structural types describing the slice of pyicloud that isynca uses.

This module is the seam that keeps the test suite off the network. Everything
above it depends on these protocols rather than on pyicloud itself, so the
planner, runner and CLI are exercised against an in-memory fake. Only the thin
adapters in :mod:`isynca.icloud.session` and :mod:`isynca.icloud.photos` touch
the real library.

The shapes mirror pyicloud 2.7. Two upload surfaces appear here because
pyicloud has two: ``PhotosService.upload`` waits for CloudKit to index the new
record and returns the hydrated asset (or ``None`` when the wait ran out),
while the CloudKit client behind ``private_client`` returns the registration as
soon as Apple has stored the bytes. isynca prefers the second -- see
:mod:`isynca.icloud.photos` for why.

The drive surface is declared at the *service* level rather than the node
level. pyicloud models Drive as ``DriveNode`` objects that carry a connection
and lazily cache their children, but every call isynca makes is available
directly on ``DriveService`` and takes plain identifiers. Depending on the
service alone keeps the fake a dictionary tree rather than an object graph,
and keeps node caching -- which a sync run must never read stale -- out of the
picture entirely.
"""

from __future__ import annotations

from typing import IO, Any, Protocol, runtime_checkable

from requests import Response


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
    def asset_id(self) -> str:
        """Return the asset record name, which album membership keys on."""
        ...


@runtime_checkable
class AlbumLike(Protocol):
    """A photo album."""

    @property
    def name(self) -> str:
        """Return the album's display name."""
        ...


@runtime_checkable
class AlbumFilingLike(Protocol):
    """An album seen from the one call isynca makes on it.

    Kept apart from :class:`AlbumLike` because pyicloud declares ``add_photo``
    on its concrete album class only, and types the parameter as its own
    hydrated asset -- neither of which an album coming back from ``find`` can
    promise. All the call actually reads is the asset's record name.
    """

    def add_photo(self, photo: PhotoAssetLike) -> bool:
        """Put an already-uploaded asset into this album."""
        ...


@runtime_checkable
class AlbumContainerLike(Protocol):
    """The album collection exposed by the photos service."""

    def find(self, name: str) -> AlbumLike | None:
        """Return the album matching ``name``, or ``None``."""
        ...


@runtime_checkable
class UploadRegistrationLike(Protocol):
    """What Apple's ``putAsset`` step hands back once the bytes are stored.

    The two record names are Apple's own field names, mirrored from pyicloud's
    model so the real result satisfies this protocol as it is. They are filled
    in for a duplicate too, pointing at the asset iCloud already holds.
    """

    cplMaster: str | None  # noqa: N815 - Apple's field name
    cplAsset: str | None  # noqa: N815 - Apple's field name

    @property
    def is_duplicate(self) -> bool:
        """Return whether iCloud reported it already held this content."""
        ...


@runtime_checkable
class DirectUploadLike(Protocol):
    """The CloudKit client's register-and-return upload."""

    def upload_file(self, path: str, *, zone_name: str) -> UploadRegistrationLike:
        """Upload one file into ``zone_name`` and return its registration."""
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

    @property
    def private_client(self) -> DirectUploadLike | None:
        """Return the CloudKit client that registers uploads, if there is one.

        The legacy photos service has no such attribute at all, so callers
        reach for it defensively rather than assuming this member exists.
        """
        ...

    def upload(self, path: str, *, album: str | None = None) -> PhotoAssetLike | None:
        """Upload one file, optionally into the album named ``album``.

        The album is addressed by name rather than by object: pyicloud accepts
        either, and a name keeps this protocol assignable from the real
        service, whose parameter is typed ``str | BasePhotoAlbum | None``.
        """
        ...


@runtime_checkable
class DriveServiceLike(Protocol):
    """The iCloud Drive surface isynca depends on.

    Every member is a plain call on pyicloud's ``DriveService``. Folder
    listings come back as the raw ``retrieveItemDetailsInFolders`` payload --
    a dict whose ``items`` key holds one dict per child -- because that is the
    only shape Apple actually returns, and re-wrapping it in node objects only
    to unwrap it again would buy nothing.
    """

    def get_node_data(
        self,
        drivewsid: str,
        share_id: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the folder node identified by ``drivewsid``, with its items."""
        ...

    def get_file(
        self,
        file_id: str,
        zone: str = ...,
        **kwargs: Any,  # noqa: ANN401 - passed straight through to requests
    ) -> Response:
        """Return a response streaming the content of one document."""
        ...

    def send_file(
        self,
        folder_id: str,
        file_object: IO[bytes],
        zone: str = ...,
        **kwargs: Any,  # noqa: ANN401 - passed straight through to requests
    ) -> None:
        """Upload ``file_object`` into the folder identified by ``folder_id``.

        The remote name is taken from ``file_object.name``, not from any
        separate argument -- see :class:`~isynca.files.client.NamedReader` for
        what that forces on callers.
        """
        ...

    def create_folders(self, parent: str, name: str) -> Any:  # noqa: ANN401
        """Create a folder called ``name`` under the ``parent`` drivewsid."""
        ...

    def move_items_to_trash(self, node_id: str, etag: str) -> Any:  # noqa: ANN401
        """Move one node into Recently Deleted, where it stays recoverable."""
        ...


@runtime_checkable
class ICloudSessionLike(Protocol):
    """The account-level surface isynca depends on."""

    @property
    def photos(self) -> PhotosServiceLike:
        """Return the photos service."""
        ...

    @property
    def drive(self) -> DriveServiceLike:
        """Return the iCloud Drive service."""
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
