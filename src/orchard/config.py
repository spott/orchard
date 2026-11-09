"""Runtime configuration, written by the NixOS module as JSON."""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass, field

from . import OrchardError

DEFAULT_PATH = "/etc/orchard/config.json"
# Repository permissions for grants that name none: enough to push, open
# pull requests and edit workflows; nothing administrative.
DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write", "workflows": "write", "metadata": "read"}

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
    # ssh user CA (OpenSSH private key on the host): `dev token ssh` signs a
    # short-lived certificate for the principals in a workspace's `ssh`
    # grants and pushes key + certificate into its tmpfs. Empty = off.
    ssh_ca_key_file: str = ""
    ssh_cert_minutes: int = 60
    # Deploy window. While a workspace holds an active `ssh` grant for
    # deploy_principal (which must carry an expiry of at most
    # deploy_max_hours), the host reads deploy_env (env var -> op://
    # reference) with the service account token in deploy_token_file — a
    # second, wider account than the one behind secrets_env — and pushes
    # them, plus that token as OP_SERVICE_ACCOUNT_TOKEN, into
    # <secrets_path>/deploy.env. The file is removed when the grant goes
    # (`dev sweep`, a timer). Empty token file = off.
    deploy_token_file: str = ""
    deploy_env: dict[str, str] = field(default_factory=dict)
    deploy_principal: str = "deploy"
    deploy_max_hours: int = 4
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
    # Host-side state that is not in Incus: minted tokens, cached lists.
    state_dir: str = "/var/lib/orchard"
    # GitHub App: orchard mints per-workspace installation tokens narrowed to
    # the repositories in the workspace's grants. Empty app id = off.
    github_app_id: str = ""
    github_key_file: str = ""
    github_owner: str = ""
    github_api: str = "https://api.github.com"
    # What a grant means when it names no permissions (a `name.repo`
    # create, a bare `dev grant`). Empty = the App's full set.
    github_default_permissions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PERMISSIONS))
    openssl: str = "openssl"
    # Secrets pushed into every workspace's tmpfs at start: environment
    # variable -> `op://` reference, read with the 1Password service account
    # token in secrets_token_file, cached under secrets_cache.
    secrets_env: dict[str, str] = field(default_factory=dict)
    secrets_token_file: str = ""
    secrets_cache: str = "/run/orchard/secrets"
    op: str = "op"
    # One custom volume attached to every workspace (idmapped, read-write) for
    # state that all of them share, such as agent logins. Empty = off.
    logins_volume: str = ""
    logins_size: str = "1GiB"

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

    def secrets_path(self) -> str:
        """Where the secrets land inside a workspace: the user's runtime tmpfs."""
        return f"/run/user/{self.uid}/orchard"

    def logins_path(self) -> str:
        return f"/home/{self.user}/.orchard/logins"

    def github_enabled(self) -> bool:
        return bool(self.github_app_id and self.github_key_file and self.github_owner)

    def ssh_ca_enabled(self) -> bool:
        return bool(self.ssh_ca_key_file)

    def deploy_enabled(self) -> bool:
        return bool(self.ssh_ca_key_file and self.deploy_token_file)

    def ssh_cert_dir(self) -> str:
        """Where a workspace's ssh certificate lands: next to the secrets env."""
        return f"{self.secrets_path()}/ssh"
