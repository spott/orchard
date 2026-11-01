"""`dev`: the operator CLI. Exit 0 ok, 1 failure, 2 usage. `--json` everywhere."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import OrchardError, __version__
from .config import Config
from .incus import Incus
from .workspace import Manager, Workspace


def _emit(args: argparse.Namespace, data: Any, human: str) -> None:
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif human:
        print(human)


def _row(ws: Workspace) -> str:
    return f"{ws.name:<24} {ws.status:<8} {ws.ip or '-':<15} {ws.parent or '-':<16} {ws.image or '-'}"


def cmd_ls(mgr: Manager, args: argparse.Namespace) -> int:
    wss = mgr.list()
    header = f"{'NAME':<24} {'STATUS':<8} {'IP':<15} {'PARENT':<16} IMAGE"
    _emit(args, [w.as_dict() for w in wss], "\n".join([header, *map(_row, wss)]) if wss else "")
    return 0


def cmd_status(mgr: Manager, args: argparse.Namespace) -> int:
    ws = mgr.require(args.name)
    _emit(args, ws.as_dict(), _row(ws))
    return 0


def cmd_create(mgr: Manager, args: argparse.Namespace) -> int:
    ws = mgr.create(args.name, repo=args.repo, agent=args.agent, image=args.image)
    _emit(args, ws.as_dict(), f"{ws.name} {ws.status} {ws.ip}")
    return 0


def cmd_start(mgr: Manager, args: argparse.Namespace) -> int:
    ws = mgr.start(args.name)
    _emit(args, ws.as_dict(), f"{ws.name} {ws.status} {ws.ip}")
    return 0


def cmd_stop(mgr: Manager, args: argparse.Namespace) -> int:
    ws = mgr.stop(args.name, force=args.force)
    _emit(args, ws.as_dict(), f"{ws.name} {ws.status}")
    return 0


def cmd_restart(mgr: Manager, args: argparse.Namespace) -> int:
    ws = mgr.restart(args.name)
    _emit(args, ws.as_dict(), f"{ws.name} {ws.status} {ws.ip}")
    return 0


def cmd_rm(mgr: Manager, args: argparse.Namespace) -> int:
    removed = mgr.rm(args.name, now=args.now, tree=args.tree)
    _emit(args, {"removed": removed}, "\n".join(f"removed {n}" for n in removed) or f"no workspace named {args.name!r}")
    return 0


def cmd_gc(mgr: Manager, args: argparse.Namespace) -> int:
    reaped = mgr.gc(dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    _emit(args, {"deleted": reaped, "dry_run": args.dry_run}, "\n".join(f"{verb} {v}" for v in reaped))
    return 0


def cmd_proxy(mgr: Manager, args: argparse.Namespace) -> int:
    from .front_door import proxy

    return proxy(args.host, mgr.cfg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dev", description="Orchard workspaces on Incus.")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--version", action="version", version=f"orchard {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="VERB")
    # `dev ls --json` must work as well as `dev --json ls`; SUPPRESS keeps the
    # subparser from overriding the top-level value when the flag is absent.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    def add(name: str, fn: Any, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, parents=[common])
        sp.set_defaults(fn=fn)
        return sp

    add("ls", cmd_ls, "list workspaces")
    add("status", cmd_status, "show one workspace").add_argument("name")
    c = add("create", cmd_create, "create (or start) a workspace")
    c.add_argument("name")
    c.add_argument("--repo", help="repository URL recorded on the workspace")
    c.add_argument("--agent", help="agent label recorded on the workspace")
    c.add_argument("--image", help="image alias or fingerprint (default: configured image)")
    add("start", cmd_start, "start a workspace and wait for ssh").add_argument("name")
    s = add("stop", cmd_stop, "stop a workspace")
    s.add_argument("name")
    s.add_argument("--force", action="store_true")
    add("restart", cmd_restart, "stop then start").add_argument("name")
    r = add("rm", cmd_rm, "delete a workspace; its volume is kept for the grace period")
    r.add_argument("name")
    r.add_argument("--now", action="store_true", help="delete the volume immediately")
    r.add_argument("--tree", action="store_true", help="also delete forks of it")
    g = add("gc", cmd_gc, "delete trashed volumes past the grace period")
    g.add_argument("--dry-run", action="store_true")
    add("proxy", cmd_proxy, "front-door path: ensure running, then relay stdio to its sshd").add_argument("host")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        cfg = Config.load()
        mgr = Manager(cfg, Incus(cfg.incus))
        return args.fn(mgr, args)
    except OrchardError as e:
        print(f"dev: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
