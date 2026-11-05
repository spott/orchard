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
        # The mode passed to open is cut by the umask; the lock must stay
        # openable by every operator in the lock dir's group (the resolver's
        # user creates most of them, the operator's shell must take them).
        try:
            os.fchmod(fd, 0o660)
        except OSError:
            pass  # not the owner: the mode is already whatever the owner set
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
