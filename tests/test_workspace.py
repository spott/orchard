import pytest

from orchard import OrchardError
from orchard.incus import IncusError
from orchard.workspace import KEY, NotFound, NotManaged, NotReady


def test_create_makes_volume_instance_device_and_starts(mgr, inc):
    ws = mgr.create("foo", repo="https://example.com/r.git", agent="claude")
    assert ws.name == "foo" and ws.running and ws.ip == "10.100.0.11"
    assert ws.volume == "ws-foo" and ws.repo == "https://example.com/r.git" and ws.agent == "claude"
    assert ws.created == "2026-11-01T12:00:00+00:00"
    ops = [c[0] for c in inc.calls]
    assert ops == ["volume_create", "init", "device_add", "start"]
    _, pool, vol, vcfg = inc.calls[0]
    assert (pool, vol) == ("default", "ws-foo")
    assert vcfg == {
        "size": "50GiB", "initial.uid": "1000", "initial.gid": "100", "initial.mode": "0755",
        "security.shifted": "true",
    }
    _, image, name, profiles, meta = inc.calls[1]
    assert (image, name, profiles) == ("workspace", "foo", ("default",))
    assert meta[KEY + "managed"] == "true"
    dev = inc.calls[2]
    assert dev[2:4] == ("work", "disk")
    assert dev[4] == {"pool": "default", "source": "ws-foo", "path": "/home/spott/work"}


def test_create_is_idempotent_and_starts_stopped(mgr, inc):
    mgr.create("foo")
    inc.stop("foo")
    inc.calls.clear()
    ws = mgr.create("foo")
    assert ws.running
    assert [c[0] for c in inc.calls] == ["start"]
    inc.calls.clear()
    assert mgr.create("foo").running
    assert inc.calls == []


def test_create_adopts_existing_volume(mgr, inc, logs):
    inc.vols["ws-foo"] = {"config": {}}
    mgr.create("foo")
    assert "volume_create" not in [c[0] for c in inc.calls]
    assert any("adopting" in m for m in logs)


def test_create_cleans_up_on_failure_but_keeps_adopted_volume(mgr, inc):
    inc.fail_on.add("device_add")
    with pytest.raises(IncusError):
        mgr.create("foo")
    assert "foo" not in inc.inst and "ws-foo" not in inc.vols

    inc.fail_on = {"start"}
    inc.vols["ws-bar"] = {"config": {}}
    with pytest.raises(IncusError):
        mgr.create("bar")
    assert "bar" not in inc.inst and "ws-bar" in inc.vols


def test_create_times_out_when_ssh_never_answers(cfg, inc, logs):
    from orchard.workspace import Manager

    cfg.ssh_wait_seconds = 0
    m = Manager(cfg, inc, log=logs.append, probe=lambda ip, port: False, sleep=lambda s: None)
    with pytest.raises(NotReady):
        m.create("foo")
    assert "foo" not in inc.inst


def test_create_rejects_unmanaged_instance(mgr, inc):
    inc.inst["raw"] = {"name": "raw", "status": "Running", "config": {}, "devices": {}, "state": {}}
    with pytest.raises(NotManaged):
        mgr.create("raw")
    with pytest.raises(NotManaged):
        mgr.ensure_running("raw")
    assert mgr.list() == []


def test_ensure_running_creates_then_starts(mgr, inc):
    ws = mgr.ensure_running("foo")
    assert ws.running
    inc.stop("foo")
    assert mgr.ensure_running("foo").running
    assert mgr.ensure_running("foo").running


def test_stop_start_restart(mgr, inc):
    mgr.create("foo")
    assert mgr.stop("foo").status == "Stopped"
    assert mgr.stop("foo").status == "Stopped"
    assert mgr.start("foo").running
    assert mgr.restart("foo").running
    with pytest.raises(NotFound):
        mgr.start("nope")


def test_rm_trashes_volume_then_gc_reaps_after_grace(mgr, inc):
    from datetime import timedelta

    from tests.conftest import FROZEN

    mgr.create("foo")
    assert mgr.rm("foo") == ["foo"]
    assert "foo" not in inc.inst
    assert list(inc.vols) == ["trash-foo-20261101T120000Z"]
    trash = inc.vols["trash-foo-20261101T120000Z"]["config"]
    assert trash[KEY + "trashed"] == "2026-11-01T12:00:00+00:00" and trash[KEY + "name"] == "foo"
    assert mgr.rm("foo") == []

    assert mgr.gc() == []
    mgr.now = lambda: FROZEN + timedelta(days=6, hours=23)
    assert mgr.gc() == []
    mgr.now = lambda: FROZEN + timedelta(days=7)
    assert mgr.gc(dry_run=True) == ["trash-foo-20261101T120000Z"]
    assert "trash-foo-20261101T120000Z" in inc.vols
    assert mgr.gc() == ["trash-foo-20261101T120000Z"]
    assert inc.vols == {}


def test_rm_now_deletes_volume(mgr, inc):
    mgr.create("foo")
    mgr.rm("foo", now=True)
    assert inc.vols == {} and inc.inst == {}


def test_rm_tree_removes_forks_first(mgr, inc):
    mgr.create("parent")
    mgr.create("child")
    inc.config_set("child", {KEY + "parent": "parent"})
    mgr.create("grandchild")
    inc.config_set("grandchild", {KEY + "parent": "child"})
    assert mgr.rm("parent", tree=True) == ["grandchild", "child", "parent"]
    assert inc.inst == {}


def test_list_and_status(mgr, inc):
    mgr.create("b")
    mgr.create("a")
    assert [w.name for w in mgr.list()] == ["a", "b"]
    assert mgr.require("a").image == "4f465639dead"
    with pytest.raises(NotFound):
        mgr.require("zzz")


def test_hook_runs_with_environment(mgr, cfg, tmp_path):
    out = tmp_path / "hook.out"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\nenv | grep ^ORCHARD_ | sort > {out}\n")
    hook.chmod(0o755)
    cfg.hooks["on_create"] = str(hook)
    ws = mgr.create("foo")
    lines = out.read_text().splitlines()
    assert f"ORCHARD_IP={ws.ip}" in lines
    assert "ORCHARD_NAME=foo" in lines and "ORCHARD_USER=spott" in lines and "ORCHARD_EVENT=on_create" in lines


def test_locks_are_per_workspace(mgr, cfg):
    import fcntl
    import os

    mgr.create("foo")
    path = os.path.join(cfg.lock_dir, "foo.lock")
    assert os.path.exists(path)
    fd = os.open(path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released after create
    os.close(fd)


def test_invalid_name_is_an_orchard_error(mgr):
    with pytest.raises(OrchardError):
        mgr.create("Not Valid")
