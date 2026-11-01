"""Per-workspace advisory locks, so two `ssh foo.orchard` racing to create
`foo` serialise instead of both calling `incus init`."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def held(lock_dir: str, name: str) -> Iterator[None]:
    os.makedirs(lock_dir, exist_ok=True)
    fd = os.open(os.path.join(lock_dir, f"{name}.lock"), os.O_RDWR | os.O_CREAT, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
