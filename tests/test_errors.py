import pytest

from isynca.errors import (
    AlbumNotFoundError,
    AuthenticationError,
    ConfigError,
    FatalError,
    IsyncaError,
    ItemError,
    LedgerError,
    TwoFactorRequiredError,
    UploadError,
)


@pytest.mark.parametrize(
    "error",
    [ConfigError, AuthenticationError, LedgerError, AlbumNotFoundError],
)
def test_fatal_errors_abort_a_run(error):
    assert issubclass(error, FatalError)
    assert issubclass(error, IsyncaError)


def test_two_factor_is_an_authentication_error():
    assert issubclass(TwoFactorRequiredError, AuthenticationError)


def test_upload_error_is_per_item_not_fatal():
    """A single bad file must not abort the run, so it is not a FatalError."""
    assert issubclass(UploadError, ItemError)
    assert not issubclass(UploadError, FatalError)
