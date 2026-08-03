"""Bridge stdin/stdout to a TCP target through an HTTP CONNECT proxy.

Intended for OpenSSH's ProxyCommand on platforms without nc/connect-proxy.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading


def pump_stdin(sock: socket.socket) -> None:
    try:
        while chunk := os.read(sys.stdin.fileno(), 65536):
            sock.sendall(chunk)
    except (BrokenPipeError, ConnectionError, OSError):
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=7897)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()

    with socket.create_connection(
        (args.proxy_host, args.proxy_port),
        timeout=args.timeout,
    ) as sock:
        authority = f"{args.host}:{args.port}"
        sock.sendall(
            (
                f"CONNECT {authority} HTTP/1.1\r\n"
                f"Host: {authority}\r\n"
                "Proxy-Connection: Keep-Alive\r\n"
                "\r\n"
            ).encode("ascii")
        )
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("proxy closed before CONNECT response completed")
            response.extend(chunk)
            if len(response) > 65536:
                raise RuntimeError("oversized HTTP CONNECT response")
        header, remainder = bytes(response).split(b"\r\n\r\n", 1)
        status_line = header.splitlines()[0].decode("iso-8859-1", errors="replace")
        fields = status_line.split()
        if len(fields) < 2 or fields[1] != "200":
            raise RuntimeError(f"HTTP CONNECT failed: {status_line}")

        sock.settimeout(None)
        writer = threading.Thread(target=pump_stdin, args=(sock,), daemon=True)
        writer.start()
        if remainder:
            os.write(sys.stdout.fileno(), remainder)
        try:
            while chunk := sock.recv(65536):
                os.write(sys.stdout.fileno(), chunk)
        except (BrokenPipeError, ConnectionError, OSError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
