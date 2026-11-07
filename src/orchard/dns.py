"""`orchard-dns`: the wake resolver.

The recursive resolvers forward the workspace zone here. A lookup of
`<name>.<zone>` is the one moment the network says which workspace it wants
(ssh itself never sends the name), so this answers with the workspace's
address and, in the background, starts it if it is stopped or creates it if
it does not exist. By the time the client's second SYN goes out the
container's sshd is up (create takes seconds). Everything that is not a
workspace name (the apex, `_gateway`, AAAA for others, unknown reverse
entries) is forwarded verbatim to Incus's dnsmasq, which knows the leases.

Standard library only: a minimal DNS codec for one question and A/PTR
answers, UDP and TCP, one thread per transport plus one per wake."""

from __future__ import annotations

import ipaddress
import socket
import socketserver
import struct
import sys
import threading
import time
from typing import Callable

from . import OrchardError, names
from .config import Config
from .incus import Incus
from .workspace import Manager

TYPE_A = 1
TYPE_PTR = 12
TYPE_ANY = 255
CLASS_IN = 1
RCODE_SERVFAIL = 2
RCODE_NXDOMAIN = 3
RCODE_REFUSED = 5
FLAG_QR = 0x8000
FLAG_AA = 0x0400
FLAG_TC = 0x0200
FLAG_RD = 0x0100
MASK_OPCODE = 0x7800


class BadPacket(ValueError):
    pass


# -- codec --------------------------------------------------------------------


