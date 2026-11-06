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
    # Public keys the front door treats as the operator. Anything else that
    # authenticates is a workspace's self-service key or is refused.
    operator_keys: list[str] = field(default_factory=list)
    # Static addresses: every workspace gets a DHCP reservation from this
    # inclusive range (its `ipv4.address` on eth0), so its name has an answer
    # while it is stopped. Empty = dynamic leases only.
    address_start: str = ""
    address_end: str = ""
    # Self-service from inside a workspace (`dev rename|rm|stop` in the
    # container): the host address the container's `dev` client sshes to
    # (the Incus bridge), and the host key it pins. Empty = not installed.
    self_host: str = ""
    self_hostkey_file: str = "/etc/ssh/ssh_host_ed25519_key.pub"
    ssh_keygen: str = "ssh-keygen"
    # The wake resolver (`orchard-dns`): a lookup of `<name>.<zone>` starts
    # a stopped workspace, or creates a missing one when dns_create is set.
    dns_listen: str = ""
    dns_port: int = 53
    dns_zone: str = ""
    dns_reverse_zone: str = ""
    dns_upstream: str = ""
    dns_upstream_port: int = 53
    dns_ttl: int = 5
    dns_create: bool = True
    # `gc` also removes workspaces that a lookup created (origin "dns") and
    # that nobody has logged into after this many hours: typos.
    untouched_hours: int = 24

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
