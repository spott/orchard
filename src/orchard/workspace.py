"""Workspaces: an Incus instance named after the workspace, a custom volume
`ws-<name>` mounted at ~/work, and `user.orchard.*` config keys as the only
metadata. There is no database; `incus list` is the source of truth."""

from __future__ import annotations

import dataclasses
import ipaddress
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
from .incus import Incus, IncusError

KEY = "user.orchard."
WORK_DEVICE = "work"
NIC_DEVICE = "eth0"
STAMP = "%Y%m%dT%H%M%SZ"
SNAPSHOT_PREFIX = "pre-rebase-"

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

    @property
    def running(self) -> bool:
        return self.status == "Running"

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


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


def from_instance(inst: dict[str, Any]) -> Workspace:
    cfg = inst.get("config") or {}
    work = (inst.get("devices") or {}).get(WORK_DEVICE) or {}
    image = cfg.get("volatile.base_image")
    return Workspace(
        name=inst["name"],
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
    ) -> None:
        self.cfg = cfg
        self.inc = inc
        self.log = log or (lambda msg: print(f"orchard: {msg}", file=sys.stderr, flush=True))
        self.probe = probe
        self.sleep = sleep
        self.now = now

    # -- read side ----------------------------------------------------------

    def list(self) -> list[Workspace]:
        return sorted((from_instance(i) for i in self.inc.instances() if _managed(i)), key=lambda w: w.name)

    def get(self, name: str) -> Workspace | None:
        inst = self.inc.instance(names.validate(name))
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
                self.inc.stop(name, force=force)
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
            if self.inc.instance(new) is not None:
                raise AlreadyExists(new)
            if self.inc.volume(self.cfg.pool, names.volume(new)) is not None:
                raise AlreadyExists(names.volume(new))
            if ws.running:
                self.reserve_address(ws)
                self.log(f"stopping {old}")
                self.inc.stop(old, force=True)
            self.log(f"renaming {old} to {new}")
            self.inc.rename(old, new)
            if ws.volume == names.volume(old):
                self.inc.volume_rename(self.cfg.pool, ws.volume, names.volume(new))
                self.inc.device_set(new, WORK_DEVICE, source=names.volume(new))
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
                self.inc.stop(name, force=True)
            self.log(f"deleting {name}")
            self.inc.delete(name, force=True)
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
            if created > stale or self.logged_in(ws):
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
        line in the container's journal."""
        if not ws.running:
            return True
        proc = self.inc.exec(ws.name, "journalctl", "-q", "-g", "Accepted publickey", check=False)
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
        self.inc.device_override(ws.name, NIC_DEVICE, **{"ipv4.address": addr})
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
                if self.inc.instance(child) is not None:
                    raise AlreadyExists(child)
                src = ws.volume or names.volume(parent)
                dst = names.volume(child)
                if self.inc.volume(cfg.pool, dst) is not None:
                    raise AlreadyExists(dst)
                if ws.running:
                    self.reserve_address(ws)
                    self.log(f"stopping {parent} for a consistent copy")
                    self.inc.stop(parent)
                try:
                    self.log(f"copying volume {src} to {dst}")
                    self.inc.volume_copy(cfg.pool, src, dst)
                finally:
                    if ws.running:
                        self._ensure_started(self.require(parent))
                try:
                    return self._create_locked(
                        child, repo=ws.repo, agent=None, image=ws.fingerprint or cfg.image, origin="fork", parent=parent
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
        while names.child(parent, n) in taken or names.volume(names.child(parent, n)) in taken:
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
            if ws.running:
                self.reserve_address(ws)
                self.log(f"stopping {name}")
                self.inc.stop(name)
            snap = SNAPSHOT_PREFIX + stamp.lower()
            self.log(f"snapshotting {vol} as {snap}")
            self.inc.volume_snapshot(cfg.pool, vol, snap)
            inst = self.inc.instance(name) or {}
            meta = {k: v for k, v in (inst.get("config") or {}).items() if k.startswith(KEY)}
            meta[KEY + "rebased"] = self.now().isoformat(timespec="seconds")
            old = self._free_old_name(name, stamp)
            tmp = tempfile.mkdtemp(prefix="orchard-rebase-")
            try:
                identity = self._pull_identity(name, tmp)
                self.inc.device_remove(name, WORK_DEVICE)
                self.log(f"keeping the old instance as {old}")
                self.inc.rename(name, old)
                self.inc.config_set(old, {KEY + "managed": "false", KEY + "old-of": name})
                try:
                    self.log(f"creating {name} from image {target[:12]}")
                    self.inc.init(target, name, cfg.profiles, meta)
                    self.inc.device_add(name, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path())
                    if ws.address:
                        self.inc.device_override(name, NIC_DEVICE, **{"ipv4.address": ws.address})
                    self._push_identity(name, identity)
                except OrchardError:
                    self.log(f"rebase failed, putting {old} back as {name}")
                    try:
                        self.inc.delete(name, force=True)
                    except IncusError:
                        pass
                    self.inc.rename(old, name)
                    self.inc.config_set(name, {KEY + "managed": "true"})
                    self.inc.config_unset(name, KEY + "old-of")
                    self.inc.device_add(name, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path())
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
            if cur.running:
                self.log(f"stopping {name}")
                self.inc.stop(name, force=True)
            parked = self._free_old_name(name, stamp)
            self.inc.device_remove(name, WORK_DEVICE)
            self.inc.rename(name, parked)
            self.inc.config_set(parked, {KEY + "managed": "false", KEY + "old-of": name})
            self.log(f"restoring {previous} as {name} (the rebased instance is kept as {parked})")
            self.inc.rename(previous, name)
            self.inc.config_set(name, {KEY + "managed": "true"})
            self.inc.config_unset(name, KEY + "old-of")
            self.inc.device_add(name, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path())
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
                    ws.name, "sh", "-lc", UPDATE_SCRIPT,
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
    ) -> Workspace:
        cfg = self.cfg
        vol = names.volume(name)
        made_volume = False
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
        self.log(f"creating {name} from image {image}")
        try:
            self.inc.init(image, name, cfg.profiles, meta)
            self.inc.device_add(name, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path())
            if address:
                self.inc.device_override(name, NIC_DEVICE, **{"ipv4.address": address})
            self.inc.start(name)
            ws = self._wait_ready(name)
        except OrchardError:
            # Leave nothing half-made behind; an adopted volume is never ours to drop.
            self.log(f"create failed, removing {name}")
            try:
                self.inc.delete(name, force=True)
            except IncusError:
                pass
            if made_volume:
                try:
                    self.inc.volume_delete(cfg.pool, vol)
                except IncusError:
                    pass
            raise
        self._install_self(ws)
        self._hook("on_create", ws)
        return ws

    def _ensure_started(self, ws: Workspace) -> Workspace:
        if not ws.running:
            self.log(f"starting {ws.name}")
            self.inc.start(ws.name)
        ws = self._wait_ready(ws.name)
        if ws.selfkey is None:
            # A workspace from before self-service existed: adopt it.
            self._install_self(ws)
        elif self.cfg.self_host:
            # Keep the client current with this version of dev.
            self._push_client(ws)
        return ws

    def _push_client(self, ws: Workspace) -> None:
        cfg = self.cfg
        tmp = tempfile.mkdtemp(prefix="orchard-self-")
        try:
            client = os.path.join(tmp, "dev")
            with open(client, "w") as f:
                f.write(SELF_CLIENT.format(host=cfg.self_host))
            self.inc.file_push(client, ws.name, f"/home/{cfg.user}/.local/bin/dev", uid=cfg.uid, gid=cfg.gid, mode="0755")
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
            self.inc.file_push(key, ws.name, f"{home}/.orchard/self.key", uid=cfg.uid, gid=cfg.gid, mode="0600")
            self.inc.file_push(known, ws.name, f"{home}/.orchard/known_hosts", uid=cfg.uid, gid=cfg.gid, mode="0644")
            self.inc.file_push(client, ws.name, f"{home}/.local/bin/dev", uid=cfg.uid, gid=cfg.gid, mode="0755")
            # --create-dirs makes the parents as root; hand them to the user.
            self.inc.exec(ws.name, "chown", f"{cfg.uid}:{cfg.gid}", f"{home}/.orchard", f"{home}/.local/bin", check=False)
            self.inc.config_set(ws.name, {KEY + "selfkey": pub})
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
            "ORCHARD_IP": ws.ip or "",
            "ORCHARD_PARENT": ws.parent or "",
            "ORCHARD_USER": self.cfg.user,
            "ORCHARD_EVENT": event,
        }
        self.log(f"running {event} hook")
        proc = subprocess.run([path], env=env, stdout=sys.stderr, stderr=sys.stderr)
        if proc.returncode != 0:
            self.log(f"warning: {event} hook exited {proc.returncode}")