def _read_name(data: bytes, off: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    end = off
    hops = 0
    while True:
        if off >= len(data):
            raise BadPacket("truncated name")
        n = data[off]
        if n == 0:
            off += 1
            break
        if n & 0xC0 == 0xC0:
            if off + 1 >= len(data):
                raise BadPacket("truncated pointer")
            ptr = struct.unpack("!H", data[off : off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            jumped = True
            off = ptr
            hops += 1
            if hops > 16:
                raise BadPacket("pointer loop")
            continue
        off += 1
        labels.append(data[off : off + n].decode("ascii", "replace"))
        off += n
    if not jumped:
        end = off
    return ".".join(labels), end


def _write_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        raw = label.encode("ascii")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


class Query:
    def __init__(self, data: bytes) -> None:
        if len(data) < 12:
            raise BadPacket("short header")
        self.id, self.flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
        if self.flags & FLAG_QR or qd != 1:
            raise BadPacket("not a single question")
        self.qname, off = _read_name(data, 12)
        if off + 4 > len(data):
            raise BadPacket("truncated question")
        self.qtype, self.qclass = struct.unpack("!HH", data[off : off + 4])
        self.question = data[12 : off + 4]
        self.data = data

    def opcode(self) -> int:
        return (self.flags & MASK_OPCODE) >> 11

    def reply(self, rcode: int = 0, answers: list[tuple[int, int, bytes]] | None = None) -> bytes:
        """Authoritative response; answers are (type, ttl, rdata) for qname."""
        answers = answers or []
        flags = FLAG_QR | FLAG_AA | (self.flags & FLAG_RD) | rcode
        head = struct.pack("!HHHHHH", self.id, flags, 1, len(answers), 0, 0)
        body = self.question
        for rtype, ttl, rdata in answers:
            # 0xC00C: pointer to the question name.
            body += struct.pack("!HHHIH", 0xC00C, rtype, CLASS_IN, ttl, len(rdata)) + rdata
        return head + body


def a_record(ip: str, ttl: int) -> tuple[int, int, bytes]:
    return TYPE_A, ttl, ipaddress.IPv4Address(ip).packed


def ptr_record(name: str, ttl: int) -> tuple[int, int, bytes]:
    return TYPE_PTR, ttl, _write_name(name)


def reverse_ip(qname: str, reverse_zone: str) -> str | None:
    """`44.11.42.10.in-addr.arpa` under `11.42.10.in-addr.arpa` -> `10.42.11.44`."""
    qname = qname.lower().rstrip(".")
    zone = reverse_zone.lower().rstrip(".")
    if not zone or not qname.endswith("." + zone):
        return None
    head = qname[: -len(zone) - 1]
    octets = head.split(".")
    zone_octets = zone.removesuffix(".in-addr.arpa").split(".")
    if len(octets) + len(zone_octets) != 4 or not all(o.isdigit() for o in octets):
        return None
    return ".".join(reversed(octets + zone_octets))


# -- the resolver -------------------------------------------------------------


class Resolver:
    """Decides every answer. Pure except for the manager and the wake
    callback, so it is testable without sockets."""

    def __init__(
        self,
        cfg: Config,
        mgr: Manager,
        forward: Callable[[bytes], bytes | None],
        wake: Callable[[str, str, bool], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.mgr = mgr
        self.forward = forward
        self.wake = wake or self._wake_now
        self.log = log or (lambda msg: print(f"orchard-dns: {msg}", file=sys.stderr, flush=True))
        self.pending: dict[str, str] = {}  # name -> address promised before the instance exists
        self.lock = threading.Lock()

    def respond(self, data: bytes) -> bytes | None:
        try:
            q = Query(data)
        except BadPacket as e:
            self.log(f"dropping packet: {e}")
            return None
        if q.opcode() != 0 or q.qclass not in (CLASS_IN, TYPE_ANY):
            return self.forward(data)
        try:
            label = names.label(q.qname, self.cfg.dns_zone)
            if label is not None:
                return self.answer_workspace(q, label)
            ip = reverse_ip(q.qname, self.cfg.dns_reverse_zone) if q.qtype in (TYPE_PTR, TYPE_ANY) else None
            if ip is not None:
                ws = self.mgr.find_by_address(ip)
                if ws is not None:
                    return q.reply(answers=[ptr_record(f"{ws.name}.{self.cfg.dns_zone}", self.cfg.dns_ttl)])
        except OrchardError as e:
            self.log(f"{q.qname}: {e}")
            return q.reply(rcode=RCODE_SERVFAIL)
        return self.forward(data)

    def answer_workspace(self, q: Query, label: str) -> bytes | None:
        try:
            ws = self.mgr.get(label)
        except OrchardError:
            # An instance of that name that is not ours: dnsmasq's business.
            return self.forward(q.data)
        if ws is None:
            if not self.cfg.dns_create:
                return q.reply(rcode=RCODE_NXDOMAIN)
            if not self.mgr.repo_allowed(label):
                # `name.repo` for a repository the App cannot reach: a typo
                # in the repo part costs nothing.
                self.log(f"{label}: no such repository, not creating")
                return q.reply(rcode=RCODE_NXDOMAIN)
            addr = self._promise(label)
            if addr is None:
                return q.reply(rcode=RCODE_NXDOMAIN)
            self.wake(label, addr, True)
        elif ws.running:
            addr = ws.ip or ws.address
        else:
            addr = ws.address or self.mgr.allocate_address(self.pending.values())
            self.wake(label, addr or "", False)
        if q.qtype in (TYPE_A, TYPE_ANY) and addr:
            return q.reply(answers=[a_record(addr, self.cfg.dns_ttl)])
        # The name exists; this type has no data (keeps AAAA from going
        # NXDOMAIN upstream, which would poison the A too).
        return q.reply()

    def _promise(self, label: str) -> str | None:
        with self.lock:
            addr = self.pending.get(label)
            if addr is None:
                addr = self.mgr.allocate_address(self.pending.values())
                if addr is not None:
                    self.pending[label] = addr
            return addr

    def _wake_now(self, name: str, addr: str, create: bool) -> None:
        try:
            if create:
                self.log(f"creating {name} at {addr}")
                self.mgr.ensure_running(name, origin="dns", address=addr or None)
            else:
                self.log(f"starting {name}")
                self.mgr.start(name)
        except OrchardError as e:
            self.log(f"{name}: {e}")
        finally:
            with self.lock:
                self.pending.pop(name, None)


class Waker:
    """Runs wakes on background threads, one per workspace at a time."""

    def __init__(self, resolver: Resolver) -> None:
        self.resolver = resolver
        self.active: set[str] = set()
        self.lock = threading.Lock()

    def __call__(self, name: str, addr: str, create: bool) -> None:
        with self.lock:
            if name in self.active:
                return
            self.active.add(name)

        def run() -> None:
            try:
                self.resolver._wake_now(name, addr, create)
            finally:
                with self.lock:
                    self.active.discard(name)

        threading.Thread(target=run, name=f"wake-{name}", daemon=True).start()


# -- transport ----------------------------------------------------------------


def make_forwarder(host: str, port: int, timeout: float = 3.0) -> Callable[[bytes], bytes | None]:
    def forward(data: bytes) -> bytes | None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout)
                s.sendto(data, (host, port))
                while True:
                    resp, _ = s.recvfrom(65535)
                    if len(resp) >= 2 and resp[:2] == data[:2]:
                        return resp
        except OSError:
            return None

    return forward


def serve(cfg: Config, resolver: Resolver) -> None:
    class UDP(socketserver.ThreadingMixIn, socketserver.UDPServer):
        allow_reuse_address = True
        daemon_threads = True

    class TCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    class UDPHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            data, sock = self.request
            resp = resolver.respond(data)
            if resp is not None:
                sock.sendto(resp, self.client_address)

    class TCPHandler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(10)
            try:
                head = self.request.recv(2)
                if len(head) < 2:
                    return
                (n,) = struct.unpack("!H", head)
                data = b""
                while len(data) < n:
                    chunk = self.request.recv(n - len(data))
                    if not chunk:
                        return
                    data += chunk
                resp = resolver.respond(data)
                if resp is not None:
                    self.request.sendall(struct.pack("!H", len(resp)) + resp)
            except OSError:
                return

    addr = (cfg.dns_listen, cfg.dns_port)
    udp = UDP(addr, UDPHandler)
    tcp = TCP(addr, TCPHandler)
    threading.Thread(target=udp.serve_forever, name="udp", daemon=True).start()
    threading.Thread(target=tcp.serve_forever, name="tcp", daemon=True).start()
    resolver.log(f"listening on {cfg.dns_listen}:{cfg.dns_port} for {cfg.dns_zone} and {cfg.dns_reverse_zone}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> int:
    cfg = Config.load()
    for key in ("dns_listen", "dns_zone", "dns_reverse_zone", "dns_upstream"):
        if not getattr(cfg, key):
            print(f"orchard-dns: {key} is not configured", file=sys.stderr)
            return 2
    mgr = Manager(cfg, Incus(cfg.incus), log=lambda m: print(f"orchard-dns: {m}", file=sys.stderr, flush=True))
    resolver = Resolver(cfg, mgr, make_forwarder(cfg.dns_upstream, cfg.dns_upstream_port))
    resolver.wake = Waker(resolver)
    serve(cfg, resolver)
    return 0


if __name__ == "__main__":
    sys.exit(main())
