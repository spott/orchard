import json

import pytest

from orchard import cli
from orchard.config import Config
from orchard.workspace import Manager


@pytest.fixture
def run(monkeypatch, inc, cfg, capsys):
    m = Manager(cfg, inc, log=lambda s: None, probe=lambda ip, port: True, sleep=lambda s: None)
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda c, path=None: cfg))
    monkeypatch.setattr(cli, "Incus", lambda binary: inc)
    monkeypatch.setattr(cli, "Manager", lambda c, i: m)

    def _run(*argv):
        code = cli.main(list(argv))
        out, err = capsys.readouterr()
        return code, out, err

    return _run


def test_create_ls_status_json(run):
    code, out, _ = run("--json", "create", "foo", "--repo", "r")
    assert code == 0 and json.loads(out)["name"] == "foo"
    code, out, _ = run("--json", "ls")
    assert [w["name"] for w in json.loads(out)] == ["foo"]
    code, out, _ = run("ls", "--json")
    assert code == 0 and [w["name"] for w in json.loads(out)] == ["foo"]
    code, out, _ = run("ls")
    assert code == 0 and out.startswith("NAME") and "foo" in out
    code, out, _ = run("status", "foo")
    assert code == 0 and out.startswith("foo")


def test_errors_and_usage(run):
    code, out, err = run("status", "nope")
    assert code == 1 and out == "" and "no workspace" in err
    code, out, err = run("bogus")
    assert code == 2
    code, out, err = run("create", "Bad Name")
    assert code == 1 and "invalid workspace name" in err


def test_rm_and_gc(run):
    run("create", "foo")
    code, out, _ = run("rm", "foo")
    assert code == 0 and out.strip() == "removed foo"
    code, out, _ = run("--json", "gc", "--dry-run")
    assert code == 0 and json.loads(out) == {"deleted": [], "dry_run": True}
