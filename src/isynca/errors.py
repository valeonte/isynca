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
    """A single file could not be processed; the run continues.

    An item error is retried by default, since most of them -- a dropped
    connection, a rate limit, a transient server fault -- clear on their own.
    A raiser that knows the failure is settled, such as iCloud refusing a
    file's format outright, passes ``retryable=False`` so the run stops
    spending attempts on an answer that will not change.

    ``transport=True`` marks the other extreme: the connection failed, so
    iCloud never gave an answer about this file at all. Nothing is wrong with
    it, and :mod:`isynca.retry` waits far longer for those -- an outage is
    measured in minutes, and a handful of one-second retries would fail every
    remaining file for the sake of a router coming back up.
    """

    def __init__(
        self, message: str, *, retryable: bool = True, transport: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.transport = transport


class UploadError(ItemError):
    """Uploading one file to iCloud failed."""


class AlbumNotFoundError(FatalError):
    """The requested album does not exist and could not be created."""


class ArchiveError(ItemError):
    """One file could not be moved into the archive target."""


class DriveError(ItemError):
    """One iCloud Drive file could not be transferred."""


class DriveListingError(FatalError):
    """A Drive folder could not be listed.

    Fatal, unlike most per-folder trouble, because of what the sync does with
    an empty folder: nothing in it locally and nothing in it remotely means
    the files it used to hold were deleted on the other side. A folder that
    *cannot be read* is indistinguishable from one that is genuinely empty,
    so guessing would delete files nobody deleted. Stopping is the only safe
    answer.
    """


class DriveNotAvailableError(FatalError):
    """The account exposes no usable iCloud Drive service."""


class SyncStateError(FatalError):
    """The sync state database could not be opened, read, or written.

    Fatal because a sync without its memory is not a sync: it cannot tell a
    new file from a deleted one, and would either re-upload everything or
    delete it.
    """
