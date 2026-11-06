from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

import pytest

from orchard.config import Config
from orchard.incus import Incus, IncusError
from orchard.workspace import Manager


class FakeIncus(Incus):
    """In-memory Incus with the same method surface as the real wrapper.
    Records every mutating call in `calls`."""

    def __init__(self) -> None:
        super().__init__("fake-incus")
        self.inst: dict[str, dict[str, Any]] = {}
        self.vols: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.fail_on: set[str] = set()
        self.next_ip = 10
        self.journal: dict[str, str] = {}
        self.files: dict[str, dict[str, str]] = {}
        self.images: dict[str, str] = {"workspace": "4f465639deadbeefcafe"}
        self.snaps: dict[str, list[str]] = {}
        self.exec_out: dict[str, tuple[int, str, str]] = {}
        self.execs: list[tuple[Any, ...]] = []

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise IncusError([self.binary, op], 1, f"injected {op} failure")

    def run(self, *args, input=None, check=True):  # pragma: no cover - never reached
        raise AssertionError(f"raw run() called: {args}")

    # instances
    def instances(self):
        return copy.deepcopy(list(self.inst.values()))

    def instance(self, name):
        return copy.deepcopy(self.inst.get(name))

    def init(self, image, name, profiles, config):
        self._maybe_fail("init")
        self.calls.append(("init", image, name, tuple(profiles), dict(config)))
        self.inst[name] = {
            "name": name,
            "status": "Stopped",
            "config": {"volatile.base_image": self.images.get(image, image), **config},
            "devices": {},
            "state": {"network": {}},
        }

    def start(self, name):
        self._maybe_fail("start")
        self.calls.append(("start", name))
        i = self.inst[name]
        i["status"] = "Running"
        reserved = (i["devices"].get("eth0") or {}).get("ipv4.address")
        if reserved is None:
            self.next_ip += 1
            reserved = f"10.100.0.{self.next_ip}"
        i["state"] = {"network": {"eth0": {"addresses": [
            {"family": "inet6", "scope": "link", "address": "fe80::1"},
            {"family": "inet", "scope": "global", "address": reserved},
        ]}}}

    def stop(self, name, force=False):
        self.calls.append(("stop", name, force))
        self.inst[name]["status"] = "Stopped"
        self.inst[name]["state"] = {"network": {}}

    def delete(self, name, force=False):
        self.calls.append(("delete", name, force))
        self.inst.pop(name, None)

    def rename(self, old, new):
        self.calls.append(("rename", old, new))
        self.inst[new] = self.inst.pop(old)
        self.inst[new]["name"] = new
        if old in self.files:
            self.files[new] = self.files.pop(old)

    def config_set(self, name, config):
        self.calls.append(("config_set", name, dict(config)))
        self.inst[name]["config"].update(config)

    def config_unset(self, name, *keys):
        self.calls.append(("config_unset", name, keys))
        for k in keys:
            self.inst[name]["config"].pop(k, None)

    def image_fingerprint(self, ref):
        if ref in self.images:
            return self.images[ref]
        for fp in self.images.values():
            if fp.startswith(ref):
                return fp
        return None

    def device_add(self, name, device, kind, **props):
        self._maybe_fail("device_add")
        self.calls.append(("device_add", name, device, kind, dict(props)))
        self.inst[name]["devices"][device] = {"type": kind, **props}

    def device_override(self, name, device, **props):
        self._maybe_fail("device_override")
        self.calls.append(("device_override", name, device, dict(props)))
        dev = self.inst[name]["devices"].setdefault(device, {"type": "nic", "network": "incusbr0"})
        dev.update(props)
        # Like Incus: a reserved address is what the next start leases.
        if device == "eth0" and props.get("ipv4.address") and self.inst[name]["status"] == "Running":
            self.inst[name]["state"]["network"]["eth0"]["addresses"] = [
                {"family": "inet", "scope": "global", "address": props["ipv4.address"]}
            ]

    def device_set(self, name, device, **props):
        self.calls.append(("device_set", name, device, dict(props)))
        self.inst[name]["devices"][device].update(props)

    def device_remove(self, name, device):
        self.calls.append(("device_remove", name, device))
        del self.inst[name]["devices"][device]

    def exec(self, name, *cmd, check=True, user=None, group=None, env=None):
        import subprocess
        self.calls.append(("exec", name, cmd))
        self.execs.append((name, cmd, user, group, env))
        if cmd and cmd[0] == "journalctl":
            return subprocess.CompletedProcess([self.binary, "exec", name, *cmd], 0, self.journal.get(name, ""), "")
        rc, out, err = self.exec_out.get(name, (0, "", ""))
        if check and rc != 0:
            raise IncusError([self.binary, "exec", name, *cmd], rc, err)
        return subprocess.CompletedProcess([self.binary, "exec", name, *cmd], rc, out, err)

    def file_push(self, src, name, dst, *, uid=None, gid=None, mode=None):
        with open(src) as f:
            content = f.read()
        self.calls.append(("file_push", name, dst, uid, gid, mode))
        self.files.setdefault(name, {})[dst] = content

    def file_pull(self, name, src, dst, check=True):
        content = self.files.get(name, {}).get(src)
        if content is None:
            if check:
                raise IncusError([self.binary, "file", "pull", f"{name}{src}"], 1, "not found")
            return False
        with open(dst, "w") as f:
            f.write(content)
        return True

    # volumes
    def volumes(self, pool):
        return copy.deepcopy([{"type": "custom", "name": n, **v} for n, v in self.vols.items()])

    def volume(self, pool, name):
        v = self.vols.get(name)
        return None if v is None else {"type": "custom", "name": name, **copy.deepcopy(v)}

    def volume_create(self, pool, name, **config):
        self._maybe_fail("volume_create")
        self.calls.append(("volume_create", pool, name, dict(config)))
        self.vols[name] = {"config": dict(config)}

    def volume_delete(self, pool, name):
        self.calls.append(("volume_delete", pool, name))
        del self.vols[name]

    def volume_rename(self, pool, old, new):
        self.calls.append(("volume_rename", pool, old, new))
        self.vols[new] = self.vols.pop(old)
        if old in self.snaps:
            self.snaps[new] = self.snaps.pop(old)

    def volume_copy(self, pool, src, dst):
        self._maybe_fail("volume_copy")
        self.calls.append(("volume_copy", pool, src, dst))
        self.vols[dst] = copy.deepcopy(self.vols[src])

    def volume_set(self, pool, name, config):
        self.calls.append(("volume_set", pool, name, dict(config)))
        self.vols[name]["config"].update(config)

    def volume_snapshots(self, pool, name):
        return list(self.snaps.get(name, []))

    def volume_snapshot(self, pool, name, snapshot):
        self.calls.append(("volume_snapshot", pool, name, snapshot))
        self.snaps.setdefault(name, []).append(snapshot)

    def volume_snapshot_restore(self, pool, name, snapshot):
        self.calls.append(("volume_snapshot_restore", pool, name, snapshot))
        assert snapshot in self.snaps.get(name, [])

    def volume_snapshot_delete(self, pool, name, snapshot):
        self.calls.append(("volume_snapshot_delete", pool, name, snapshot))
        self.snaps[name].remove(snapshot)


FROZEN = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def inc() -> FakeIncus:
    return FakeIncus()


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(lock_dir=str(tmp_path / "locks"), ssh_wait_seconds=5)


@pytest.fixture
def logs() -> list[str]:
    return []


@pytest.fixture
def mgr(cfg, inc, logs) -> Manager:
    return Manager(cfg, inc, log=logs.append, probe=lambda ip, port: True, sleep=lambda s: None, now=lambda: FROZEN)
