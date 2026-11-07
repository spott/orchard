"""Workspace names, the Incus instance names they map onto, and the ssh host
names that map onto them.

A workspace name is `<name>` or `<name>.<repo>`. Each part is a lowercase DNS
label. The repo part matters only at create (clone `<owner>/<repo>`, seed the
grant list with that repository); afterwards the whole thing is just the
workspace's name and what it may reach is in its grants. Incus instance names
cannot contain dots, so the dot is encoded as `--` (`blah.iac` is instance
`blah--iac`, volume `ws-blah--iac`) and `--` is reserved inside a label."""

from __future__ import annotations

import re

from . import OrchardError

# One label: 1-59 chars, letters/digits/dashes, no leading digit or dash, no
# trailing dash, lowercase (so it is also a usable DNS label). 59 keeps the
# `ws-`/`trash-...` volume names derived from it valid Incus names too.
LABEL_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,57}[a-z0-9])?$")
SEP = "--"
MAX_INSTANCE = 59
VOLUME_PREFIX = "ws-"
TRASH_PREFIX = "trash-"


class InvalidName(OrchardError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"invalid workspace name {name!r}: `name` or `name.repo`, lowercase letters, digits and "
            "single dashes, each part starting with a letter, at most 59 characters in all"
        )


def validate(name: str) -> str:
    parts = name.split(".")
    if not 1 <= len(parts) <= 2:
        raise InvalidName(name)
    for p in parts:
        if not LABEL_RE.match(p) or SEP in p:
            raise InvalidName(name)
    if len(SEP.join(parts)) > MAX_INSTANCE:
        raise InvalidName(name)
    return name


def instance(name: str) -> str:
    """The Incus instance name of workspace `name`: `blah.iac` -> `blah--iac`."""
    return validate(name).replace(".", SEP)


def display(inst: str) -> str:
    """Inverse of `instance` for names it produced; anything else unchanged."""
    if SEP in inst:
        candidate = inst.replace(SEP, ".", 1)
        try:
            if instance(candidate) == inst:
                return candidate
        except InvalidName:
            pass
    return inst


def split(name: str) -> tuple[str, str | None]:
    """`blah.iac` -> (`blah`, `iac`); `foo` -> (`foo`, None)."""
    head, _, repo = validate(name).partition(".")
    return head, repo or None


def repo(name: str) -> str | None:
    return split(name)[1]


def from_host(host: str, suffixes: list[str]) -> str:
    """`foo.orchard` -> `foo`, `blah.iac.orchard` -> `blah.iac`, for any
    configured suffix; a bare name passes through."""
    host = host.strip().lower().rstrip(".")
    for suffix in sorted(suffixes, key=len, reverse=True):
        if suffix and host.endswith(suffix) and len(host) > len(suffix):
            host = host[: -len(suffix)]
            break
    return validate(host)


def label(qname: str, zone: str) -> str | None:
    """`foo.orchard.example.com` under zone `orchard.example.com` -> `foo`,
    `blah.iac.orchard.example.com` -> `blah.iac`; None for the apex, deeper
    names, or anything that is not a valid workspace name (so `_gateway` and
    friends are someone else's)."""
    qname = qname.lower().rstrip(".")
    zone = zone.lower().rstrip(".")
    if not zone or not qname.endswith("." + zone):
        return None
    head = qname[: -len(zone) - 1]
    try:
        return validate(head)
    except InvalidName:
        return None


def volume(name: str) -> str:
    return VOLUME_PREFIX + instance(name)


def trash_volume(name: str, stamp: str) -> str:
    return f"{TRASH_PREFIX}{instance(name)}-{stamp}"


def child(parent: str, n: int) -> str:
    """Default name of the n-th fork of `parent`: `foo-N`, `blah-N.iac`."""
    head, rep = split(parent)
    return f"{head}-{n}" + (f".{rep}" if rep else "")


def old_instance(name: str, stamp: str) -> str:
    """Where `rebase` parks the instance it replaced (lowercase so it is still
    a valid instance name; never a workspace: it is marked unmanaged)."""
    return f"{instance(name)}-old-{stamp.lower()}"
