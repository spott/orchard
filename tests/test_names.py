import pytest

from orchard import names
from orchard.names import InvalidName


@pytest.mark.parametrize("ok", ["a", "foo", "foo-bar", "f00", "a" * 59])
def test_valid(ok):
    assert names.validate(ok) == ok


@pytest.mark.parametrize("bad", ["", "Foo", "1foo", "-foo", "foo-", "foo.bar", "foo_bar", "a" * 60, "foo bar"])
def test_invalid(bad):
    with pytest.raises(InvalidName):
        names.validate(bad)


def test_from_host_strips_longest_suffix():
    suffixes = [".orchard", ".orchard.example.com"]
    assert names.from_host("foo.orchard", suffixes) == "foo"
    assert names.from_host("foo.orchard.example.com", suffixes) == "foo"
    assert names.from_host("foo.orchard.example.com.", suffixes) == "foo"
    assert names.from_host("FOO.Orchard", suffixes) == "foo"
    assert names.from_host("foo", suffixes) == "foo"


def test_from_host_rejects_bare_suffix_and_foreign_names():
    with pytest.raises(InvalidName):
        names.from_host(".orchard", [".orchard"])
    with pytest.raises(InvalidName):
        names.from_host("foo.example.com", [".orchard"])


def test_volume_names():
    assert names.volume("foo") == "ws-foo"
    assert names.trash_volume("foo", "20261101T120000Z") == "trash-foo-20261101T120000Z"
