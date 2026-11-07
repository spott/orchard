"""fork, rebase (+rollback, prune) and update."""

import pytest

from orchard import OrchardError, cli
from orchard.incus import IncusError
from orchard.workspace import KEY, Manager, NoPrevious

NEW = "aaaa1111bbbb2222cccc"


def _calls(inc, op):
    return [c for c in inc.calls if c[0] == op]


# -- fork ---------------------------------------------------------------------


def test_fork_copies_volume_from_parent_image_with_fresh_identity(mgr, inc, cfg, tmp_path, monkeypatch):
    import subprocess

    from orchard import workspace

    cfg.self_host = "10.42.11.2"
    hk = tmp_path / "hk.pub"
    hk.write_text("ssh-ed25519 AAAAHOST\n")
    cfg.self_hostkey_file = str(hk)

    def fake_keygen(argv, **kw):  # no ssh-keygen in the nix sandbox
        key, comment = argv[argv.index("-f") + 1], argv[argv.index("-C") + 1]
        open(key, "w").write("PRIVATE\n")
        open(key + ".pub", "w").write(f"ssh-ed25519 AAAA{comment.split(':')[1].upper()} {comment}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(workspace.subprocess, "run", fake_keygen)
    cfg.address_start, cfg.address_end = "10.100.0.100", "10.100.0.110"
    parent = mgr.create("foo", repo="github:spott/foo", agent="claude")
    inc.vols["ws-foo"]["config"]["marker"] = "parent-data"
    child = mgr.fork("foo")

    assert child.name == "foo-1" and child.parent == "foo" and child.running
    assert child.repo == "github:spott/foo" and child.agent is None and child.origin == "fork"
    assert inc.vols["ws-foo-1"]["config"]["marker"] == "parent-data"
    # Made from the parent's own image, not the configured alias.
    init = [c for c in _calls(inc, "init") if c[2] == "foo-1"][0]
    assert init[1] == parent.fingerprint == inc.images["workspace"]
    # Parent was stopped for the copy and is running again, child has its own address.
    assert [c[1] for c in _calls(inc, "stop")] == ["foo"]
    assert mgr.require("foo").running
    assert child.address and child.address != mgr.require("foo").address
    # Fresh self-service key, different from the parent's.
    assert child.selfkey and child.selfkey != parent.selfkey
    assert "orchard:foo-1" in child.selfkey


def test_fork_names_lowest_free_child_and_honours_limit(mgr, inc, cfg):
    cfg.fork_limit = 2
    mgr.create("foo")
    mgr.create("foo-1")  # an unrelated workspace that happens to use the name
    a = mgr.fork("foo")
    assert a.name == "foo-2"
    b = mgr.fork("foo", "exp")
    assert b.name == "exp" and b.parent == "foo"
    with pytest.raises(OrchardError, match="fork_limit"):
        mgr.fork("foo")
    with pytest.raises(OrchardError, match="already exists"):
        mgr.fork("foo-1", "exp")


def test_fork_of_stopped_parent_leaves_it_stopped(mgr, inc):
    mgr.create("foo")
    mgr.stop("foo")
    inc.calls.clear()
    child = mgr.fork("foo")
    assert child.running and not mgr.require("foo").running
    assert [c[1] for c in _calls(inc, "start")] == ["foo-1"]


def test_fork_failure_removes_the_copied_volume(mgr, inc):
    mgr.create("foo")
    inc.fail_on.add("init")
    with pytest.raises(OrchardError):
        mgr.fork("foo")
    assert "ws-foo-1" not in inc.vols and "foo-1" not in inc.inst
    assert mgr.require("foo").running


def test_rm_tree_after_fork(mgr, inc):
    mgr.create("foo")
    mgr.fork("foo")
    mgr.fork("foo-1")
    assert mgr.rm("foo", tree=True) == ["foo-1-1", "foo-1", "foo"]


# -- rebase -------------------------------------------------------------------


def _seed_identity(inc, name):
    inc.files.setdefault(name, {}).update({
        "/etc/machine-id": "0123456789abcdef\n",
        "/etc/ssh/ssh_host_ed25519_key": "PRIVATE\n",
        "/etc/ssh/ssh_host_ed25519_key.pub": "ssh-ed25519 AAAAHOSTKEY\n",
        "/home/spott/.orchard/self.key": "SELFPRIVATE\n",
        "/home/spott/.orchard/known_hosts": "10.42.11.2 ssh-ed25519 AAAAHOST\n",
    })


def test_rebase_keeps_volume_address_metadata_and_identity(mgr, inc, cfg):
    cfg.address_start, cfg.address_end = "10.100.0.100", "10.100.0.110"
    ws = mgr.create("foo", repo="github:spott/foo")
    inc.inst["foo"]["config"][KEY + "selfkey"] = "ssh-ed25519 AAAASELF orchard:foo"
    inc.inst["foo"]["config"]["volatile.eth0.hwaddr"] = "10:66:6a:00:00:01"
    _seed_identity(inc, "foo")
    inc.images["workspace-next"] = NEW
    inc.calls.clear()

    new, old = mgr.rebase("foo", "workspace-next")

    assert old == "foo-old-20261101t120000z"
    assert new.name == "foo" and new.running and new.fingerprint == NEW and new.image == NEW[:12]
    assert new.volume == "ws-foo" and new.address == ws.address
    assert new.repo == "github:spott/foo" and new.created == ws.created and new.selfkey == "ssh-ed25519 AAAASELF orchard:foo"
    assert new.rebased is not None
    # Snapshot first, old instance parked unmanaged with its volume detached.
    assert _calls(inc, "volume_snapshot") == [("volume_snapshot", "default", "ws-foo", "pre-rebase-20261101t120000z")]
    parked = inc.inst[old]["config"]
    assert parked[KEY + "managed"] == "false" and parked[KEY + "old-of"] == "foo"
    assert "work" not in inc.inst[old]["devices"] and inc.inst["foo"]["devices"]["work"]["source"] == "ws-foo"
    assert inc.inst[old]["status"] == "Stopped"
    # The address moved too: Incus allows one static address per network.
    assert "ipv4.address" not in inc.inst[old]["devices"]["eth0"]
    assert inc.inst["foo"]["devices"]["eth0"]["ipv4.address"] == ws.address
    # ... and the MAC, so dnsmasq's lease for that address follows the workspace.
    assert inc.inst["foo"]["devices"]["eth0"]["hwaddr"] == "10:66:6a:00:00:01"
    # Identity moved: host keys, machine-id, self key; dev client re-pushed.
    assert inc.files["foo"]["/etc/ssh/ssh_host_ed25519_key"] == "PRIVATE\n"
    assert inc.files["foo"]["/etc/machine-id"] == "0123456789abcdef\n"
    assert inc.files["foo"]["/home/spott/.orchard/self.key"] == "SELFPRIVATE\n"
    pushes = {c[2]: (c[3], c[4], c[5]) for c in _calls(inc, "file_push")}
    assert pushes["/etc/ssh/ssh_host_ed25519_key"] == (0, 0, "0600")
    assert pushes["/home/spott/.orchard/self.key"] == (cfg.uid, cfg.gid, "0600")
    # The parked instance is invisible to the workspace list and to lookups.
    assert [w.name for w in mgr.list()] == ["foo"]
    assert mgr.old_instances("foo") == [old]


def test_rebase_is_a_noop_on_the_same_image(mgr, inc):
    mgr.create("foo")
    inc.calls.clear()
    ws, old = mgr.rebase("foo")
    assert old is None and ws.running and not _calls(inc, "rename") and not _calls(inc, "volume_snapshot")


def test_rebase_unknown_image_changes_nothing(mgr, inc):
    mgr.create("foo")
    inc.calls.clear()
    with pytest.raises(OrchardError, match="no image"):
        mgr.rebase("foo", "nope")
    assert inc.calls == []


def test_rebase_failure_restores_the_old_instance(mgr, inc, cfg):
    cfg.address_start, cfg.address_end = "10.100.0.100", "10.100.0.110"
    mgr.create("foo")
    inc.images["next"] = NEW
    inc.fail_on.add("init")
    with pytest.raises(OrchardError):
        mgr.rebase("foo", "next")
    ws = mgr.require("foo")
    assert ws.fingerprint == inc.images["workspace"] and ws.volume == "ws-foo"
    assert inc.inst["foo"]["config"].get(KEY + "managed") == "true" and KEY + "old-of" not in inc.inst["foo"]["config"]
    assert mgr.old_instances("foo") == []
    # The volume and the address are back on the restored instance.
    assert inc.inst["foo"]["devices"]["work"]["source"] == "ws-foo"
    assert inc.inst["foo"]["devices"]["eth0"]["ipv4.address"] == ws.address


def test_rebase_pulls_identity_while_running_and_waits_out_a_busy_rename(mgr, inc):
    mgr.create("foo")
    _seed_identity(inc, "foo")
    inc.images["next"] = NEW
    inc.busy_renames = 2
    inc.calls.clear()
    new, old = mgr.rebase("foo", "next")
    ops = [c[0] for c in inc.calls]
    # Pull from the running instance (no host mount), then stop, then rename.
    assert ops.index("file_pull") < ops.index("stop") < ops.index("rename")
    assert [c for c in inc.calls if c[0] == "rename"] == [("rename", "foo", old)] * 3
    assert new.running and new.fingerprint == NEW and inc.files["foo"]["/etc/machine-id"] == "0123456789abcdef\n"


def test_rebase_gives_up_on_a_rename_that_stays_busy(mgr, inc):
    from orchard import workspace

    mgr.create("foo")
    inc.images["next"] = NEW
    inc.busy_renames = 10**6
    with pytest.raises(IncusError, match="resource busy"):
        mgr.rebase("foo", "next")
    assert len(_calls(inc, "rename")) == workspace.RENAME_TRIES
    # Nothing changed: still the same instance with its volume attached.
    assert inc.inst["foo"]["devices"]["work"]["source"] == "ws-foo" and mgr.old_instances("foo") == []


def test_rebase_rollback_and_prune(mgr, inc, cfg):
    cfg.address_start, cfg.address_end = "10.100.0.100", "10.100.0.110"
    created = mgr.create("foo")
    inc.images["next"] = NEW
    _, old = mgr.rebase("foo", "next")
    inc.calls.clear()

    ws, parked = mgr.rebase_rollback("foo", restore=True)
    assert ws.running and ws.fingerprint == inc.images["workspace"] and ws.volume == "ws-foo"
    assert ws.address == created.address and "ipv4.address" not in inc.inst[parked]["devices"]["eth0"]
    assert parked == old + "-2"  # same frozen second as the rebase: counter, no collision
    assert inc.inst[parked]["config"][KEY + "old-of"] == "foo"
    assert _calls(inc, "volume_snapshot_restore") == [("volume_snapshot_restore", "default", "ws-foo", "pre-rebase-20261101t120000z")]
    assert [w.name for w in mgr.list()] == ["foo"]

    removed = mgr.rebase_prune("foo")
    assert removed == [parked, "ws-foo/pre-rebase-20261101t120000z"]
    assert mgr.old_instances("foo") == [] and inc.snaps["ws-foo"] == []
    with pytest.raises(NoPrevious):
        mgr.rebase_rollback("foo")


def test_volume_snapshots_strip_the_volume_prefix(monkeypatch):
    from orchard.incus import Incus

    real = Incus("incus")
    monkeypatch.setattr(real, "json", lambda *a: [{"name": "ws-foo/pre-rebase-1"}, {"name": "ws-foo/other"}])
    assert real.volume_snapshots("default", "ws-foo") == ["pre-rebase-1", "other"]


# -- update -------------------------------------------------------------------


def test_update_runs_as_the_user_in_running_workspaces(mgr, inc, cfg):
    mgr.create("foo")
    mgr.create("bar")
    mgr.stop("bar")
    inc.exec_out["foo"] = (0, "ok proj nixpkgs\nok proj nixpkgs-stable\n", "")
    report = mgr.update()
    assert report == {"foo": ["ok proj nixpkgs", "ok proj nixpkgs-stable"]}
    name, cmd, user, group, env = inc.execs[-1]
    assert name == "foo" and cmd[:2] == ("sh", "-lc") and "nix flake update" in cmd[2]
    assert (user, group, env) == (cfg.uid, cfg.gid, {"HOME": "/home/spott"})
    assert mgr.update(["bar"]) == {"bar": ["skipped: not running"]}
    inc.exec_out["foo"] = (1, "failed proj nixpkgs\n", "")
    assert mgr.update(["foo"]) == {"foo": ["failed proj nixpkgs"]}


# -- cli ----------------------------------------------------------------------


def test_cli_verbs(monkeypatch, inc, cfg, capsys):
    import json

    from orchard.config import Config

    inc.images["next"] = NEW
    m = Manager(cfg, inc, log=lambda s: None, probe=lambda ip, port: True, sleep=lambda s: None)
    monkeypatch.setattr(Config, "load", classmethod(lambda c, path=None: cfg))
    monkeypatch.setattr(cli, "Manager", lambda *a, **k: m)
    monkeypatch.setattr(cli, "Incus", lambda *a, **k: inc)
    assert cli.main(["create", "foo"]) == 0
    capsys.readouterr()
    assert cli.main(["fork", "foo", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["parent"] == "foo"
    assert cli.main(["rebase", "foo", "--image", "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["previous"].startswith("foo-old-")
    assert cli.main(["rebase", "foo", "--rollback"]) == 0
    capsys.readouterr()
    assert cli.main(["rebase", "foo", "--prune", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["removed"]) == 2  # the parked rebased instance + the snapshot
    inc.exec_out["foo"] = (1, "failed p nixpkgs\n", "")
    assert cli.main(["update", "foo"]) == 1
    assert "foo: failed p nixpkgs" in capsys.readouterr().out
    # Not available from inside a workspace.
    assert cli.main(["fork", "foo"], self_name="foo") == 1
    assert "not available" in capsys.readouterr().err
