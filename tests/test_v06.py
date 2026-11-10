"""0.6: the deploy window (`ssh:deploy` grants carry deploy.env while active) and `dev sweep`."""

import stat
from datetime import timedelta

import pytest

from orchard import OrchardError
from orchard.workspace import KEY, Manager

from .conftest import FROZEN


@pytest.fixture
def dep_cfg(ca_cfg, tmp_path):
    tok = tmp_path / "deploy-token"
    tok.write_text("DEPLOYTOKEN\n")
    op = tmp_path / "op"
    op.write_text('#!/bin/sh\ncase "$*" in *pve/password*) printf pw-secret ;; *) echo "no such item" >&2; exit 1 ;; esac\n')
    op.chmod(op.stat().st_mode | stat.S_IEXEC)
    ca_cfg.op = str(op)
    ca_cfg.deploy_token_file = str(tok)
    ca_cfg.deploy_env = {"PROXMOX_VE_PASSWORD": "op://homelab/pve/password"}
    return ca_cfg


@pytest.fixture
def clock():
    return {"now": FROZEN}


@pytest.fixture
def dmgr(dep_cfg, inc, logs, clock) -> Manager:
    return Manager(dep_cfg, inc, log=logs.append, probe=lambda ip, port: True, sleep=lambda s: None, now=lambda: clock["now"])


def _rm_calls(inc):
    return [c[2] for c in inc.calls if c[0] == "exec" and c[2][0] == "rm"]


CLOSE = ("rm", "-rf", "/run/user/1000/orchard/deploy.env", "/run/user/1000/orchard/deploy")


def test_deploy_grant_is_time_boxed(dmgr, inc, keygen):
    dmgr.create("iac")
    with pytest.raises(OrchardError, match="needs --for"):
        dmgr.grant("iac", "ssh", "deploy")
    with pytest.raises(OrchardError, match="at most 4h"):
        dmgr.grant("iac", "ssh", "deploy", expires=(FROZEN + timedelta(hours=5)).isoformat())
    dmgr.grant("iac", "ssh", "deploy", expires=(FROZEN + timedelta(hours=2)).isoformat())


def test_deploy_window_opens_and_closes_with_the_grant(dmgr, inc, keygen, logs):
    dmgr.create("iac")
    inc.calls.clear()
    until = (FROZEN + timedelta(hours=2)).isoformat()
    ws = dmgr.grant("iac", "ssh", "deploy", expires=until)
    env = inc.files["iac"]["/run/user/1000/orchard/deploy.env"]
    assert "OP_SERVICE_ACCOUNT_TOKEN=DEPLOYTOKEN\n" in env and "PROXMOX_VE_PASSWORD=pw-secret\n" in env
    pushes = {c[2]: (c[3], c[4], c[5]) for c in inc.calls if c[0] == "file_push"}
    assert pushes["/run/user/1000/orchard/deploy.env"] == (1000, 100, "0600")
    assert ws.deploy == until and ws.ssh_principals == "deploy"
    assert any("deploy window open" in m for m in logs)
    # The certificate carries the principal.
    sign = [a for a in keygen if "-s" in a][-1]
    assert sign[sign.index("-n") + 1] == "deploy"

    inc.calls.clear()
    ws = dmgr.revoke("iac", "ssh", "deploy")
    assert CLOSE in _rm_calls(inc)
    assert ws.deploy is None and ws.ssh_principals is None and ws.grants == []


def test_sweep_closes_a_lapsed_window_and_narrows_the_certificate(dmgr, inc, keygen, clock):
    dmgr.create("iac")
    dmgr.grant("iac", "ssh", "ro")
    dmgr.grant("iac", "ssh", "deploy", expires=(FROZEN + timedelta(hours=1)).isoformat())
    assert dmgr.require("iac").ssh_principals == "deploy,ro"
    assert dmgr.sweep() == [], "nothing lapsed yet"

    clock["now"] = FROZEN + timedelta(hours=1, minutes=1)
    inc.calls.clear()
    keygen.clear()
    assert dmgr.sweep() == ["iac"]
    ws = dmgr.require("iac")
    assert ws.ssh_principals == "ro" and ws.deploy is None
    assert CLOSE in _rm_calls(inc)
    sign = [a for a in keygen if "-s" in a][-1]
    assert sign[sign.index("-n") + 1] == "ro"
    # Expired grants remain listed (dev ls marks them) but confer nothing.
    assert [g["value"] for g in ws.grants] == ["ro", "deploy"]

    # Everything expired: the certificate goes away entirely.
    inc.inst["iac"]["config"][KEY + "grants"] = '[{"kind":"ssh","value":"ro","permissions":null,"expires":"2026-01-01T00:00:00+00:00","by":"t","at":"x"}]'
    inc.calls.clear()
    assert dmgr.sweep() == ["iac"]
    assert dmgr.require("iac").ssh_principals is None
    assert ("rm", "-f", "/run/user/1000/orchard/ssh/id_ed25519", "/run/user/1000/orchard/ssh/id_ed25519-cert.pub") in _rm_calls(inc)


