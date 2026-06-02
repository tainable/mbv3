"""Quick geo-restriction check for Polymarket trading endpoint."""
import httpx
import sys

PROXY = "socks5://127.0.0.1:1082"
HOST  = "https://clob.polymarket.com"

print("Exit IP via bridge ...")
try:
    with httpx.Client(proxy=PROXY, timeout=10) as c:
        ip = c.get("https://ifconfig.me/ip", headers={"User-Agent": "curl/8.0"}).text.strip()
    print(f"  {ip}")
except Exception as e:
    print(f"  FAILED: {e}")
    sys.exit(1)

print()
print("Trading geo-check  POST /order (no credentials) ...")
try:
    with httpx.Client(proxy=PROXY, timeout=10) as c:
        r = c.post(f"{HOST}/order", content=b"{}")
    print(f"  HTTP {r.status_code}")
    if r.status_code == 403:
        try:
            msg = r.json().get("error", r.text[:120])
        except Exception:
            msg = r.text[:120]
        print("  -> GEOBLOCKED  (this relay IP is on Polymarket's blocklist)")
        print(f"  -> {msg}")
        print()
        print("  Fix: switch to a different Mullvad relay and restart the bridge.")
    elif r.status_code == 401:
        print("  -> IP ALLOWED  (401 = auth rejected, not geo-rejected)")
        print("  -> This relay is clear for trading.")
    else:
        try:
            body = r.text[:200]
        except Exception:
            body = ""
        print(f"  -> IP likely OK  (HTTP {r.status_code})  {body}")
except Exception as e:
    print(f"  FAILED: {e}")
