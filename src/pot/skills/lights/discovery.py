"""Find Tuya devices on the LAN by listening to their discovery broadcasts.

Devices are addressed by gwId, never by IP, because DHCP moves them. Every Tuya device
broadcasts a small encrypted JSON blob every few seconds on UDP 6666/6667/7000;
tinytuya.decrypt_udp handles both the legacy 55AA framing and the 6699/GCM framing that
protocol 3.5 devices use.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import select
import socket
import time
from dataclasses import dataclass

import tinytuya

log = logging.getLogger(__name__)

PORTS = (tinytuya.UDPPORT, tinytuya.UDPPORTS, tinytuya.UDPPORTAPP)
CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class Discovered:
    gwid: str
    ip: str
    version: float
    product_key: str = ""


_cache: dict[str, tuple[float, Discovered]] = {}


def forget(gwid: str) -> None:
    """Drop a cached address, e.g. after a failed connection."""
    _cache.pop(gwid, None)


async def find(gwid: str, *, timeout: float = 8.0, refresh: bool = False) -> Discovered | None:
    cached = _cache.get(gwid)
    if cached and not refresh and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    found = await asyncio.to_thread(listen, {gwid}, timeout)
    return found.get(gwid)


async def scan(timeout: float = 8.0) -> list[Discovered]:
    found = await asyncio.to_thread(listen, None, timeout)
    return sorted(found.values(), key=lambda d: d.ip)


def listen(want: set[str] | None, timeout: float) -> dict[str, Discovered]:
    """Blocking listen. Returns as soon as every wanted gwId has been heard."""
    sockets = []
    for port in PORTS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind(("", port))
        except OSError as exc:
            log.debug("cannot bind udp/%s: %s", port, exc)
            sock.close()
            continue
        sockets.append(sock)

    found: dict[str, Discovered] = {}
    if not sockets:
        log.warning("no UDP discovery ports could be bound")
        return found

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select(sockets, [], [], min(0.5, deadline - time.monotonic()))
            for sock in ready:
                data, addr = sock.recvfrom(4096)
                device = _parse(data, addr[0])
                if device is None:
                    continue
                found[device.gwid] = device
                _cache[device.gwid] = (time.monotonic(), device)
            if want and want <= found.keys():
                break
    finally:
        for sock in sockets:
            sock.close()
    return found


def _parse(data: bytes, source_ip: str) -> Discovered | None:
    try:
        payload = tinytuya.decrypt_udp(data)
        info = json.loads(payload)
    except Exception:  # malformed or foreign broadcast
        return None
    gwid = info.get("gwId") or info.get("devId")
    if not gwid:
        return None
    try:
        version = float(info.get("version") or 3.3)
    except (TypeError, ValueError):
        version = 3.3
    return Discovered(
        gwid=gwid,
        ip=info.get("ip") or source_ip,
        version=version,
        product_key=info.get("productKey", ""),
    )
