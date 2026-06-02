"""
SOCKS5 bridge: 127.0.0.1:1082  ->  Mullvad SOCKS5 (10.64.0.1:1080)  ->  target host

Accepts SOCKS5 CONNECT requests from python.exe (which is excluded from the
Mullvad VPN tunnel) and forwards them through the Mullvad built-in SOCKS5 proxy
at 10.64.0.1:1080 (only reachable from pythonw.exe, which is inside the tunnel).

This double-hop guarantees VPN exit even when Windows WFP inherits the excluded
process's routing context on sockets that originate from an excluded-process
connection.  Direct TCP from pythonw.exe is NOT used because WFP can tag those
sockets with the excluded caller's route.

If Mullvad is disconnected, 10.64.0.1:1080 becomes unreachable and the bridge
logs an ERROR rather than silently routing through the Azure public IP.

Matchbook is never routed here: it uses HttpClient() with no proxy_url.
"""
import socket
import struct
import threading
import os
import datetime

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 1082

# Mullvad's built-in SOCKS5 proxy — only reachable from pythonw.exe (inside tunnel).
# Using this as the upstream guarantees VPN exit regardless of Windows WFP socket tagging.
MULLVAD_SOCKS5_HOST = "10.64.0.1"
MULLVAD_SOCKS5_PORT = 1080

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


def _connect_via_mullvad(host: str, port: int) -> socket.socket:
    """Open a SOCKS5 connection through Mullvad's built-in proxy at 10.64.0.1:1080.

    Using the Mullvad proxy as the upstream hop guarantees VPN exit: Windows WFP
    can inherit an excluded process's routing context on raw sockets, but 10.64.0.1
    is only reachable from inside the tunnel, so any routing mistake fails loudly.
    """
    sock = socket.create_connection((MULLVAD_SOCKS5_HOST, MULLVAD_SOCKS5_PORT), timeout=10)
    # SOCKS5 greeting — no auth
    sock.sendall(b"\x05\x01\x00")
    resp = _recvall(sock, 2)
    if resp[1] != 0x00:
        sock.close()
        raise ConnectionError(f"Mullvad SOCKS5: unexpected auth method {resp[1]:#x}")
    # CONNECT request
    host_bytes = host.encode()
    req = (
        b"\x05\x01\x00\x03"
        + bytes([len(host_bytes)])
        + host_bytes
        + struct.pack("!H", port)
    )
    sock.sendall(req)
    # Response: VER REP RSV ATYP ...
    hdr = _recvall(sock, 4)
    if hdr[1] != 0x00:
        sock.close()
        raise ConnectionError(f"Mullvad SOCKS5 CONNECT failed: reply={hdr[1]:#x}")
    # Consume the bound address from the response
    atyp = hdr[3]
    if atyp == 0x01:
        _recvall(sock, 4 + 2)
    elif atyp == 0x03:
        length = _recvall(sock, 1)[0]
        _recvall(sock, length + 2)
    elif atyp == 0x04:
        _recvall(sock, 16 + 2)
    return sock


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

        # --- connect via Mullvad's built-in SOCKS5 proxy ---
        # 10.64.0.1:1080 is only reachable from inside the tunnel, so if the VPN
        # is down this fails loudly instead of silently routing via the Azure IP.
        try:
            upstream = _connect_via_mullvad(host, port)
        except OSError as exc:
            # Distinguish upstream (Mullvad proxy) failures from target failures.
            # WinError 10013 = WFP blocked the socket — pythonw.exe is likely
            # in Mullvad's split-tunnel excluded list and can't reach 10.64.0.1.
            if getattr(exc, "winerror", None) == 10013:
                _log(f"ERROR upstream Mullvad SOCKS5 (10.64.0.1:1080) BLOCKED by WFP — "
                     f"pythonw.exe may be in Mullvad's excluded-apps list (target: {host}:{port})")
            else:
                _log(f"ERROR upstream Mullvad SOCKS5 (10.64.0.1:1080) failed for {host}:{port} — {exc}")
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
