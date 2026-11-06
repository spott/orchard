"""The sshd ForceCommand for the jump user. Reads SSH_ORIGINAL_COMMAND:

    proxy <host>   create/start workspace <host minus suffix>, then relay to its sshd
    dev <args...>  run the CLI (for `ssh dev@orchard dev ls --json`)

Who is talking is decided by the key sshd says authenticated the session
(ExposeAuthInfo): a configured operator key gets everything; a workspace's
self-service key gets `dev` on that workspace only; anything else, including a
session sshd gave no auth info for, is refused. "Cannot tell who this is" is
never "operator". Nothing but the ssh transport ever goes to stdout."""

from __future__ import annotations

import os
import shlex
import socket
import sys

from . import OrchardError, cli, names
from .config import Config
from .incus import Incus
from .relay import relay
from .workspace import Manager

USAGE = "usage: proxy <host> | dev <args...>"


def parse(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def proxy(host: str, cfg: Config | None = None) -> int:
    cfg = cfg or Config.load()
    mgr = Manager(cfg, Incus(cfg.incus))
    name = names.from_host(host, cfg.suffixes)
    ws = mgr.ensure_running(name)
    assert ws.ip
    sock = socket.create_connection((ws.ip, 22), timeout=10)
    relay(sock)
    return 0


OPERATOR = "operator"
WORKSPACE = "workspace"
DENIED = "denied"


def authenticated_keys(auth_file: str | None = None) -> list[str] | None:
    """`<type> <b64>` of every key sshd says authenticated this session, from
    the file ExposeAuthInfo makes it write (`publickey <type> <b64>` lines).
    None when sshd gave us no file at all."""
    path = auth_file or os.environ.get("SSH_USER_AUTH")
    if not path:
        return None
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    return [" ".join(p[1:3]) for p in (line.split() for line in lines) if len(p) >= 3 and p[0] == "publickey"]


def identify(
    auth_file: str | None = None, cfg: Config | None = None, mgr: Manager | None = None
) -> tuple[str, str | None]:
    """(OPERATOR, None), (WORKSPACE, name) or (DENIED, reason).

    Operator keys are checked first and need no Incus, so an operator can still
    get in to fix a broken host. Everything that is not positively recognised
    is denied: a missing or empty auth file (ExposeAuthInfo off, sshd
    misconfigured) and keys that map to nothing (a workspace removed after its
    key was issued, a stale AuthorizedKeysCommand)."""
    keys = authenticated_keys(auth_file)
    if keys is None:
        return DENIED, "sshd exposed no authentication info (ExposeAuthInfo off?)"
    if not keys:
        return DENIED, "no public key authenticated this session"
    cfg = cfg or Config.load()
    operators = {" ".join(k.split()[:2]) for k in cfg.operator_keys if len(k.split()) >= 2}
    if any(key in operators for key in keys):
        return OPERATOR, None
    if mgr is None:
        mgr = Manager(cfg, Incus(cfg.incus))
    for key in keys:
        ws = mgr.find_by_selfkey(key)
        if ws is not None:
            return WORKSPACE, ws.name
    return DENIED, "key is neither an operator key nor a workspace's self-service key"


def main(argv: list[str] | None = None) -> int:
    args = parse(os.environ.get("SSH_ORIGINAL_COMMAND", "")) if argv is None else argv
    try:
        kind, who = identify()
        if kind == DENIED:
            print(f"orchard: access denied: {who}", file=sys.stderr)
            return 1
        if kind == WORKSPACE:
            if args and args[0] == "dev":
                return cli.main(args[1:], self_name=who)
            print(f"usage (from inside workspace {who}): dev <args...>", file=sys.stderr)
            return 2
        if len(args) == 2 and args[0] == "proxy":
            return proxy(args[1])
        if args and args[0] == "dev":
            return cli.main(args[1:])
    except OrchardError as e:
        print(f"orchard: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
