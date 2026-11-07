"""0.4: `name.repo` workspaces, grants, GitHub App tokens, secrets at start,
the shared logins volume, clone at create."""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from orchard import OrchardError, cli, dns
from orchard.cli import SelfRefused, self_argv
from orchard.github import GitHub, GitHubError
from orchard.secrets import Secrets
from orchard.workspace import KEY, LOGINS_DEVICE, Manager

from .conftest import FROZEN
from .test_dns import query


class FakeGitHub(GitHub):
    """No openssl, no network: records requests, mints predictable tokens."""

    def __init__(self, cfg, log=None, now=None):
        super().__init__(cfg, log=log, now=now or (lambda: FROZEN.timestamp()))
        self.requests: list[tuple[str, str, str, dict | None]] = []
        self.minted = 0
        self.revoked: list[str] = []
        self.repos = ["spott/iac", "spott/orchard", "spott/mcps"]
        self.installations = [{"id": 77, "account": {"login": "spott"}}]

    def sign(self, data):
        return b"SIG" + data[:4]

    def request(self, method, path, auth, body=None):
        self.requests.append((method, path, auth, body))
        if path.startswith("/app/installations?"):
            return 200, self.installations
        if path.startswith("/app/installations/77/access_tokens"):
            self.minted += 1
            repos = (body or {}).get("repositories") or [r.split("/")[1] for r in self.repos]
            expires = (FROZEN + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return 201, {
                "token": f"ghs_{self.minted}_{'+'.join(repos)}",
                "expires_at": expires,
                "repositories": [{"full_name": f"spott/{r}"} for r in repos],
                "permissions": (body or {}).get("permissions") or {"contents": "write", "pull_requests": "write"},
            }
        if path.startswith("/installation/repositories"):
            return 200, {"repositories": [{"full_name": r} for r in self.repos]}
        if path == "/installation/token" and method == "DELETE":
            self.revoked.append(auth)
            return 204, None
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def gh_cfg(cfg, tmp_path):
    cfg.github_app_id = "1234"
    cfg.github_key_file = str(tmp_path / "app.pem")
    cfg.github_owner = "spott"
    cfg.state_dir = str(tmp_path / "state")
    cfg.address_start, cfg.address_end = "10.100.0.100", "10.100.0.110"
    return cfg


@pytest.fixture
def gh(gh_cfg, logs):
    return FakeGitHub(gh_cfg, log=logs.append)


@pytest.fixture
def gmgr(gh_cfg, inc, logs, gh):
    return Manager(gh_cfg, inc, log=logs.append, probe=lambda ip, port: True, sleep=lambda s: None, now=lambda: FROZEN, github=gh)


def _calls(inc, op):
    return [c for c in inc.calls if c[0] == op]


# -- names through the manager --------------------------------------------------


def test_dotted_workspace_is_an_encoded_instance(mgr, inc):
    ws = mgr.create("blah.iac")
    assert ws.name == "blah.iac" and ws.instance == "blah--iac"
    assert "blah--iac" in inc.inst and ws.volume == "ws-blah--iac"
    assert [w.name for w in mgr.list()] == ["blah.iac"]
    assert mgr.require("blah.iac").instance == "blah--iac"
    mgr.rename("blah.iac", "exp.iac")
    assert "exp--iac" in inc.inst and "ws-exp--iac" in inc.vols and "blah--iac" not in inc.inst
    child = mgr.fork("exp.iac")
    assert child.name == "exp-1.iac" and child.instance == "exp-1--iac" and child.parent == "exp.iac"
    assert mgr.rm("exp.iac", tree=True) == ["exp-1.iac", "exp.iac"]
    assert inc.inst == {} and any(v.startswith("trash-exp--iac-") for v in inc.vols)


def test_rebase_and_rollback_keep_the_encoded_name(mgr, inc):
    inc.images["new"] = "aaaa1111bbbb2222cccc"
    mgr.create("blah.iac")
    ws, old = mgr.rebase("blah.iac", "new")
    assert ws.instance == "blah--iac" and old.startswith("blah--iac-old-")
    assert mgr.old_instances("blah.iac") == [old]
    ws, parked = mgr.rebase_rollback("blah.iac")
    assert ws.instance == "blah--iac" and parked.startswith("blah--iac-old-")
    assert len(mgr.rebase_prune("blah.iac")) >= 2


# -- grants -------------------------------------------------------------------


def test_name_repo_seeds_a_grant_and_plain_names_get_none(gmgr):
    ws = gmgr.create("blah.iac")
    assert ws.repos() == ["spott/iac"] and ws.grants[0]["by"] == "create"
    assert ws.repo == "https://github.com/spott/iac"
    assert gmgr.create("foo").grants == []


def test_create_repo_flag_grants_and_urls_are_only_recorded(gmgr):
    assert gmgr.create("a", repo="orchard").repos() == ["spott/orchard"]
    assert gmgr.create("b", repo="other/thing").repos() == ["other/thing"]
    ws = gmgr.create("c", repo="https://example.com/x.git")
    assert ws.grants == [] and ws.repo == "https://example.com/x.git"


def test_grant_revoke_expiry_and_fork_inheritance(gmgr, gh):
    gmgr.create("blah.iac")
    ws = gmgr.grant("blah.iac", "github", "orchard")
    assert ws.repos() == ["spott/iac", "spott/orchard"]
    # Replacing an existing grant keeps one entry.
    ws = gmgr.grant("blah.iac", "github", "spott/orchard", expires=(FROZEN + timedelta(hours=2)).isoformat())
    assert [g["value"] for g in ws.grants] == ["spott/iac", "spott/orchard"]
    assert ws.repos(FROZEN + timedelta(hours=3)) == ["spott/iac"]
    assert ws.repos(FROZEN + timedelta(hours=1)) == ["spott/iac", "spott/orchard"]
    child = gmgr.fork("blah.iac")
    assert child.repos() == ws.repos()
    ws = gmgr.revoke("blah.iac", "github", "orchard")
    assert ws.repos() == ["spott/iac"]
    with pytest.raises(OrchardError, match="no github grant"):
        gmgr.revoke("blah.iac", "github", "orchard")
    with pytest.raises(OrchardError, match="unknown grant kind"):
        gmgr.grant("blah.iac", "aws", "x")
    with pytest.raises(OrchardError, match="owner/repo"):
        gmgr.grant("blah.iac", "github", "https://github.com/a/b")


# -- tokens -------------------------------------------------------------------


def test_token_is_narrowed_to_the_grants_cached_and_revoked(gmgr, gh, inc):
    gmgr.create("blah.iac")
    gmgr.grant("blah.iac", "github", "orchard")
    tok = gmgr.token("blah.iac")
    assert tok["token"] == "ghs_1_iac+orchard" and tok["repositories"] == ["spott/iac", "spott/orchard"]
    mint = [r for r in gh.requests if r[1].startswith("/app/installations/77/access_tokens")]
    assert mint[-1][3] == {"repositories": ["iac", "orchard"]}
    assert mint[-1][2].startswith("Bearer ") and mint[-1][2].count(".") == 2
    # Second ask within the hour: the cache answers, nothing minted.
    assert gmgr.token("blah.iac")["token"] == tok["token"] and gh.minted == 1
    # A grant change revokes the cached token; the next mint is fresh and narrower.
    gmgr.revoke("blah.iac", "github", "orchard")
    assert gh.revoked == ["token ghs_1_iac+orchard"]
    assert gmgr.token("blah.iac")["token"] == "ghs_2_iac"
    # rm revokes what is live.
    gmgr.rm("blah.iac")
    assert gh.revoked[-1] == "token ghs_2_iac"
    assert not os.path.exists(gh._cache_path("blah.iac"))


def test_token_refusals(gmgr, mgr, inc):
    gmgr.create("foo")
    with pytest.raises(OrchardError, match="no active github grants"):
        gmgr.token("foo")
    from orchard.config import Config

    plain = Manager(Config(lock_dir=gmgr.cfg.lock_dir), inc, log=lambda m: None, probe=lambda ip, port: True, sleep=lambda s: None)
    plain.create("bar")
    with pytest.raises(OrchardError, match="not configured"):
        plain.token("bar")
    gmgr.grant("foo", "github", "other/thing")
    with pytest.raises(GitHubError, match="only repositories under"):
        gmgr.token("foo")


def test_token_permissions_are_the_union_of_the_grants(gmgr, gh):
    gmgr.create("foo")
    gmgr.grant("foo", "github", "iac", permissions={"contents": "read"})
    gmgr.grant("foo", "github", "orchard", permissions={"contents": "write", "metadata": "read"})
    tok = gmgr.token("foo")
    assert tok["permissions"] == {"contents": "write", "metadata": "read"}
    # One grant without permissions = the App's full set for the whole token.
    gmgr.grant("foo", "github", "mcps")
    gmgr.token("foo")
    assert [r for r in gh.requests if "access_tokens" in r[1]][-1][3] == {"repositories": ["iac", "mcps", "orchard"]}


def test_installation_and_repository_list_are_cached(gh):
    assert gh.installation_id() == 77
    assert gh.installation_id() == 77
    assert len([r for r in gh.requests if r[1].startswith("/app/installations?")]) == 1
    assert gh.installed_repos() == ["spott/iac", "spott/mcps", "spott/orchard"]
    # The metadata-only token used for the listing is revoked right away.
    assert gh.revoked and gh.repos == gh.repos
    n = len(gh.requests)
    assert gh.installed_repos() == ["spott/iac", "spott/mcps", "spott/orchard"] and len(gh.requests) == n


def test_jwt_shape(gh):
    head, claims, sig = gh.jwt().split(".")
    import base64

    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"alg": "RS256", "typ": "JWT"}
    c = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert c["iss"] == "1234" and c["exp"] - c["iat"] == 600
    assert base64.urlsafe_b64decode(pad(sig)).startswith(b"SIG")


