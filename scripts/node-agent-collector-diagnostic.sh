#!/bin/sh
set -eu

container='remnawave-node-agent'
docker inspect "$container" >/dev/null 2>&1

docker exec "$container" python -c '
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from src.config import Settings


def host_kind(host):
    value = (host or "").lower()
    if value == "host.docker.internal":
        return "host_docker_internal"
    if value in {"localhost", "127.0.0.1", "::1"}:
        return "loopback"
    try:
        return "private_ip" if ipaddress.ip_address(value).is_private else "public_ip"
    except ValueError:
        return "hostname" if value else "missing"


s = Settings()
collector = urlparse(s.collector_url)
ws = urlparse(s.ws_url or s.collector_url)
host = collector.hostname or ""
port = collector.port or (443 if collector.scheme == "https" else 80)
print("collector_scheme=%s" % (collector.scheme or "missing"))
print(f"collector_host_kind={host_kind(host)}")
print("ws_scheme=%s" % (ws.scheme or "missing"))
print(f"ws_host_kind={host_kind(ws.hostname)}")
print("same_endpoint_host=%s" % str(host == (ws.hostname or "")).lower())
print(f"token_set={str(bool(s.auth_token)).lower()}")
print(f"command_enabled={str(bool(s.command_enabled)).lower()}")
print(f"host_mode={str(bool(s.host_mode)).lower()}")
try:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    print("dns_ok=true")
except Exception as exc:
    addresses = []
    print("dns_ok=false")
    print(f"dns_error={type(exc).__name__}")
v4 = sorted({item[4][0] for item in addresses if item[0] == socket.AF_INET})
v6 = sorted({item[4][0] for item in addresses if item[0] == socket.AF_INET6})
print(f"dns_v4_count={len(v4)}")
print(f"dns_v6_count={len(v6)}")


def tcp_success(family, values):
    successful = 0
    for address in values:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(4)
        try:
            target = (address, port) if family == socket.AF_INET else (address, port, 0, 0)
            sock.connect(target)
            successful += 1
        except Exception:
            pass
        finally:
            sock.close()
    return successful


print(f"tcp_v4_success={tcp_success(socket.AF_INET, v4)}")
print(f"tcp_v6_success={tcp_success(socket.AF_INET6, v6)}")
try:
    with socket.create_connection((host, port), timeout=8):
        print("tcp_ok=true")
except Exception as exc:
    print("tcp_ok=false")
    print(f"tcp_error={type(exc).__name__}")
try:
    url = f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health"
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {s.auth_token}"},
        timeout=15,
        follow_redirects=True,
    )
    print(f"http_status={response.status_code}")
    print(f"http_ok={str(response.is_success).lower()}")
except Exception as exc:
    print("http_ok=false")
    print(f"http_error={type(exc).__name__}")
' 2>/dev/null

logs="$(docker logs --tail 1000 "$container" 2>&1 || true)"
printf 'collector_unreachable_logs=%s\n' "$(printf '%s\n' "$logs" | grep -Fc 'Collector API unreachable' || true)"
printf 'send_failed_logs=%s\n' "$(printf '%s\n' "$logs" | grep -Fc 'Send failed' || true)"
printf 'batch_failed_logs=%s\n' "$(printf '%s\n' "$logs" | grep -Fc 'Batch failed' || true)"
printf 'ws_connected_logs=%s\n' "$(printf '%s\n' "$logs" | grep -Fc 'Agent v2 WS connected' || true)"
