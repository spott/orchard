"""Workspaces: an Incus instance named after the workspace, a custom volume
`ws-<name>` mounted at ~/work, and `user.orchard.*` config keys as the only
metadata. There is no database; `incus list` is the source of truth.

Names: the Manager speaks workspace names (`foo`, `blah.iac`); every call
into Incus uses the instance name (`names.instance`: `blah--iac`), which is
what `Workspace.instance` holds."""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import re
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from . import OrchardError, locks, names
from .config import Config
from .github import GitHub
from .incus import Incus, IncusError
from .secrets import ENV_FILE, Secrets

KEY = "user.orchard."
WORK_DEVICE = "work"
LOGINS_DEVICE = "logins"
NIC_DEVICE = "eth0"
GRANT_KINDS = {"github", "ssh"}
# An ssh certificate principal (the role a grant conveys: ro, printers, deploy).
PRINCIPAL_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
SSH_KEY_NAME = "id_ed25519"
# How long `dev` waits for the workspace user's runtime tmpfs (/run/user/<uid>,
# made by logind for the lingering user) before pushing secrets into it.
RUNTIME_DIR_TRIES = 40
RUNTIME_DIR_WAIT = 0.5
GIT_CREDENTIALS = "git-credentials"
STAMP = "%Y%m%dT%H%M%SZ"
SNAPSHOT_PREFIX = "pre-rebase-"
# A rename may hit EBUSY while Incus still has the instance mounted: retry.
RENAME_TRIES = 60
RENAME_WAIT = 1.0

# What `rebase` carries from the old instance to its replacement so the
# workspace keeps its ssh identity (host keys, machine-id) and its
# self-service key. (root-owned, mode) per path; the user's files are below.
IDENTITY_FILES = {
    "/etc/machine-id": "0444",
    "/etc/ssh/ssh_host_ed25519_key": "0600",
    "/etc/ssh/ssh_host_ed25519_key.pub": "0644",
    "/etc/ssh/ssh_host_rsa_key": "0600",
    "/etc/ssh/ssh_host_rsa_key.pub": "0644",
    "/etc/ssh/ssh_host_ecdsa_key": "0600",
    "/etc/ssh/ssh_host_ecdsa_key.pub": "0644",
}
USER_IDENTITY_FILES = {".orchard/self.key": "0600", ".orchard/known_hosts": "0644"}

# `update`: re-lock every project flake under ~/work to the image's pinned
# nixpkgs (the registry inside the image points `nixpkgs`/`nixpkgs-stable`
# at the exact revs). One line per input touched: `ok|failed <dir> <input>`.
UPDATE_SCRIPT = r"""
set -u
cd "$HOME/work" 2>/dev/null || exit 0
status=0
for d in */; do
  [ -f "$d/flake.nix" ] || continue
  for input in nixpkgs nixpkgs-stable; do
    grep -q "\"$input\": {" "$d/flake.lock" 2>/dev/null || continue
    if (cd "$d" && nix flake update "$input" >/dev/null 2>&1); then
      echo "ok ${d%/} $input"
    else
      echo "failed ${d%/} $input"; status=1
    fi
  done
done
exit $status
"""

# Installed into every workspace as ~/.local/bin/dev: the self-service client.
# It sshes to the host as the jump user with a per-workspace key; the host
# recognises the key and lets it act on this workspace only.
SELF_CLIENT = """#!/bin/sh
# orchard: manage THIS workspace from inside it (rename, rm, stop, status, ls).
# Installed by the host; the key in ~/.orchard only speaks for this workspace.
d="$HOME/.orchard"
# -F /dev/null: the user's own ssh config must not apply (ControlPath,
# ProxyCommand, IdentityAgent, ...); this connection is fully specified here.
exec ssh -F /dev/null -o LogLevel=ERROR -o BatchMode=yes -o IdentitiesOnly=yes -i "$d/self.key" \
  -o UserKnownHostsFile="$d/known_hosts" -o StrictHostKeyChecking=yes \
  "dev@{host}" dev "$@"
"""


class NotManaged(OrchardError):
    def __init__(self, name: str) -> None:
        super().__init__(f"instance {name!r} exists but is not an orchard workspace")


