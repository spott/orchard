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
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = config.virtualisation.incus.enable;
        message = "services.orchard needs virtualisation.incus.enable = true";
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
    '';

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
