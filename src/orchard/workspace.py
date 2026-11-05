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

# Installed into every workspace as ~/.local/bin/dev: the self-service client.
# It sshes to the host as the jump user with a per-workspace key; the host
# recognises the key and lets it act on this workspace only.
SELF_CLIENT = """#!/bin/sh
# orchard: manage THIS workspace from inside it (rename, rm, stop, status, ls).
# Installed by the host; the key in ~/.orchard only speaks for this workspace.
d="$HOME/.orchard"
exec ssh -q -o BatchMode=yes -o IdentitiesOnly=yes -i "$d/self.key" \
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

    # -- internals ----------------------------------------------------------

    def _create_locked(
        self, name: str, *, repo: str | None, agent: str | None, image: str, origin: str = "cli", address: str | None = None
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
        return ws

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
