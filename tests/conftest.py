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
            "config": {"volatile.base_image": "4f465639deadbeefcafe", **config},
            "devices": {},
            "state": {"network": {}},
        }

    def start(self, name):
        self._maybe_fail("start")
        self.calls.append(("start", name))
        i = self.inst[name]
        i["status"] = "Running"
        self.next_ip += 1
        i["state"] = {"network": {"eth0": {"addresses": [
            {"family": "inet6", "scope": "link", "address": "fe80::1"},
            {"family": "inet", "scope": "global", "address": f"10.100.0.{self.next_ip}"},
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

    def config_set(self, name, config):
        self.calls.append(("config_set", name, dict(config)))
        self.inst[name]["config"].update(config)

    def device_add(self, name, device, kind, **props):
        self._maybe_fail("device_add")
        self.calls.append(("device_add", name, device, kind, dict(props)))
        self.inst[name]["devices"][device] = {"type": kind, **props}

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

    def volume_copy(self, pool, src, dst):
        self.calls.append(("volume_copy", pool, src, dst))
        self.vols[dst] = copy.deepcopy(self.vols[src])

    def volume_set(self, pool, name, config):
        self.calls.append(("volume_set", pool, name, dict(config)))
        self.vols[name]["config"].update(config)


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
