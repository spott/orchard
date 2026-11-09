"""0.5: ssh grants and certificates (`dev grant --kind ssh`, `dev token ssh`)."""

import os
import subprocess
from datetime import timedelta

import pytest

from orchard import OrchardError, workspace
from orchard.workspace import KEY

from .conftest import FROZEN


@pytest.fixture
def ca_cfg(cfg, tmp_path):
    ca = tmp_path / "ca"
    ca.write_text("FAKE CA PRIVATE KEY\n")
    cfg.ssh_ca_key_file = str(ca)
    cfg.ssh_keygen = "ssh-keygen"
    return cfg


@pytest.fixture
def keygen(monkeypatch):
    """A fake ssh-keygen: writes a key pair for `-t`, a certificate for `-s`;
    records every argv."""
    calls: list[list[str]] = []

    def fake(argv, **kw):
        calls.append(argv)
        if "-t" in argv:
            key = argv[argv.index("-f") + 1]
            open(key, "w").write("PRIVATE\n")
            open(key + ".pub", "w").write(f"ssh-ed25519 AAAAWS {argv[argv.index('-C') + 1]}\n")
        elif "-s" in argv:
            pub = argv[-1]
            principals = argv[argv.index("-n") + 1]
            open(pub[: -len(".pub")] + "-cert.pub", "w").write(f"ssh-ed25519-cert-v01@openssh.com AAAACERT {principals}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(workspace.subprocess, "run", fake)
    return calls


def _pushes(inc):
    return {c[2]: (c[3], c[4], c[5]) for c in inc.calls if c[0] == "file_push"}


def test_ssh_grant_mints_a_certificate_into_the_tmpfs(mgr, ca_cfg, inc, keygen):
    mgr.create("foo")
    inc.calls.clear()
    ws = mgr.grant("foo", "ssh", "ro")
    ws = mgr.grant("foo", "ssh", "printers", expires=(FROZEN + timedelta(hours=4)).isoformat())
    assert [(g["kind"], g["value"]) for g in ws.grants] == [("ssh", "ro"), ("ssh", "printers")]
    # The grant itself pushed a certificate (twice: once per grant); the last
    # one names both principals.
    sign = [a for a in keygen if "-s" in a][-1]
    assert sign[sign.index("-s") + 1] == ca_cfg.ssh_ca_key_file
    assert sign[sign.index("-I") + 1] == "ws:foo"
    assert sign[sign.index("-n") + 1] == "printers,ro"
    assert sign[sign.index("-V") + 1] == "+60m"
    pushes = _pushes(inc)
    assert pushes["/run/user/1000/orchard/ssh/id_ed25519"] == (1000, 100, "0600")
    assert pushes["/run/user/1000/orchard/ssh/id_ed25519-cert.pub"] == (1000, 100, "0644")
    assert inc.files["foo"]["/run/user/1000/orchard/ssh/id_ed25519-cert.pub"].endswith("printers,ro\n")
    assert inc.files["foo"]["/run/user/1000/orchard/ssh/id_ed25519"] == "PRIVATE\n"
    # Nothing of the key pair stays on the host.
    key_files = [a[a.index("-f") + 1] for a in keygen if "-f" in a]
    assert key_files and not any(os.path.exists(k) for k in key_files)

    tok = mgr.token("foo", "ssh")
    assert tok["principals"] == ["printers", "ro"] and tok["identity"] == "ws:foo"
    assert tok["expires"] == (FROZEN + timedelta(minutes=60)).isoformat(timespec="seconds")
    assert tok["certificate"] == "/run/user/1000/orchard/ssh/id_ed25519-cert.pub"
    assert "token" not in tok


def test_ssh_token_refusals_and_expiry(mgr, cfg, ca_cfg, inc, keygen):
    mgr.create("foo")
    with pytest.raises(OrchardError, match="no active ssh grants"):
        mgr.token("foo", "ssh")
    with pytest.raises(OrchardError, match="principal"):
        mgr.grant("foo", "ssh", "Not A Principal")
    mgr.grant("foo", "ssh", "ro", expires=(FROZEN - timedelta(minutes=1)).isoformat())
    with pytest.raises(OrchardError, match="no active ssh grants"):
        mgr.token("foo", "ssh")
    mgr.grant("foo", "ssh", "ro")
    mgr.stop("foo")
    with pytest.raises(OrchardError, match="not running"):
        mgr.token("foo", "ssh")
    cfg.ssh_ca_key_file = ""
    with pytest.raises(OrchardError, match="not configured"):
        mgr.token("foo", "ssh")


def test_start_pushes_a_certificate_and_revoke_drops_it(mgr, ca_cfg, inc, keygen):
    mgr.create("foo")
    mgr.grant("foo", "ssh", "ro")
    mgr.stop("foo")
    inc.calls.clear()
    keygen.clear()
    mgr.start("foo")
    assert any("-s" in a for a in keygen), "start re-mints for the active grants"
    assert "/run/user/1000/orchard/ssh/id_ed25519-cert.pub" in _pushes(inc)

    inc.calls.clear()
    keygen.clear()
    ws = mgr.revoke("foo", "ssh", "ro")
    assert ws.grants == []
    rm = [c for c in inc.calls if c[0] == "exec" and c[2][:2] == ("rm", "-f")]
    assert rm and rm[0][2][2:] == ("/run/user/1000/orchard/ssh/id_ed25519", "/run/user/1000/orchard/ssh/id_ed25519-cert.pub")
    assert not keygen, "nothing left to mint"


def test_ssh_grants_survive_rebase_and_fork_mints_afresh(mgr, ca_cfg, inc, keygen):
    mgr.create("foo")
    mgr.grant("foo", "ssh", "ro")
    keygen.clear()
    child = mgr.fork("foo", "bar")
    assert [(g["kind"], g["value"]) for g in child.grants] == [("ssh", "ro")]
    assert [a for a in keygen if "-s" in a and a[a.index("-I") + 1] == "ws:bar"], "the fork got its own certificate"
    assert KEY + "grants" in inc.inst["bar"]["config"]
