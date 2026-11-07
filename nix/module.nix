# services.orchard: one ssh front door for per-workspace Incus containers.
#
# The lab's values (keys, suffixes, pool, image) are all options; this file
# knows nothing about any particular network.
{ config, lib, pkgs, ... }:
let
  cfg = config.services.orchard;
  settings = {
    inherit (cfg) pool image profiles suffixes;
    user = cfg.workspaceUser;
    uid = cfg.workspaceUid;
    gid = cfg.workspaceGid;
    incus = "${config.virtualisation.incus.package}/bin/incus";
    volume_size = cfg.volumeSize;
    fork_limit = cfg.forkLimit;
    rm_grace_days = cfg.rmGraceDays;
    ssh_wait_seconds = cfg.sshWaitSeconds;
    lock_dir = "/run/orchard";
    hooks = lib.filterAttrs (_: v: v != null) {
      on_create = cfg.hooks.onCreate;
    };
    operator_keys = cfg.authorizedKeys;
    address_start = if cfg.addresses == null then "" else cfg.addresses.start;
    address_end = if cfg.addresses == null then "" else cfg.addresses.end;
    self_host = if cfg.self.enable then cfg.self.host else "";
    self_hostkey_file = cfg.self.hostKeyFile;
    ssh_keygen = "${pkgs.openssh}/bin/ssh-keygen";
    dns_listen = cfg.wake.listen;
    dns_port = cfg.wake.port;
    dns_zone = cfg.wake.zone;
    dns_reverse_zone = cfg.wake.reverseZone;
    dns_upstream = cfg.wake.upstream;
    dns_upstream_port = cfg.wake.upstreamPort;
    dns_ttl = cfg.wake.ttl;
    dns_create = cfg.wake.createOnLookup;
    untouched_hours = cfg.wake.untouchedHours;
    state_dir = "/var/lib/orchard";
    github_app_id = if cfg.github.enable then toString cfg.github.appId else "";
    github_key_file = if cfg.github.enable then cfg.github.privateKeyFile else "";
    github_owner = cfg.github.owner;
    github_api = cfg.github.apiUrl;
    openssl = "${pkgs.openssl}/bin/openssl";
    secrets_env = cfg.secrets.env;
    secrets_token_file = if cfg.secrets.tokenFile == null then "" else cfg.secrets.tokenFile;
    secrets_cache = "/run/orchard/secrets";
    op = "${cfg.secrets.package}/bin/op";
    logins_volume = if cfg.logins.enable then cfg.logins.volume else "";
    logins_size = cfg.logins.size;
  };
  configFile = pkgs.writeText "orchard-config.json" (builtins.toJSON settings);
