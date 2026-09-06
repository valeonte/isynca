"""Exception hierarchy for isynca.

The split that matters operationally is :class:`FatalError` versus
:class:`ItemError`. A fatal error aborts the whole run because continuing
cannot help -- bad credentials, an untrusted session, an unreadable ledger.
An item error concerns a single file, gets recorded in the report, and the
run moves on to the next file.
"""


class IsyncaError(Exception):
    """Base class for every error raised by isynca."""


class FatalError(IsyncaError):
    """An error that makes continuing the current run pointless."""


class ConfigError(FatalError):
    """The configuration file or supplied options are unusable."""


class AuthenticationError(FatalError):
    """Authentication with iCloud failed or the session is not trusted."""


class TwoFactorRequiredError(AuthenticationError):
    """The session needs interactive two-factor authentication.

    Raised by non-interactive commands so the caller can tell the user to run
    ``isynca auth login`` rather than reporting a generic auth failure.
    """


class LedgerError(FatalError):
    """The upload ledger could not be opened, read, or written."""


class ItemError(IsyncaError):
    """A single file could not be processed; the run continues."""


class UploadError(ItemError):
    """Uploading one file to iCloud failed."""


class AlbumNotFoundError(FatalError):
    """The requested album does not exist and could not be created."""


class ArchiveError(ItemError):
    """One file could not be moved into the archive target."""
