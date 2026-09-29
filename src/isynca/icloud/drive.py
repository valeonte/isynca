"""iCloud Drive uploads that stream from disk instead of loading into memory.

pyicloud's ``DriveService.send_file`` posts the document with
``requests``' ``files=`` argument. ``requests`` builds that multipart body in
memory: it reads the whole file into one ``bytes`` object and then copies it
into the encoded body. For a multi-gigabyte video that is several times the
file's size in RAM, and the kernel's OOM killer ends the run before the upload
finishes -- every run, on the same file, so nothing behind it is reached.

:class:`StreamingDriveService` keeps every other part of pyicloud's upload
(the registration that reserves the document, the ``update/documents`` call
that files it into a folder) and replaces only the content POST with a body
that is read from the file as the socket drains it.
"""

from __future__ import annotations

import os
from typing import IO, Any

from pyicloud import PyiCloudService
from pyicloud.services.drive import CLOUD_DOCS_ZONE, DriveService
from urllib3.fields import RequestField
from urllib3.filepost import choose_boundary, encode_multipart_formdata


class MultipartBody:
    """A ``multipart/form-data`` body holding one file, produced on demand.

    The body is the same bytes ``requests`` would build for
    ``files={name: stream}``: urllib3 encodes the part headers, so filenames
    outside ASCII are quoted exactly as before. Only the file's content is
    left out of that encoding and read from ``stream`` as it is sent.

    ``requests`` takes the Content-Length from ``len()`` and sends any object
    with a ``read`` method in blocks, so these two methods are all it needs.
    """

    def __init__(
        self, stream: IO[bytes], name: str, boundary: str | None = None
    ) -> None:
        boundary = boundary or choose_boundary()
        field = RequestField(name=name, data=b"", filename=name)
        field.make_multipart()
        empty, self.content_type = encode_multipart_formdata([field], boundary=boundary)
        tail = f"\r\n--{boundary}--\r\n".encode()
        self._parts: list[bytes | IO[bytes]] = [
            empty[: -len(tail)],
            stream,
            tail,
        ]

        start = stream.tell()
        size = stream.seek(0, os.SEEK_END) - start
        stream.seek(start, os.SEEK_SET)
        self._length = len(empty) + size

    def __len__(self) -> int:
        """Return the size of the whole body, in bytes."""
        return self._length

    def read(self, size: int = -1) -> bytes:
        """Return up to ``size`` bytes of the body; everything left if negative."""
        out = bytearray()
        while self._parts and (size < 0 or len(out) < size):
            part = self._parts[0]
            want = -1 if size < 0 else size - len(out)
            if isinstance(part, bytes):
                chunk = part if want < 0 else part[:want]
                rest = part[len(chunk) :]
                if rest:
                    self._parts[0] = rest
                else:
                    self._parts.pop(0)
            else:
                chunk = part.read(want)
                if not chunk or want < 0:
                    self._parts.pop(0)
            out += chunk
        return bytes(out)


class StreamingDriveService(DriveService):
    """pyicloud's Drive service, with uploads streamed from the file."""

    @classmethod
    def replacing(cls, drive: DriveService) -> StreamingDriveService:
        """Return a streaming service sharing ``drive``'s session and endpoints."""
        return cls(
            service_root=drive.service_root,
            document_root=drive._document_root,
            session=drive.session,
            params=drive.params,
        )

    def send_file(
        self,
        folder_id: str,
        file_object: IO[bytes],
        zone: str = CLOUD_DOCS_ZONE,
        **kwargs: Any,  # noqa: ANN401 - passed on to pyicloud unchanged
    ) -> None:
        """Send a new file to iCloud Drive without holding it in memory.

        The same three calls as pyicloud's ``send_file``; only the body of
        the middle one differs.
        """
        document_id, content_url = self._get_upload_contentws_url(
            file_object=file_object, zone=zone
        )
        body = MultipartBody(file_object, file_object.name)
        response = self.session.post(
            content_url, data=body, headers={"Content-Type": body.content_type}
        )
        self._raise_if_error(response)
        self._update_contentws(
            folder_id,
            response.json()["singleFile"],
            document_id,
            file_object,
            zone,
            **kwargs,
        )


class ICloudService(PyiCloudService):
    """pyicloud's account service, handing out :class:`StreamingDriveService`.

    pyicloud builds its Drive service once and caches it, then rebuilds it
    after a re-authentication clears the cache. Swapping the cached instance
    here catches both, while leaving pyicloud to decide when one is needed.
    """

    @property
    def drive(self) -> DriveService:
        """Return the Drive service, with streaming uploads."""
        drive = super().drive
        if not isinstance(drive, StreamingDriveService):
            drive = StreamingDriveService.replacing(drive)
            self._drive = drive
        return drive
