"""0.8.1: ssh certificate mints take turns and swap key + certificate in together."""

import subprocess
import threading
import time

from orchard import locks

D = "/run/user/1000/orchard/ssh"


def _numbered_keygen(monkeypatch):
    """ssh-keygen whose every key pair is distinguishable: key N, cert N."""
    n = {"i": 0}
    guard = threading.Lock()
    real_run = subprocess.run

    def fake(argv, **kw):
        if not str(argv[0]).endswith("ssh-keygen"):
            return real_run(argv, **kw)
        if "-t" in argv:
            with guard:
                n["i"] += 1
                i = n["i"]
            key = argv[argv.index("-f") + 1]
            open(key, "w").write(f"KEY {i}\n")
            open(key + ".pub", "w").write(f"PUB {i}\n")
        elif "-s" in argv:
            pub = argv[-1]
            i = open(pub).read().split()[1]
            open(pub[: -len(".pub")] + "-cert.pub", "w").write(f"CERT {i}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake)


def test_concurrent_ssh_mints_leave_a_matching_pair(mgr, ca_cfg, inc, monkeypatch):
    _numbered_keygen(monkeypatch)
    mgr.create("foo")
    mgr.grant("foo", "ssh", "ro")

    # Make every push slow and let other threads in, as two round trips to
    # Incus are: without the lock the pushes of different mints interleave.
    push = inc.file_push
    order = {"i": 0}

    def slow_push(src, name, dst, **kw):
        order["i"] += 1
        time.sleep(0.02 if order["i"] % 2 else 0.001)
        push(src, name, dst, **kw)

    monkeypatch.setattr(inc, "file_push", slow_push)
    errors: list[BaseException] = []

    def mint():
        try:
            mgr.token("foo", "ssh")
        except BaseException as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=mint) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    files = inc.files["foo"]
    key, cert = files[f"{D}/id_ed25519"], files[f"{D}/id_ed25519-cert.pub"]
    assert key.split()[1] == cert.split()[1], f"key {key!r} beside certificate {cert!r}"
    assert not [f for f in files if f.endswith(".new")]


def test_mint_swaps_both_files_in_with_one_command(mgr, ca_cfg, inc, keygen):
    mgr.create("foo")
    mgr.grant("foo", "ssh", "ro")
    inc.calls.clear()
    mgr.token("foo", "ssh")
    steps = [c for c in inc.calls if c[0] == "file_push" or (c[0] == "exec" and c[2][:1] == ("sh",))]
    assert [c[0] for c in steps] == ["file_push", "file_push", "exec"]
    assert [c[2] for c in steps[:2]] == [f"{D}/id_ed25519.new", f"{D}/id_ed25519-cert.pub.new"]
    assert steps[2][2][4:] == (
        f"{D}/id_ed25519.new", f"{D}/id_ed25519", f"{D}/id_ed25519-cert.pub.new", f"{D}/id_ed25519-cert.pub",
    )


def test_sweep_waits_for_the_workspace_lock(mgr, ca_cfg, inc, keygen):
    cfg = ca_cfg
    mgr.create("iac")
    mgr.grant("iac", "ssh", "ro")
    done = threading.Event()

    def sweep():
        mgr.sweep()
        done.set()

    with locks.held(cfg.lock_dir, "iac"):
        t = threading.Thread(target=sweep)
        t.start()
        assert not done.wait(0.2), "sweep ran while the workspace lock was held"
    t.join(10)
    assert done.is_set()
