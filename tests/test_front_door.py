import pytest

from orchard import front_door


def test_parse_handles_quotes_and_garbage():
    assert front_door.parse("proxy foo.orchard") == ["proxy", "foo.orchard"]
    assert front_door.parse("dev ls --json") == ["dev", "ls", "--json"]
    assert front_door.parse("dev create 'a b'") == ["dev", "create", "a b"]
    assert front_door.parse("unterminated '") == []


def test_usage_on_empty_or_unknown(monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "")
    assert front_door.main() == 2
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "bash -i")
    assert front_door.main() == 2
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "proxy")
    assert front_door.main() == 2
    out, err = capsys.readouterr()
    assert out == "" and "usage" in err


def test_proxy_rejects_bad_host_without_touching_incus(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(front_door, "proxy", lambda host: called.append(host) or 0)
    assert front_door.main(["proxy", "foo.orchard"]) == 0
    assert called == ["foo.orchard"]


def test_dev_passthrough_to_cli(monkeypatch):
    seen = []
    monkeypatch.setattr(front_door.cli, "main", lambda args: seen.append(args) or 0)
    assert front_door.main(["dev", "ls", "--json"]) == 0
    assert seen == [["ls", "--json"]]
