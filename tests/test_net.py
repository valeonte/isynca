import errno
import socket
import ssl

import pytest
from requests import ConnectionError as RequestsConnectionError
from requests import RequestException
from urllib3.exceptions import ProtocolError

from isynca.net import is_transport_error


@pytest.mark.parametrize(
    "exc",
    [
        RequestException("boom"),
        RequestsConnectionError("connection aborted"),
        ProtocolError("connection broken"),
        ssl.SSLError("handshake failed"),
        socket.gaierror("name resolution failed"),
        socket.herror("host lookup failed"),
        ConnectionResetError("peer hung up"),
        TimeoutError("read timed out"),
        OSError(errno.EHOSTUNREACH, "No route to host"),
        OSError(errno.ENETUNREACH, "Network is unreachable"),
    ],
)
def test_connection_failures_are_transport_errors(exc):
    assert is_transport_error(exc)


@pytest.mark.parametrize(
    "exc",
    [
        OSError("disk vanished"),
        OSError(errno.ENOENT, "No such file or directory"),
        PermissionError(errno.EACCES, "Permission denied"),
        ValueError("not even an OSError"),
    ],
)
def test_local_failures_are_not_transport_errors(exc):
    """Waiting does not make an unreadable file readable."""
    assert not is_transport_error(exc)
