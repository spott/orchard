import os
import stat

from orchard.incus import Incus, IncusError


def test_run_does_not_inherit_stdin(tmp_path):
    # A fake incus that would block forever on inherited stdin and reports
    # what it saw on fd 0.
    fake = tmp_path / "incus"
    fake.write_text("#!/bin/sh\nif [ \"$1\" = fail ]; then echo boom >&2; exit 3; fi\ncat; echo stdin-eof\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    r, w = os.pipe()  # an open pipe nobody writes to: inheriting it would hang
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        proc = Incus(str(fake)).run("list")
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(r)
        os.close(w)
    assert proc.stdout.strip() == "stdin-eof"

    try:
        Incus(str(fake)).run("fail")
    except IncusError as e:
        assert e.returncode == 3 and e.stderr == "boom" and "boom" in str(e)
    else:
        raise AssertionError("expected IncusError")

    proc = Incus(str(fake)).run("fail", check=False)
    assert proc.returncode == 3