# -- self-service and cli -------------------------------------------------------


def test_self_argv_token_is_self_only():
    assert self_argv(["token", "github"], "foo") == ["token", "github", "foo"]
    assert self_argv(["token", "github", "--git"], "foo") == ["token", "github", "foo", "--git"]
    assert self_argv(["token", "github", "foo"], "foo") == ["token", "github", "foo"]
    with pytest.raises(SelfRefused):
        self_argv(["token", "github", "bar"], "foo")
    with pytest.raises(SelfRefused):
        self_argv(["grant", "foo", "x"], "foo")


@pytest.fixture
def grun(monkeypatch, gmgr, gh_cfg, inc, capsys):
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda c, path=None: gh_cfg))
    monkeypatch.setattr(cli, "Incus", lambda binary: inc)
    monkeypatch.setattr(cli, "Manager", lambda c, i: gmgr)

    def _run(*argv, self_name=None):
        code = cli.main(list(argv), self_name=self_name)
        out, err = capsys.readouterr()
        return code, out, err

    return _run


def test_cli_grant_token_revoke(grun):
    code, out, _ = grun("create", "blah.iac")
    assert code == 0 and out.startswith("blah.iac ")
    code, out, _ = grun("grant", "blah.iac", "orchard", "--for", "2h", "--permissions", "contents=read")
    assert code == 0 and "spott/orchard" in out
    code, out, _ = grun("--json", "status", "blah.iac")
    g = json.loads(out)["grants"]
    assert g[1]["expires"] == "2026-11-01T14:00:00+00:00" and g[1]["permissions"] == {"contents": "read"}
    code, out, _ = grun("token", "github", "--git", self_name="blah.iac")
    assert code == 0 and out == "username=x-access-token\npassword=ghs_1_iac+orchard\n"
    code, out, _ = grun("token", "github", "blah.iac", "--json")
    assert code == 0 and json.loads(out)["workspace"] == "blah.iac"
    code, out, err = grun("grant", "blah.iac", "x", "--for", "soon")
    assert code == 1 and "--for" in err
    code, out, _ = grun("revoke", "blah.iac", "orchard")
    assert code == 0 and "orchard" not in out
    code, out, _ = grun("ls")
    assert "spott/iac" in out and "GRANTS" in out


