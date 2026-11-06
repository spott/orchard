"""The self-service path: the CLI's self mode and the front door's key detection."""

import pytest

from orchard import cli, front_door
from orchard.cli import SelfRefused, self_argv
from orchard.workspace import KEY, Manager


def test_self_argv_implies_and_checks_the_name():
    assert self_argv(["ls", "--json"], "foo") == ["ls", "--json"]
    assert self_argv(["status"], "foo") == ["status", "foo"]
    assert self_argv(["status", "foo"], "foo") == ["status", "foo"]
    assert self_argv(["rm"], "foo") == ["rm", "foo"]
    assert self_argv(["--json", "stop"], "foo") == ["stop", "foo", "--json"]
    assert self_argv(["rename", "bar"], "foo") == ["rename", "foo", "bar"]
    assert self_argv(["rename", "foo", "bar"], "foo") == ["rename", "foo", "bar"]
    assert self_argv([], "foo") == []
    for argv in (["status", "bar"], ["rm", "bar"], ["rename", "bar", "baz"]):
        with pytest.raises(SelfRefused):
            self_argv(argv, "foo")
    for argv in (["create", "x"], ["start"], ["gc"], ["proxy", "x"], ["restart"]):
        with pytest.raises(SelfRefused):
            self_argv(argv, "foo")


def test_cli_self_mode_refuses_and_lists(monkeypatch, inc, cfg, capsys):
    m = Manager(cfg, inc, log=lambda s: None, probe=lambda ip, port: True, sleep=lambda s: None)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda c, path=None: cfg))
    monkeypatch.setattr(cli, "Incus", lambda binary: inc)
    monkeypatch.setattr(cli, "Manager", lambda c, i: m)
    m.create("foo")
    assert cli.main(["create", "bar"], self_name="foo") == 1
    assert "not available" in capsys.readouterr().err
    assert cli.main(["status"], self_name="foo") == 0
    assert capsys.readouterr().out.startswith("foo")
    # A detached verb: the parent announces and returns at once.
    monkeypatch.setattr(cli, "detach", lambda: True)
    assert cli.main(["rename", "bar"], self_name="foo") == 0
    err = capsys.readouterr().err
    assert "renaming foo to bar" in err and "foo" in inc.inst
    # ...and the child does the work with stdout silenced.
    monkeypatch.setattr(cli, "detach", lambda: False)
    monkeypatch.setattr(cli.syslog, "openlog", lambda *a, **k: None)
    monkeypatch.setattr(cli.syslog, "syslog", lambda *a, **k: None)
    assert cli.main(["rename", "bar"], self_name="foo") == 0
    assert "bar" in inc.inst and "foo" not in inc.inst


def test_front_door_detects_self_key(tmp_path, inc, cfg):
    m = Manager(cfg, inc, log=lambda s: None, probe=lambda ip, port: True, sleep=lambda s: None)
    m.create("foo")
    inc.inst["foo"]["config"][KEY + "selfkey"] = "ssh-ed25519 AAAASELF orchard:foo"
    auth = tmp_path / "auth"
    auth.write_text("publickey ssh-ed25519 AAAASELF\n")
    assert front_door.identify(str(auth), cfg, m) == (front_door.WORKSPACE, "foo")
    # Not an operator key either: a stranger is refused, never promoted.
    auth.write_text("publickey ssh-ed25519 AAAAOPERATOR\n")
    assert front_door.identify(str(auth), cfg, m)[0] == front_door.DENIED


def test_front_door_self_mode_only_runs_dev(monkeypatch, capsys):
    monkeypatch.setattr(front_door, "identify", lambda: (front_door.WORKSPACE, "foo"))
    seen = []
    monkeypatch.setattr(front_door.cli, "main", lambda args, self_name=None: seen.append((args, self_name)) or 0)
    assert front_door.main(["dev", "rm"]) == 0
    assert seen == [(["rm"], "foo")]
    assert front_door.main(["proxy", "bar.orchard"]) == 2
    assert "inside workspace foo" in capsys.readouterr().err
