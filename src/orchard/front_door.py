"""The sshd ForceCommand for the jump user. Reads SSH_ORIGINAL_COMMAND:

    proxy <host>   create/start workspace <host minus suffix>, then relay to its sshd
    dev <args...>  run the CLI (for `ssh dev@orchard dev ls --json`)

Nothing but the ssh transport ever goes to stdout."""

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


def main(argv: list[str] | None = None) -> int:
    args = parse(os.environ.get("SSH_ORIGINAL_COMMAND", "")) if argv is None else argv
    try:
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
