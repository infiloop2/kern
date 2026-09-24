"""Run as root under `unshare --net`; never changes the host network namespace.

Load the deployed firewall and reproduce client-first TCP close plus source-port
reuse. --without-cleanup removes only the fix as a negative control.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import threading
import time


CLIENT = """
import json, socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
# Finish the negative control before TCP's first SYN retransmission (~1s).
s.settimeout(0.5)
s.bind(('127.0.0.1', int(sys.argv[2])))
port = s.getsockname()[1]
try:
    s.connect(('127.0.0.1', int(sys.argv[1])))
    s.sendall(b'a')
    assert s.recv(1) == b'x'
    status = 'ok'
except TimeoutError:
    status = 'timeout'
finally:
    s.close()
print(json.dumps({'port': port, 'status': status}))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--without-cleanup", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
        raise SystemExit("requires root in an isolated network namespace (unshare --net)")
    rules = Path("/etc/nftables.conf").read_text()
    cleanup = f"oif lo tcp dport {args.port} tcp flags & syn == 0 ct state established accept"
    if rules.count(cleanup) != 1:
        raise AssertionError("expected one admin TCP cleanup rule in the deployed firewall")
    if args.without_cleanup:
        rules = rules.replace(cleanup, "")
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    subprocess.run(["nft", "-f", "-"], input=rules, text=True, check=True)

    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
        listener.listen(8)
        listener.settimeout(0.1)
        stopped = threading.Event()

        def serve() -> None:
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(2)
                    try:
                        connection.recv(1)
                        connection.sendall(b"x")
                        # The client reads the complete body and closes before
                        # the server's FIN, as an HTTP client is allowed to do.
                        while connection.recv(1):
                            pass
                        time.sleep(0.1)
                    except OSError:
                        pass

        server = threading.Thread(target=serve, daemon=True)
        server.start()

        def probe(user: str, source_port: int = 0) -> dict:
            account = pwd.getpwnam(user)
            result = subprocess.run(
                [sys.executable, "-c", CLIENT, str(args.port), str(source_port)],
                user=account.pw_uid, group=account.pw_gid, extra_groups=[],
                capture_output=True, text=True, timeout=5, check=True,
            )
            return json.loads(result.stdout)

        try:
            first = probe("cloudflared")
            assert first["status"] == "ok", first
            time.sleep(1.2)
            reused = probe("cloudflared", first["port"])
            expected = "timeout" if args.without_cleanup else "ok"
            assert reused["status"] == expected, (first, reused, expected)
            assert probe("cloudflared")["status"] == "ok", "fresh-port control failed"
            if not args.without_cleanup:
                for user in ("root", "kern-admin", "kern-operator"):
                    assert probe(user)["status"] == "ok", user
                for user in ("kern-agent", "kern-tools", "kern-proxy", "kern-workspace"):
                    assert probe(user)["status"] == "timeout", user
        finally:
            stopped.set()
            server.join(timeout=3)
    print("admin TCP cleanup: " + ("old-rule stall reproduced" if args.without_cleanup else "reconnect and UID boundary passed"))


if __name__ == "__main__":
    main()
