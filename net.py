"""One TLS setup for every HTTPS call Gusan makes (2026-11-16 speed audit).

Loading the trusted certificate list (certifi's cacert.pem) takes 0.7-1.1 s on a typical laptop, and requests /
urllib3 did it again for EVERY new connection: each Smart Action, each cloud dictation piece, each caption
translation, the live transcription's start - about a second each, before a byte was sent. Measured: a new
request 1.0 s, the same request on a context already loaded 0.05 s.

Here the list is loaded once, into one SSL context (shared: an SSLContext is safe to use from many threads):

    net.install()          # once, at start: requests' default adapter uses the shared context
    net.prewarm()          # load it on a worker now, so the first request does not wait for it
    net.ssl_context()      # the context itself (the live stream's own socket)

Only the default certificate check changes: a call that names its own certificate file (verify="path") or turns
the check off (verify=False) works as before. Never raises; if anything is missing, requests keeps its own way.
"""
import logging
import ssl
import threading

logger = logging.getLogger("transcribe")

_lock = threading.Lock()
_context = None
_socket_context = None
_installed = False


def _bundle():
    try:
        import certifi
        return certifi.where()
    except Exception:
        return None


def ssl_context():
    """The shared SSL context, the trusted certificates loaded (once)."""
    global _context
    if _context is not None:
        return _context
    with _lock:
        if _context is None:
            ctx = None
            try:
                from urllib3.util.ssl_ import create_urllib3_context
                ctx = create_urllib3_context()
                bundle = _bundle()
                if bundle:
                    ctx.load_verify_locations(cafile=bundle)
                else:
                    ctx.load_default_certs()
            except Exception:
                logger.debug("urllib3 context unavailable: the standard one", exc_info=True)
                bundle = _bundle()
                ctx = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
            _context = ctx
    return _context


def socket_context():
    """A standard-library SSL context with the trusted certificates loaded (once), for a socket of our own (the
    live stream's WebSocket): it checks the host name itself, which urllib3's context leaves to urllib3."""
    global _socket_context
    if _socket_context is not None:
        return _socket_context
    with _lock:
        if _socket_context is None:
            bundle = _bundle()
            try:
                _socket_context = ssl.create_default_context(cafile=bundle) if bundle else ssl.create_default_context()
            except Exception:
                _socket_context = ssl.create_default_context()
    return _socket_context


def prewarm():
    """On a worker thread: set requests up (install) and load the certificates, so neither the start nor the first
    request waits for them (a request made in the meantime simply takes the old, slower way once)."""
    def warm():
        install()
        ssl_context()
        socket_context()
    threading.Thread(target=warm, daemon=True, name="gusan-tls-warm").start()


def install():
    """Make requests use the shared context for the default certificate check. Once; never raises. Returns True
    when requests uses it.

    Only connections made with the default check (verify=True, no client certificate) get the shared context: they
    go to connection pools of their own (urllib3 keys its pools by the context), and urllib3 sets nothing on the
    context but the check those connections already ask for. A call with verify=False or its own certificate file
    keeps urllib3's own context, made for it alone."""
    global _installed
    if _installed:
        return True
    try:
        from requests import adapters
    except Exception:
        return False
    base = getattr(adapters, "HTTPAdapter", None)
    key_attrs = getattr(base, "build_connection_pool_key_attributes", None)
    cert_verify = getattr(base, "cert_verify", None)
    if base is None or not callable(key_attrs) or not callable(cert_verify):
        return False

    def _default_check(url, verify_arg, cert=None):
        return verify_arg is True and not cert and str(url or "").lower().startswith("https")

    def build_connection_pool_key_attributes(self, request, verify_arg, cert=None):
        host_params, pool_kwargs = key_attrs(self, request, verify_arg, cert)
        if _default_check(getattr(request, "url", ""), verify_arg, cert):
            pool_kwargs = dict(pool_kwargs, ssl_context=ssl_context())
        return host_params, pool_kwargs

    def verify(self, conn, url, verify_arg, cert):
        cert_verify(self, conn, url, verify_arg, cert)
        if _default_check(url, verify_arg, cert):           # (its pool was given the shared context above)
            # The shared context already holds the default certificates: urllib3 loads a file only when told
            # one (it did, on every connection - a second each).
            conn.ca_certs = None
            conn.ca_cert_dir = None

    try:
        base.build_connection_pool_key_attributes = build_connection_pool_key_attributes
        base.cert_verify = verify
    except Exception:
        logger.debug("requests not patched", exc_info=True)
        return False
    _installed = True
    return True


__all__ = ["install", "prewarm", "socket_context", "ssl_context"]
