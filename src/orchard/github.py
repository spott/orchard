"""GitHub App installation tokens, one per workspace, narrowed to what the
workspace's grants name.

Orchard holds the App's private key (on the host, never in a workspace).
`token()` signs a JWT with it, asks GitHub for an installation token limited
to the given repositories and permissions, and caches the result per
workspace under the state dir so a credential helper calling every few
seconds costs one request an hour. `revoke()` kills what was cached.

Standard library only: the RS256 signature is `openssl dgst -sha256 -sign`,
the HTTP is urllib."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Callable

from . import OrchardError
from .config import Config

TOKEN_MARGIN = 5 * 60  # reuse a cached token unless it expires within this
REPOS_TTL = 3600  # how long the installation's repository list is trusted
TIMEOUT = 10.0


class GitHubError(OrchardError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def parse_expiry(stamp: str) -> float:
    """GitHub's `2026-11-07T12:00:00Z` -> epoch seconds."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


class GitHub:
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

    def sign(self, data: bytes) -> bytes:
        proc = subprocess.run(
            [self.cfg.openssl, "dgst", "-sha256", "-sign", self.cfg.github_key_file],
            input=data, capture_output=True,
        )
        if proc.returncode != 0:
            raise GitHubError(f"signing the App JWT failed: {proc.stderr.decode().strip()}")
        return proc.stdout

    def request(self, method: str, path: str, auth: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        """One API call. `auth` is the full Authorization header value."""
        url = path if path.startswith("http") else self.cfg.github_api.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", auth)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "orchard")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw.decode(errors="replace")
            return e.code, payload
        except (urllib.error.URLError, OSError) as e:
            raise GitHubError(f"GitHub unreachable: {e}") from e

    # -- the App -----------------------------------------------------------

    def jwt(self) -> str:
        now = int(self.now())
        head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        claims = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(self.cfg.github_app_id)}, separators=(",", ":")).encode())
        signing = f"{head}.{claims}"
        return f"{signing}.{_b64(self.sign(signing.encode()))}"

    def installation_id(self) -> int:
        """The App's installation on the configured owner, cached forever
        (an uninstall + reinstall changes it: delete the cache file)."""
        path = os.path.join(self._dir(), "installation")
        try:
            return int(open(path).read().strip())
        except (OSError, ValueError):
            pass
        status, payload = self.request("GET", "/app/installations?per_page=100", f"Bearer {self.jwt()}")
        if status != 200:
            raise GitHubError(f"listing App installations failed ({status}): {_msg(payload)}")
        for inst in payload or []:
            if (inst.get("account") or {}).get("login", "").lower() == self.cfg.github_owner.lower():
                self._write(path, str(inst["id"]))
                return int(inst["id"])
        raise GitHubError(f"the App is not installed on {self.cfg.github_owner!r}")

    def installed_repos(self, max_age: float = REPOS_TTL) -> list[str] | None:
        """`owner/repo` of everything the installation can reach, from a cache
        at most max_age old; refreshed with a metadata-only token. None when
        GitHub cannot be asked and nothing is cached (callers decide)."""
        path = os.path.join(self._dir(), "repositories.json")
        try:
            st = os.stat(path)
            cached = json.load(open(path))
            if self.now() - st.st_mtime < max_age:
                return cached
        except (OSError, ValueError):
            cached = None
        try:
            tok = self._mint(None, {"metadata": "read"})["token"]
            repos: list[str] = []
            page = 1
            while True:
                status, payload = self.request("GET", f"/installation/repositories?per_page=100&page={page}", f"token {tok}")
                if status != 200:
                    raise GitHubError(f"listing repositories failed ({status}): {_msg(payload)}")
                batch = [r["full_name"] for r in (payload or {}).get("repositories", [])]
                repos += batch
                if len(batch) < 100:
                    break
                page += 1
            self.request("DELETE", "/installation/token", f"token {tok}")
            self._write(path, json.dumps(sorted(repos)))
            return sorted(repos)
        except GitHubError as e:
            self.log(f"warning: {e}; using the cached repository list" if cached is not None else f"warning: {e}")
            return cached

    def token(self, workspace: str, repos: list[str], permissions: dict[str, str] | None = None) -> dict[str, Any]:
        """An installation token for `workspace` limited to `repos`
        (`owner/repo`, all under the configured owner) and `permissions`
        (default: everything the App was installed with). Cached per
        workspace and request shape until shortly before it expires."""
        if not repos:
            raise GitHubError(f"{workspace} has no repository grants")
        owner = self.cfg.github_owner.lower()
        short: list[str] = []
        for full in sorted(set(repos)):
            o, _, r = full.partition("/")
            if o.lower() != owner or not r:
                raise GitHubError(f"{full!r}: only repositories under {self.cfg.github_owner!r} can be minted for")
            short.append(r)
        want = {"repositories": short, "permissions": permissions or None}
        key = hashlib.sha256(json.dumps(want, sort_keys=True).encode()).hexdigest()[:16]
        cache = self._cached(workspace)
        hit = cache.get(key)
        if hit and parse_expiry(hit["expires_at"]) - self.now() > TOKEN_MARGIN:
            return hit
        minted = self._mint(short, permissions)
        cache[key] = minted
        self._write(self._cache_path(workspace), json.dumps(cache))
        return minted

    def revoke(self, workspace: str) -> int:
        """Revoke every token cached for the workspace. Returns how many."""
        cache = self._cached(workspace)
        n = 0
        for entry in cache.values():
            try:
                status, _ = self.request("DELETE", "/installation/token", f"token {entry['token']}")
                n += 1 if status in (204, 401) else 0  # 401: already dead
            except GitHubError as e:
                self.log(f"warning: {e}")
        try:
            os.unlink(self._cache_path(workspace))
        except OSError:
            pass
        return n

    # -- internals ---------------------------------------------------------

    def _mint(self, repositories: list[str] | None, permissions: dict[str, str] | None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if repositories:
            body["repositories"] = repositories
        if permissions:
            body["permissions"] = permissions
        status, payload = self.request(
            "POST", f"/app/installations/{self.installation_id()}/access_tokens", f"Bearer {self.jwt()}", body
        )
        if status != 201:
            raise GitHubError(f"minting a token failed ({status}): {_msg(payload)}")
        return {
            "token": payload["token"],
            "expires_at": payload["expires_at"],
            "repositories": sorted(r["full_name"] for r in payload.get("repositories") or []),
            "permissions": payload.get("permissions") or {},
        }

    def _dir(self) -> str:
        d = os.path.join(self.cfg.state_dir, "github")
        os.makedirs(d, exist_ok=True)
        return d

    def _cache_path(self, workspace: str) -> str:
        return os.path.join(self._dir(), f"token-{workspace}.json")

    def _cached(self, workspace: str) -> dict[str, Any]:
        try:
            return json.load(open(self._cache_path(workspace)))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write(path: str, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise


def _msg(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload)
    return str(payload)
