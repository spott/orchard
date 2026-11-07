import pytest

from orchard import names
from orchard.names import InvalidName


@pytest.mark.parametrize("ok", ["a", "foo", "foo-bar", "f00", "a" * 59, "blah.iac", "exp-2.bkmk", "a" * 28 + "." + "a" * 29])
def test_valid(ok):
    assert names.validate(ok) == ok


@pytest.mark.parametrize(
    "bad",
    ["", "Foo", "1foo", "-foo", "foo-", "foo_bar", "a" * 60, "foo bar", "a.b.c", ".iac", "blah.", "foo--bar", "x.1iac",
     "a" * 29 + "." + "a" * 29],
)
def test_invalid(bad):
    with pytest.raises(InvalidName):
        names.validate(bad)


def test_instance_encodes_the_dot_and_display_decodes_it():
    assert names.instance("foo") == "foo"
    assert names.instance("blah.iac") == "blah--iac"
    assert names.display("blah--iac") == "blah.iac"
    assert names.display("foo") == "foo"
    # Not something `instance` would produce: left alone.
    assert names.display("blah--iac--x") == "blah--iac--x"
    # A parked instance's name round-trips too; it is never shown as a workspace anyway.
    assert names.display("blah--iac-old-20261101t120000z") == "blah.iac-old-20261101t120000z"


def test_split_and_repo():
    assert names.split("blah.iac") == ("blah", "iac")
    assert names.split("foo") == ("foo", None)
    assert names.repo("blah.iac") == "iac" and names.repo("foo") is None


def test_from_host_strips_longest_suffix():
    suffixes = [".orchard", ".orchard.example.com"]
    assert names.from_host("foo.orchard", suffixes) == "foo"
    assert names.from_host("foo.orchard.example.com", suffixes) == "foo"
    assert names.from_host("foo.orchard.example.com.", suffixes) == "foo"
    assert names.from_host("FOO.Orchard", suffixes) == "foo"
    assert names.from_host("foo", suffixes) == "foo"
    assert names.from_host("blah.iac.orchard.example.com", suffixes) == "blah.iac"


def test_from_host_rejects_bare_suffix_and_foreign_names():
    with pytest.raises(InvalidName):
        names.from_host(".orchard", [".orchard"])
    with pytest.raises(InvalidName):
        names.from_host("foo.example.com", [".orchard"])


def test_label_accepts_one_or_two_labels_under_the_zone():
    z = "orchard.example.com"
    assert names.label("foo.orchard.example.com", z) == "foo"
    assert names.label("blah.iac.orchard.example.com.", z) == "blah.iac"
    assert names.label("a.b.c.orchard.example.com", z) is None
    assert names.label("orchard.example.com", z) is None
    assert names.label("_gateway.orchard.example.com", z) is None
    assert names.label("foo.other.example.com", z) is None


def test_derived_names():
    assert names.volume("foo") == "ws-foo"
    assert names.volume("blah.iac") == "ws-blah--iac"
    assert names.trash_volume("blah.iac", "20261101T120000Z") == "trash-blah--iac-20261101T120000Z"
    assert names.child("foo", 2) == "foo-2"
    assert names.child("blah.iac", 1) == "blah-1.iac"
    assert names.old_instance("blah.iac", "20261101T120000Z") == "blah--iac-old-20261101t120000z"
