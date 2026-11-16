"""Discover Sonos speakers on the local network via SSDP."""

from __future__ import annotations

import re
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass


SSDP_MCAST = ("239.255.255.250", 1900)
SEARCH_TARGET = "urn:schemas-upnp-org:device:ZonePlayer:1"
SEARCH_MESSAGE = (
    "M-SEARCH * HTTP/1.1\r\n"
    f"HOST: {SSDP_MCAST[0]}:{SSDP_MCAST[1]}\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 2\r\n"
    f"ST: {SEARCH_TARGET}\r\n"
    "\r\n"
).encode("ascii")

LOCATION_RE = re.compile(r"^LOCATION:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class DiscoveredSpeaker:
    ip: str
    location_url: str


def discover_speakers(
    timeout: float = 3.0,
    interface_ips: list[str] | None = None,
) -> list[DiscoveredSpeaker]:
    """Send SSDP M-SEARCH out of every local IPv4 interface and collect replies.

    On machines with several interfaces (Docker bridges, VPNs, link-local
    ports) the kernel's multicast route often points at the wrong one, so a
    single unbound socket never reaches the speakers. Sending once per
    interface address avoids depending on that route.
    """
    addrs = interface_ips or local_ipv4_addresses()
    sockets: list[socket.socket] = []
    for addr in addrs:
        sock = _open_search_socket(addr)
        if sock is not None:
            sockets.append(sock)

    seen: set[str] = set()
    speakers: list[DiscoveredSpeaker] = []
    try:
        for sock in sockets:
            for _ in range(2):  # UDP is lossy; a second send is cheap insurance
                try:
                    sock.sendto(SEARCH_MESSAGE, SSDP_MCAST)
                except OSError:
                    pass

        end = time.monotonic() + timeout
        while sockets:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select(sockets, [], [], min(0.5, remaining))
            for sock in ready:
                try:
                    data, _addr = sock.recvfrom(65535)
                except OSError:
                    continue
                speaker = _parse_reply(data)
                if speaker and speaker.ip not in seen:
                    seen.add(speaker.ip)
                    speakers.append(speaker)
    finally:
        for sock in sockets:
            sock.close()

    return sorted(speakers, key=lambda s: tuple(int(p) for p in s.ip.split(".")))


def local_ipv4_addresses() -> list[str]:
    """Best-effort list of this machine's non-loopback IPv4 addresses."""
    found: list[str] = []

    if sys.platform.startswith("linux"):
        found.extend(_linux_interface_addresses())

    # The address used for the default route (connect() on UDP sends nothing).
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never contacted
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    unique: list[str] = []
    for addr in found:
        if addr and not addr.startswith("127.") and addr not in unique:
            unique.append(addr)
    return unique or ["0.0.0.0"]


def _linux_interface_addresses() -> list[str]:
    try:
        import fcntl
    except ImportError:
        return []

    siocgifaddr = 0x8915
    addrs: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in socket.if_nameindex():
            try:
                packed = struct.pack("256s", name.encode()[:15])
                result = fcntl.ioctl(probe.fileno(), siocgifaddr, packed)
                addrs.append(socket.inet_ntoa(result[20:24]))
            except OSError:
                continue  # interface has no IPv4 address
    finally:
        probe.close()
    return addrs


def _open_search_socket(addr: str) -> socket.socket | None:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    except OSError:
        return None
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if addr != "0.0.0.0":
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
        sock.bind((addr, 0))
        sock.setblocking(False)
        return sock
    except OSError:
        sock.close()
        return None


def _parse_reply(data: bytes) -> DiscoveredSpeaker | None:
    text = data.decode("utf-8", errors="replace")
    match = LOCATION_RE.search(text)
    if not match:
        return None
    location = match.group(1).strip()
    ip = _ip_from_location(location)
    if not ip:
        return None
    return DiscoveredSpeaker(ip=ip, location_url=location)


def _ip_from_location(location: str) -> str | None:
    # http://192.168.1.165:1400/xml/device_description.xml
    match = re.match(r"https?://([^:/]+)", location)
    return match.group(1) if match else None