# -- dns ------------------------------------------------------------------------


def test_lookup_of_name_repo_creates_it_unless_the_repo_is_unknown(gmgr, gh_cfg, gh, inc):
    gh_cfg.dns_zone = "orchard.example.com"
    gh_cfg.dns_reverse_zone = "100.10.in-addr.arpa"
    woken = []
    r = dns.Resolver(gh_cfg, gmgr, forward=lambda d: None, wake=lambda n, a, c: woken.append((n, a, c)), log=lambda m: None)
    resp = r.respond(query("exp.iac.orchard.example.com"))
    assert resp[3] & 0x0F == 0 and woken == [("exp.iac", "10.100.0.100", True)]
    resp = r.respond(query("exp.nosuch.orchard.example.com"))
    assert resp[3] & 0x0F == dns.RCODE_NXDOMAIN and len(woken) == 1
    # Plain names are never checked against GitHub.
    r.respond(query("typo.orchard.example.com"))
    assert woken[-1][0] == "typo"
    # Creating through the wake path seeds the grant.
    r._wake_now("exp.iac", "10.100.0.100", True)
    assert gmgr.require("exp.iac").repos() == ["spott/iac"]


def test_repo_check_fails_open_without_github(mgr):
    assert mgr.repo_allowed("exp.anything")


# -- secrets ----------------------------------------------------------------------


