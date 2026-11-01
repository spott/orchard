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
        proc = subprocess.run(argv, capture_output=True, text=True, input=input)
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

    def device_add(self, name: str, device: str, kind: str, **props: str) -> None:
        self.run("config", "device", "add", name, device, kind, *[f"{k}={v}" for k, v in props.items()])

    def exec(self, name: str, *cmd: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self.run("exec", name, "--", *cmd, check=check)

    def file_pull(self, name: str, src: str, dst: str) -> None:
        self.run("file", "pull", f"{name}{src}", dst)

    def file_push(self, src: str, name: str, dst: str) -> None:
        self.run("file", "push", src, f"{name}{dst}")

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