class NotFound(OrchardError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no workspace named {name!r}")


class NotReady(OrchardError):
    pass


class AlreadyExists(OrchardError):
    def __init__(self, name: str) -> None:
        super().__init__(f"a workspace named {name!r} already exists")


class NoAddress(OrchardError):
    def __init__(self) -> None:
        super().__init__("no free address left in the workspace address range")


class ForkLimit(OrchardError):
    def __init__(self, parent: str, limit: int) -> None:
        super().__init__(f"{parent!r} already has {limit} forks (fork_limit)")


class NoPrevious(OrchardError):
    def __init__(self, name: str) -> None:
        super().__init__(f"{name!r} has no previous instance to roll back to")


@dataclass
class Workspace:
    name: str
    status: str
    ip: str | None
    parent: str | None
    repo: str | None
    agent: str | None
    created: str | None
    image: str | None
    volume: str | None
    # The DHCP reservation (eth0 `ipv4.address`): the answer for the name
    # while the workspace is stopped. None until one is assigned.
    address: str | None = None
    # "dns" when a name lookup created it, "front-door" or "cli" otherwise.
    origin: str | None = None
    # Public half of the workspace's self-service key, if installed.
    selfkey: str | None = None
    # Full fingerprint of the image the instance was made from (`image` is
    # its short form); forks are made from it, rebase compares against it.
    fingerprint: str | None = None
    # When `rebase` last replaced the instance, if ever.
    rebased: str | None = None
    # When `gc` first saw someone logged into this lookup-created workspace.
    # Login evidence lives in the instance's journal, which a rebase does not
    # carry over; this stamp is metadata, so it does.
    used: str | None = None
    # The Incus instance name (`blah--iac` for workspace `blah.iac`).
    instance: str = ""
    # What the workspace may reach, as `dev grant` recorded it: one dict per
    # grant (kind, value, permissions, expires, by, at). Tokens are minted
    # from this list, never stored in it.
    grants: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    # Result of the clone `create` did for a `name.repo` workspace, if any.
    clone: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "Running"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def repos(self, now: datetime | None = None) -> list[str]:
        """`owner/repo` of every unexpired github grant."""
        return [g["value"] for g in active_grants(self.grants, now) if g.get("kind") == "github"]


def parse_grants(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [g for g in data if isinstance(g, dict) and g.get("kind") and g.get("value")] if isinstance(data, list) else []


def active_grants(grants: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    out = []
    for g in grants:
        exp = g.get("expires")
        if exp:
            when = datetime.fromisoformat(exp)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= now:
                continue
        out.append(g)
    return out


def _ip(inst: dict[str, Any]) -> str | None:
    net = ((inst.get("state") or {}).get("network") or {}).get("eth0") or {}
    for addr in net.get("addresses", []):
        if addr.get("family") == "inet" and addr.get("scope") == "global":
            return addr["address"]
    return None


def _nic(inst: dict[str, Any]) -> dict[str, Any]:
    for key in ("devices", "expanded_devices"):
        nic = (inst.get(key) or {}).get(NIC_DEVICE)
        if nic and nic.get("ipv4.address"):
            return nic
    return {}


def _fresh_mac() -> str:
    """A random locally administered MAC (02:...), outside Incus's own 10:66:6a range."""
    return "02:" + ":".join(f"{b:02x}" for b in os.urandom(5))


def from_instance(inst: dict[str, Any]) -> Workspace:
    cfg = inst.get("config") or {}
    work = (inst.get("devices") or {}).get(WORK_DEVICE) or {}
    image = cfg.get("volatile.base_image")
    return Workspace(
        name=names.display(inst["name"]),
        status=inst.get("status", "Unknown"),
        ip=_ip(inst),
        parent=cfg.get(KEY + "parent"),
        repo=cfg.get(KEY + "repo"),
        agent=cfg.get(KEY + "agent"),
        created=cfg.get(KEY + "created"),
        image=image[:12] if image else None,
        volume=work.get("source"),
        address=_nic(inst).get("ipv4.address") or None,
        origin=cfg.get(KEY + "origin"),
        selfkey=cfg.get(KEY + "selfkey"),
        fingerprint=image or None,
        rebased=cfg.get(KEY + "rebased"),
        used=cfg.get(KEY + "used"),
        instance=inst["name"],
        grants=parse_grants(cfg.get(KEY + "grants")),
        clone=cfg.get(KEY + "clone"),
    )


def _managed(inst: dict[str, Any]) -> bool:
    return (inst.get("config") or {}).get(KEY + "managed") == "true"


def _tcp_open(ip: str, port: int) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=2):
            return True
    except OSError:
        return False


class Manager:
    """All verbs. Mutating ones take the per-name lock; all are idempotent."""

    def __init__(
        self,
        cfg: Config,
        inc: Incus,
        log: Callable[[str], None] | None = None,
        probe: Callable[[str, int], bool] = _tcp_open,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        github: GitHub | None = None,
        secrets: Secrets | None = None,
    ) -> None:
        self.cfg = cfg
        self.inc = inc
        self.log = log or (lambda msg: print(f"orchard: {msg}", file=sys.stderr, flush=True))
        self.probe = probe
        self.sleep = sleep
        self.now = now
        self.github = github or (GitHub(cfg, log=self.log) if cfg.github_enabled() else None)
        self.secrets = secrets or Secrets(cfg, log=self.log)
        self._warned_no_cache = False

    # -- read side ----------------------------------------------------------

    def list(self) -> list[Workspace]:
        return sorted((from_instance(i) for i in self.inc.instances() if _managed(i)), key=lambda w: w.name)

    def get(self, name: str) -> Workspace | None:
        inst = self.inc.instance(names.instance(name))
        if inst is None:
            return None
        if not _managed(inst):
            raise NotManaged(name)
        return from_instance(inst)

    def require(self, name: str) -> Workspace:
        ws = self.get(name)
        if ws is None:
            raise NotFound(name)
        return ws

    # -- lifecycle ----------------------------------------------------------

    def create(
        self,
        name: str,
        *,
        repo: str | None = None,
        agent: str | None = None,
        image: str | None = None,
        origin: str = "cli",
        address: str | None = None,
    ) -> Workspace:
        names.validate(name)
        with locks.held(self.cfg.lock_dir, name):
            existing = self.get(name)
            if existing is not None:
                return self._ensure_started(existing)
            return self._create_locked(
                name, repo=repo, agent=agent, image=image or self.cfg.image, origin=origin, address=address
            )

    def ensure_running(self, name: str, *, origin: str = "front-door", address: str | None = None) -> Workspace:
        """The front-door path: create if missing, start if stopped, wait for sshd."""
        names.validate(name)
        with locks.held(self.cfg.lock_dir, name):
            ws = self.get(name)
            if ws is None:
                return self._create_locked(name, repo=None, agent=None, image=self.cfg.image, origin=origin, address=address)
            return self._ensure_started(ws)

    def start(self, name: str) -> Workspace:
        with locks.held(self.cfg.lock_dir, name):
            return self._ensure_started(self.require(name))

    def stop(self, name: str, force: bool = False) -> Workspace:
        with locks.held(self.cfg.lock_dir, name):
            ws = self.require(name)
            if ws.status != "Stopped":
                # Pin the address it has now, so its name keeps answering.
                self.reserve_address(ws)
                self.log(f"stopping {name}")
                self.inc.stop(ws.instance, force=force)
            return self.require(name)

    def restart(self, name: str) -> Workspace:
        self.stop(name)
        return self.start(name)

    def rename(self, old: str, new: str) -> Workspace:
        """Rename a workspace and its volume. Incus only renames stopped
        instances, so a running one is stopped and started again (its ssh
        sessions end; reconnect under the new name). The address, origin and
        self-service key move with it."""
        names.validate(old)
        names.validate(new)
        if old == new:
            return self.require(old)
        first, second = sorted([old, new])
        with locks.held(self.cfg.lock_dir, first), locks.held(self.cfg.lock_dir, second):
            ws = self.require(old)
            new_inst = names.instance(new)
            if self.inc.instance(new_inst) is not None:
                raise AlreadyExists(new)
            if self.inc.volume(self.cfg.pool, names.volume(new)) is not None:
                raise AlreadyExists(names.volume(new))
            if ws.running:
                self.reserve_address(ws)
                self.log(f"stopping {old}")
                self.inc.stop(ws.instance, force=True)
            self.log(f"renaming {old} to {new}")
            self.inc.rename(ws.instance, new_inst)
            if ws.volume == names.volume(old):
                self.inc.volume_rename(self.cfg.pool, ws.volume, names.volume(new))
                self.inc.device_set(new_inst, WORK_DEVICE, source=names.volume(new))
            if ws.running:
                return self._ensure_started(self.require(new))
            return self.require(new)

    def rm(self, name: str, *, now: bool = False, tree: bool = False) -> list[str]:
        """Delete the instance. The volume is renamed `trash-<name>-<stamp>`
        and reaped by `gc` after the grace period, or deleted at once with now=True.
        Returns the names removed (descendants first when tree=True)."""
        removed: list[str] = []
        if tree:
            for child in self.list():
                if child.parent == name:
                    removed += self.rm(child.name, now=now, tree=True)
        with locks.held(self.cfg.lock_dir, name):
            ws = self.get(name)
            if ws is None:
                return removed
            if ws.running:
                self.log(f"stopping {name}")
                self.inc.stop(ws.instance, force=True)
            for old in self.old_instances(name):
                self.log(f"deleting parked {old}")
                self.inc.delete(old, force=True)
                removed.append(old)
            self.log(f"deleting {name}")
            self.inc.delete(ws.instance, force=True)
            self._revoke_tokens(name)
            vol = ws.volume or names.volume(name)
            if self.inc.volume(self.cfg.pool, vol) is not None:
                if now:
                    self.inc.volume_delete(self.cfg.pool, vol)
                else:
                    stamp = self.now().strftime(STAMP)
                    trash = names.trash_volume(name, stamp)
                    self.inc.volume_rename(self.cfg.pool, vol, trash)
                    self.inc.volume_set(
                        self.cfg.pool, trash,
                        {KEY + "trashed": self.now().isoformat(timespec="seconds"), KEY + "name": name},
                    )
                    self.log(f"volume {vol} kept as {trash} for {self.cfg.rm_grace_days} days")
        removed.append(name)
        return removed

    def gc(self, dry_run: bool = False) -> list[str]:
        """Delete trashed volumes older than the grace period, and remove
        (to trash) workspaces a name lookup created that nobody has logged
        into within untouched_hours: a typo in a host name."""
        reaped: list[str] = []
        stale = self.now() - timedelta(hours=self.cfg.untouched_hours)
        for ws in self.list():
            if ws.origin != "dns" or not ws.running or not ws.created:
                continue
            created = datetime.fromisoformat(ws.created)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if ws.used:
                continue
            if self.logged_in(ws):
                # Remember it in metadata the moment it is seen, however
                # young the workspace: the journal that proved it is on the
                # instance's rootfs and does not survive a rebase, which can
                # happen before the workspace is old enough to judge.
                self.inc.config_set(ws.instance, {KEY + "used": self.now().isoformat(timespec="seconds")})
                continue
            if created > stale:
                continue
            self.log(f"{ws.name}: created by a lookup {ws.created}, never logged into")
            if not dry_run:
                self.rm(ws.name)
            reaped.append(ws.name)
        cutoff = self.now() - timedelta(days=self.cfg.rm_grace_days)
        for vol in self.inc.volumes(self.cfg.pool):
            if not vol["name"].startswith(names.TRASH_PREFIX):
                continue
            trashed = (vol.get("config") or {}).get(KEY + "trashed")
            if trashed is None:
                continue
            when = datetime.fromisoformat(trashed)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when <= cutoff:
                if not dry_run:
                    self.inc.volume_delete(self.cfg.pool, vol["name"])
                reaped.append(vol["name"])
        return reaped

    def logged_in(self, ws: Workspace) -> bool:
        """Has anyone ever authenticated to this (running) workspace's sshd?
        Both the front door and direct ssh/mosh end up as an `Accepted`
        line in the container's journal. Only this instance's journal: see
        `Workspace.used` for what carries across a rebase."""
        if not ws.running:
            return True
        proc = self.inc.exec(ws.instance, "journalctl", "-q", "-g", "Accepted publickey", check=False)
        return proc.returncode == 0 and proc.stdout.strip() != ""

    # -- addresses ----------------------------------------------------------

    def address_range(self) -> tuple[ipaddress.IPv4Address, ipaddress.IPv4Address] | None:
        if not self.cfg.address_start or not self.cfg.address_end:
            return None
        return ipaddress.IPv4Address(self.cfg.address_start), ipaddress.IPv4Address(self.cfg.address_end)

    def used_addresses(self) -> set[str]:
        """Every address any instance holds or has reserved, managed or not."""
        used: set[str] = set()
        for inst in self.inc.instances():
            for addr in (_ip(inst), _nic(inst).get("ipv4.address")):
                if addr:
                    used.add(addr)
        return used

    def allocate_address(self, exclude: Iterable[str] = ()) -> str | None:
        """The lowest free address in the range; None when no range is set."""
        rng = self.address_range()
        if rng is None:
            return None
        taken = self.used_addresses() | set(exclude)
        lo, hi = rng
        addr = lo
        while addr <= hi:
            if str(addr) not in taken:
                return str(addr)
            addr += 1
        raise NoAddress()

    def reserve_address(self, ws: Workspace) -> str | None:
        """Give the workspace a DHCP reservation if it has none: the address
        it is using now, or a fresh one from the range."""
        if ws.address:
            return ws.address
        addr = ws.ip if ws.ip and self._in_range(ws.ip) else self.allocate_address()
        if addr is None:
            return None
        self.log(f"reserving {addr} for {ws.name}")
        self.inc.device_override(ws.instance, NIC_DEVICE, **{"ipv4.address": addr})
        ws.address = addr
        return addr

    def _in_range(self, ip: str) -> bool:
        rng = self.address_range()
        if rng is None:
            return False
        return rng[0] <= ipaddress.IPv4Address(ip) <= rng[1]

    def find_by_address(self, ip: str) -> Workspace | None:
        for ws in self.list():
            if ip in (ws.ip, ws.address):
                return ws
        return None

    def find_by_selfkey(self, pubkey: str) -> Workspace | None:
        """The workspace whose self-service key this is (type and base64
        compared, comment ignored)."""
        want = pubkey.split()[:2]
        if len(want) != 2:
            return None
        for ws in self.list():
            if ws.selfkey and ws.selfkey.split()[:2] == want:
                return ws
        return None

    # -- fork / rebase / update ---------------------------------------------

    def fork(self, parent: str, child: str | None = None) -> Workspace:
        """Copy a workspace: its volume (stopped for the copy, so databases
        on it are consistent) under a new instance made from the parent's
        own image, with `user.orchard.parent` set. Identity is fresh: new
        host keys, new self-service key, new address. The parent is started
        again before the child is built, so it is down for the copy only."""
        names.validate(parent)
        cfg = self.cfg
        with locks.held(cfg.lock_dir, parent):
            ws = self.require(parent)
            if child is None:
                child = self.next_child_name(parent)
            names.validate(child)
            if child == parent:
                raise AlreadyExists(child)
            kids = [w for w in self.list() if w.parent == parent]
            if len(kids) >= cfg.fork_limit:
                raise ForkLimit(parent, cfg.fork_limit)
            with locks.held(cfg.lock_dir, child):
                if self.inc.instance(names.instance(child)) is not None:
                    raise AlreadyExists(child)
                src = ws.volume or names.volume(parent)
                dst = names.volume(child)
                if self.inc.volume(cfg.pool, dst) is not None:
                    raise AlreadyExists(dst)
                if ws.running:
                    self.reserve_address(ws)
                    self.log(f"stopping {parent} for a consistent copy")
                    self.inc.stop(ws.instance)
                try:
                    self.log(f"copying volume {src} to {dst}")
                    self.inc.volume_copy(cfg.pool, src, dst)
                finally:
                    if ws.running:
                        self._ensure_started(self.require(parent))
                try:
                    # Forks inherit the parent's scope (its grants), not its tokens.
                    return self._create_locked(
                        child, repo=ws.repo, agent=None, image=ws.fingerprint or cfg.image, origin="fork", parent=parent,
                        grants=ws.grants,
                    )
                except OrchardError:
                    # _create_locked keeps volumes it did not make; this one is ours.
                    try:
                        self.inc.volume_delete(cfg.pool, dst)
                    except IncusError:
                        pass
                    raise

    def next_child_name(self, parent: str) -> str:
        """`<parent>-N` for the lowest N whose instance and volume are both free."""
        taken = {i["name"] for i in self.inc.instances()} | {v["name"] for v in self.inc.volumes(self.cfg.pool)}
        n = 1
        while names.instance(names.child(parent, n)) in taken or names.volume(names.child(parent, n)) in taken:
            n += 1
        return names.child(parent, n)

    def rebase(self, name: str, image: str | None = None) -> tuple[Workspace, str | None]:
        """Move a workspace onto another image, keeping its volume, address,
        metadata and identity (host keys, machine-id, self-service key). The
        volume gets a `pre-rebase-<stamp>` snapshot first; the old instance is
        kept, stopped and unmanaged, as `<name>-old-<stamp>` for `--rollback`
        until `--prune`. Returns (workspace, old instance name or None if
        nothing was done)."""
        cfg = self.cfg
        with locks.held(cfg.lock_dir, name):
            ws = self.require(name)
            ref = image or cfg.image
            target = self.inc.image_fingerprint(ref)
            if target is None:
                raise OrchardError(f"no image {ref!r}")
            if ws.fingerprint == target:
                self.log(f"{name} is already on image {target[:12]}")
                return ws, None
            stamp = self.now().strftime(STAMP)
            vol = ws.volume or names.volume(name)
            inst_name = ws.instance
            tmp = tempfile.mkdtemp(prefix="orchard-rebase-")
            try:
                # Pull the identity BEFORE stopping: a file pull from a stopped
                # instance mounts its rootfs on the host and Incus leaves that
                # mount behind for a while (~15 s on zfs), which makes the
                # rename below fail with EBUSY. From a running instance the
                # pull goes through the container and leaves nothing mounted.
                identity = self._pull_identity(inst_name, tmp)
                if ws.running:
                    self.reserve_address(ws)
                    self.log(f"stopping {name}")
                    self.inc.stop(inst_name)
                snap = SNAPSHOT_PREFIX + stamp.lower()
                self.log(f"snapshotting {vol} as {snap}")
                self.inc.volume_snapshot(cfg.pool, vol, snap)
                inst = self.inc.instance(inst_name) or {}
                meta = {k: v for k, v in (inst.get("config") or {}).items() if k.startswith(KEY)}
                meta[KEY + "rebased"] = self.now().isoformat(timespec="seconds")
                # The MAC is identity too: dnsmasq hands the reserved address
                # to the MAC holding its lease, and a force-stopped instance
                # (rollback) never releases one.
                hwaddr = _nic(inst).get("hwaddr") or (inst.get("config") or {}).get(f"volatile.{NIC_DEVICE}.hwaddr")
                old = self._free_old_name(name, stamp)
                self.log(f"keeping the old instance as {old}")
                # Rename first, with the volume still attached: until this
                # succeeds nothing about the workspace has changed.
                self._rename_settled(inst_name, old)
                try:
                    self.inc.config_set(old, {KEY + "managed": "false", KEY + "old-of": name})
                    self.inc.device_remove(old, WORK_DEVICE)
                    # Incus refuses one static address or MAC on two NICs, even
                    # with the other instance stopped: the parked one lets go.
                    self._release_nic(old, ws.address, hwaddr)
                    self.log(f"creating {name} from image {target[:12]}")
                    self.inc.init(target, inst_name, cfg.profiles, meta)
                    self.inc.device_add(inst_name, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path())
                    self._attach_logins(inst_name)
                    self._claim_nic(inst_name, ws.address, hwaddr)
                    self._push_identity(inst_name, identity)
                except OrchardError:
                    self.log(f"rebase failed, putting {old} back as {name}")
                    try:
                        self.inc.delete(inst_name, force=True)
                    except IncusError:
                        pass
                    self._rename_settled(old, inst_name)
                    self.inc.config_set(inst_name, {KEY + "managed": "true"})
                    self.inc.config_unset(inst_name, KEY + "old-of")
                    if WORK_DEVICE not in ((self.inc.instance(inst_name) or {}).get("devices") or {}):
                        self.inc.device_add(inst_name, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path())
                    self._claim_nic(inst_name, ws.address, hwaddr)
                    raise
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            # Always start: a rebase that does not boot is a failed rebase.
            new = self._ensure_started(self.require(name))
            self.log(f"{name} is on image {target[:12]}; `dev rebase --rollback {name}` returns to {old}, `--prune` drops it")
            return new, old

    def rebase_rollback(self, name: str, restore: bool = False) -> tuple[Workspace, str]:
        """Swap the newest `<name>-old-*` instance back in; the rebased
        instance is parked the same way (prune removes both). With restore,
        the volume is reset to the newest pre-rebase snapshot as well."""
        cfg = self.cfg
        with locks.held(cfg.lock_dir, name):
            cur = self.require(name)
            olds = self.old_instances(name)
            if not olds:
                raise NoPrevious(name)
            previous = olds[-1]
            stamp = self.now().strftime(STAMP)
            vol = cur.volume or names.volume(name)
            inst_name = cur.instance
            if cur.running:
                self.log(f"stopping {name}")
                self.inc.stop(inst_name, force=True)
            parked = self._free_old_name(name, stamp)
            cur_inst = self.inc.instance(inst_name) or {}
            addr = cur.address
            hwaddr = _nic(cur_inst).get("hwaddr") or (cur_inst.get("config") or {}).get(f"volatile.{NIC_DEVICE}.hwaddr")
            self.inc.device_remove(inst_name, WORK_DEVICE)
            self._release_nic(inst_name, addr, hwaddr)
            self._rename_settled(inst_name, parked)
            self.inc.config_set(parked, {KEY + "managed": "false", KEY + "old-of": name})
            self.log(f"restoring {previous} as {name} (the rebased instance is kept as {parked})")
            self._rename_settled(previous, inst_name)
            self.inc.config_set(inst_name, {KEY + "managed": "true"})
            self.inc.config_unset(inst_name, KEY + "old-of")
            self.inc.device_add(inst_name, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path())
            self._attach_logins(inst_name)
            self._claim_nic(inst_name, addr, hwaddr)
            if restore:
                snaps = sorted(s for s in self.inc.volume_snapshots(cfg.pool, vol) if s.startswith(SNAPSHOT_PREFIX))
                if snaps:
                    self.log(f"restoring {vol} from snapshot {snaps[-1]}")
                    self.inc.volume_snapshot_restore(cfg.pool, vol, snaps[-1])
                else:
                    self.log(f"warning: {vol} has no pre-rebase snapshot to restore")
            return self._ensure_started(self.require(name)), parked

    def rebase_prune(self, name: str) -> list[str]:
        """Delete every parked instance of `name` and its pre-rebase snapshots."""
        cfg = self.cfg
        with locks.held(cfg.lock_dir, name):
            ws = self.require(name)
            removed: list[str] = []
            for old in self.old_instances(name):
                self.log(f"deleting {old}")
                self.inc.delete(old, force=True)
                removed.append(old)
            vol = ws.volume or names.volume(name)
            for snap in self.inc.volume_snapshots(cfg.pool, vol):
                if snap.startswith(SNAPSHOT_PREFIX):
                    self.inc.volume_snapshot_delete(cfg.pool, vol, snap)
                    removed.append(f"{vol}/{snap}")
            return removed

    def _release_nic(self, name: str, addr: str | None, hwaddr: str | None) -> None:
        """Take the reserved address and the MAC off an instance being parked
        so another instance may hold them (Incus validates both per network).
        The MAC is *replaced*, not unset: a device update is what makes Incus
        rewrite the instance's dnsmasq host entry, and dnsmasq honours the
        last entry it read for a MAC, so a stale one would steal the lease."""
        inst = self.inc.instance(name) or {}
        nic = (inst.get("devices") or {}).get(NIC_DEVICE) or {}
        props: dict[str, str] = {}
        if hwaddr:
            props["hwaddr"] = _fresh_mac()
        if props:
            if NIC_DEVICE in (inst.get("devices") or {}):
                self.inc.device_set(name, NIC_DEVICE, **props)
            else:
                self.inc.device_override(name, NIC_DEVICE, **props)
        if addr and nic.get("ipv4.address"):
            self.inc.device_unset(name, NIC_DEVICE, "ipv4.address")

    def _claim_nic(self, name: str, addr: str | None, hwaddr: str | None) -> None:
        """Pin the address and MAC on the instance's NIC: `set` when the NIC is
        already an instance-level device, `override` when it still comes from
        the profile."""
        props = {k: v for k, v in (("ipv4.address", addr), ("hwaddr", hwaddr)) if v}
        if not props:
            return
        devices = (self.inc.instance(name) or {}).get("devices") or {}
        if NIC_DEVICE in devices:
            self.inc.device_set(name, NIC_DEVICE, **props)
        else:
            self.inc.device_override(name, NIC_DEVICE, **props)

    def _rename_settled(self, old: str, new: str) -> None:
        """`incus rename` that waits out the host-side mount Incus leaves
        behind after a file operation on a stopped instance (the rename
        fails with "device or resource busy" until it clears, ~15 s on zfs)."""
        for attempt in range(RENAME_TRIES):
            try:
                self.inc.rename(old, new)
                return
            except IncusError as e:
                if "resource busy" not in str(e).lower() or attempt == RENAME_TRIES - 1:
                    raise
                if attempt == 0:
                    self.log(f"{old} is still mounted on the host; waiting to rename it")
                self.sleep(RENAME_WAIT)

    def _free_old_name(self, name: str, stamp: str) -> str:
        """`<name>-old-<stamp>`, with a counter if that already exists (a
        rebase and its rollback inside the same second)."""
        taken = {i["name"] for i in self.inc.instances()}
        base = names.old_instance(name, stamp)
        candidate, n = base, 1
        while candidate in taken:
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def old_instances(self, name: str) -> list[str]:
        """Parked instances of `name`, oldest first (the stamp in the name sorts)."""
        return sorted(i["name"] for i in self.inc.instances() if (i.get("config") or {}).get(KEY + "old-of") == name)

    def update(self, only: list[str] | None = None) -> dict[str, list[str]]:
        """Re-lock each project flake under ~/work of the named (default: all
        running) workspaces to the image's pinned nixpkgs. Returns one line
        per (project, input) per workspace."""
        cfg = self.cfg
        targets = [self.require(n) for n in only] if only else [w for w in self.list() if w.running]
        report: dict[str, list[str]] = {}
        for ws in targets:
            if not ws.running:
                report[ws.name] = ["skipped: not running"]
                continue
            with locks.held(cfg.lock_dir, ws.name):
                self.log(f"updating project flakes in {ws.name}")
                proc = self.inc.exec(
                    ws.instance, "sh", "-lc", UPDATE_SCRIPT,
                    check=False, user=cfg.uid, group=cfg.gid, env={"HOME": f"/home/{cfg.user}"},
                )
                lines = proc.stdout.strip().splitlines()
                if not lines:
                    lines = ["nothing to update"] if proc.returncode == 0 else [f"failed: {proc.stderr.strip()}"]
                report[ws.name] = lines
        return report

    def _pull_identity(self, name: str, tmp: str) -> dict[str, tuple[str, int, int, str]]:
        """Copy the identity files out of a (stopped) instance. Returns
        {container path: (local path, uid, gid, mode)} for the ones present."""
        cfg = self.cfg
        found: dict[str, tuple[str, int, int, str]] = {}
        wanted = dict(IDENTITY_FILES)
        home = f"/home/{cfg.user}"
        for rel, mode in USER_IDENTITY_FILES.items():
            wanted[f"{home}/{rel}"] = mode
        for i, (path, mode) in enumerate(wanted.items()):
            local = os.path.join(tmp, f"id{i}")
            if self.inc.file_pull(name, path, local, check=False):
                owner = (cfg.uid, cfg.gid) if path.startswith(home + "/") else (0, 0)
                found[path] = (local, owner[0], owner[1], mode)
        return found

    def _push_identity(self, name: str, identity: dict[str, tuple[str, int, int, str]]) -> None:
        for path, (local, uid, gid, mode) in identity.items():
            self.inc.file_push(local, name, path, uid=uid, gid=gid, mode=mode)
        home = f"/home/{self.cfg.user}"
        if any(p.startswith(home + "/") for p in identity):
            self.inc.exec(name, "chown", f"{self.cfg.uid}:{self.cfg.gid}", f"{home}/.orchard", check=False)

    # -- internals ----------------------------------------------------------

    def _create_locked(
        self,
        name: str,
        *,
        repo: str | None,
        agent: str | None,
        image: str,
        origin: str = "cli",
        address: str | None = None,
        parent: str | None = None,
        grants: list[dict[str, Any]] | None = None,
    ) -> Workspace:
        cfg = self.cfg
        inst = names.instance(name)
        vol = names.volume(name)
        made_volume = False
        # `name.repo`, `--repo owner/repo`, or a bare name that is one of the
        # owner's repositories: the repository to clone and the first grant.
        # Anything else given as --repo is just recorded.
        clone_repo = self._repo_for(name, repo)
        if clone_repo and not repo:
            repo = f"https://github.com/{clone_repo}"
        elif clone_repo and repo and "/" not in repo.strip("/").split("/", 1)[-1] and "://" not in repo:
            repo = f"https://github.com/{clone_repo}"
        if grants is None:
            grants = []
            if clone_repo:
                # A typo in the repo part must not leave a workspace behind.
                if not self._repo_reachable(clone_repo):
                    raise OrchardError(f"no such repository {clone_repo} (the GitHub App cannot reach it)")
                grants.append(self._grant("github", clone_repo, by="create"))
        if address is None:
            address = self.allocate_address()
        if self.inc.volume(cfg.pool, vol) is None:
            self.log(f"creating volume {vol} ({cfg.volume_size})")
            self.inc.volume_create(
                cfg.pool, vol,
                size=cfg.volume_size,
                # security.shifted: idmapped mount, so host uid 1000 on the volume is
                # uid 1000 inside the unprivileged container (shift= is not allowed on
                # custom-volume disk devices).
                **{
                    "initial.uid": str(cfg.uid),
                    "initial.gid": str(cfg.gid),
                    "initial.mode": "0755",
                    "security.shifted": "true",
                },
            )
            made_volume = True
        else:
            self.log(f"adopting existing volume {vol}")
        meta = {
            KEY + "managed": "true",
            KEY + "created": self.now().isoformat(timespec="seconds"),
            KEY + "origin": origin,
        }
        if repo:
            meta[KEY + "repo"] = repo
        if agent:
            meta[KEY + "agent"] = agent
        if parent:
            meta[KEY + "parent"] = parent
        if grants:
            meta[KEY + "grants"] = json.dumps(grants, separators=(",", ":"))
        self.log(f"creating {name} from image {image}")
        try:
            self.inc.init(image, inst, cfg.profiles, meta)
            self.inc.device_add(inst, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path())
            self._attach_logins(inst)
            if address:
                self.inc.device_override(inst, NIC_DEVICE, **{"ipv4.address": address})
            self.inc.start(inst)
            ws = self._wait_ready(name)
        except OrchardError:
            # Leave nothing half-made behind; an adopted volume is never ours to drop.
            self.log(f"create failed, removing {name}")
            try:
                self.inc.delete(inst, force=True)
            except IncusError:
                pass
            if made_volume:
                try:
                    self.inc.volume_delete(cfg.pool, vol)
                except IncusError:
                    pass
            raise
        self._install_self(ws)
        self._push_secrets(ws)
        self._push_ssh_cert(ws)
        if clone_repo:
            self._clone(ws, clone_repo)
        self._hook("on_create", ws)
        return ws

    def _ensure_started(self, ws: Workspace) -> Workspace:
        if not ws.running:
            self.log(f"starting {ws.name}")
            self.inc.start(ws.instance)
        ws = self._wait_ready(ws.name)
        if ws.selfkey is None:
            # A workspace from before self-service existed: adopt it.
            self._install_self(ws)
        elif self.cfg.self_host:
            # Keep the client current with this version of dev.
            self._push_client(ws)
        # Both are idempotent and cheap: a workspace from before they existed
        # gets them the next time dev starts it, a running one on `dev start`.
        self._attach_logins(ws.instance)
        self._push_secrets(ws)
        self._push_ssh_cert(ws)
        return ws

    # -- grants and tokens ----------------------------------------------------

    def _grant(
        self, kind: str, value: str, *, by: str, permissions: dict[str, str] | None = None, expires: str | None = None
    ) -> dict[str, Any]:
        if kind not in GRANT_KINDS:
            raise OrchardError(f"unknown grant kind {kind!r} (one of: {', '.join(sorted(GRANT_KINDS))})")
        if kind == "github":
            value = self._full_repo(value)
        elif kind == "ssh":
            value = value.strip()
            if not PRINCIPAL_RE.fullmatch(value):
                raise OrchardError(f"{value!r}: an ssh principal is lowercase letters, digits and dashes (ro, printers, deploy)")
        return {
            "kind": kind, "value": value, "permissions": permissions or None, "expires": expires,
            "by": by, "at": self.now().isoformat(timespec="seconds"),
        }

    def _full_repo(self, value: str) -> str:
        """`repo` -> `<owner>/repo` with the configured owner; `owner/repo` as is."""
        value = value.strip().strip("/")
        if "/" not in value:
            if not self.cfg.github_owner:
                raise OrchardError(f"{value!r}: give the repository as owner/repo (no github owner is configured)")
            return f"{self.cfg.github_owner}/{value}"
        if value.count("/") != 1 or "://" in value:
            raise OrchardError(f"{value!r}: give the repository as owner/repo")
        return value

    def _repo_for(self, name: str, repo: str | None) -> str | None:
        """The repository a new workspace is about: `--repo owner/repo` (or a
        bare repo name), else the repo part of `name.repo`, else — for a bare
        `name` — `<owner>/name` when the App's installation is known to list
        it (so `iac` and `iac.orchard` at the front door clone iac; `foo` is a
        plain workspace unless the owner has a repository called foo). URLs
        recorded with --repo are left alone (None)."""
        if repo:
            if "://" in repo or repo.startswith("github:") or repo.count("/") > 1:
                return None
            try:
                return self._full_repo(repo)
            except OrchardError:
                return None
        if not self.cfg.github_owner:
            return None
        rep = names.repo(name)
        if rep:
            return f"{self.cfg.github_owner}/{rep}"
        # Fail closed, unlike `name.repo`: a bare name only becomes a clone
        # when the repository list is in hand and has it.
        full = f"{self.cfg.github_owner}/{name}"
        return full if self._repo_listed(full) is True else None

    def _set_grants(self, ws: Workspace, grants: list[dict[str, Any]]) -> Workspace:
        if grants:
            self.inc.config_set(ws.instance, {KEY + "grants": json.dumps(grants, separators=(",", ":"))})
        else:
            self.inc.config_unset(ws.instance, KEY + "grants")
        return self.require(ws.name)

    def grant(
        self, name: str, kind: str, value: str, *, permissions: dict[str, str] | None = None,
        expires: str | None = None, by: str = "operator",
    ) -> Workspace:
        """Add (or replace) a grant. Takes effect on the next token mint."""
        with locks.held(self.cfg.lock_dir, name):
            ws = self.require(name)
            new = self._grant(kind, value, by=by, permissions=permissions, expires=expires)
            if kind == "github" and not self._repo_reachable(new["value"]):
                raise OrchardError(f"no such repository {new['value']} (the GitHub App cannot reach it)")
            kept = [g for g in ws.grants if (g.get("kind"), g.get("value")) != (new["kind"], new["value"])]
            self.log(f"granting {name} {kind} {new['value']}" + (f" until {expires}" if expires else ""))
            ws = self._set_grants(ws, kept + [new])
            # A cached token may be wider or narrower than the new list.
            self._revoke_tokens(name)
            if kind == "ssh":
                # Takes effect now, not at the next connection's re-mint.
                self._push_ssh_cert(ws)
            return ws

    def revoke(self, name: str, kind: str, value: str) -> Workspace:
        """Drop a grant and revoke every token minted for the workspace."""
        with locks.held(self.cfg.lock_dir, name):
            ws = self.require(name)
            if kind == "github":
                value = self._full_repo(value)
            kept = [g for g in ws.grants if (g.get("kind"), g.get("value")) != (kind, value)]
            if len(kept) == len(ws.grants):
                raise OrchardError(f"{name} has no {kind} grant for {value!r}")
            self.log(f"revoking {kind} {value} from {name}")
            ws = self._set_grants(ws, kept)
            self._revoke_tokens(name)
            if kind == "ssh":
                # Drop the live certificate; a narrower one follows if grants remain.
                self._drop_ssh_cert(ws)
                self._push_ssh_cert(ws)
            return ws

    def token(self, name: str, kind: str = "github") -> dict[str, Any]:
        """A fresh (or cached, unexpired) token for the workspace, limited to
        its active grants of that kind."""
        if kind == "ssh":
            return self._mint_ssh(self.require(name))
        if kind != "github":
            raise OrchardError(f"unknown token kind {kind!r}")
        if self.github is None:
            raise OrchardError("GitHub tokens are not configured on this host (services.orchard.github)")
        ws = self.require(name)
        grants = [g for g in active_grants(ws.grants, self.now()) if g.get("kind") == "github"]
        if not grants:
            raise OrchardError(f"{name} has no active github grants")
        repos = sorted({g["value"] for g in grants})
        # A token has one permission set for all its repositories: the union
        # over the grants (write beats read). A grant that names none means
        # the configured default set; an empty default means the App's full set.
        permissions: dict[str, str] | None = {}
        for g in grants:
            perms = g.get("permissions") or self.cfg.github_default_permissions
            if not perms:
                permissions = None
                break
            for k, v in perms.items():
                permissions[k] = "write" if "write" in (v, permissions.get(k)) else v
        tok = self.github.token(name, repos, permissions)
        return {**tok, "workspace": name}

    # -- ssh certificates ---------------------------------------------------

    def _ssh_principals(self, ws: Workspace) -> list[str]:
        return sorted({g["value"] for g in active_grants(ws.grants, self.now()) if g.get("kind") == "ssh"})

    def _mint_ssh(self, ws: Workspace) -> dict[str, Any]:
        """Sign a fresh ed25519 key for the workspace's active ssh principals
        and push key + certificate into its runtime tmpfs. The private key is
        generated on the host, pushed once, and deleted here: it only ever
        speaks for this workspace, for `ssh_cert_minutes`. The identity
        `ws:<name>` is what sshd on the target logs."""
        cfg = self.cfg
        if not cfg.ssh_ca_enabled():
            raise OrchardError("ssh certificates are not configured on this host (services.orchard.sshCA)")
        principals = self._ssh_principals(ws)
        if not principals:
            raise OrchardError(f"{ws.name} has no active ssh grants")
        if not ws.running:
            raise OrchardError(f"{ws.name} is not running; a certificate lives in its runtime tmpfs")
        if not self._runtime_dir_ready(ws.instance):
            raise OrchardError(f"/run/user/{cfg.uid} never appeared in {ws.name}")
        identity = f"ws:{ws.name}"
        expires = self.now() + timedelta(minutes=cfg.ssh_cert_minutes)
        tmp = tempfile.mkdtemp(prefix="orchard-cert-")
        try:
            key = os.path.join(tmp, SSH_KEY_NAME)
            self._keygen(["-q", "-t", "ed25519", "-N", "", "-C", identity, "-f", key])
            self._keygen([
                "-q", "-s", cfg.ssh_ca_key_file, "-I", identity, "-n", ",".join(principals),
                "-V", f"+{cfg.ssh_cert_minutes}m", key + ".pub",
            ])
            cert = key + "-cert.pub"
            dst_dir = cfg.ssh_cert_dir()
            self.inc.file_push(key, ws.instance, f"{dst_dir}/{SSH_KEY_NAME}", uid=cfg.uid, gid=cfg.gid, mode="0600")
            self.inc.file_push(cert, ws.instance, f"{dst_dir}/{SSH_KEY_NAME}-cert.pub", uid=cfg.uid, gid=cfg.gid, mode="0644")
            self.inc.exec(ws.instance, "chown", f"{cfg.uid}:{cfg.gid}", cfg.secrets_path(), dst_dir, check=False)
            self.inc.exec(ws.instance, "chmod", "0700", cfg.secrets_path(), dst_dir, check=False)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.log(f"{ws.name}: ssh certificate for {', '.join(principals)} until {expires.isoformat(timespec='minutes')}")
        return {
            "workspace": ws.name, "principals": principals, "identity": identity,
            "expires": expires.isoformat(timespec="seconds"),
            "key": f"{dst_dir}/{SSH_KEY_NAME}", "certificate": f"{dst_dir}/{SSH_KEY_NAME}-cert.pub",
        }

    def _keygen(self, argv: list[str]) -> None:
        proc = subprocess.run(
            [self.cfg.ssh_keygen, *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            raise OrchardError(f"ssh-keygen failed: {proc.stderr.strip()}")

    def _push_ssh_cert(self, ws: Workspace) -> None:
        """At start (and on grant): a certificate for whatever ssh grants are
        active, if any. Never fails the start."""
        if not self.cfg.ssh_ca_enabled() or not ws.running or not self._ssh_principals(ws):
            return
        try:
            self._mint_ssh(ws)
        except (OrchardError, IncusError) as e:
            self.log(f"warning: no ssh certificate for {ws.name}: {e}")

    def _drop_ssh_cert(self, ws: Workspace) -> None:
        if not ws.running:
            return
        d = self.cfg.ssh_cert_dir()
        self.inc.exec(ws.instance, "rm", "-f", f"{d}/{SSH_KEY_NAME}", f"{d}/{SSH_KEY_NAME}-cert.pub", check=False)

    def _revoke_tokens(self, name: str) -> None:
        if self.github is None:
            return
        try:
            n = self.github.revoke(name)
            if n:
                self.log(f"revoked {n} token(s) of {name}")
        except OrchardError as e:
            self.log(f"warning: {e}")

    def repo_allowed(self, name: str) -> bool:
        """For a `name.repo` lookup: does the App reach that repository? True
        when the name has no repo part or no owner is configured."""
        rep = names.repo(name)
        if not rep or not self.cfg.github_owner:
            return True
        return self._repo_reachable(f"{self.cfg.github_owner}/{rep}")

    def _repo_reachable(self, full_repo: str) -> bool:
        """Is `owner/repo` in the App's installation? True when GitHub is not
        configured, or cannot be asked and nothing is cached (fail open: the
        mint then fails with GitHub's own answer)."""
        return self._repo_listed(full_repo) is not False

    def _repo_listed(self, full_repo: str) -> bool | None:
        """Whether the App's installation lists `owner/repo`; None when there
        is no App or its repository list cannot be had right now."""
        if self.github is None:
            return None
        repos = self.github.installed_repos()
        if repos is None:
            return None
        return full_repo.lower() in {r.lower() for r in repos}

    # -- secrets, shared volume, clone ---------------------------------------

    def _runtime_dir_ready(self, inst: str) -> bool:
        """Wait for logind to mount the workspace user's /run/user/<uid>
        (the lingering user's session starts with the container)."""
        path = f"/run/user/{self.cfg.uid}"
        for attempt in range(RUNTIME_DIR_TRIES):
            proc = self.inc.exec(inst, "mountpoint", "-q", path, check=False)
            if proc.returncode == 0:
                return True
            if attempt == 0:
                self.log(f"waiting for {path} in {names.display(inst)}")
            self.sleep(RUNTIME_DIR_WAIT)
        return False

    def _push_secrets(self, ws: Workspace) -> None:
        """Copy the cached secrets env file into the workspace's tmpfs."""
        if not self.secrets.configured:
            return
        keys, age = self.secrets.cached()
        if age is None:
            if not self._warned_no_cache:
                self.log("warning: no secrets cached yet (`dev secrets refresh`); none pushed")
                self._warned_no_cache = True
            return
        cfg = self.cfg
        if not self._runtime_dir_ready(ws.instance):
            self.log(f"warning: /run/user/{cfg.uid} never appeared in {ws.name}; secrets not pushed")
            return
        dst = f"{cfg.secrets_path()}/{ENV_FILE}"
        try:
            self.inc.file_push(self.secrets.env_file(), ws.instance, dst, uid=cfg.uid, gid=cfg.gid, mode="0600")
            self.inc.exec(ws.instance, "chown", f"{cfg.uid}:{cfg.gid}", cfg.secrets_path(), check=False)
            self.inc.exec(ws.instance, "chmod", "0700", cfg.secrets_path(), check=False)
        except IncusError as e:
            self.log(f"warning: could not push secrets into {ws.name}: {e}")

    def _ensure_logins_volume(self) -> str | None:
        cfg = self.cfg
        if not cfg.logins_volume:
            return None
        if self.inc.volume(cfg.pool, cfg.logins_volume) is None:
            self.log(f"creating shared volume {cfg.logins_volume} ({cfg.logins_size})")
            self.inc.volume_create(
                cfg.pool, cfg.logins_volume,
                size=cfg.logins_size,
                **{
                    "initial.uid": str(cfg.uid),
                    "initial.gid": str(cfg.gid),
                    "initial.mode": "0700",
                    "security.shifted": "true",
                },
            )
        return cfg.logins_volume

    def _attach_logins(self, inst: str) -> None:
        """Mount the shared volume (agent logins) into the instance if it is
        not there yet; Incus hot-plugs disk devices into running containers."""
        vol = self._ensure_logins_volume()
        if vol is None:
            return
        devices = (self.inc.instance(inst) or {}).get("devices") or {}
        if LOGINS_DEVICE in devices:
            return
        try:
            self.inc.device_add(inst, LOGINS_DEVICE, "disk", pool=self.cfg.pool, source=vol, path=self.cfg.logins_path())
        except IncusError as e:
            self.log(f"warning: could not attach {vol} to {names.display(inst)}: {e}")

    def _clone(self, ws: Workspace, full_repo: str) -> None:
        """Clone `owner/repo` into ~/work/<repo> with a token minted from the
        workspace's grants. Never fatal: the result goes on the manifest."""
        cfg = self.cfg
        target = f"{cfg.work_path()}/{full_repo.split('/', 1)[1]}"
        if self.github is None:
            self._note_clone(ws, "skipped: GitHub tokens are not configured")
            return
        if self.inc.exec(ws.instance, "test", "-e", target, check=False).returncode == 0:
            self._note_clone(ws, f"skipped: {target} exists")
            return
        if not self._runtime_dir_ready(ws.instance):
            self._note_clone(ws, f"failed: /run/user/{cfg.uid} never appeared")
            return
        creds = f"{cfg.secrets_path()}/{GIT_CREDENTIALS}"
        tmp = tempfile.mkdtemp(prefix="orchard-clone-")
        try:
            tok = self.token(ws.name)
            local = os.path.join(tmp, "creds")
            with open(local, "w") as f:
                f.write(f"https://x-access-token:{tok['token']}@github.com\n")
            os.chmod(local, 0o600)
            self.inc.file_push(local, ws.instance, creds, uid=cfg.uid, gid=cfg.gid, mode="0600")
            self.log(f"cloning {full_repo} into {target}")
            proc = self.inc.exec(
                ws.instance, "git", "-c", f"credential.helper=store --file={creds}",
                "clone", "--quiet", f"https://github.com/{full_repo}", target,
                check=False, user=cfg.uid, group=cfg.gid, env={"HOME": f"/home/{cfg.user}"},
            )
            if proc.returncode == 0:
                self._note_clone(ws, "ok")
            else:
                self._note_clone(ws, f"failed: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else proc.returncode}")
        except OrchardError as e:
            self._note_clone(ws, f"failed: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                self.inc.exec(ws.instance, "rm", "-f", creds, check=False)
            except IncusError:
                pass

    def _note_clone(self, ws: Workspace, result: str) -> None:
        self.log(f"clone {ws.name}: {result}")
        try:
            self.inc.config_set(ws.instance, {KEY + "clone": result})
        except IncusError:
            pass
        ws.clone = result

    def _push_client(self, ws: Workspace) -> None:
        cfg = self.cfg
        tmp = tempfile.mkdtemp(prefix="orchard-self-")
        try:
            client = os.path.join(tmp, "dev")
            with open(client, "w") as f:
                f.write(SELF_CLIENT.format(host=cfg.self_host))
            self.inc.file_push(client, ws.instance, f"/home/{cfg.user}/.local/bin/dev", uid=cfg.uid, gid=cfg.gid, mode="0755")
        except IncusError as e:
            self.log(f"warning: could not update the dev client in {ws.name}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _install_self(self, ws: Workspace) -> None:
        """Generate the workspace's self-service key pair: public half on the
        instance config (the host's AuthorizedKeysCommand serves it), private
        half plus the pinned host key and the `dev` client inside the
        container. Nothing here is a secret the workspace could not already
        use: the key only speaks for this workspace."""
        cfg = self.cfg
        if not cfg.self_host:
            return
        try:
            hostkey = open(cfg.self_hostkey_file).read().strip()
        except OSError as e:
            self.log(f"warning: self-service not installed, cannot read {cfg.self_hostkey_file}: {e}")
            return
        tmp = tempfile.mkdtemp(prefix="orchard-self-")
        try:
            key = os.path.join(tmp, "self.key")
            proc = subprocess.run(
                [cfg.ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"orchard:{ws.name}", "-f", key],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                self.log(f"warning: self-service not installed, ssh-keygen failed: {proc.stderr.strip()}")
                return
            pub = open(key + ".pub").read().strip()
            known = os.path.join(tmp, "known_hosts")
            with open(known, "w") as f:
                f.write(f"{cfg.self_host} {hostkey}\n")
            client = os.path.join(tmp, "dev")
            with open(client, "w") as f:
                f.write(SELF_CLIENT.format(host=cfg.self_host))
            home = f"/home/{cfg.user}"
            self.inc.file_push(key, ws.instance, f"{home}/.orchard/self.key", uid=cfg.uid, gid=cfg.gid, mode="0600")
            self.inc.file_push(known, ws.instance, f"{home}/.orchard/known_hosts", uid=cfg.uid, gid=cfg.gid, mode="0644")
            self.inc.file_push(client, ws.instance, f"{home}/.local/bin/dev", uid=cfg.uid, gid=cfg.gid, mode="0755")
            # --create-dirs makes the parents as root; hand them to the user.
            self.inc.exec(ws.instance, "chown", f"{cfg.uid}:{cfg.gid}", f"{home}/.orchard", f"{home}/.local/bin", check=False)
            self.inc.config_set(ws.instance, {KEY + "selfkey": pub})
            ws.selfkey = pub
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _wait_ready(self, name: str) -> Workspace:
        deadline = time.monotonic() + self.cfg.ssh_wait_seconds
        announced = False
        while True:
            ws = self.require(name)
            if ws.ip and self.probe(ws.ip, 22):
                return ws
            if time.monotonic() >= deadline:
                raise NotReady(f"{name}: sshd not reachable after {self.cfg.ssh_wait_seconds}s (ip={ws.ip})")
            if not announced:
                self.log(f"waiting for {name} to accept ssh")
                announced = True
            self.sleep(0.5)

    def _hook(self, event: str, ws: Workspace) -> None:
        path = self.cfg.hooks.get(event)
        if not path:
            return
        env = {
            **os.environ,
            "ORCHARD_NAME": ws.name,
            "ORCHARD_INSTANCE": ws.instance,
            "ORCHARD_IP": ws.ip or "",
            "ORCHARD_PARENT": ws.parent or "",
            "ORCHARD_USER": self.cfg.user,
            "ORCHARD_EVENT": event,
        }
        self.log(f"running {event} hook")
        proc = subprocess.run([path], env=env, stdout=sys.stderr, stderr=sys.stderr)
        if proc.returncode != 0:
            self.log(f"warning: {event} hook exited {proc.returncode}")