class FakeSecrets(Secrets):
    def __init__(self, cfg, log=None):
        super().__init__(cfg, log)
        self.reads: list[str] = []
        self.values = {"op://dev/claude/credential": "sk-ant-1", "op://dev/obsidian-mcp/bearer": "ob 1"}

    def read(self, ref):
        self.reads.append(ref)
        return self.values[ref]


@pytest.fixture
def smgr(cfg, inc, logs, tmp_path):
    cfg.secrets_env = {"CLAUDE_CODE_OAUTH_TOKEN": "op://dev/claude/credential", "OBSIDIAN_MCP_TOKEN": "op://dev/obsidian-mcp/bearer"}
    cfg.secrets_cache = str(tmp_path / "cache")
    sec = FakeSecrets(cfg, logs.append)
    m = Manager(cfg, inc, log=logs.append, probe=lambda ip, port: True, sleep=lambda s: None, now=lambda: FROZEN, secrets=sec)
    return m


def test_secrets_are_cached_once_and_pushed_at_every_start(smgr, inc, logs):
    smgr.create("foo")
    assert not any(c[0] == "file_push" and c[2].endswith("/orchard/env") for c in inc.calls)
    assert any("no secrets cached yet" in m for m in logs)
    assert smgr.secrets.refresh() == ["CLAUDE_CODE_OAUTH_TOKEN", "OBSIDIAN_MCP_TOKEN"]
    text = open(smgr.secrets.env_file()).read()
    assert "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-1\n" in text and "OBSIDIAN_MCP_TOKEN='ob 1'\n" in text
    assert smgr.secrets.cached()[0] == ["CLAUDE_CODE_OAUTH_TOKEN", "OBSIDIAN_MCP_TOKEN"]
    smgr.start("foo")
    pushes = [c for c in inc.calls if c[0] == "file_push" and c[2] == "/run/user/1000/orchard/env"]
    assert pushes == [("file_push", "foo", "/run/user/1000/orchard/env", 1000, 100, "0600")]
    assert inc.files["foo"]["/run/user/1000/orchard/env"] == text
    assert ("foo", ("mountpoint", "-q", "/run/user/1000"), None, None, None) in inc.execs
    # A stopped workspace holds nothing; the next start pushes again.
    smgr.stop("foo")
    smgr.start("foo")
    assert len([c for c in inc.calls if c[0] == "file_push" and c[2].endswith("/orchard/env")]) == 2


def test_secrets_wait_for_the_runtime_dir_and_give_up(smgr, inc, logs):
    smgr.secrets.refresh()
    inc.exec_out["foo"] = (1, "", "")  # mountpoint never succeeds
    smgr.create("foo")
    assert not any(c[0] == "file_push" and c[2].endswith("/orchard/env") for c in inc.calls)
    assert any("never appeared" in m for m in logs)


def test_secrets_refresh_keeps_the_old_cache_on_failure(smgr):
    smgr.secrets.refresh()
    before = open(smgr.secrets.env_file()).read()
    smgr.secrets.values.pop("op://dev/obsidian-mcp/bearer")
    with pytest.raises(KeyError):
        smgr.secrets.refresh()
    assert open(smgr.secrets.env_file()).read() == before


def test_cli_secrets(monkeypatch, smgr, cfg, inc, capsys):
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda c, path=None: cfg))
    monkeypatch.setattr(cli, "Incus", lambda binary: inc)
    monkeypatch.setattr(cli, "Manager", lambda c, i: smgr)
    smgr.create("foo")
    smgr.create("bar")
    smgr.stop("bar")
    assert cli.main(["secrets", "ls"]) == 0 and "no secrets cached" in capsys.readouterr().out
    assert cli.main(["--json", "secrets", "refresh"]) == 0
    assert json.loads(capsys.readouterr().out) == {"refreshed": ["CLAUDE_CODE_OAUTH_TOKEN", "OBSIDIAN_MCP_TOKEN"], "pushed": ["foo"]}
    assert cli.main(["secrets", "push", "foo"]) == 0 and "foo" in capsys.readouterr().out
    assert cli.main(["--json", "secrets", "ls"]) == 0
    assert json.loads(capsys.readouterr().out)["keys"] == ["CLAUDE_CODE_OAUTH_TOKEN", "OBSIDIAN_MCP_TOKEN"]


def test_cli_secrets_unconfigured(monkeypatch, mgr, cfg, inc, capsys):
    monkeypatch.setattr(cli.Config, "load", classmethod(lambda c, path=None: cfg))
    monkeypatch.setattr(cli, "Incus", lambda binary: inc)
    monkeypatch.setattr(cli, "Manager", lambda c, i: mgr)
    assert cli.main(["secrets", "ls"]) == 1 and "no secrets are configured" in capsys.readouterr().err


