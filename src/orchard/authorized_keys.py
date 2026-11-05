"""`orchard-authorized-keys`: sshd's AuthorizedKeysCommand for the jump user.

Prints the self-service public key of every workspace, so a container can
ssh to the host as the jump user with the key the host put inside it. The
front door then recognises which workspace is talking (ExposeAuthInfo) and
limits it to acting on itself. The operator's own keys stay in the jump
user's authorized_keys; this only adds to them."""

from __future__ import annotations

import sys

from . import OrchardError
from .config import Config
from .incus import Incus
from .workspace import Manager


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = Config.load()
        for ws in Manager(cfg, Incus(cfg.incus)).list():
            if ws.selfkey:
                print(f"{ws.selfkey.split()[0]} {ws.selfkey.split()[1]} orchard:{ws.name}")
    except OrchardError as e:
        # Say nothing on stdout: sshd would try to parse it.
        print(f"orchard-authorized-keys: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
