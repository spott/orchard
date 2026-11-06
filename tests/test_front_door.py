import pytest

from orchard import front_door
from orchard.config import Config
from orchard.workspace import KEY, Manager


@pytest.fixture
def operator(monkeypatch):
    monkeypatch.setattr(front_door, "identify", lambda: (front_door.OPERATOR, None))


def test_parse_handles_quotes_and_garbage():
    assert front_door.parse("proxy foo.orchard") == ["proxy", "foo.orchard"]
    assert front_door.parse("dev ls --json") == ["dev", "ls", "--json"]
    assert front_door.parse("dev create 'a b'") == ["dev", "create", "a b"]
    assert front_door.parse("unterminated '") == []


def test_usage_on_empty_or_unknown(operator, monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "")
    assert front_door.main() == 2
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "bash -i")
    assert front_door.main() == 2
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "proxy")
    assert front_door.main() == 2
    out, err = capsys.readouterr()
    assert out == "" and "usage" in err


def test_proxy_rejects_bad_host_without_touching_incus(operator, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(front_door, "proxy", lambda host: called.append(host) or 0)
    assert front_door.main(["proxy", "foo.orchard"]) == 0
    assert called == ["foo.orchard"]


def test_dev_passthrough_to_cli(operator, monkeypatch):
    seen = []
    monkeypatch.setattr(front_door.cli, "main", lambda args: seen.append(args) or 0)
    assert front_door.main(["dev", "ls", "--json"]) == 0
    assert seen == [["ls", "--json"]]


def test_identify_denies_unless_positively_recognised(tmp_path, inc, cfg):
    cfg.operator_keys = ["ssh-ed25519 AAAAOPERATOR me@laptop", "garbage"]
    m = Manager(cfg, inc, log=lambda s: None, probe=lambda ip, port: True, sleep=lambda s: None)
    m.create("foo")
    inc.inst["foo"]["config"][KEY + "selfkey"] = "ssh-ed25519 AAAASELF orchard:foo"
    auth = tmp_path / "auth"

    auth.write_text("publickey ssh-ed25519 AAAAOPERATOR\n")
    assert front_door.identify(str(auth), cfg, m) == (front_door.OPERATOR, None)
    auth.write_text("publickey ssh-ed25519 AAAASELF\n")
    assert front_door.identify(str(auth), cfg, m) == (front_door.WORKSPACE, "foo")

    auth.write_text("publickey ssh-ed25519 AAAASTRANGER\n")
    kind, why = front_door.identify(str(auth), cfg, m)
    assert kind == front_door.DENIED and "neither" in why
    auth.write_text("")
    assert front_door.identify(str(auth), cfg, m)[0] == front_door.DENIED
    auth.write_text("hostbased ssh-ed25519 AAAAOPERATOR\n")
    assert front_door.identify(str(auth), cfg, m)[0] == front_door.DENIED
    assert front_door.identify(str(tmp_path / "missing"), cfg, m)[0] == front_door.DENIED


def test_identify_denies_without_auth_info(monkeypatch, cfg):
    monkeypatch.delenv("SSH_USER_AUTH", raising=False)
    kind, why = front_door.identify(None, cfg)
    assert kind == front_door.DENIED and "ExposeAuthInfo" in why


def test_operator_key_needs_no_incus(tmp_path, cfg):
    cfg.operator_keys = ["ssh-ed25519 AAAAOPERATOR"]
    auth = tmp_path / "auth"
    auth.write_text("publickey ssh-ed25519 AAAAOPERATOR\n")

    class Boom:
        def find_by_selfkey(self, key):
            raise AssertionError("incus must not be consulted for an operator key")

    assert front_door.identify(str(auth), cfg, Boom()) == (front_door.OPERATOR, None)


def test_main_refuses_denied_sessions(monkeypatch, capsys):
    monkeypatch.setattr(front_door, "identify", lambda: (front_door.DENIED, "no idea"))
    called = []
    monkeypatch.setattr(front_door.cli, "main", lambda *a, **k: called.append(a) or 0)
    assert front_door.main(["dev", "ls"]) == 1
    assert front_door.main(["proxy", "foo.orchard"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "access denied: no idea" in err and called == []


def test_main_unknown_subject_loads_config(monkeypatch, tmp_path, capsys):
    # A real run with no SSH_USER_AUTH: denied before anything else happens.
    monkeypatch.delenv("SSH_USER_AUTH", raising=False)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, path=None: Config()))
    assert front_door.main(["dev", "ls"]) == 1
    assert "access denied" in capsys.readouterr().err
