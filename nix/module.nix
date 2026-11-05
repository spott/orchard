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
    address_start = if cfg.addresses == null then "" else cfg.addresses.start;
    address_end = if cfg.addresses == null then "" else cfg.addresses.end;
    self_host = if cfg.self.enable then cfg.self.host else "";
    self_hostkey_file = cfg.self.hostKeyFile;
    dns_listen = cfg.wake.listen;
    dns_port = cfg.wake.port;
    dns_zone = cfg.wake.zone;
    dns_reverse_zone = cfg.wake.reverseZone;
    dns_upstream = cfg.wake.upstream;
    dns_upstream_port = cfg.wake.upstreamPort;
    dns_ttl = cfg.wake.ttl;
    dns_create = cfg.wake.createOnLookup;
    untouched_hours = cfg.wake.untouchedHours;
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
      description = "Public keys allowed to use the front door.";
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
    environment.systemPackages = [ cfg.package ];

    systemd.tmpfiles.rules = [ "d /run/orchard 0770 root incus-admin -" ];

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
          # Workspaces' self-service keys, and which one authenticated.
          AuthorizedKeysCommand ${cfg.package}/bin/orchard-authorized-keys %u
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
  };
}
