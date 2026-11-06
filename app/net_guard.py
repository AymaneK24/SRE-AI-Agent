"""Controls where this server may connect, so a saved address can't be used to reach things it shouldn't.

Two layers work together:
- check_url and check_host give a clear error when someone saves or tests an address.
- guarded_transport repeats the check at the moment of connecting, on the addresses actually used.
  A name that resolves safely during the first check and differently afterwards (DNS rebinding) is still stopped.

This product is installed inside a company's network, and its job is to reach internal systems such as Prometheus
and Ollama. So private addresses are allowed by default on a self-hosted installation. Only an admin can save a target.
Set RCC_TARGET_ALLOWLIST to limit targets further.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

import httpcore
import httpx


class BlockedTarget(ValueError):
    pass


METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}
# These public services answer any name with whatever address is written inside the name.
REBINDING_SUFFIXES = (".nip.io", ".sslip.io", ".xip.io", ".localtest.me", ".lvh.me", ".vcap.me", ".traefik.me", ".lacolhost.com", ".rebind.it", ".rbndr.us", ".1u.ms")
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}$")
_CANONICAL_IPV4 = re.compile(r"^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$")


@dataclass(frozen=True)
class TargetPolicy:
    allow_private: bool = True
    allow_loopback: bool = False
    allowlist: tuple[str, ...] = ()


def _policy(value: TargetPolicy | bool) -> TargetPolicy:
    return value if isinstance(value, TargetPolicy) else TargetPolicy(allow_private=bool(value))


def _allowlisted(name: str, ip: Any, policy: TargetPolicy) -> bool:
    for entry in policy.allowlist:
        if entry == name:
            return True
        if ip is not None and "/" in entry:
            try:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        elif ip is not None and entry == str(ip):
            return True
    return False


def _check_address(address: str, policy: TargetPolicy, name: str = "") -> None:
    ip = ipaddress.ip_address(address.split("%")[0])
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    label = name or address
    # Link-local addresses include cloud metadata services, which hold server credentials. No setting allows these.
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise BlockedTarget(f"{label} is a reserved address and can't be used as a target.")
    if policy.allowlist:
        if _allowlisted(name, ip, policy):
            return
        raise BlockedTarget(f"{label} isn't in this installation's list of allowed targets (RCC_TARGET_ALLOWLIST).")
    if ip.is_loopback:
        if policy.allow_loopback:
            return
        raise BlockedTarget(f"{label} points to this server itself, which can't be used as a target. In Docker, use host.docker.internal or the host's IP address.")
    if ip.is_reserved:
        raise BlockedTarget(f"{label} is a reserved address and can't be used as a target.")
    if not policy.allow_private and ip.is_private:
        raise BlockedTarget(
            f"{label} is a private address. This shared service can only reach public addresses. "
            "Use a self-hosted deployment to monitor private networks."
        )


def _check_name(host: str) -> str:
    """Refuse the disguised ways of writing an address, which have no honest use in a settings form."""
    name = host.strip("[]").lower().rstrip(".")
    if name in METADATA_HOSTS:
        raise BlockedTarget(f"{host} can't be used as a target.")
    if name.endswith(REBINDING_SUFFIXES):
        raise BlockedTarget(f"{host} uses a public service that can point to any address, so it can't be used as a target.")
    if _NUMERIC_HOST.match(name) and not _CANONICAL_IPV4.match(name):
        raise BlockedTarget(f"{host} isn't a normal IP address. Write it like 10.0.0.5.")
    if ":" in name:
        try:
            ip = ipaddress.ip_address(name.split("%")[0])
        except ValueError as error:
            raise BlockedTarget(f"{host} isn't a valid address.") from error
        if ip.ipv4_mapped or ip.sixtofour or ip.teredo:
            raise BlockedTarget(f"{host} hides an IPv4 address inside IPv6. Write the IPv4 address directly.")
    return name


async def _resolve(name: str, port: int | None = None) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(name, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def check_host(host: str, policy: TargetPolicy | bool) -> None:
    rules = _policy(policy)
    name = _check_name(host)
    try:
        ipaddress.ip_address(name)
        _check_address(name, rules, name)
        return
    except BlockedTarget:
        raise
    except ValueError:
        pass
    if rules.allowlist and name in rules.allowlist:
        return
    try:
        addresses = await _resolve(name)
    except socket.gaierror:
        return  # An unknown name fails later with a clearer connection error.
    for address in addresses:
        _check_address(address, rules, name)


async def check_url(url: str, policy: TargetPolicy | bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BlockedTarget("Enter an address that starts with http:// or https://.")
    await check_host(parsed.hostname, policy)


class _GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolves the name, checks every address, then connects to a checked address and not the name."""

    def __init__(self, policy: TargetPolicy) -> None:
        self._policy = policy
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Iterable[Any] | None = None) -> Any:
        name = _check_name(host)
        try:
            ipaddress.ip_address(name)
            addresses = [name]
        except ValueError:
            try:
                addresses = await _resolve(name, port)
            except socket.gaierror as error:
                raise httpcore.ConnectError(str(error)) from error
        try:
            for address in addresses:
                _check_address(address, self._policy, name)
        except BlockedTarget as error:
            raise httpcore.ConnectError(str(error)) from error
        return await self._inner.connect_tcp(addresses[0], port, timeout=timeout, local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Iterable[Any] | None = None) -> Any:
        raise httpcore.ConnectError("Unix sockets can't be used as a target.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(policy: TargetPolicy | bool, verify: Any = True) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(verify=verify, trust_env=False)
    # httpx has no public option for this. tests/test_security.py fails if an upgrade changes it.
    transport._pool._network_backend = _GuardedBackend(_policy(policy))
    return transport