def test_deploy_window_is_reopened_at_start_and_survives_a_bad_op(dmgr, dep_cfg, inc, keygen, logs):
    dmgr.create("iac")
    dmgr.grant("iac", "ssh", "deploy", expires=(FROZEN + timedelta(hours=2)).isoformat())
    dmgr.stop("iac")
    inc.calls.clear()
    dmgr.start("iac")
    assert "/run/user/1000/orchard/deploy.env" in {c[2] for c in inc.calls if c[0] == "file_push"}

    # A reference the account cannot read: start still succeeds, window stays shut.
    dep_cfg.deploy_env = {"NOPE": "op://homelab/missing/field"}
    dmgr.revoke("iac", "ssh", "deploy")
    logs.clear()
    dmgr.grant("iac", "ssh", "deploy", expires=(FROZEN + timedelta(hours=2)).isoformat())
    assert any("deploy window for iac not opened" in m for m in logs)
    assert dmgr.require("iac").deploy is None


def test_deploy_disabled_changes_nothing(mgr, ca_cfg, inc, keygen):
    mgr.create("iac")
    mgr.grant("iac", "ssh", "deploy", expires=(FROZEN + timedelta(hours=2)).isoformat())
    assert "/run/user/1000/orchard/deploy.env" not in inc.files.get("iac", {})
    assert mgr.require("iac").deploy is None


def test_sweep_copes_with_a_certificate_minted_before_principals_were_recorded(dmgr, inc, keygen):
    dmgr.create("old")
    dmgr.grant("old", "ssh", "printers")
    # Pretend 0.5 minted it: a certificate exists but no record of its principals.
    inc.inst["old"]["config"].pop(KEY + "ssh-principals")
    assert dmgr.sweep() == ["old"]
    assert dmgr.require("old").ssh_principals == "printers"
    # And a second sweep is a no-op.
    assert dmgr.sweep() == []


def test_deploy_files_come_and_go_with_the_window(dmgr, dep_cfg, inc, keygen, tmp_path):
    op = tmp_path / "op"
    op.write_text('#!/bin/sh\ncase "$*" in *pve/password*) printf pw-secret ;; *nixbuild*) printf "PRIVATE KEY\\n" ;; *) exit 1 ;; esac\n')
    dep_cfg.deploy_files = {"nixbuild-key": "op://homelab/nixbuild key/private key"}
    dmgr.create("iac")
    dmgr.grant("iac", "ssh", "deploy", expires=(FROZEN + timedelta(hours=1)).isoformat())
    pushes = {c[2]: (c[3], c[4], c[5]) for c in inc.calls if c[0] == "file_push"}
    assert pushes["/run/user/1000/orchard/deploy/nixbuild-key"] == (1000, 100, "0600")
    assert inc.files["iac"]["/run/user/1000/orchard/deploy/nixbuild-key"] == "PRIVATE KEY\n"
    inc.calls.clear()
    dmgr.revoke("iac", "ssh", "deploy")
    assert CLOSE in _rm_calls(inc)


def test_secrets_files_are_cached_and_pushed_at_start(dep_cfg, inc, logs, tmp_path, keygen):
    from orchard.workspace import Manager

    op = tmp_path / "op"
    op.write_text('#!/bin/sh\ncase "$*" in *claude*) printf tok ;; *hostkey*) printf "HOSTKEY\\n" ;; *) exit 1 ;; esac\n')
    tok = tmp_path / "token"
    tok.write_text("T\n")
    dep_cfg.secrets_token_file = str(tok)
    dep_cfg.secrets_cache = str(tmp_path / "cache")
    dep_cfg.secrets_env = {"CLAUDE": "op://dev/claude/credential"}
    dep_cfg.secrets_files = {"known_hosts": "op://dev/hostkey/notes"}
    m = Manager(dep_cfg, inc, log=logs.append, probe=lambda ip, port: True, sleep=lambda s: None, now=lambda: FROZEN)
    assert m.secrets.refresh() == ["CLAUDE", "known_hosts"]
    assert m.secrets.cached_files() == ["known_hosts"]
    m.create("foo")
    pushes = {c[2]: (c[3], c[4], c[5]) for c in inc.calls if c[0] == "file_push"}
    assert pushes["/run/user/1000/orchard/files/known_hosts"] == (1000, 100, "0600")
    assert inc.files["foo"]["/run/user/1000/orchard/files/known_hosts"] == "HOSTKEY\n"
    assert "CLAUDE=tok" in inc.files["foo"]["/run/user/1000/orchard/env"]
