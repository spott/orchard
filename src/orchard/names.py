"""Workspace names and the ssh host names that map onto them."""

from __future__ import annotations

import re

from . import OrchardError

# Incus instance names: 1-63 chars, letters/digits/dashes, no leading digit or
# dash, no trailing dash. We cap at 59 so the `ws-`/`trash-...` volume names
# derived from them stay valid too, and we insist on lowercase so a name is
# also a usable DNS label.
NAME_RE = re.compile(r"^[a-z](?:[a-z0-9-]{0,57}[a-z0-9])?$")
VOLUME_PREFIX = "ws-"
TRASH_PREFIX = "trash-"


class InvalidName(OrchardError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"invalid workspace name {name!r}: lowercase letters, digits and dashes, "
            "starting with a letter, at most 59 characters"
        )


def validate(name: str) -> str:
    if not NAME_RE.match(name):
        raise InvalidName(name)
    return name


def from_host(host: str, suffixes: list[str]) -> str:
    """`foo.orchard` -> `foo` for any configured suffix; a bare name passes through."""
    host = host.strip().lower().rstrip(".")
    for suffix in sorted(suffixes, key=len, reverse=True):
        if suffix and host.endswith(suffix) and len(host) > len(suffix):
            host = host[: -len(suffix)]
            break
    return validate(host)


def volume(name: str) -> str:
    return VOLUME_PREFIX + name


def trash_volume(name: str, stamp: str) -> str:
    return f"{TRASH_PREFIX}{name}-{stamp}"
