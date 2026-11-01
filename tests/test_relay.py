import os
import socket
import threading

from orchard.relay import relay


def test_relay_both_directions_and_half_close():
    client, server = socket.socketpair()
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()

    t = threading.Thread(target=relay, args=(client, in_r, out_w))
    t.start()

    os.write(in_w, b"hello sshd")
    assert server.recv(100) == b"hello sshd"
    server.sendall(b"banner")
    assert os.read(out_r, 100) == b"banner"

    os.close(in_w)  # client went away: server must see EOF but can still speak
    assert server.recv(100) == b""
    server.sendall(b"bye")
    assert os.read(out_r, 100) == b"bye"
    server.close()
    t.join(timeout=5)
    assert not t.is_alive()
    os.close(out_w)
    os.close(out_r)
    os.close(in_r)


def test_relay_large_payload():
    client, server = socket.socketpair()
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    payload = os.urandom(3 * 1024 * 1024)

    t = threading.Thread(target=relay, args=(client, in_r, out_w))
    t.start()

    def feed():
        os.write(in_w, payload)
        os.close(in_w)

    threading.Thread(target=feed).start()
    got = bytearray()
    while len(got) < len(payload):
        got += server.recv(1 << 20)
    assert bytes(got) == payload
    server.close()
    t.join(timeout=5)
    for fd in (in_r, out_r, out_w):
        os.close(fd)


def test_relay_with_unpollable_stdin_still_drains_socket():
    client, server = socket.socketpair()
    out_r, out_w = os.pipe()
    devnull = os.open(os.devnull, os.O_RDONLY)
    t = threading.Thread(target=relay, args=(client, devnull, out_w))
    t.start()
    assert server.recv(10) == b""  # half-closed from our side immediately
    server.sendall(b"banner")
    assert os.read(out_r, 100) == b"banner"
    server.close()
    t.join(timeout=5)
    assert not t.is_alive()
    for fd in (devnull, out_r, out_w):
        os.close(fd)
