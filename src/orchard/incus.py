"""A thin, typed wrapper over the `incus` CLI. Everything the rest of the
package knows about Incus goes through this class, so tests can swap in a
fake with the same methods."""

from __future__ import annotations

import json
import subprocess
from typing import Any

from . import OrchardError


class IncusError(OrchardError):
    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(f"{' '.join(argv)} failed ({returncode}): {self.stderr}")


class Incus:
    def __init__(self, binary: str = "incus") -> None:
        self.binary = binary

    # -- plumbing -----------------------------------------------------------

    def run(self, *args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        argv = [self.binary, *args]
        # Never inherit stdin: several incus subcommands (init, storage volume
        # create, ...) read a YAML config from stdin when it is not a terminal
        # and block until EOF. In the front door stdin is the ssh transport.
        stdin = None if input is not None else subprocess.DEVNULL
        proc = subprocess.run(argv, capture_output=True, text=True, input=input, stdin=stdin)
        if check and proc.returncode != 0:
            raise IncusError(argv, proc.returncode, proc.stderr)
        return proc

    def json(self, *args: str) -> Any:
        return json.loads(self.run(*args, "--format", "json").stdout)

    # -- instances ----------------------------------------------------------

    def instances(self) -> list[dict[str, Any]]:
        return self.json("list")

    def instance(self, name: str) -> dict[str, Any] | None:
        for inst in self.json("list", f"^{name}$"):
            if inst["name"] == name:
                return inst
        return None

    def init(self, image: str, name: str, profiles: list[str], config: dict[str, str]) -> None:
        args = ["init", image, name]
        for p in profiles:
            args += ["--profile", p]
        for k, v in config.items():
            args += ["--config", f"{k}={v}"]
        self.run(*args)

    def start(self, name: str) -> None:
        self.run("start", name)

    def stop(self, name: str, force: bool = False) -> None:
        self.run("stop", name, *(["--force"] if force else []))

    def delete(self, name: str, force: bool = False) -> None:
        self.run("delete", name, *(["--force"] if force else []))

    def rename(self, old: str, new: str) -> None:
        self.run("rename", old, new)

    def config_set(self, name: str, config: dict[str, str]) -> None:
        self.run("config", "set", name, *[f"{k}={v}" for k, v in config.items()])

    def config_unset(self, name: str, *keys: str) -> None:
        for key in keys:
            self.run("config", "unset", name, key)

    def device_add(self, name: str, device: str, kind: str, **props: str) -> None:
        self.run("config", "device", "add", name, device, kind, *[f"{k}={v}" for k, v in props.items()])

    def device_override(self, name: str, device: str, **props: str) -> None:
        """Copy a profile device onto the instance with these properties
        changed (works on a running instance; for `eth0 ipv4.address` Incus
        writes the DHCP reservation at once)."""
        self.run("config", "device", "override", name, device, *[f"{k}={v}" for k, v in props.items()])

    def device_set(self, name: str, device: str, **props: str) -> None:
        self.run("config", "device", "set", name, device, *[f"{k}={v}" for k, v in props.items()])

    def device_unset(self, name: str, device: str, key: str) -> None:
        self.run("config", "device", "unset", name, device, key)

    def device_remove(self, name: str, device: str) -> None:
        self.run("config", "device", "remove", name, device)

    def exec(
        self,
        name: str,
        *cmd: str,
        check: bool = True,
        user: int | None = None,
        group: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = ["exec", name]
        if user is not None:
            args += ["--user", str(user)]
        if group is not None:
            args += ["--group", str(group)]
        for k, v in (env or {}).items():
            args += ["--env", f"{k}={v}"]
        return self.run(*args, "--", *cmd, check=check)

    def file_pull(self, name: str, src: str, dst: str, check: bool = True) -> bool:
        """Works on stopped instances too. False when the file is missing (check=False)."""
        return self.run("file", "pull", f"{name}{src}", dst, check=check).returncode == 0

    def file_push(self, src: str, name: str, dst: str, *, uid: int | None = None, gid: int | None = None, mode: str | None = None) -> None:
        args = ["file", "push", "--create-dirs"]
        if uid is not None:
            args += ["--uid", str(uid)]
        if gid is not None:
            args += ["--gid", str(gid)]
        if mode is not None:
            args += ["--mode", mode]
        self.run(*args, src, f"{name}{dst}")

    # -- images -------------------------------------------------------------

    def image_fingerprint(self, ref: str) -> str | None:
        """Full fingerprint of the image an alias or fingerprint prefix names."""
        for img in self.json("image", "list", ref):
            fp = img.get("fingerprint", "")
            if fp.startswith(ref) or any(a.get("name") == ref for a in img.get("aliases") or []):
                return fp
        return None

    # -- custom storage volumes ---------------------------------------------

    def volumes(self, pool: str) -> list[dict[str, Any]]:
        return [v for v in self.json("storage", "volume", "list", pool) if v.get("type") == "custom"]

    def volume(self, pool: str, name: str) -> dict[str, Any] | None:
        for v in self.volumes(pool):
            if v["name"] == name:
                return v
        return None

    def volume_create(self, pool: str, name: str, **config: str) -> None:
        self.run("storage", "volume", "create", pool, name, *[f"{k}={v}" for k, v in config.items()])

    def volume_delete(self, pool: str, name: str) -> None:
        self.run("storage", "volume", "delete", pool, name)

    def volume_rename(self, pool: str, old: str, new: str) -> None:
        self.run("storage", "volume", "rename", pool, old, new)

    def volume_copy(self, pool: str, src: str, dst: str) -> None:
        self.run("storage", "volume", "copy", f"{pool}/{src}", f"{pool}/{dst}")

    def volume_set(self, pool: str, name: str, config: dict[str, str]) -> None:
        self.run("storage", "volume", "set", pool, name, *[f"{k}={v}" for k, v in config.items()])

    def volume_snapshots(self, pool: str, name: str) -> list[str]:
        # Incus lists them as "<volume>/<snapshot>".
        return [s["name"].split("/", 1)[-1] for s in self.json("storage", "volume", "snapshot", "list", pool, name)]

    def volume_snapshot(self, pool: str, name: str, snapshot: str) -> None:
        self.run("storage", "volume", "snapshot", "create", pool, name, snapshot)

    def volume_snapshot_restore(self, pool: str, name: str, snapshot: str) -> None:
        self.run("storage", "volume", "snapshot", "restore", pool, name, snapshot)

    def volume_snapshot_delete(self, pool: str, name: str, snapshot: str) -> None:
        self.run("storage", "volume", "snapshot", "delete", pool, name, snapshot)
