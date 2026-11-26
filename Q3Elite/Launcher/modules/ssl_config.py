"""Shared, strict TLS configuration for Q3Elite Launcher network traffic.

On Windows, truststore uses the native Windows certificate stores. If the
optional truststore package is unavailable, Python's default SSL context is
used; on Windows this also loads certificates from the Windows certificate
store. Certificate verification is never disabled.
"""
from __future__ import annotations

import ssl
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache


@lru_cache(maxsize=1)
def get_ssl_context() -> ssl.SSLContext:
    """Return the shared TLS client context with certificate checks enabled."""
    try:
        import truststore
        context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        backend = "truststore / Windows system certificate store"
    except ImportError:
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        backend = "Python default trust store (Windows ROOT/CA on Windows)"

    # Keep strict validation explicit. Never downgrade to CERT_NONE.
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def tls_backend() -> str:
    """Human-readable description for diagnostics."""
    try:
        import truststore  # noqa: F401
        return "truststore / Windows system certificate store"
    except ImportError:
        return "Python default trust store (Windows ROOT/CA on Windows)"


def build_opener(proxy: str | None = None) -> urllib.request.OpenerDirector:
    """Build an opener using the shared TLS context and optional explicit proxy.

    When proxy is None, urllib's normal system/environment proxy discovery is
    retained. When a proxy URL is supplied, it is used for both HTTP and HTTPS.
    """
    handlers = [urllib.request.HTTPSHandler(context=get_ssl_context())]
    if proxy:
        handlers.insert(
            0,
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        )
    return urllib.request.build_opener(*handlers)


def open_url(request, timeout: float = 30, proxy: str | None = None):
    """Open a URL with strict shared TLS settings and useful TLS diagnostics."""
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    host = urllib.parse.urlsplit(url).hostname or "unknown host"
    try:
        return build_opener(proxy=proxy).open(request, timeout=timeout)
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise urllib.error.URLError(
                f"TLS certificate verification failed for {host}: {reason}. "
                f"TLS backend: {tls_backend()}. Check the Windows date/time, "
                "system root certificates, and any antivirus/VPN HTTPS inspection."
            ) from error
        raise
    except ssl.SSLCertVerificationError as error:
        raise ssl.SSLCertVerificationError(
            f"TLS certificate verification failed for {host}: {error}. "
            f"TLS backend: {tls_backend()}."
        ) from error
