"""Static addresses, rename, self-service install and the untouched gc."""

import pytest

from orchard import names
from orchard.config import Config
from orchard.workspace import KEY, AlreadyExists, Manager, NoAddress

from .conftest import FROZEN


@pytest.fixture
def acfg(tmp_path) -> Config:
    return Config(
        lock_dir=str(tmp_path / "locks"), ssh_wait_seconds=5,
        address_start="10.100.0.100", address_end="10.100.0.102",
    )


@pytest.fixture
def amgr(acfg, inc, logs) -> Manager:
    return Manager(acfg, inc, log=logs.append, probe=lambda ip, port: True, sleep=lambda s: None, now=lambda: FROZEN)


def test_label():
    assert names.label("foo.orchard.example.com", "orchard.example.com") == "foo"
    assert names.label("FOO.Orchard.Example.com.", "orchard.example.com") == "foo"
    assert names.label("orchard.example.com", "orchard.example.com") is None
    assert names.label("a.foo.orchard.example.com", "orchard.example.com") == "a.foo"
    assert names.label("a.b.foo.orchard.example.com", "orchard.example.com") is None
    assert names.label("_gateway.orchard.example.com", "orchard.example.com") is None
    assert names.label("foo.example.com", "orchard.example.com") is None
    assert names.label("foo", "") is None


def test_create_reserves_lowest_free_address_and_records_origin(amgr, inc):
    ws = amgr.create("foo")
    assert ws.address == "10.100.0.100" and ws.ip == "10.100.0.100" and ws.origin == "cli"
    assert ("device_override", "foo", "eth0", {"ipv4.address": "10.100.0.100"}) in inc.calls
    ops = [c[0] for c in inc.calls]
    assert ops.index("device_override") < ops.index("start")
    bar = amgr.create("bar", origin="dns")
    assert bar.address == "10.100.0.101" and bar.origin == "dns"


def test_address_range_exhausts(amgr):
    amgr.create("a")
    amgr.create("b")
    amgr.create("c")
    with pytest.raises(NoAddress):
        amgr.create("d")


def test_no_range_means_no_reservation(mgr, inc):
    ws = mgr.create("foo")
    assert ws.address is None
    assert "device_override" not in [c[0] for c in inc.calls]


def test_stop_pins_current_address_of_an_adopted_workspace(amgr, inc):
    # A workspace from before reservations existed: dynamic lease in range.
    inc.init("old", "old", ["default"], {KEY + "managed": "true"})
    inc.start("old")
    inc.inst["old"]["state"]["network"]["eth0"]["addresses"][1]["address"] = "10.100.0.101"
    ws = amgr.stop("old")
    assert ws.address == "10.100.0.101" and not ws.running
    # ...and a dynamic lease outside the range gets a fresh one.
    inc.init("far", "far", ["default"], {KEY + "managed": "true"})
    inc.start("far")
    assert amgr.stop("far").address == "10.100.0.100"


def test_rename_moves_instance_and_volume_and_restarts(amgr, inc):
    amgr.create("foo")
    inc.calls.clear()
    ws = amgr.rename("foo", "bar")
    assert ws.name == "bar" and ws.running and ws.volume == "ws-bar" and ws.address == "10.100.0.100"
    ops = [c[0] for c in inc.calls]
    assert ops == ["stop", "rename", "volume_rename", "device_set", "start"]
    assert ("device_set", "bar", "work", {"source": "ws-bar"}) in inc.calls
    assert amgr.get("foo") is None and "ws-foo" not in inc.vols and "ws-bar" in inc.vols


def test_rename_stopped_stays_stopped_and_refuses_collisions(amgr, inc):
    amgr.create("foo")
    amgr.create("bar")
    amgr.stop("foo")
    with pytest.raises(AlreadyExists):
        amgr.rename("foo", "bar")
    inc.vols["ws-baz"] = {"config": {}}
    with pytest.raises(AlreadyExists):
        amgr.rename("foo", "baz")
    ws = amgr.rename("foo", "qux")
    assert ws.name == "qux" and not ws.running
    assert amgr.rename("qux", "qux").name == "qux"


def test_gc_removes_lookup_created_workspaces_nobody_logged_into(amgr, inc):
    amgr.create("typo", origin="dns")
    amgr.create("mine", origin="dns")
    amgr.create("explicit")
    inc.journal["mine"] = "Nov 04 sshd-session[9]: Accepted publickey for spott from 10.42.1.42"
    old = "2026-10-01T00:00:00+00:00"
    for n in ("typo", "mine", "explicit"):
        inc.inst[n]["config"][KEY + "created"] = old
    assert amgr.gc(dry_run=True) == ["typo"]
    assert "typo" in inc.inst
    assert amgr.gc() == ["typo"]
    assert "typo" not in inc.inst and any(v.startswith("trash-typo-") for v in inc.vols)
    # Too young to judge.
    amgr.create("fresh", origin="dns")
    assert amgr.gc() == []


def test_self_service_install(acfg, inc, logs, tmp_path, monkeypatch):
    import subprocess

    from orchard import workspace

    hostkey = tmp_path / "host.pub"
    hostkey.write_text("ssh-ed25519 AAAAHOST root@host\n")
    acfg.self_host = "10.100.0.1"
    acfg.self_hostkey_file = str(hostkey)

    def fake_keygen(argv, **kw):
        key = argv[argv.index("-f") + 1]
        open(key, "w").write("PRIVATE\n")
        open(key + ".pub", "w").write("ssh-ed25519 AAAASELF orchard:foo\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(workspace.subprocess, "run", fake_keygen)
    mgr = Manager(acfg, inc, log=logs.append, probe=lambda ip, port: True, sleep=lambda s: None, now=lambda: FROZEN)
    ws = mgr.create("foo")
    assert ws.selfkey == "ssh-ed25519 AAAASELF orchard:foo"
    files = inc.files["foo"]
    assert files["/home/spott/.orchard/self.key"] == "PRIVATE\n"
    assert files["/home/spott/.orchard/known_hosts"] == "10.100.0.1 ssh-ed25519 AAAAHOST root@host\n"
    client = files["/home/spott/.local/bin/dev"]
    assert client.startswith("#!/bin/sh") and "dev@10.100.0.1" in client and "self.key" in client
    pushes = [c for c in inc.calls if c[0] == "file_push"]
    assert all(c[3:] == (1000, 100, m) for c, m in zip(pushes, ("0600", "0644", "0755")))
    assert mgr.find_by_selfkey("ssh-ed25519 AAAASELF whatever").name == "foo"
    assert mgr.find_by_selfkey("ssh-ed25519 AAAAOTHER") is None
    # A pre-existing workspace without a key gets one when next started.
    inc.init("old", "old", ["default"], {KEY + "managed": "true"})
    inc.calls.clear()
    assert mgr.start("old").selfkey is not None
    assert ("config_set", "old", {KEY + "selfkey": "ssh-ed25519 AAAASELF orchard:foo"}) in inc.calls
    # Starting one that has a key only refreshes the client.
    inc.stop("foo")
    inc.calls.clear()
    mgr.start("foo")
    assert [c[0] for c in inc.calls] == ["start", "file_push"]
    assert inc.calls[1][2] == "/home/spott/.local/bin/dev"


def test_self_service_skipped_without_host_or_hostkey(amgr, inc, logs, tmp_path):
    assert amgr.create("foo").selfkey is None
    amgr.cfg.self_host = "10.100.0.1"
    amgr.cfg.self_hostkey_file = str(tmp_path / "missing")
    assert amgr.create("bar").selfkey is None
    assert any("cannot read" in m for m in logs)
