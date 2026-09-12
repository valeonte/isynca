"""Telling a lost connection apart from a rejected file.

Two very different failures arrive as exceptions from the same call. iCloud
can answer about a file -- an unsupported codec, a full storage plan, a
server fault -- or the connection can simply go: a dropped wifi link, a
router reboot, a laptop that slept mid-upload. The bytes and the file are
fine in the second case, and the only useful response is to wait.

The distinction is not in the exception type. ``requests`` wraps most
transport trouble in :class:`~requests.RequestException`, but a socket can
also raise a bare :class:`OSError` straight through a library that does its
own HTTP -- ``[Errno 113] No route to host`` is exactly that, and it is not
a :class:`ConnectionError` subclass either, because Python only maps a
handful of errnos to those. So the errno is read directly.

A local read failure is an :class:`OSError` too, which is why this is a
question about one exception rather than a blanket rule for a call: an
unreadable file is not something waiting will fix.
"""

from __future__ import annotations

import errno
import socket
import ssl

from requests import RequestException
from urllib3.exceptions import HTTPError as Urllib3Error

TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (OSError, Urllib3Error)
"""What to catch around a network call.

Deliberately wide: :class:`OSError` covers sockets, SSL, DNS, timeouts and
``requests`` (whose base error subclasses it), and urllib3's own hierarchy
sits outside :class:`OSError` entirely. Catching widely and then asking
:func:`is_transport_error` which it was keeps the decision in one place,
rather than spread over every ``except`` clause in the codebase.
"""

_NETWORK_ERRNOS = frozenset(
    {
        errno.EADDRNOTAVAIL,
        errno.EAGAIN,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.ENETUNREACH,
        errno.ENOTCONN,
        errno.EPIPE,
        errno.ESHUTDOWN,
        errno.ETIMEDOUT,
    }
)
"""The errnos that mean the network, not the file or the disk."""


def is_transport_error(exc: BaseException) -> bool:
    """Return whether ``exc`` means the connection failed, not the transfer.

    A true answer says nothing was decided about the file: it never reached
    iCloud, or iCloud's answer never came back. Retrying it later is the
    whole remedy, however long later has to be.
    """
    if isinstance(
        exc,
        RequestException
        | Urllib3Error
        | ssl.SSLError
        | socket.gaierror
        | socket.herror,
    ):
        return True
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    return isinstance(exc, OSError) and exc.errno in _NETWORK_ERRNOS


__all__ = ["TRANSPORT_ERRORS", "is_transport_error"]