# -- shared logins volume ---------------------------------------------------------


def test_logins_volume_is_made_once_and_attached_everywhere(mgr, cfg, inc):
    cfg.logins_volume = "logins"
    mgr.create("foo")
    assert inc.vols["logins"]["config"]["security.shifted"] == "true" and inc.vols["logins"]["config"]["initial.mode"] == "0700"
    assert inc.inst["foo"]["devices"][LOGINS_DEVICE] == {
        "type": "disk", "pool": "default", "source": "logins", "path": "/home/spott/.orchard/logins",
    }
    # Create order: volume attached before the first start.
    ops = [c[0] for c in inc.calls]
    assert ops.index("device_add") < ops.index("start")
    # An existing workspace without it gets it on the next start, live.
    inc.inst["foo"]["devices"].pop(LOGINS_DEVICE)
    inc.calls.clear()
    mgr.start("foo")
    assert [c for c in _calls(inc, "device_add") if c[2] == LOGINS_DEVICE] and len(_calls(inc, "volume_create")) == 0
    # rm never touches it; a fork and a rebase carry it.
    mgr.fork("foo")
    assert LOGINS_DEVICE in inc.inst["foo-1"]["devices"]
    inc.images["new"] = "aaaa1111bbbb2222cccc"
    mgr.rebase("foo", "new")
    assert LOGINS_DEVICE in inc.inst["foo"]["devices"]
    mgr.rm("foo", tree=True)
    assert "logins" in inc.vols


def test_logins_off_by_default(mgr, inc):
    mgr.create("foo")
    assert LOGINS_DEVICE not in inc.inst["foo"]["devices"] and "logins" not in inc.vols


# -- clone at create -------------------------------------------------------------


def test_create_clones_the_repo_with_a_scoped_token_and_cleans_up(gmgr, gh, inc):
    inc.exec_out["blah--iac"] = (1, "", "")  # `test -e target` says missing...
    # ...but then every exec would fail; use a per-command answer instead.
    inc.exec_out.pop("blah--iac")
    seen = []

    orig = inc.exec

    def exec_(name, *cmd, check=True, user=None, group=None, env=None):
        seen.append(cmd)
        if cmd[:2] == ("test", "-e"):
            import subprocess

            return subprocess.CompletedProcess([], 1, "", "")
        return orig(name, *cmd, check=check, user=user, group=group, env=env)

    inc.exec = exec_
    ws = gmgr.create("blah.iac")
    assert ws.clone == "ok" and inc.inst["blah--iac"]["config"][KEY + "clone"] == "ok"
    clone = [c for c in seen if "clone" in c][0]
    assert clone[-1] == "/home/spott/work/iac" and clone[-2] == "https://github.com/spott/iac"
    assert "credential.helper=store --file=/run/user/1000/orchard/git-credentials" in clone
    push = [c for c in inc.calls if c[0] == "file_push" and c[2].endswith("git-credentials")][0]
    assert push[3:] == (1000, 100, "0600")
    assert inc.files["blah--iac"]["/run/user/1000/orchard/git-credentials"] == "https://x-access-token:ghs_1_iac@github.com\n"
    assert ("rm", "-f", "/run/user/1000/orchard/git-credentials") in seen
    # Nothing is cloned for a plain name, and an existing checkout is left alone.
    assert gmgr.create("foo").clone is None
    inc.exec = orig  # `test -e` succeeds again: the checkout is there
    assert gmgr.create("exp.orchard").clone == "skipped: /home/spott/work/orchard exists"


def test_clone_failure_is_recorded_not_fatal(gmgr, gh, inc):
    orig = inc.exec

    def exec_(name, *cmd, check=True, user=None, group=None, env=None):
        import subprocess

        if cmd[:2] == ("test", "-e"):
            return subprocess.CompletedProcess([], 1, "", "")
        if cmd[0] == "git":
            return subprocess.CompletedProcess([], 128, "", "fatal: repository not found\n")
        return orig(name, *cmd, check=check, user=user, group=group, env=env)

    inc.exec = exec_
    ws = gmgr.create("blah.iac")
    assert ws.running and ws.clone == "failed: fatal: repository not found"


def test_clone_without_github_is_skipped(mgr, cfg, inc):
    cfg.github_owner = "spott"  # owner known, App not configured: grant seeded, no clone
    ws = mgr.create("blah.iac")
    assert ws.repos() == ["spott/iac"] and ws.clone == "skipped: GitHub tokens are not configured"
