#!/usr/bin/env python3
"""SSH ProxyCommand: 通过 mihomo SOCKS5 代理 (127.0.0.1:7890, mixed 端口) 建立隧道。
用法: GIT_SSH_COMMAND="ssh -o ProxyCommand='python3 .../ssh_proxy.py %h %p'" git push
"""
import os
import select
import socket
import struct
import sys

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 7890


def main() -> int:
    host, port = sys.argv[1], int(sys.argv[2])
    s = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=15)
    # SOCKS5 握手 (no auth)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        sys.stderr.write("ssh_proxy: socks5 handshake failed\n")
        return 1
    # CONNECT
    req = (b"\x05\x01\x00\x03" + bytes([len(host)]) + host.encode()
           + struct.pack(">H", port))
    s.sendall(req)
    resp = s.recv(10)
    if len(resp) < 2 or resp[1] != 0:
        sys.stderr.write(f"ssh_proxy: socks5 CONNECT failed: {resp[:10]!r}\n")
        return 1
    # 双向转发: socket <-> stdin/stdout
    while True:
        r, _, _ = select.select([s, sys.stdin], [], [], 60)
        if s in r:
            data = s.recv(65536)
            if not data:
                break
            os.write(1, data)
        if sys.stdin in r:
            data = os.read(0, 65536)
            if not data:
                break
            s.sendall(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
