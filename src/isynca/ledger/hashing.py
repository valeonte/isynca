"""Content hashing used to identify a file independently of its path."""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


def hash_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """Return the hex SHA-256 of ``path``, read in chunks.

    Videos routinely run to gigabytes, so the file is streamed rather than
    read into memory.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
