"""DoH URL/stub policy — pure stdlib, deliberately free of dns/httpx imports.

Split out of ``doh.py`` because ``core.config.qc`` needs only
``normalize_doh_url``, and importing it from ``doh`` dragged
``dns -> httpx -> httpx._main -> click/pygments/rich`` onto the app startup
path (~1.8s cold). Keep this module dependency-free; transports and resolvers
belong in ``doh.py``.
"""
from __future__ import annotations

from urllib.parse import urlparse

DNS_STUB_HOST = "127.0.0.1"
DNS_STUB_PORT = 53
DOH_WEBENGINE_PROXY_HOST = DOH_CONNECT_PROXY_HOST = "127.0.0.1"
DEFAULT_DOH_URL = "https://cloudflare-dns.com/dns-query"


def normalize_doh_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("DoH URL must be a full https://... endpoint")
    return text


def is_doh_enabled(doh_url: object) -> bool:
    return bool(normalize_doh_url(doh_url))


#: Schemes httpx/aria2 accept for a proxy URL. The user types the scheme now, so
#: an unknown one is a typo, not something to silently "fix".
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")


def proxy_url(value: object) -> str:
    """Return a scheme-qualified proxy URL, or ``""`` for "no proxy".

    The settings field asks for a *full* proxy URL, so the scheme is the user's (socks5:// and
    http:// both valid, must reach httpx/aria2 intact). A bare ``host:port`` still gets
    ``http://`` because every other tool assumes it; anything else is rejected loudly instead of
    being prefixed. A bare value must actually look like host:port.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        # Scheme-less form: accept only ``host:port``.
        #
        # The port is required, not optional. That compatibility branch exists
        # for legacy ``127.0.0.1:7890`` entries; a bare word like ``socks5``
        # (the scheme name, typed while still deciding) would otherwise be
        # accepted and stored as ``http://socks5`` -- reaching aria2/httpx as a
        # "proxy" nothing can connect to, and poisoning the remembered list.
        host, _, port = text.partition(":")
        if not host or " " in host:
            raise ValueError(f"proxy is missing a host: {text!r}")
        if not port:
            raise ValueError(
                f"proxy is missing a port: {text!r} (expected host:port or scheme://host:port)"
            )
        if not port.isdigit():
            raise ValueError(f"proxy port is not a number: {port!r}")
        return f"http://{text}"
    scheme, _, rest = text.partition("://")
    scheme = scheme.strip().lower()
    if scheme not in PROXY_SCHEMES:
        raise ValueError(
            f"unsupported proxy scheme {scheme!r}; expected one of "
            f"{', '.join(PROXY_SCHEMES)}"
        )
    if not rest.strip("/"):
        raise ValueError(f"proxy URL has no host: {text!r}")
    return text


def dns_stub_server(doh_url: object) -> str:
    return DNS_STUB_HOST if is_doh_enabled(doh_url) else ""


def dns_stub_endpoint(doh_url: object) -> str:
    return f"{DNS_STUB_HOST}:{DNS_STUB_PORT}" if is_doh_enabled(doh_url) else ""
