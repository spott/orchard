"""0.8: k8s grants (`dev grant NAME k8s:<role>`, `dev token k8s`, the kubeconfig)."""

import json
import os
from datetime import timedelta

import pytest

from orchard import OrchardError
from orchard.cli import SelfRefused, build_parser, cmd_token, self_argv
from orchard.workspace import KEY

from .conftest import FROZEN

KUBECONFIG = "/run/user/1000/orchard/kubeconfig"


def test_k8s_grant_pushes_a_kubeconfig_without_secrets(mgr, k8s_api, inc):
    mgr.create("foo")
    assert KUBECONFIG not in inc.files.get("foo", {})
    ws = mgr.grant("foo", "k8s", "mcp")
    assert [(g["kind"], g["value"]) for g in ws.grants] == [("k8s", "mcp")]
    assert ws.k8s_roles == "mcp"
    doc = json.loads(inc.files["foo"][KUBECONFIG])
    assert doc["current-context"] == "mcp"
    assert doc["clusters"][0]["cluster"]["server"] == "https://k3s.example:6443"
    assert doc["contexts"] == [{"name": "mcp", "context": {"cluster": "cluster", "user": "mcp", "namespace": "mcp"}}]
    ex = doc["users"][0]["user"]["exec"]
    assert ex["command"] == "/home/spott/.local/bin/dev" and ex["args"] == ["token", "k8s", "mcp", "--exec"]
    # Granting mints nothing and the file carries no token.
    assert k8s_api.calls == [] and "TOKEN" not in inc.files["foo"][KUBECONFIG]

    ws = mgr.grant("foo", "k8s", "cluster-view")
    doc = json.loads(inc.files["foo"][KUBECONFIG])
    assert [c["name"] for c in doc["contexts"]] == ["cluster-view", "mcp"]
    assert ws.k8s_roles == "cluster-view,mcp"


def test_k8s_token_mints_caches_and_names_the_serviceaccount(mgr, k8s_api, cfg):
    mgr.create("foo")
    mgr.grant("foo", "k8s", "mcp")
    tok = mgr.token("foo", "k8s")
    assert tok["token"] == "TOKEN-1" and tok["namespace"] == "mcp" and tok["workspace"] == "foo"
    assert tok["expires_at"] == (FROZEN + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path, body = k8s_api.calls[0]
    assert path == "/api/v1/namespaces/mcp/serviceaccounts/orchard-view/token"
    assert body["spec"]["expirationSeconds"] == 3600
    # Cached: a second call is no request; the cache is private.
    assert mgr.token("foo", "k8s", "mcp")["token"] == "TOKEN-1" and len(k8s_api.calls) == 1
    cache = os.path.join(cfg.state_dir, "k8s", "foo.mcp.json")
    assert os.stat(cache).st_mode & 0o777 == 0o600
    # Close to expiry: minted again.
    mgr.k8s.now = lambda: (FROZEN + timedelta(minutes=56)).timestamp()
    assert mgr.token("foo", "k8s")["token"] == "TOKEN-2"


def test_k8s_refusals(mgr, k8s_api, cfg):
    mgr.create("foo")
    with pytest.raises(OrchardError, match="no active k8s grants"):
        mgr.token("foo", "k8s")
    with pytest.raises(OrchardError, match="unknown k8s role"):
        mgr.grant("foo", "k8s", "kube-system")
    mgr.grant("foo", "k8s", "mcp")
    with pytest.raises(OrchardError, match="no active k8s grant for 'cluster-view'"):
        mgr.token("foo", "k8s", "cluster-view")
    mgr.grant("foo", "k8s", "cluster-view")
    with pytest.raises(OrchardError, match="several k8s grants"):
        mgr.token("foo", "k8s")
    k8s_api.status = 403
    with pytest.raises(OrchardError, match="refused a token for mcp/orchard-view"):
        mgr.token("foo", "k8s", "mcp")
    mgr.k8s = None
    with pytest.raises(OrchardError, match="not configured"):
        mgr.token("foo", "k8s", "mcp")
    with pytest.raises(OrchardError, match="not configured"):
        mgr.grant("foo", "k8s", "mcp")


def test_k8s_revoke_drops_cache_and_context(mgr, k8s_api, inc, cfg):
    mgr.create("foo")
    mgr.grant("foo", "k8s", "mcp")
    mgr.grant("foo", "k8s", "cluster-view")
    mgr.token("foo", "k8s", "mcp")
    mgr.token("foo", "k8s", "cluster-view")
    ws = mgr.revoke("foo", "k8s", "mcp")
    assert ws.k8s_roles == "cluster-view"
    assert [c["name"] for c in json.loads(inc.files["foo"][KUBECONFIG])["contexts"]] == ["cluster-view"]
    assert sorted(os.listdir(os.path.join(cfg.state_dir, "k8s"))) == ["foo.cluster-view.json"]
    with pytest.raises(OrchardError, match="no active k8s grant"):
        mgr.token("foo", "k8s", "mcp")
    ws = mgr.revoke("foo", "k8s", "cluster-view")
    assert ws.k8s_roles is None
    assert ("exec", "foo", ("rm", "-f", KUBECONFIG)) in [(c[0], c[1], tuple(c[2])) for c in inc.calls if c[0] == "exec"]
    assert os.listdir(os.path.join(cfg.state_dir, "k8s")) == []


def test_sweep_narrows_the_kubeconfig_when_a_grant_expires(mgr, k8s_api, inc):
    mgr.create("foo")
    mgr.grant("foo", "k8s", "mcp")
    mgr.grant("foo", "k8s", "cluster-view", expires=(FROZEN + timedelta(hours=1)).isoformat())
    assert mgr.sweep() == []
    mgr.now = lambda: FROZEN + timedelta(hours=2)
    assert mgr.sweep() == ["foo"]
    assert [c["name"] for c in json.loads(inc.files["foo"][KUBECONFIG])["contexts"]] == ["mcp"]
    assert inc.instance("foo")["config"][KEY + "k8s-roles"] == "mcp"
    with pytest.raises(OrchardError, match="no active k8s grant"):
        mgr.token("foo", "k8s", "cluster-view")


def test_restart_pushes_the_kubeconfig_again(mgr, k8s_api, inc):
    mgr.create("foo")
    mgr.grant("foo", "k8s", "mcp")
    mgr.stop("foo")
    inc.files["foo"].pop(KUBECONFIG, None)
    mgr.start("foo")
    assert KUBECONFIG in inc.files["foo"]


def test_self_service_token_k8s():
    assert self_argv(["token", "k8s"], "foo") == ["token", "k8s", "foo"]
    assert self_argv(["token", "k8s", "mcp", "--exec"], "foo") == ["token", "k8s", "foo", "mcp", "--exec"]
    assert self_argv(["token", "k8s", "foo", "mcp"], "foo") == ["token", "k8s", "foo", "mcp"]
    with pytest.raises(SelfRefused):
        self_argv(["token", "k8s", "bar", "mcp"], "foo")
    with pytest.raises(SelfRefused):
        self_argv(["token", "github", "bar"], "foo")


def test_exec_credential_output(mgr, k8s_api, capsys):
    mgr.create("foo")
    mgr.grant("foo", "k8s", "mcp")
    args = build_parser().parse_args(["token", "k8s", "foo", "mcp", "--exec"])
    assert cmd_token(mgr, args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "ExecCredential" and out["apiVersion"] == "client.authentication.k8s.io/v1"
    assert out["status"]["token"] == "TOKEN-1"
    assert out["status"]["expirationTimestamp"].endswith("Z")
