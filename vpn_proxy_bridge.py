"""
SOCKS5 bridge: 127.0.0.1:1081  ->  target host (direct from pythonw.exe inside VPN)

Accepts SOCKS5 CONNECT requests from python.exe (which is excluded from the
Mullvad VPN tunnel) and opens the upstream connection from pythonw.exe, which
IS inside the VPN tunnel.  Outbound connections therefore always egress through
the Mullvad exit node.

If Mullvad is disconnected the upstream connect will fail (exit node
unreachable) and the bridge logs an ERROR rather than silently routing through
the Azure public IP.

Matchbook is never routed here: it uses HttpClient() with no proxy_url.
"""
import socket
import struct
import threading
import os
import datetime

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 1081

LOG = os.path.join(os.path.dirname(__file__), "vpn_bridge.log")


def _log(msg: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a") as f:
        f.write(f"[{ts}] {msg}\n")


def forward(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _recvall(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client disconnected mid-handshake")
        buf += chunk
    return buf


def handle(client: socket.socket) -> None:
    try:
        # --- auth negotiation ---
        header = _recvall(client, 2)
        ver, nmethods = header[0], header[1]
        if ver != 5:
            client.close()
            return
        _recvall(client, nmethods)          # discard offered methods
        client.sendall(b"\x05\x00")         # no auth required

        # --- CONNECT request ---
        req = _recvall(client, 4)
        ver, cmd, _, atyp = req[0], req[1], req[2], req[3]
        if ver != 5 or cmd != 1:            # only CONNECT supported
            client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            client.close()
            return

        if atyp == 0x01:                    # IPv4
            raw = _recvall(client, 4)
            host = socket.inet_ntoa(raw)
        elif atyp == 0x03:                  # domain name
            length = _recvall(client, 1)[0]
            host = _recvall(client, length).decode()
        elif atyp == 0x04:                  # IPv6
            raw = _recvall(client, 16)
            host = socket.inet_ntop(socket.AF_INET6, raw)
        else:
            client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            client.close()
            return

        port = struct.unpack("!H", _recvall(client, 2))[0]

        # --- connect directly (pythonw.exe is inside VPN tunnel) ---
        # This process runs as pythonw.exe which is NOT in Mullvad's split-
        # tunnel exclusion list, so its connections always exit via Mullvad.
        # Failure here means VPN is down — explicit error, no silent bypass.
        try:
            upstream = socket.create_connection((host, port), timeout=10)
        except OSError as exc:
            _log(f"ERROR connecting to {host}:{port} — {exc}")
            client.sendall(b"\x05\x04\x00\x01" + b"\x00" * 6)
            client.close()
            return

        _log(f"CONNECT {host}:{port}")
        client.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

        # --- relay ---
        t = threading.Thread(target=forward, args=(upstream, client), daemon=True)
        t.start()
        forward(client, upstream)

    except Exception as exc:
        _log(f"ERROR in handle — {exc}")
        try:
            client.close()
        except OSError:
            pass


def main() -> None:
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((LISTEN_HOST, LISTEN_PORT))
        server.listen(64)
        _log(f"Bridge started (direct/VPN): {LISTEN_HOST}:{LISTEN_PORT}")
    except OSError as exc:
        _log(f"FATAL: could not bind {LISTEN_HOST}:{LISTEN_PORT} — {exc}")
        return

    while True:
        try:
            client, addr = server.accept()
            threading.Thread(target=handle, args=(client,), daemon=True).start()
        except Exception as exc:
            _log(f"ERROR in accept loop — {exc}")


if __name__ == "__main__":
    main()
