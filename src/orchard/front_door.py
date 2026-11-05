"""The sshd ForceCommand for the jump user. Reads SSH_ORIGINAL_COMMAND:

    proxy <host>   create/start workspace <host minus suffix>, then relay to its sshd
    dev <args...>  run the CLI (for `ssh dev@orchard dev ls --json`)

When the key that authenticated is a workspace's self-service key (sshd's
ExposeAuthInfo tells us which key it was), only `dev` runs, and only on that
workspace. Nothing but the ssh transport ever goes to stdout."""

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


def self_workspace(auth_file: str | None = None, mgr: Manager | None = None) -> str | None:
    """The workspace whose self-service key authenticated this session, from
    the file sshd writes when ExposeAuthInfo is on (`publickey <type> <b64>`
    lines); None for an operator's key or when the file is absent."""
    path = auth_file or os.environ.get("SSH_USER_AUTH")
    if not path:
        return None
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    keys = [" ".join(p[1:3]) for p in (line.split() for line in lines) if len(p) >= 3 and p[0] == "publickey"]
    if not keys:
        return None
    if mgr is None:
        cfg = Config.load()
        mgr = Manager(cfg, Incus(cfg.incus))
    for key in keys:
        ws = mgr.find_by_selfkey(key)
        if ws is not None:
            return ws.name
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse(os.environ.get("SSH_ORIGINAL_COMMAND", "")) if argv is None else argv
    try:
        self_name = self_workspace()
        if self_name is not None:
            if args and args[0] == "dev":
                return cli.main(args[1:], self_name=self_name)
            print(f"usage (from inside workspace {self_name}): dev <args...>", file=sys.stderr)
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
