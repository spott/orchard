"""Kubernetes access for workspaces: short-lived ServiceAccount tokens.

A `k8s:<role>` grant names a role from the configured catalog; a role is one
ServiceAccount in one namespace, whose RBAC lives in the cluster (not here).
Orchard holds a minter token that may do exactly one thing: create tokens
for those ServiceAccounts (the TokenRequest API). `token()` asks for one,
caches it per workspace and role under the state dir, and hands it out until
it is close to expiry. Nothing long-lived ever enters a workspace: its
kubeconfig runs `dev token k8s <role> --exec` as a credential plugin.

A bound token cannot be recalled; revoking a grant stops the next mint and
drops the cache, and what is out there dies within `k8s_token_minutes`.

Standard library only."""

from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable

from . import OrchardError
from .config import Config

TOKEN_MARGIN = 5 * 60  # reuse a cached token unless it expires within this
TIMEOUT = 10.0


class K8sError(OrchardError):
    pass


class K8s:
    def __init__(
        self,
        cfg: Config,
        log: Callable[[str], None] | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.log = log or (lambda msg: print(f"orchard: {msg}", file=sys.stderr, flush=True))
        self.now = now

    # -- plumbing (overridden by the test fake) -----------------------------

    def request(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        """One POST to the API server as the minter."""
        cfg = self.cfg
        try:
            with open(cfg.k8s_token_file) as f:
                minter = f.read().strip()
        except OSError as e:
            raise K8sError(f"cannot read the minter token {cfg.k8s_token_file}: {e}") from e
        ctx = ssl.create_default_context(cafile=cfg.k8s_ca_file)
        req = urllib.request.Request(cfg.k8s_server.rstrip("/") + path, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", f"Bearer {minter}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "orchard")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"null")
            except json.JSONDecodeError:
                return e.code, {"message": raw.decode(errors="replace")[:200]}
        except (urllib.error.URLError, OSError) as e:
            raise K8sError(f"cannot reach the cluster at {cfg.k8s_server}: {e}") from e

    # -- roles ---------------------------------------------------------------

    def role(self, name: str) -> dict[str, str]:
        role = self.cfg.k8s_roles.get(name)
        if not role:
            known = ", ".join(sorted(self.cfg.k8s_roles)) or "none configured"
            raise K8sError(f"unknown k8s role {name!r} (one of: {known})")
        return role

    # -- tokens --------------------------------------------------------------

    def _cache_file(self, workspace: str, role: str) -> str:
        return os.path.join(self.cfg.state_dir, "k8s", f"{workspace}.{role}.json")

    def token(self, workspace: str, role_name: str) -> dict[str, Any]:
        """A token for the role's ServiceAccount: cached while it has more
        than TOKEN_MARGIN left, else a fresh TokenRequest."""
        role = self.role(role_name)
        path = self._cache_file(workspace, role_name)
        try:
            with open(path) as f:
                cached = json.load(f)
            if cached.get("serviceaccount") == role["serviceAccount"] and cached.get("namespace") == role["namespace"] \
                    and cached["expires_epoch"] - self.now() > TOKEN_MARGIN:
                return cached
        except (OSError, ValueError, KeyError):
            pass
        ns, sa = role["namespace"], role["serviceAccount"]
        status, data = self.request(
            f"/api/v1/namespaces/{ns}/serviceaccounts/{sa}/token",
            {"apiVersion": "authentication.k8s.io/v1", "kind": "TokenRequest",
             "spec": {"expirationSeconds": self.cfg.k8s_token_minutes * 60}},
        )
        if status not in (200, 201) or not isinstance(data, dict) or not data.get("status", {}).get("token"):
            msg = data.get("message") if isinstance(data, dict) else data
            raise K8sError(f"the cluster refused a token for {ns}/{sa} (HTTP {status}): {msg}")
        expires = data["status"]["expirationTimestamp"]
        tok = {
            "token": data["status"]["token"], "expires_at": expires,
            "expires_epoch": datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp(),
            "role": role_name, "namespace": ns, "serviceaccount": sa,
        }
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        os.replace(path + ".tmp", path)
        self.log(f"{workspace}: k8s token for {role_name} ({ns}/{sa}) until {expires}")
        return tok

    def forget(self, workspace: str, role_name: str | None = None) -> None:
        """Drop cached tokens for the workspace (one role, or all)."""
        d = os.path.join(self.cfg.state_dir, "k8s")
        try:
            entries = os.listdir(d)
        except OSError:
            return
        for e in entries:
            if e == f"{workspace}.{role_name}.json" or (role_name is None and e.startswith(f"{workspace}.") and e.endswith(".json")
                                                       and e[len(workspace) + 1:-5] in self.cfg.k8s_roles):
                try:
                    os.unlink(os.path.join(d, e))
                except OSError:
                    pass

    # -- kubeconfig ----------------------------------------------------------

    def kubeconfig(self, roles: list[str], client: str) -> str:
        """A kubeconfig with one context per role; each user is the `dev`
        client run as a credential plugin, so the file holds no secret."""
        cfg = self.cfg
        with open(cfg.k8s_ca_file, "rb") as f:
            ca = base64.b64encode(f.read()).decode()
        doc = {
            "apiVersion": "v1", "kind": "Config", "current-context": roles[0],
            "clusters": [{"name": "cluster", "cluster": {"server": cfg.k8s_server, "certificate-authority-data": ca}}],
            "contexts": [
                {"name": r, "context": {"cluster": "cluster", "user": r, "namespace": self.role(r)["namespace"]}}
                for r in roles
            ],
            "users": [
                {"name": r, "user": {"exec": {
                    "apiVersion": "client.authentication.k8s.io/v1", "command": client,
                    "args": ["token", "k8s", r, "--exec"], "interactiveMode": "Never",
                }}}
                for r in roles
            ],
        }
        # JSON is YAML; kubectl reads it as is.
        return json.dumps(doc, indent=2) + "\n"
