import io
import json
from typing import IO, cast

import pytest
from pyicloud import PyiCloudService
from pyicloud.services.drive import COOKIE_APPLE_WEBAUTH_VALIDATE, DriveService
from pyicloud.session import PyiCloudSession
from requests import Response
from requests.cookies import RequestsCookieJar
from requests.models import RequestEncodingMixin

from isynca.files.client import NamedReader
from isynca.icloud.drive import ICloudService, MultipartBody, StreamingDriveService

BOUNDARY = "b0und4ry"


def _requests_body(monkeypatch, name, content):
    """Return the body and content type ``requests`` builds for ``files=``."""
    monkeypatch.setattr("urllib3.filepost.choose_boundary", lambda: BOUNDARY)
    stream = NamedReader(io.BytesIO(content), name)
    return RequestEncodingMixin._encode_files({name: stream}, None)


@pytest.mark.parametrize(
    "name", ["notes.md", "η Βάπτιση του Γιάννη.mp4", 'odd "quoted" name.txt']
)
def test_body_matches_what_requests_would_have_sent(monkeypatch, name):
    content = bytes(range(256)) * 50
    expected, expected_type = _requests_body(monkeypatch, name, content)

    body = MultipartBody(io.BytesIO(content), name, boundary=BOUNDARY)

    assert body.content_type == expected_type
    assert len(body) == len(expected)
    assert body.read() == expected


def test_body_read_in_blocks_matches_a_whole_read(monkeypatch):
    """Blocks smaller than the part headers split them across reads."""
    content = b"x" * 1_000 + b"y" * 7
    expected, _ = _requests_body(monkeypatch, "a.bin", content)
    body = MultipartBody(io.BytesIO(content), "a.bin", boundary=BOUNDARY)

    blocks = []
    while block := body.read(7):
        assert len(block) <= 7
        blocks.append(block)

    assert b"".join(blocks) == expected
    assert body.read(7) == b""


class _SpyStream(io.BytesIO):
    """Records the largest single read asked of it."""

    largest = 0

    def read(self, size=-1):
        data = super().read(size)
        self.largest = max(self.largest, len(data))
        return data


def test_body_never_reads_more_of_the_file_than_asked():
    """The whole point: a block of body costs at most a block of file."""
    stream = _SpyStream(b"z" * 100_000)
    body = MultipartBody(stream, "big.bin")

    assert len(body) > 100_000
    assert stream.largest == 0
    while body.read(4096):
        pass
    assert stream.largest <= 4096


def test_body_counts_from_the_streams_current_position():
    stream = io.BytesIO(b"skipme" + b"payload")
    stream.seek(6)
    body = MultipartBody(stream, "a.bin", boundary=BOUNDARY)
    sent = body.read()
    assert len(body) == len(sent)
    assert b"payload" in sent
    assert b"skipme" not in sent


def _json_response(payload):
    response = Response()
    response.status_code = 200
    response._content = json.dumps(payload).encode()
    return response


class _RecordingSession:
    """Enough of ``PyiCloudSession`` for pyicloud's upload helpers."""

    def __init__(self):
        self.cookies = RequestsCookieJar()
        self.cookies.set(COOKIE_APPLE_WEBAUTH_VALIDATE, "v=1:t=token")
        self.posts = []

    def post(self, url, **kwargs):
        if "data" in kwargs:
            kwargs["sent"] = kwargs["data"].read()
        self.posts.append((url, kwargs))
        if url.endswith("/upload/web"):
            return _json_response(
                [{"document_id": "doc-1", "url": "https://content.example/up"}]
            )
        if url == "https://content.example/up":
            return _json_response(
                {
                    "singleFile": {
                        "fileChecksum": "sum",
                        "wrappingKey": "key",
                        "referenceChecksum": "ref",
                        "size": 5,
                        "receipt": "rcpt",
                    }
                }
            )
        return _json_response({})


def test_send_file_streams_the_content_and_files_the_document(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"hello")
    session = _RecordingSession()
    drive = StreamingDriveService(
        service_root="https://drivews.example",
        document_root="https://docws.example",
        session=cast(PyiCloudSession, session),
        params={},
    )

    with source.open("rb") as handle:
        drive.send_file(
            "folder-1",
            cast(IO[bytes], NamedReader(handle, "clip.mp4")),
            mtime=1.5,
            ctime=1.5,
        )

    (register_url, _), (content_url, sent), (update_url, update) = session.posts
    assert register_url.endswith("/upload/web")
    assert content_url == "https://content.example/up"
    assert "files" not in sent
    assert sent["headers"]["Content-Type"].startswith("multipart/form-data")
    assert b'filename="clip.mp4"' in sent["sent"]
    assert b"\r\n\r\nhello\r\n--" in sent["sent"]
    assert update_url.endswith("/update/documents")
    assert update["json"]["document_id"] == "doc-1"
    assert update["json"]["path"] == {
        "starting_document_id": "folder-1",
        "path": "clip.mp4",
    }
    assert update["json"]["data"]["receipt"] == "rcpt"
    assert update["json"]["mtime"] == 1500


def test_replacing_keeps_the_session_and_endpoints():
    session = _RecordingSession()
    original = DriveService(
        service_root="https://drivews.example",
        document_root="https://docws.example",
        session=cast(PyiCloudSession, session),
        params={"clientId": "x"},
    )
    streaming = StreamingDriveService.replacing(original)
    assert streaming.session is session
    assert streaming.params is original.params
    assert streaming.service_root == "https://drivews.example"
    assert streaming._document_root == "https://docws.example"


def _plain_drive(session):
    return DriveService(
        service_root="https://drivews.example",
        document_root="https://docws.example",
        session=cast(PyiCloudSession, session),
        params={},
    )


def test_account_service_hands_out_the_streaming_drive(monkeypatch):
    """Built once and cached, and rebuilt when re-authentication clears it."""
    session = _RecordingSession()
    monkeypatch.setattr(
        PyiCloudService,
        "drive",
        property(lambda self: self._drive or _plain_drive(session)),
    )
    service = ICloudService.__new__(ICloudService)
    service._drive = None

    first = service.drive
    assert isinstance(first, StreamingDriveService)
    assert first.session is session
    assert service.drive is first

    service._drive = None
    assert isinstance(service.drive, StreamingDriveService)
    assert service.drive is not first