in
{
  options.services.orchard = {
    enable = lib.mkEnableOption "the orchard workspace front door and `dev` CLI";

    package = lib.mkOption {
      type = lib.types.package;
      description = "The orchard package providing `dev` and `orchard-front-door`.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "dev";
      description = "Jump user clients ssh to; every login runs the front door.";
    };

    authorizedKeys = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = ''
        Public keys that get the full front door (the operator). A key that is
        not listed here and is not a workspace's self-service key (see
        `self.enable`) is refused, as is a session sshd exposes no auth info
        for: the front door never falls back to operator access.
      '';
    };

    extraGroups = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      description = "Extra groups for the jump user (incus-admin is always included).";
    };

    workspaceUser = lib.mkOption {
      type = lib.types.str;
      default = "spott";
      description = "Account inside the workspace image that ssh lands on; ~/work is the volume.";
    };
    workspaceUid = lib.mkOption { type = lib.types.int; default = 1000; };
    workspaceGid = lib.mkOption { type = lib.types.int; default = 100; };

    pool = lib.mkOption {
      type = lib.types.str;
      default = "default";
      description = "Incus storage pool for workspace volumes.";
    };
    image = lib.mkOption {
      type = lib.types.str;
      default = "workspace";
      description = "Incus image alias new workspaces are created from.";
    };
    profiles = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "default" ];
      description = "Incus profiles applied to new workspaces.";
    };
    volumeSize = lib.mkOption {
      type = lib.types.str;
      default = "50GiB";
      description = "Size of each workspace's ~/work volume.";
    };
    suffixes = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ".orchard" ];
      description = "Host-name suffixes the front door strips: `foo.orchard` -> `foo`.";
    };
    forkLimit = lib.mkOption { type = lib.types.int; default = 5; };
    rmGraceDays = lib.mkOption {
      type = lib.types.int;
      default = 7;
      description = "Days a removed workspace's volume is kept before `dev gc` deletes it.";
    };
    sshWaitSeconds = lib.mkOption { type = lib.types.int; default = 120; };

    hooks.onCreate = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        Executable run on the host after a workspace is created and reachable,
        with ORCHARD_NAME, ORCHARD_IP, ORCHARD_PARENT, ORCHARD_USER, ORCHARD_EVENT
        in the environment. Secrets injection belongs here, not in orchard.
      '';
    };

    gc.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Run `dev gc` daily to reap trashed volumes.";
    };

    github = {
      enable = lib.mkEnableOption ''
        per-workspace GitHub tokens: orchard holds a GitHub App's private key
        and mints 1-hour installation tokens limited to the repositories in a
        workspace's grants (`dev grant`, `dev token github`)
      '';
      appId = lib.mkOption {
        type = lib.types.either lib.types.int lib.types.str;
        description = "The GitHub App's id (App settings -> General).";
      };
      privateKeyFile = lib.mkOption {
        type = lib.types.str;
        description = ''
          Path to the App's private key (PEM), deployed out of band (a
          Colmena/agenix/sops key). Readable by the `user` and, if operators
          run `dev token` from their own shells, by the incus-admin group.
          Never inside a workspace.
        '';
      };
      owner = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = ''
          GitHub account the App is installed on. `name.repo` workspaces
          clone and are granted `<owner>/<repo>`; a bare `dev grant NAME repo`
          means the same. Read even with `enable = false` (grants can be
          recorded before tokens are minted); empty = `name.repo` seeds no grant.
        '';
      };
      apiUrl = lib.mkOption {
        type = lib.types.str;
        default = "https://api.github.com";
      };
    };

    secrets = {
      env = lib.mkOption {
        type = lib.types.attrsOf lib.types.str;
        default = { };
        example = { CLAUDE_CODE_OAUTH_TOKEN = "op://dev/claude/credential"; };
        description = ''
          Environment variables every workspace gets at start, as `op://`
          references. `dev secrets refresh` (the timer below) reads them
          with the 1Password service account in `tokenFile` into a cache on
          the host; `dev` pushes the cache into
          `/run/user/<uid>/orchard/env` (a tmpfs, mode 0600) of every
          workspace it starts, so starts cost no 1Password requests and a
          stopped workspace holds nothing. The image's shell should source
          that file.
        '';
      };
      tokenFile = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        description = "File holding the 1Password service account token (read-only access to the vault the references point into). Readable by `user`.";
      };
      refreshOnCalendar = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 00/6:00:00";
        description = "systemd OnCalendar for `dev secrets refresh`, which also re-pushes into running workspaces.";
      };
      package = lib.mkOption {
        type = lib.types.package;
        default = pkgs._1password-cli;
        description = "The `op` CLI.";
      };
    };

    logins = {
      enable = lib.mkEnableOption ''
        one shared custom volume mounted read-write into every workspace at
        `~/.orchard/logins` (idmapped to the workspace user), for state all
        workspaces share such as agent login files: a re-login in any
        workspace is seen by all
      '';
      volume = lib.mkOption {
        type = lib.types.str;
        default = "logins";
        description = "Name of the custom volume in `pool`; created on first use.";
      };
      size = lib.mkOption {
        type = lib.types.str;
        default = "1GiB";
      };
    };

    addresses = lib.mkOption {
      type = lib.types.nullOr (lib.types.submodule {
        options = {
          start = lib.mkOption { type = lib.types.str; description = "First address (inclusive)."; };
          end = lib.mkOption { type = lib.types.str; description = "Last address (inclusive)."; };
        };
      });
      default = null;
      description = ''
        Range on the Incus bridge's subnet from which every workspace gets a
        DHCP reservation (its eth0 `ipv4.address`), so its name has an
        answer while it is stopped. Required by `wake`. Existing workspaces
        get one the first time they are stopped or started by `dev`.
      '';
    };

    self = {
      enable = lib.mkEnableOption "self-service: `dev rename|rm|stop` from inside a workspace";
      host = lib.mkOption {
        type = lib.types.str;
        description = ''
          Address of this host that the containers can ssh to (normally the
          Incus bridge address). `dev` installs a per-workspace key, this
          host's key and a `dev` client into every workspace; the key is only
          accepted for acting on that workspace.
        '';
      };
      hostKeyFile = lib.mkOption {
        type = lib.types.str;
        default = "/etc/ssh/ssh_host_ed25519_key.pub";
        description = "Host public key the containers pin.";
      };
    };

    wake = {
      enable = lib.mkEnableOption ''
        the wake resolver: serve the workspace zone and start (or create) a
        workspace when its name is looked up
      '';
      listen = lib.mkOption { type = lib.types.str; description = "Address to serve DNS on (UDP and TCP)."; };
      port = lib.mkOption { type = lib.types.port; default = 53; };
      zone = lib.mkOption { type = lib.types.str; description = "Forward zone, e.g. orchard.example.com."; };
      reverseZone = lib.mkOption { type = lib.types.str; description = "Reverse zone, e.g. 11.42.10.in-addr.arpa."; };
      upstream = lib.mkOption {
        type = lib.types.str;
        description = "Incus's dnsmasq (the bridge address): everything that is not a workspace name is forwarded there.";
      };
      upstreamPort = lib.mkOption { type = lib.types.port; default = 53; };
      ttl = lib.mkOption { type = lib.types.int; default = 5; description = "TTL of the answers (short: a stopped workspace's next lookup must reach us)."; };
      createOnLookup = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Create a workspace when an unknown valid name is looked up (else NXDOMAIN).";
      };
      untouchedHours = lib.mkOption {
        type = lib.types.int;
        default = 24;
        description = "`dev gc` removes lookup-created workspaces nobody logged into within this many hours.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.virtualisation.incus.enable;
        message = "services.orchard needs virtualisation.incus.enable = true";
      }
      {
        assertion = !cfg.wake.enable || cfg.addresses != null;
        message = "services.orchard.wake needs services.orchard.addresses (a stopped workspace must have an address to answer with)";
      }
      {
        assertion = cfg.secrets.env == { } || cfg.secrets.tokenFile != null;
        message = "services.orchard.secrets.env needs services.orchard.secrets.tokenFile";
      }
    ];

    users.groups.${cfg.user} = { };
    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.user;
      description = "orchard ssh front door";
      home = "/var/lib/orchard";
      createHome = true;
      shell = pkgs.bash;
      extraGroups = [ "incus-admin" ] ++ cfg.extraGroups;
      openssh.authorizedKeys.keys = cfg.authorizedKeys;
    };

    environment.etc."orchard/config.json".source = configFile;
    # A real file (mode set => copied, not symlinked): sshd resolves symlinks
    # before its ownership check and then refuses /nix/store, which is
    # group-writable ("bad ownership or modes for directory /nix/store").
    environment.etc."ssh/orchard-authorized-keys" = lib.mkIf cfg.self.enable {
      mode = "0755";
      text = ''
        #!${pkgs.runtimeShell}
        exec ${cfg.package}/bin/orchard-authorized-keys "$@"
      '';
    };
    environment.systemPackages = [ cfg.package ];

    # setgid: lock files made by the resolver's user (dev) and by operators'
    # shells must all belong to incus-admin, or `dev rm` fails on a lock the
    # other side created.
    systemd.tmpfiles.rules = [
      "d /run/orchard 2770 root incus-admin -"
      # The secrets cache and the host-side state (minted tokens, cached
      # lists): the jump user writes, every operator (incus-admin) reads.
      "d /run/orchard/secrets 2770 ${cfg.user} incus-admin -"
      "d /var/lib/orchard 2770 ${cfg.user} incus-admin -"
      "d /var/lib/orchard/github 2770 ${cfg.user} incus-admin -"
    ];

    # Match blocks must come after every global directive; NixOS appends
    # extraConfig after its generated settings, and its own extraConfig
    # entries are mkOrder 0, so a late order keeps ours last.
    services.openssh.extraConfig = lib.mkOrder 2000 ''
      Match User ${cfg.user}
        ForceCommand ${cfg.package}/bin/orchard-front-door
        PermitTTY no
        AllowTcpForwarding no
        AllowAgentForwarding no
        AllowStreamLocalForwarding no
        X11Forwarding no
        PermitTunnel no
        ${lib.optionalString cfg.self.enable ''
          # Workspaces' self-service keys, and which one authenticated. A
          # wrapper copied into /etc: sshd refuses a command that resolves
          # into /nix/store (see environment.etc above).
          AuthorizedKeysCommand /etc/ssh/orchard-authorized-keys %u
          AuthorizedKeysCommandUser ${cfg.user}
          ExposeAuthInfo yes
        ''}
    '';

    systemd.services.orchard-dns = lib.mkIf cfg.wake.enable {
      description = "orchard: workspace zone resolver that wakes what is looked up";
      after = [ "incus.service" "network-online.target" ];
      requires = [ "incus.service" ];
      wants = [ "network-online.target" ];
      wantedBy = [ "multi-user.target" ];
      path = [ config.virtualisation.incus.package pkgs.openssh pkgs.openssl ];
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/orchard-dns";
        User = cfg.user;
        Group = cfg.user;
        AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
        CapabilityBoundingSet = [ "CAP_NET_BIND_SERVICE" ];
        Restart = "always";
        RestartSec = "2s";
        NoNewPrivileges = true;
        PrivateTmp = true;
      };
    };

    systemd.services.orchard-gc = lib.mkIf cfg.gc.enable {
      description = "orchard: reap trashed workspace volumes";
      after = [ "incus.service" ];
      requires = [ "incus.service" ];
      path = [ config.virtualisation.incus.package ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.user;
        ExecStart = "${cfg.package}/bin/dev gc";
      };
    };
    systemd.timers.orchard-gc = lib.mkIf cfg.gc.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "daily";
        RandomizedDelaySec = "1h";
        Persistent = true;
      };
    };

    systemd.services.orchard-secrets = lib.mkIf (cfg.secrets.env != { }) {
      description = "orchard: refresh the secrets cache and push it into running workspaces";
      after = [ "incus.service" "network-online.target" ];
      requires = [ "incus.service" ];
      wants = [ "network-online.target" ];
      path = [ config.virtualisation.incus.package cfg.secrets.package ];
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.user;
        ExecStart = "${cfg.package}/bin/dev secrets refresh";
      };
    };
    systemd.timers.orchard-secrets = lib.mkIf (cfg.secrets.env != { }) {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnCalendar = cfg.secrets.refreshOnCalendar;
        RandomizedDelaySec = "10min";
        Persistent = true;
      };
    };
  };
}
