# orchard

Per-workspace dev containers on [Incus](https://linuxcontainers.org/incus/),
behind one ssh front door.

```
ssh foo.orchard
```

lands you in workspace `foo`. If it does not exist it is created from the
configured image; if it is stopped it is started. Nothing to provision, no
chatter on stdout, and your ssh client authenticates against the container
with your own key. Orchard never sees it.

Orchard is a small Python package (standard library only) plus a NixOS module:

| Piece | What |
|---|---|
| `dev` | the operator CLI: `create`, `ls`, `status`, `start`, `stop`, `restart`, `rm`, `gc`, `proxy` |
| `orchard-front-door` | the sshd `ForceCommand` for the jump user; speaks `proxy <host>` and `dev <args>` |
| `services.orchard` | NixOS module: jump user, sshd `Match` block, config file, daily `gc` timer |

## How it works

- A **workspace is an Incus instance** with the workspace's name. Its only
  metadata are `user.orchard.*` config keys on the instance (`managed`,
  `created`, `repo`, `agent`, `parent`). There is no database; `dev ls` is a
  filtered `incus list`.
- Each workspace has a **custom storage volume `ws-<name>`** mounted at
  `~/work` inside the container (idmapped, owned by the workspace user). The
  root filesystem is disposable and comes from the image; the volume is the
  thing that persists. `rm` renames the volume to `trash-<name>-<stamp>` and
  `gc` deletes it after the grace period, so a mistaken `rm` is recoverable.
- The **front door** is a dedicated user (default `dev`) whose every ssh
  login runs `orchard-front-door`. For `proxy foo.orchard` it strips the
  suffix, takes a per-workspace lock, creates or starts `foo`, waits for its
  sshd, then relays bytes between the ssh connection and the container's
  port 22. Two clients racing to create the same workspace serialise on the
  lock; the loser just attaches.
- All verbs are idempotent and `--json`-capable. Exit codes: 0 ok, 1 failure,
  2 usage. A failed `create` removes what it made; a volume it adopted is
  never deleted.

## Image contract

Orchard does not build images. Any Incus image works that:

1. runs an sshd on port 22 that accepts the workspace user's key;
2. has the workspace user (default `spott`, uid 1000) with `~/work` present
   or creatable as a mount point;
3. brings up `eth0` with DHCP on the Incus bridge.

The reference image is a NixOS container built in
[spott/iac](https://github.com/spott/iac) (`orchard/image/`).

## Client setup

`~/.ssh/config` on a laptop, or Blink on iOS:

```
Host *.orchard
  ProxyCommand ssh -q dev@orchard.example.com proxy %h
  User spott
  StrictHostKeyChecking accept-new
```

`ssh foo.orchard` then does everything. The name is matched by this stanza,
not resolved by DNS, so no resolver setup is needed on the client.

Operator commands from anywhere:

```
ssh dev@orchard.example.com dev ls --json
ssh dev@orchard.example.com dev rm old-experiment
```

## NixOS module

```nix
{
  inputs.orchard.url = "github:spott/orchard";
  inputs.orchard.inputs.nixpkgs.follows = "nixpkgs";

  # in the host's configuration:
  imports = [ inputs.orchard.nixosModules.default ];
  virtualisation.incus.enable = true;
  services.orchard = {
    enable = true;
    authorizedKeys = [ "ssh-ed25519 AAAA..." ];
    suffixes = [ ".orchard" ".orchard.example.com" ];
    image = "workspace";          # Incus image alias
    pool = "default";
    volumeSize = "50GiB";
    rmGraceDays = 7;
    # hooks.onCreate = pkgs.writeShellScript "inject-secrets" "...";
  };
}
```

All options have defaults; see `nix/module.nix`. The module writes
`/etc/orchard/config.json`; the CLI reads it (override with
`ORCHARD_CONFIG=`).

## Hooks

`hooks.onCreate` is an executable run on the host after a new workspace is
reachable, with `ORCHARD_NAME`, `ORCHARD_IP`, `ORCHARD_PARENT`,
`ORCHARD_USER` and `ORCHARD_EVENT` in its environment and the output going
to the caller's stderr. Secrets injection, repo cloning and anything
site-specific belongs there. A non-zero exit is logged, not fatal.

## Development

```
nix develop          # python + pytest + ruff
PYTHONPATH=src pytest
nix build            # also runs the tests
```

Tests use an in-memory fake of the `incus` CLI wrapper, so they need no
Incus. The relay has its own socketpair tests.

## Roadmap

- `fork`, `rebase` (image upgrade preserving the volume and ssh identity),
  `update` — v0.2
- nightly `incus storage volume export` backups with a restore test
- an MCP server over the same library

## License

MIT.
