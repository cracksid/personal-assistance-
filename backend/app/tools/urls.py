"""
The URL guard.

Every network tool resolves its target through here. This is the network
equivalent of paths.py: that one stops a tool escaping the filesystem
sandbox, this one stops it reaching back into the machine it runs on.

THE ATTACK THIS PREVENTS (SSRF -- server-side request forgery). JARVIS runs
a web server on 127.0.0.1 and sits on your home network. "Fetch this URL for
me" is therefore a way to make JARVIS read things only JARVIS can reach:

    http://127.0.0.1:8000/tools        its own API
    http://192.168.1.1/                your router's admin page
    http://169.254.169.254/latest/     cloud instance metadata, on a server

None of those are reachable from the internet, which is exactly why they are
worth attacking through something that IS. The request would come from a
trusted place and carry any cookies or network position that place has.

RESOLUTION MUST HAPPEN BEFORE THE CHECK, for the same reason as in paths.py.
A hostname is not an address: `localtest.me` and countless other public
domains resolve to 127.0.0.1, so blocking the literal string "localhost"
stops nothing. The check has to run against the IP the name actually
resolves to.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

from app.tools.base import ToolError

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})


def safe_url(raw_url: str) -> str:
    """
    Check a caller-supplied URL is safe to fetch, and return it.

    Args:
        raw_url: whatever the model or the user asked for. Untrusted.

    Returns:
        The URL, unchanged, once it has passed every check.

    Raises:
        ToolError: if the scheme is wrong, the host will not resolve, or it
            resolves to an address inside the machine or the local network.
    """
    if not raw_url or not raw_url.strip():
        raise ToolError("No URL was given.")

    url = raw_url.strip()
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        # file:// would read the disk, bypassing the filesystem sandbox
        # entirely. gopher:// and friends have been used to smuggle
        # arbitrary bytes at other services.
        raise ToolError(
            f"Only http and https URLs can be fetched, not {parsed.scheme or 'that'!r}."
        )

    host = parsed.hostname
    if not host:
        raise ToolError(f"{url!r} does not contain a hostname.")

    for address in _resolve(host):
        if _is_internal(address):
            raise ToolError(
                f"Refusing to fetch {host} -- it resolves to {address}, which is "
                "inside this machine or the local network, not the public internet."
            )

    return url


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """
    Look up every address a hostname resolves to.

    EVERY address, not just the first. A name can return several, and a
    check that only inspected one could be walked straight past by a host
    that returns a harmless public address alongside 127.0.0.1.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ToolError(f"Could not resolve {host!r}: {exc}") from exc

    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue

    if not addresses:
        raise ToolError(f"Could not resolve {host!r} to any address.")
    return addresses


def _is_internal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    Is this address somewhere a public web page could never live?

    is_private covers RFC1918 (10.x, 192.168.x, 172.16-31.x) and their IPv6
    equivalents. is_link_local covers 169.254.x, which is where cloud
    providers put their instance metadata service -- the single most
    valuable SSRF target on a hosted machine, because it hands out
    credentials to anything that asks.
    """
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )
