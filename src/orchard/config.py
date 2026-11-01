"""Runtime configuration, written by the NixOS module as JSON."""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field

from . import OrchardError

DEFAULT_PATH = "/etc/orchard/config.json"
ENV_VAR = "ORCHARD_CONFIG"


class ConfigError(OrchardError):
    pass


@dataclass
class Config:
    # The account inside every workspace that ssh lands on; the work volume
    # is mounted at its ~/work and owned by uid/gid.
    user: str = "spott"
    uid: int = 1000
    gid: int = 100
    # Incus side.
    incus: str = "incus"
    pool: str = "default"
    image: str = "workspace"
    profiles: list[str] = field(default_factory=lambda: ["default"])
    volume_size: str = "50GiB"
    # `ssh foo.orchard` -> workspace `foo`. Longest suffix wins.
    suffixes: list[str] = field(default_factory=lambda: [".orchard"])
    fork_limit: int = 5
    rm_grace_days: int = 7
    ssh_wait_seconds: int = 120
    lock_dir: str = "/run/orchard"
    # event name -> executable path; see README "Hooks".
    hooks: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.environ.get(ENV_VAR, DEFAULT_PATH)
        try:
            with open(path) as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"cannot read {path}: {e}") from e
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(f"{path}: unknown keys {', '.join(unknown)}")
        return cls(**data)

    def work_path(self) -> str:
        return f"/home/{self.user}/work"
