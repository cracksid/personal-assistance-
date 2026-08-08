"""
Tests for the URL guard.

Alongside test_paths.py, these are the security tests. That file stops a
tool escaping the filesystem; this one stops it reaching back into the
machine JARVIS runs on.

Most of what follows is deliberate attempts to get through. The rule under
test: a URL is safe only if, AFTER resolving the hostname to real
addresses, none of them is inside this machine or the local network.
"""

import ipaddress
import socket

import pytest

from app.tools.base import ToolError
from app.tools.urls import _is_internal, safe_url


# --- what should be allowed ------------------------------------------------


def test_a_public_https_url_is_allowed():
    assert safe_url("https://example.com/article") == "https://example.com/article"


def test_plain_http_is_allowed():
    assert safe_url("http://example.com") == "http://example.com"


def test_surrounding_whitespace_is_tolerated():
    assert safe_url("  https://example.com  ") == "https://example.com"


# --- schemes ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url,why",
    [
        ("file:///C:/Users/Admin/.env", "would read the disk, bypassing paths.py"),
        ("ftp://example.com/x", "not a web page"),
        ("gopher://example.com/x", "has been used to smuggle bytes at other services"),
        ("javascript:alert(1)", "not a fetchable resource at all"),
    ],
)
def test_non_web_schemes_are_refused(url: str, why: str):
    with pytest.raises(ToolError, match="http and https"):
        safe_url(url)


def test_an_empty_url_is_refused():
    with pytest.raises(ToolError, match="No URL"):
        safe_url("   ")


# --- SSRF: the addresses that matter ---------------------------------------


@pytest.mark.parametrize(
    "url,target",
    [
        ("http://127.0.0.1:8000/tools", "JARVIS's own API"),
        ("http://localhost:8000/health", "the same, by name"),
        ("http://0.0.0.0:8000/", "the unspecified address"),
        ("http://[::1]:8000/", "loopback over IPv6"),
    ],
)
def test_the_machine_itself_cannot_be_fetched(url: str, target: str):
    """
    JARVIS serves its own API on 127.0.0.1. Without this check, "fetch this
    URL" would be a way to make JARVIS talk to JARVIS.
    """
    with pytest.raises(ToolError, match="inside this machine"):
        safe_url(url)


@pytest.mark.parametrize(
    "url,target",
    [
        ("http://192.168.1.1/", "a home router's admin page"),
        ("http://10.0.0.5/", "a private network host"),
        ("http://172.16.0.1/", "the other RFC1918 range"),
    ],
)
def test_the_local_network_cannot_be_fetched(url: str, target: str):
    with pytest.raises(ToolError, match="inside this machine"):
        safe_url(url)


def test_cloud_metadata_cannot_be_fetched():
    """
    169.254.169.254 is where cloud providers serve instance metadata, and it
    hands credentials to anything that asks. It is the single most valuable
    SSRF target on a hosted machine -- caught here as link-local.
    """
    with pytest.raises(ToolError, match="inside this machine"):
        safe_url("http://169.254.169.254/latest/meta-data/")


def test_a_public_hostname_resolving_to_loopback_is_refused(monkeypatch):
    """
    THE test that justifies resolving before checking. Plenty of real public
    domains resolve to 127.0.0.1 -- localtest.me is one -- so blocking the
    literal string "localhost" stops nothing at all.
    """

    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, None, None, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ToolError, match="inside this machine"):
        safe_url("https://totally-innocent-domain.com/")


def test_every_resolved_address_is_checked_not_just_the_first(monkeypatch):
    """
    A hostname can return several addresses. Checking only the first would
    let an attacker list one harmless public IP alongside 127.0.0.1.
    """

    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (socket.AF_INET, None, None, "", ("93.184.216.34", 0)),  # public
            (socket.AF_INET, None, None, "", ("127.0.0.1", 0)),  # sneaky
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ToolError, match="inside this machine"):
        safe_url("https://mixed-answers.example/")


def test_a_hostname_that_does_not_resolve_is_refused(monkeypatch):
    def fake_getaddrinfo(host, *args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ToolError, match="Could not resolve"):
        safe_url("https://this-does-not-exist.invalid/")


# --- the classifier itself -------------------------------------------------


@pytest.mark.parametrize(
    "address,internal",
    [
        ("8.8.8.8", False),
        ("93.184.216.34", False),
        ("127.0.0.1", True),
        ("10.1.2.3", True),
        ("192.168.0.1", True),
        ("169.254.169.254", True),
        ("::1", True),
        ("2606:2800:220:1:248:1893:25c8:1946", False),
    ],
)
def test_internal_address_classification(address: str, internal: bool):
    assert _is_internal(ipaddress.ip_address(address)) is internal
