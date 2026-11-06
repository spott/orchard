"""`dev`: the operator CLI. Exit 0 ok, 1 failure, 2 usage. `--json` everywhere."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import syslog
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


def cmd_rename(mgr: Manager, args: argparse.Namespace) -> int:
    ws = mgr.rename(args.old, args.new)
    _emit(args, ws.as_dict(), f"{ws.name} {ws.status} {ws.ip or '-'}")
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


def cmd_fork(mgr: Manager, args: argparse.Namespace) -> int:
    ws = mgr.fork(args.parent, args.as_name)
    _emit(args, ws.as_dict(), f"{ws.name} {ws.status} {ws.ip} (fork of {ws.parent})")
    return 0


def cmd_rebase(mgr: Manager, args: argparse.Namespace) -> int:
    if args.prune:
        removed = mgr.rebase_prune(args.name)
        _emit(args, {"removed": removed}, "\n".join(f"removed {n}" for n in removed) or f"nothing to prune for {args.name}")
        return 0
    if args.rollback:
        ws, parked = mgr.rebase_rollback(args.name, restore=args.restore)
        _emit(args, {**ws.as_dict(), "parked": parked}, f"{ws.name} {ws.status} {ws.ip} image {ws.image} (rebased instance kept as {parked})")
        return 0
    ws, old = mgr.rebase(args.name, args.image)
    if old is None:
        _emit(args, {**ws.as_dict(), "previous": None}, f"{ws.name} already on image {ws.image}")
    else:
        _emit(args, {**ws.as_dict(), "previous": old}, f"{ws.name} {ws.status} {ws.ip} image {ws.image} (previous instance kept as {old})")
    return 0


def cmd_update(mgr: Manager, args: argparse.Namespace) -> int:
    report = mgr.update(args.names or None)
    human = "\n".join(f"{name}: {line}" for name, lines in report.items() for line in lines)
    _emit(args, report, human)
    return 0 if all(not line.startswith("failed") for lines in report.values() for line in lines) else 1


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
    n = add("rename", cmd_rename, "rename a workspace (stops and restarts a running one)")
    n.add_argument("old")
    n.add_argument("new")
    r = add("rm", cmd_rm, "delete a workspace; its volume is kept for the grace period")
    r.add_argument("name")
    r.add_argument("--now", action="store_true", help="delete the volume immediately")
    r.add_argument("--tree", action="store_true", help="also delete forks of it")
    g = add("gc", cmd_gc, "delete trashed volumes past the grace period")
    g.add_argument("--dry-run", action="store_true")
    f = add("fork", cmd_fork, "copy a workspace (volume and image) into a new one with fresh identity")
    f.add_argument("parent")
    f.add_argument("--as", dest="as_name", metavar="CHILD", help="name of the fork (default: <parent>-N)")
    b = add("rebase", cmd_rebase, "move a workspace onto another image, keeping volume, address and identity")
    b.add_argument("name")
    b.add_argument("--image", help="image alias or fingerprint (default: configured image)")
    b.add_argument("--rollback", action="store_true", help="swap the previous instance back in")
    b.add_argument("--restore", action="store_true", help="with --rollback: also reset the volume to the pre-rebase snapshot")
    b.add_argument("--prune", action="store_true", help="delete the previous instances and pre-rebase snapshots")
    u = add("update", cmd_update, "re-lock project flakes under ~/work to the image's nixpkgs")
    u.add_argument("names", nargs="*", help="workspaces (default: every running one)")
    add("proxy", cmd_proxy, "front-door path: ensure running, then relay stdio to its sshd").add_argument("host")
    return p


# What a workspace may do to itself through its self-service key.
SELF_VERBS = {"ls", "status", "stop", "rename", "rm"}
# ...and which of those end its own ssh session, so they run detached.
SELF_DETACHED = {"stop", "rename", "rm"}


class SelfRefused(OrchardError):
    pass


def self_argv(argv: list[str], self_name: str) -> list[str]:
    """Rewrite a command typed inside workspace `self_name` so it can only
    act on that workspace: the name argument is implied (`dev rename bar`,
    `dev rm`) and, if given, must be its own."""
    flags = [a for a in argv if a.startswith("-")]
    words = [a for a in argv if not a.startswith("-")]
    if not words:
        return argv
    verb, rest = words[0], words[1:]
    if verb not in SELF_VERBS:
        raise SelfRefused(f"{verb!r} is not available from inside a workspace (only: {', '.join(sorted(SELF_VERBS))})")
    if verb == "ls":
        return [verb, *flags]
    if verb == "rename":
        if len(rest) == 1:
            rest = [self_name, rest[0]]
        elif len(rest) == 2 and rest[0] != self_name:
            raise SelfRefused(f"a workspace may only rename itself ({self_name})")
    else:
        if not rest:
            rest = [self_name]
        elif rest[0] != self_name:
            raise SelfRefused(f"a workspace may only act on itself ({self_name})")
    return [verb, *rest, *flags]


def detach() -> bool:
    """Double-fork. Returns True in the parent (which should exit now) and
    False in the detached child, whose stdio is /dev/null and whose log
    goes to syslog, so finishing the job does not depend on the ssh
    session that asked for it (which the job is about to end)."""
    if os.fork() > 0:
        return True
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    return False


def main(argv: list[str] | None = None, self_name: str | None = None) -> int:
    parser = build_parser()
    try:
        if self_name is not None:
            argv = self_argv(list(argv or []), self_name)
        args = parser.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    except OrchardError as e:
        print(f"dev: {e}", file=sys.stderr)
        return 1
    try:
        cfg = Config.load()
        mgr = Manager(cfg, Incus(cfg.incus))
        if self_name is not None and args.cmd in SELF_DETACHED:
            what = {
                "stop": f"stopping {self_name}",
                "rm": f"removing {self_name}; its volume is kept {cfg.rm_grace_days} days as trash",
                "rename": f"renaming {self_name} to {getattr(args, 'new', '?')}; reconnect under the new name",
            }[args.cmd]
            print(f"{what}; this session ends now", file=sys.stderr, flush=True)
            if detach():
                return 0
            syslog.openlog("orchard")
            mgr.log = lambda m: syslog.syslog(f"{self_name}: {m}")
            args.json = False
            sys.stdout = open(os.devnull, "w")
        return args.fn(mgr, args)
    except OrchardError as e:
        print(f"dev: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
