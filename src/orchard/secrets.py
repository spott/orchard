"""Secrets every workspace gets at start, into its runtime tmpfs.

The host reads each configured `op://` reference once with a 1Password
service account (`secrets_token_file`), writes them as `KEY=value` lines to a
cache file (`secrets_cache/env`, only the orchard group can read it), and
`dev` pushes that file into `/run/user/<uid>/orchard/env` of every workspace
it starts. Workspace starts therefore cost no 1Password requests; `dev
secrets refresh` (a timer) is what talks to 1Password. Nothing is written to
a workspace's volume or root filesystem: a stopped workspace holds no
secrets, and the file is gone after a restart until `dev` starts it again."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Callable

from . import OrchardError
from .config import Config

ENV_FILE = "env"


class SecretsError(OrchardError):
    pass


class Secrets:
    def __init__(self, cfg: Config, log: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.log = log or (lambda msg: print(f"orchard: {msg}", file=sys.stderr, flush=True))

    @property
    def configured(self) -> bool:
        return bool(self.cfg.secrets_env)

    def env_file(self) -> str:
        return os.path.join(self.cfg.secrets_cache, ENV_FILE)

    def cached(self) -> tuple[list[str], float | None]:
        """(keys in the cache, its age in seconds) or ([], None)."""
        try:
            st = os.stat(self.env_file())
            keys = [line.split("=", 1)[0] for line in open(self.env_file()) if "=" in line and not line.startswith("#")]
            return keys, time.time() - st.st_mtime
        except OSError:
            return [], None

    def read(self, ref: str, *, token_file: str | None = None) -> str:
        """`op read` one reference with the service account token (the
        workspace-wide one by default; the deploy window passes its own)."""
        token_file = token_file or self.cfg.secrets_token_file
        if not token_file:
            raise SecretsError("secrets_token_file is not configured")
        try:
            token = open(token_file).read().strip()
        except OSError as e:
            raise SecretsError(f"cannot read the 1Password service account token: {e}") from e
        proc = subprocess.run(
            [self.cfg.op, "read", "--no-newline", ref],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": token},
        )
        if proc.returncode != 0:
            raise SecretsError(f"op read {ref}: {proc.stderr.strip()}")
        return proc.stdout

    def refresh(self) -> list[str]:
        """Fetch every configured reference and rewrite the cache atomically.
        One failure leaves the old cache in place and raises."""
        if not self.configured:
            return []
        values: dict[str, str] = {}
        for key, ref in sorted(self.cfg.secrets_env.items()):
            values[key] = self.read(ref)
        os.makedirs(self.cfg.secrets_cache, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.cfg.secrets_cache, prefix=".env-")
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w") as f:
                f.write("# orchard: pushed into every workspace at start; `dev secrets refresh` rewrites it\n")
                for key, value in values.items():
                    f.write(f"{key}={shlex.quote(value)}\n")
            os.replace(tmp, self.env_file())
        except BaseException:
            os.unlink(tmp)
            raise
        self.log(f"secrets cache refreshed: {', '.join(values)}")
        return list(values)
