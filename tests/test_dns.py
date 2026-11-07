"""The wake resolver: codec and decisions, no sockets."""

import struct

import pytest

from orchard import dns
from orchard.config import Config
from orchard.workspace import Manager

from .conftest import FROZEN

ZONE = "orchard.example.com"
REV = "0.100.10.in-addr.arpa"


def query(name: str, qtype: int = dns.TYPE_A, qid: int = 0x1234) -> bytes:
    return struct.pack("!HHHHHH", qid, dns.FLAG_RD, 1, 0, 0, 0) + dns._write_name(name) + struct.pack("!HH", qtype, 1)


def parse_answers(resp: bytes):
    qid, flags, qd, an, _, _ = struct.unpack("!HHHHHH", resp[:12])
    _, off = dns._read_name(resp, 12)
    off += 4
    answers = []
    for _ in range(an):
        name, off = dns._read_name(resp, off)
        rtype, rclass, ttl, n = struct.unpack("!HHIH", resp[off : off + 10])
        off += 10
        rdata = resp[off : off + n]
        off += n
        answers.append((name, rtype, ttl, rdata))
    return qid, flags, answers


@pytest.fixture
def dcfg(tmp_path) -> Config:
    return Config(
        lock_dir=str(tmp_path / "locks"), ssh_wait_seconds=5,
        address_start="10.100.0.100", address_end="10.100.0.101",
        dns_zone=ZONE, dns_reverse_zone=REV, dns_ttl=7,
    )


@pytest.fixture
def resolver(dcfg, inc):
    mgr = Manager(dcfg, inc, log=lambda s: None, probe=lambda ip, port: True, sleep=lambda s: None, now=lambda: FROZEN)
    forwarded = []
    wakes = []

    def forward(data):
        forwarded.append(data)
        return b"FWD" + data[:2]

    r = dns.Resolver(dcfg, mgr, forward, wake=lambda n, a, c: wakes.append((n, a, c)), log=lambda s: None)
    r.forwarded = forwarded
    r.wakes = wakes
    r.mgr = mgr
    return r


def test_codec_roundtrip():
    q = dns.Query(query("foo.orchard.example.com"))
    assert (q.qname, q.qtype, q.id) == ("foo.orchard.example.com", dns.TYPE_A, 0x1234)
    resp = q.reply(answers=[dns.a_record("10.100.0.5", 9)])
    qid, flags, answers = parse_answers(resp)
    assert qid == 0x1234 and flags & dns.FLAG_QR and flags & dns.FLAG_AA and flags & dns.FLAG_RD
    assert answers == [("foo.orchard.example.com", dns.TYPE_A, 9, bytes([10, 100, 0, 5]))]
    with pytest.raises(dns.BadPacket):
        dns.Query(b"\0" * 5)
    with pytest.raises(dns.BadPacket):
        dns.Query(resp)  # a response is not a query


def test_reverse_ip():
    assert dns.reverse_ip("44.0.100.10.in-addr.arpa", REV) == "10.100.0.44"
    assert dns.reverse_ip("44.0.100.10.in-addr.arpa.", REV) == "10.100.0.44"
    assert dns.reverse_ip("1.44.0.100.10.in-addr.arpa", REV) is None
    assert dns.reverse_ip("44.1.100.10.in-addr.arpa", REV) is None
    assert dns.reverse_ip("x.0.100.10.in-addr.arpa", REV) is None


def test_running_workspace_is_answered_without_waking(resolver):
    ws = resolver.mgr.create("foo")
    _, _, answers = parse_answers(resolver.respond(query("foo.orchard.example.com")))
    assert answers == [("foo.orchard.example.com", dns.TYPE_A, 7, bytes([10, 100, 0, 100]))]
    assert resolver.wakes == [] and resolver.forwarded == []
    # Other types for a known name: NOERROR, no data (never NXDOMAIN).
    resp = resolver.respond(query("foo.orchard.example.com", qtype=28))
    _, flags, answers = parse_answers(resp)
    assert flags & 0xF == 0 and answers == []
    assert ws.name == "foo"


def test_stopped_workspace_is_answered_with_its_reservation_and_woken(resolver):
    resolver.mgr.create("foo")
    resolver.mgr.stop("foo")
    _, _, answers = parse_answers(resolver.respond(query("foo.orchard.example.com")))
    assert answers[0][3] == bytes([10, 100, 0, 100])
    assert resolver.wakes == [("foo", "10.100.0.100", False)]


def test_unknown_name_is_promised_an_address_and_created(resolver):
    _, _, answers = parse_answers(resolver.respond(query("new.orchard.example.com")))
    assert answers[0][3] == bytes([10, 100, 0, 100])
    assert resolver.wakes == [("new", "10.100.0.100", True)]
    # Asked again before the create finished: same promise, no second wake
    # beyond the resolver's (the Waker dedupes the thread).
    _, _, answers = parse_answers(resolver.respond(query("new.orchard.example.com")))
    assert answers[0][3] == bytes([10, 100, 0, 100])
    # Another new name does not get the promised address.
    _, _, answers = parse_answers(resolver.respond(query("other.orchard.example.com")))
    assert answers[0][3] == bytes([10, 100, 0, 101])
    # Doing the wake creates it at the promised address and clears the promise.
    resolver._wake_now("new", "10.100.0.100", True)
    ws = resolver.mgr.get("new")
    assert ws.running and ws.address == "10.100.0.100" and ws.origin == "dns"
    assert "new" not in resolver.pending


def test_create_on_lookup_can_be_off(resolver):
    resolver.cfg.dns_create = False
    _, flags, answers = parse_answers(resolver.respond(query("new.orchard.example.com")))
    assert flags & 0xF == dns.RCODE_NXDOMAIN and answers == [] and resolver.wakes == []


def test_everything_else_is_forwarded(resolver):
    for name in ("orchard.example.com", "_gateway.orchard.example.com", "a.b.c.orchard.example.com", "foo.example.com"):
        q = query(name)
        assert resolver.respond(q) == b"FWD" + q[:2]
    assert len(resolver.forwarded) == 4 and resolver.wakes == []
    # An unmanaged instance with a workspace-like name is dnsmasq's.
    resolver.mgr.inc.init("img", "plain", ["default"], {})
    q = query("plain.orchard.example.com")
    assert resolver.respond(q) == b"FWD" + q[:2]


def test_reverse_lookups(resolver):
    resolver.mgr.create("foo")
    resolver.mgr.stop("foo")
    _, _, answers = parse_answers(resolver.respond(query("100.0.100.10.in-addr.arpa", qtype=dns.TYPE_PTR)))
    assert answers == [("100.0.100.10.in-addr.arpa", dns.TYPE_PTR, 7, dns._write_name("foo.orchard.example.com"))]
    q = query("9.0.100.10.in-addr.arpa", qtype=dns.TYPE_PTR)
    assert resolver.respond(q) == b"FWD" + q[:2]


def test_garbage_is_dropped(resolver):
    assert resolver.respond(b"\x12\x34") is None
