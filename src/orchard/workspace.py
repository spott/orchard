"""Workspaces: an Incus instance named after the workspace, a custom volume
`ws-<name>` mounted at ~/work, and `user.orchard.*` config keys as the only
metadata. There is no database; `incus list` is the source of truth."""

from __future__ import annotations

import dataclasses
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import OrchardError, locks, names
from .config import Config
from .incus import Incus, IncusError

KEY = "user.orchard."
WORK_DEVICE = "work"
STAMP = "%Y%m%dT%H%M%SZ"


class NotManaged(OrchardError):
    def __init__(self, name: str) -> None:
        super().__init__(f"instance {name!r} exists but is not an orchard workspace")


class NotFound(OrchardError):
    def __init__(self, name: str) -> None:
        super().__init__(f"no workspace named {name!r}")


class NotReady(OrchardError):
    pass


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

    def create(self, name: str, *, repo: str | None = None, agent: str | None = None, image: str | None = None) -> Workspace:
        names.validate(name)
        with locks.held(self.cfg.lock_dir, name):
            existing = self.get(name)
            if existing is not None:
                return self._ensure_started(existing)
            return self._create_locked(name, repo=repo, agent=agent, image=image or self.cfg.image)

    def ensure_running(self, name: str) -> Workspace:
        """The front-door path: create if missing, start if stopped, wait for sshd."""
        names.validate(name)
        with locks.held(self.cfg.lock_dir, name):
            ws = self.get(name)
            if ws is None:
                return self._create_locked(name, repo=None, agent=None, image=self.cfg.image)
            return self._ensure_started(ws)

    def start(self, name: str) -> Workspace:
        with locks.held(self.cfg.lock_dir, name):
            return self._ensure_started(self.require(name))

    def stop(self, name: str, force: bool = False) -> Workspace:
        with locks.held(self.cfg.lock_dir, name):
            ws = self.require(name)
            if ws.status != "Stopped":
                self.log(f"stopping {name}")
                self.inc.stop(name, force=force)
            return self.require(name)

    def restart(self, name: str) -> Workspace:
        self.stop(name)
        return self.start(name)

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
        """Delete trashed volumes older than the grace period."""
        cutoff = self.now() - timedelta(days=self.cfg.rm_grace_days)
        reaped: list[str] = []
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

    # -- internals ----------------------------------------------------------

    def _create_locked(self, name: str, *, repo: str | None, agent: str | None, image: str) -> Workspace:
        cfg = self.cfg
        vol = names.volume(name)
        made_volume = False
        if self.inc.volume(cfg.pool, vol) is None:
            self.log(f"creating volume {vol} ({cfg.volume_size})")
            self.inc.volume_create(
                cfg.pool, vol,
                size=cfg.volume_size,
                **{"initial.uid": str(cfg.uid), "initial.gid": str(cfg.gid), "initial.mode": "0755"},
            )
            made_volume = True
        else:
            self.log(f"adopting existing volume {vol}")
        meta = {
            KEY + "managed": "true",
            KEY + "created": self.now().isoformat(timespec="seconds"),
        }
        if repo:
            meta[KEY + "repo"] = repo
        if agent:
            meta[KEY + "agent"] = agent
        self.log(f"creating {name} from image {image}")
        try:
            self.inc.init(image, name, cfg.profiles, meta)
            self.inc.device_add(name, WORK_DEVICE, "disk", pool=cfg.pool, source=vol, path=cfg.work_path(), shift="true")
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
        self._hook("on_create", ws)
        return ws

    def _ensure_started(self, ws: Workspace) -> Workspace:
        if not ws.running:
            self.log(f"starting {ws.name}")
            self.inc.start(ws.name)
        return self._wait_ready(ws.name)

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
