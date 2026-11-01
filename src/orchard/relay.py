"""Pump bytes between our stdin/stdout and a TCP socket. This is what a
ProxyCommand is: ssh on the far side of our stdio, sshd on the far side of
the socket. Pure Python so there is no netcat flavour to argue about."""

from __future__ import annotations

import os
import selectors
import socket

CHUNK = 65536


def relay(sock: socket.socket, stdin_fd: int = 0, stdout_fd: int = 1) -> None:
    sock.setblocking(True)
    sel = selectors.DefaultSelector()
    sel.register(sock, selectors.EVENT_READ, "sock")
    stdin_open = True
    try:
        sel.register(stdin_fd, selectors.EVENT_READ, "in")
    except (PermissionError, OSError):
        # stdin is not pollable (/dev/null, a regular file): nothing will
        # ever arrive from the client, so half-close and just drain sshd.
        stdin_open = False
        sock.shutdown(socket.SHUT_WR)
    try:
        while True:
            for key, _ in sel.select():
                if key.data == "in":
                    data = os.read(stdin_fd, CHUNK)
                    if not data:
                        # Client closed its side: tell sshd, keep draining what it still has to say.
                        sel.unregister(stdin_fd)
                        stdin_open = False
                        try:
                            sock.shutdown(socket.SHUT_WR)
                        except OSError:
                            return
                        continue
                    sock.sendall(data)
                else:
                    data = sock.recv(CHUNK)
                    if not data:
                        return
                    view = memoryview(data)
                    while view:
                        n = os.write(stdout_fd, view)
                        view = view[n:]
            if not stdin_open and not sel.get_map():
                return
    except (BrokenPipeError, ConnectionResetError):
        return
    finally:
        sel.close()
