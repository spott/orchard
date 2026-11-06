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
| `dev` | the operator CLI: `create`, `ls`, `status`, `start`, `stop`, `restart`, `rename`, `rm`, `gc`, `proxy` |
| `orchard-front-door` | the sshd `ForceCommand` for the jump user; speaks `proxy <host>` and `dev <args>` |
| `orchard-dns` | the wake resolver: serves the workspace zone, starts or creates what is looked up |
| `orchard-authorized-keys` | sshd `AuthorizedKeysCommand`: the workspaces' self-service keys |
| `services.orchard` | NixOS module: jump user, sshd `Match` block, config file, resolver service, daily `gc` timer |

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
- **`fork PARENT [--as CHILD]`** copies a workspace: the parent is stopped
  for the volume copy (so databases on it are consistent) and started
  again, then the child is made from the parent's *own* image fingerprint
  with the copy attached and `user.orchard.parent` set. Identity is fresh:
  new host keys, new self-service key, new address. Default child name
  `<parent>-N`; `fork_limit` caps the children per parent; `rm --tree`
  removes a workspace with its forks.
- **`rebase NAME [--image ALIAS]`** moves a workspace onto another image
  keeping its volume, address, metadata and identity (host keys,
  machine-id, self-service key are copied over). The volume gets a
  `pre-rebase-<stamp>` snapshot first and the old instance is parked,
  stopped and unmanaged, as `<name>-old-<stamp>`. `rebase --rollback NAME`
  swaps the parked instance back (`--restore` also resets the volume to the
  snapshot); `rebase --prune NAME` deletes parked instances and snapshots.
  Same image as the current one: no-op.
- **`update [NAME...]`** runs `nix flake update nixpkgs` (and
  `nixpkgs-stable`) in every `~/work/*` project with a flake, as the
  workspace user, in the named or all running workspaces: the re-lock step
  after an image bump when the image's registry pins moved.
- All verbs are idempotent and `--json`-capable. Exit codes: 0 ok, 1 failure,
  2 usage. A failed `create` removes what it made; a volume it adopted is
  never deleted; a failed `fork` drops its copy; a failed `rebase` puts the
  old instance back.

## Image contract

Orchard does not build images. Any Incus image works that:

1. runs an sshd on port 22 that accepts the workspace user's key;
2. has the workspace user (default `spott`, uid 1000) with `~/work` present
   or creatable as a mount point;
3. brings up `eth0` with DHCP on the Incus bridge.

The reference image is a NixOS container built in
[spott/iac](https://github.com/spott/iac) (`orchard/image/`).

## Wake on lookup (no jump host)

When the workspaces are real hosts on the LAN (their Incus bridge is on a
VLAN, no NAT) the front door is optional: `services.orchard.wake` runs a
small resolver that the site's DNS forwards the workspace zone to. A lookup
of `foo.orchard.example.com` answers with `foo`'s address and, in the
background, starts `foo` if it is stopped or creates it if it does not
exist. The client's first SYN goes unanswered; the retransmit a second or
two later reaches the container's sshd (create takes a few seconds). So from
anything that can only do plain ssh or mosh, such as Blink on iOS:

```
mosh spott@foo.orchard.example.com      # creates foo if needed, then connects
```

What makes it work: ssh sends no host name, so the name must be recoverable
from the address. Every workspace therefore gets a DHCP reservation from
`services.orchard.addresses` (its eth0 `ipv4.address`; Incus's dnsmasq hands
it out), and the resolver answers it even while the workspace is stopped,
which dnsmasq alone would not. Everything that is not a workspace name
(the zone apex, `_gateway`, reverse entries dnsmasq knows) is forwarded to
dnsmasq. Answers for a known name that have no data (AAAA, ...) are empty
NOERROR, never NXDOMAIN, so a caching resolver cannot poison the A record.

The price of creating on lookup is that a typo creates a workspace. `dev gc`
(daily) removes a lookup-created workspace that nobody has ever logged into
after `untouchedHours` (24), volume to trash as usual. Set
`wake.createOnLookup = false` to answer NXDOMAIN for unknown names instead;
then `dev create` (from anywhere: `ssh dev@orchard.example.com dev create
foo`) stays the one explicit step.

## Self-service from inside a workspace

With `services.orchard.self.enable`, `dev` installs into every workspace a
key pair (`~/.orchard/self.key`, public half on the instance's
`user.orchard.selfkey`), the host's pinned key, and a `dev` client in
`~/.local/bin`. The host's sshd accepts those keys for the jump user
(`AuthorizedKeysCommand`) and the front door, told by `ExposeAuthInfo` which
key authenticated, lets such a session act on its own workspace only:

```
dev status            # this workspace
dev rename bar        # stop, rename instance and volume, start again; reconnect as bar
dev stop
dev rm                # volume kept for the grace period as trash-<name>-<stamp>
dev ls                # read-only view of the others
```

`rename`, `stop` and `rm` end the session they were typed in (the container
stops), so the host runs them detached after acknowledging on stderr. The
client talks to `self.host` (the Incus bridge address), which the containers
reach without crossing the site firewall. A workspace from before this
existed gets its key the next time `dev` starts it.

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

- nightly `incus storage volume export` backups with a restore test
- an MCP server over the same library

## License

MIT.
