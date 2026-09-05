import hashlib

from isynca.ledger.hashing import hash_file


def test_hash_matches_hashlib(tmp_path):
    path = tmp_path / "clip.mp4"
    payload = b"some video bytes"
    path.write_bytes(payload)
    assert hash_file(path) == hashlib.sha256(payload).hexdigest()


def test_hash_streams_in_chunks(tmp_path):
    """A tiny chunk size must not change the digest."""
    path = tmp_path / "big.mp4"
    payload = bytes(range(256)) * 40
    path.write_bytes(payload)
    assert hash_file(path, chunk_size=7) == hashlib.sha256(payload).hexdigest()


def test_hash_of_empty_file(tmp_path):
    path = tmp_path / "empty.mp4"
    path.write_bytes(b"")
    assert hash_file(path) == hashlib.sha256(b"").hexdigest()
