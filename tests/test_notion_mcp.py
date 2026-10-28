"""Notion through its hosted MCP server (notion_mcp.py): discovery, one registration per
install, PKCE and the loopback listener, the token chain that rotates, the MCP client
(JSON and SSE answers), the private "Gusan meetings" page, one page per meeting, and
the wiring into notion_connect / notion_sync / notion_export.

No Qt and no real network. A fake "mcp.notion.com" runs on 127.0.0.1 in a thread and the
module's one transport function (notion_mcp._request) is swapped for a stdlib client that
talks to it over a real socket - so headers, bodies, JSON and event streams are the real
thing, and nothing depends on ``requests`` (tests/test_core replaces it with a stub when
the whole suite runs in one process). The default transport has a test of its own."""
import contextlib
import hashlib
import base64
import http.client
import http.server
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs main needs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import meeting_store as ms
import notion_connect as nc
import notion_export as ne
import notion_mcp as nm
import notion_sync
import storage
from notion_connect import ConnectCancelled, ConnectError
from notion_export import NotionAuthError, NotionError, NotionNotFoundError

ROOT = Path(__file__).resolve().parent.parent
SECRET_WORDS = []          # filled per test by the fake: every token, code and verifier it ever issued or saw
REAL_RETURN_URL = nm.RETURN_URL        # NetCase points nm.RETURN_URL at the fake site; this is the real address
# What gusan.aibuben.xyz/notion/callback asks of the state before it hands anything on (transcribe-site).
SITE_STATE = re.compile(r"^(\d{4,5})\.[A-Za-z0-9_-]{16,128}$")
SITE_PAGE = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="gusan-return" content="1"><meta name="robots" content="noindex">'
             '<title>Gusan</title></head><body><p>Taking you back to Gusan...</p></body></html>')
SAVED = {"notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}      # a client that knows both returns
FULL_TOOL_TEXT = ("Your workspace has used all of its free blocks. Upgrade your plan to create more content. "
                  "[Learn more](https://www.notion.com/help/free-plan-limits) · "
                  "[Upgrade plan](https://app.notion.com/notion-mcp?source=mcp_tool_upsell&tool=create_pages&product=plus)")


# ── a fake mcp.notion.com ────────────────────────────────────────────────────
class ToolFail(Exception):
    """A tool that answers isError with this text."""

    def __init__(self, text):
        super().__init__(text)
        self.text = text


def tool_result(data=None, text=None, is_error=False, structured=False):
    if data is not None and text is None:
        text = json.dumps(data)
    out = {"content": [{"type": "text", "text": text or ""}], "isError": is_error}
    if structured and data is not None:
        out["structuredContent"] = data
    return out


def hex_id(n):
    return "%032x" % n


def b64_s256(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class FakeNotion:
    """Discovery, /register, /authorize-less consent (``issue_code``), /token with rotation and
    replay detection, revocation, and /mcp (initialize, notifications, tools/call) answering
    JSON or an event stream. ``log`` records what happened in order."""

    def __init__(self, reply="json", ttl=28800, hold_open=False):
        self.reply, self.ttl, self.hold_open = reply, ttl, hold_open
        self.log = []
        self.registered = {}
        self.codes = {}
        self.valid = set()
        self.chain = []                  # every refresh token issued, oldest first
        self.revoked = False
        self.counter = 0
        self.mcp_calls = []              # [(tool, arguments)]
        self.mcp_headers = []            # the headers of every /mcp request
        self.token_requests = []
        self.token_fail = []             # queue of (status, body) answered instead of the real thing
        self.mcp_fail = []               # queue of (status, headers) answered before looking at the request
        self.rpc_error = []              # queue of JSON-RPC error messages for tools/call
        self.authorize_calls = []        # the query of every GET /authorize
        self.authorize_status = 0        # when set, /authorize answers this instead of looking at the client
        self.refuse_redirects = set()    # return addresses /authorize answers "Invalid redirect URI" for
        self.site = "ok"                 # what GET /notion/callback (our website's page) answers: see on_get
        self.site_delay = 0.0
        self.site_requests = []          # the path+query of every GET of the website's page
        self.site_seen_clients = []      # how many clients were registered when each of them came
        self.pages = {}                  # hex id -> {"title", "content", "parent", "icon"}
        self.trashed = set()
        self.max_content = None
        self.max_append = None
        self.gone_text = "object_not_found: Could not find page with ID {id}."
        self.trash_marker = False        # a trashed page answers fetch with an in_trash marker instead of an error
        self.tools = {"notion-create-pages": self._create_pages, "notion-update-page": self._update_page,
                      "notion-get-self": self._get_self, "notion-fetch": self._fetch}
        self.hold = threading.Event()
        self.lock = threading.Lock()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, body=b"", headers=None, ctype="application/json"):
                self.send_response(status)
                if body or status not in (202, 204, 401):
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status, obj, headers=None):
                self._send(status, json.dumps(obj).encode("utf-8"), headers)

            def do_GET(self):
                fake.on_get(self)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.on_post(self, self.rfile.read(n))

        self.http = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.http.server_address[1]
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def stop(self):
        self.hold.set()
        self.http.shutdown()
        self.http.server_close()

    # ── helpers for tests ──
    def new_id(self, prefix):
        with self.lock:
            self.counter += 1
            value = "%s-%d" % (prefix, self.counter)
        SECRET_WORDS.append(value)
        return value

    def issue_tokens(self, extra=None):
        access, refresh = self.new_id("at"), self.new_id("rt")
        with self.lock:
            self.valid.add(access)
            self.chain.append(refresh)
        out = {"access_token": access, "token_type": "bearer", "expires_in": self.ttl, "refresh_token": refresh}
        out.update(extra or {})
        return out

    def issue_code(self, client_id, challenge, redirect_uri):
        code = self.new_id("code")
        self.codes[code] = {"client_id": client_id, "challenge": challenge, "redirect": redirect_uri}
        return code

    def expire(self, access):
        self.valid.discard(access)

    def calls(self, tool):
        return [a for t, a in self.mcp_calls if t == tool]

    # ── routes ──
    def on_get(self, h):
        path = urllib.parse.urlsplit(h.path).path
        if path == "/.well-known/oauth-protected-resource/mcp":
            h._json(200, {"resource": self.base + "/mcp", "authorization_servers": [self.base],
                          "scopes_supported": ["default"], "bearer_methods_supported": ["header"]})
        elif path == "/.well-known/oauth-authorization-server":
            h._json(200, {"issuer": self.base, "authorization_endpoint": self.base + "/authorize",
                          "token_endpoint": self.base + "/token", "registration_endpoint": self.base + "/register",
                          "revocation_endpoint": self.base + "/token", "response_types_supported": ["code"],
                          "code_challenge_methods_supported": ["S256"],
                          "token_endpoint_auth_methods_supported": ["none"]})
        elif path == "/authorize":
            # What Notion's authorize address does without a browser: a client it knows is sent on to the
            # consent page (302), one it does not know gets a plain 400 (no redirect back to the app).
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(h.path).query))
            self.authorize_calls.append(q)
            if self.authorize_status:
                h._json(self.authorize_status, {"error": "temporarily_unavailable"})
            elif q.get("client_id") not in self.registered:
                h._json(400, {"error": "invalid_request", "error_description": "Invalid client"})
            elif not self.redirect_ok(q.get("client_id"), q.get("redirect_uri", "")):
                h._json(400, {"error": "invalid_request", "error_description": "Invalid redirect URI"})
            else:
                h._send(302, b"", {"Location": "https://api.notion.com/v1/oauth/authorize?state=signed"})
        elif path == "/notion/callback":
            # Our website's return page (nm.RETURN_URL points here in NetCase). Modes: ok = the real page with
            # its mark; 404; nomarker = a catch-all 200 that is somebody else's page; redirect = a 302 to a
            # page that WOULD be fine (a redirect must never be followed); slow = ok, after site_delay.
            self.site_requests.append(h.path)
            self.site_seen_clients.append(len(self.registered))
            if self.site == "slow":
                time.sleep(self.site_delay)
            if self.site in ("ok", "slow"):
                h._send(200, SITE_PAGE.encode("utf-8"), ctype="text/html; charset=utf-8")
            elif self.site == "nomarker":
                h._send(200, b"<!doctype html><html><body>Welcome. Nothing here.</body></html>", ctype="text/html")
            elif self.site == "redirect":
                h._send(302, b"", {"Location": "/somewhere-else"})
            elif self.site == "huge":
                h._send(200, b"<p>x</p>" * 20000 + SITE_PAGE.encode("utf-8"), ctype="text/html")
            else:
                h._send(404, b"Not found", ctype="text/plain")
        elif path == "/somewhere-else":
            self.site_requests.append(h.path)
            h._send(200, SITE_PAGE.encode("utf-8"), ctype="text/html")
        else:
            h._send(404)

    def redirect_ok(self, client_id, redirect_uri):
        """What Notion does with a return address (RFC 8252): it must be one the client was registered with,
        exactly - except that a 127.0.0.1 address may use any port. A client the tests made by hand
        (no redirect_uris in its record) takes anything."""
        if redirect_uri in self.refuse_redirects:
            return False
        uris = (self.registered.get(client_id) or {}).get("redirect_uris")
        if uris is None:
            return True
        want = urllib.parse.urlsplit(redirect_uri)
        for uri in uris:
            have = urllib.parse.urlsplit(uri)
            if uri == redirect_uri:
                return True
            # (this fake server's own address stands in for the website: it is never "a loopback port")
            if (have.scheme, have.hostname, have.path) == (want.scheme, want.hostname, want.path) \
                    and want.hostname == "127.0.0.1" and want.port != self.http.server_address[1]:
                return True
        return False

    def on_post(self, h, raw):
        path = urllib.parse.urlsplit(h.path).path
        if path == "/register":
            body = json.loads(raw.decode("utf-8"))
            cid = "cid%d" % (len(self.registered) + 1)
            self.registered[cid] = body
            self.log.append("register")
            h._json(201, {"client_id": cid, "client_id_issued_at": 1, "client_name": body.get("client_name"),
                          "redirect_uris": body.get("redirect_uris"), "grant_types": body.get("grant_types"),
                          "response_types": body.get("response_types"), "token_endpoint_auth_method": "none"})
        elif path == "/token":
            self.on_token(h, dict(urllib.parse.parse_qsl(raw.decode("utf-8"))))
        elif path == "/mcp":
            self.on_mcp(h, raw)
        else:
            h._send(404)

    def on_token(self, h, f):
        self.token_requests.append(f)
        if "grant_type" not in f:                                       # RFC 7009 revocation
            self.log.append("revoke")
            self.revoked = True
            h._send(200, b"{}")
            return
        self.log.append("token:" + f["grant_type"])
        if self.token_fail:
            status, body = self.token_fail.pop(0)
            h._json(status, body)
            return
        if f["client_id"] not in self.registered:
            h._json(401, {"error": "invalid_client"})
            return
        if f["grant_type"] == "authorization_code":
            c = self.codes.pop(f.get("code"), None)
            ok = (c and c["client_id"] == f["client_id"] and c["redirect"] == f.get("redirect_uri")
                  and b64_s256(f.get("code_verifier", "")) == c["challenge"])
            if not ok:
                h._json(400, {"error": "invalid_grant"})
                return
            SECRET_WORDS.append(f["code_verifier"])
            h._json(200, self.issue_tokens({"workspace_id": "ws-1", "workspace_name": "Aram's workspace",
                                            "bot_id": "bot-1", "user_id": "u-1"}))
        elif f["grant_type"] == "refresh_token":
            old = f.get("refresh_token")
            with self.lock:
                if self.revoked or old not in self.chain:
                    ok = False
                else:
                    index = self.chain.index(old)
                    if index < len(self.chain) - 2:                     # older than the previous one: replay
                        self.revoked = True
                        ok = False
                    else:
                        ok = True
            if not ok:
                h._json(400, {"error": "invalid_grant"})
                return
            h._json(200, self.issue_tokens())
        else:
            h._json(400, {"error": "unsupported_grant_type"})

    def on_mcp(self, h, raw):
        self.mcp_headers.append({k.lower(): v for k, v in h.headers.items()})
        if self.mcp_fail:
            status, headers = self.mcp_fail.pop(0)
            self.log.append("mcp:%d" % status)
            h._send(status, b"", headers)
            return
        auth = h.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.valid:
            self.log.append("mcp:401")
            h._send(401, b"", {"WWW-Authenticate": 'Bearer realm="OAuth"'})
            return
        msg = json.loads(raw.decode("utf-8"))
        method = msg.get("method")
        if "id" not in msg:                                             # a notification
            self.log.append("mcp:" + method)
            h._send(202)
            return
        if method == "initialize":
            self.log.append("mcp:initialize")
            self.answer(h, msg, {"protocolVersion": msg["params"]["protocolVersion"],
                                 "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}},
                        headers={"Mcp-Session-Id": "sess-1"})
        elif method == "tools/call":
            name, args = msg["params"]["name"], msg["params"]["arguments"]
            self.log.append("mcp:tools/call:" + name)
            self.mcp_calls.append((name, args))
            if self.rpc_error:
                item = self.rpc_error.pop(0)
                code, text = item if isinstance(item, tuple) else (-32602, item)
                self.answer(h, msg, error={"code": code, "message": text})
                return
            try:
                result = self.tools[name](args)
            except ToolFail as e:
                result = tool_result(text=e.text, is_error=True)
            self.answer(h, msg, result)
        else:
            self.answer(h, msg, error={"code": -32601, "message": "Method not found"})

    def answer(self, h, msg, result=None, error=None, headers=None):
        body = {"jsonrpc": "2.0", "id": msg["id"]}
        body.update({"error": error} if error is not None else {"result": result})
        if self.reply == "json":
            h._json(200, body, headers)
            return
        events = [{"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}}, body]
        text = "".join("event: message\ndata: %s\n\n" % json.dumps(e) for e in events)
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(text.encode("utf-8"))
        h.wfile.flush()
        if self.hold_open:                                              # a server that keeps the stream open
            self.hold.wait(3)

    # ── the Notion tools ──
    def _page(self, raw_id):
        pid = ne.extract_page_id(raw_id or "")
        pid = pid.replace("-", "") if pid else ""
        if pid not in self.pages or pid in self.trashed:
            raise ToolFail(self.gone_text.replace("{id}", pid))
        return pid

    def _fetch(self, args):
        pid = ne.extract_page_id(args["id"] or "")
        key = pid.replace("-", "") if pid else ""
        if key in self.trashed and self.trash_marker and key in self.pages:
            return tool_result(text='<page url="https://www.notion.so/%s" in_trash="true">%s</page>'
                                    % (key, self.pages[key]["title"]))
        self._page(args["id"])
        return tool_result(text='<page url="https://www.notion.so/%s">%s</page>' % (key, self.pages[key]["title"]))

    def _create_pages(self, args):
        if len(args["pages"]) != 1:
            raise ToolFail("validation_error: one page at a time in this fake")
        page = args["pages"][0]
        parent = args.get("parent")
        if args.get("creation_mode") == "draft":
            assert parent is None, "draft cannot be combined with a parent"
        elif parent:
            self._page(parent["page_id"])
        content = page.get("content", "")
        if self.max_content is not None and len(content) > self.max_content:
            raise ToolFail("validation_error: content is too large")
        pid = hex_id(len(self.pages) + 1 + 0xabc000)
        self.pages[pid] = {"title": page["properties"]["title"], "content": content,
                           "parent": parent["page_id"] if parent else None, "icon": page.get("icon")}
        return tool_result({"pages": [{"id": pid, "url": "https://www.notion.so/%s" % pid}]})

    def _update_page(self, args):
        pid = self._page(args["page_id"])
        assert args["command"] == "insert_content"
        if self.max_content is not None and len(args["content"]) > self.max_content:
            raise ToolFail("validation_error: content is too large")
        if self.max_append is not None and len(args["content"]) > self.max_append:
            raise ToolFail("validation_error: content is too large")
        self.pages[pid]["content"] += "\n" + args["content"]
        return tool_result({"ok": True})

    def _get_self(self, args):
        return tool_result({"workspace": {"id": "ws-1", "name": "Fake Workspace"}, "user": {"id": "u-1"}})


def stdlib_request(method, url, *, headers=None, json_body=None, form=None, timeout=15):
    """notion_mcp._request over a real socket with the standard library only."""
    p = urllib.parse.urlsplit(url)
    body, hdrs = None, dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        body = urllib.parse.urlencode(form).encode("utf-8")
    wait = timeout[0] if isinstance(timeout, tuple) else timeout
    conn = http.client.HTTPConnection(p.hostname, p.port, timeout=min(10, wait))
    try:
        conn.request(method, p.path + ("?" + p.query if p.query else ""), body=body, headers=hdrs)
        resp = conn.getresponse()
    except OSError as e:
        conn.close()
        raise nm.NetworkError("offline") from e
    h = dict(resp.getheaders())
    if (resp.getheader("Content-Type") or "").startswith("text/event-stream") and 200 <= resp.status < 300:
        def lines():
            while True:
                raw = resp.readline()
                if not raw:
                    return
                yield raw.rstrip(b"\r\n")
        return nm.Reply(resp.status, h, lines=lines(), close=conn.close)
    data = resp.read()
    conn.close()
    return nm.Reply(resp.status, h, data)


def reset_module():
    nm.forget_tokens()
    nm.forget_discovery()
    nm.forget_hubs()
    with nm._REG_LOCK:
        nm._CLIENT_MEMO.clear()


class Persist:
    """persist(dict) stand-in that keeps what it got, in order, and notes it in the fake's log."""

    def __init__(self, fake=None):
        self.fake = fake
        self.calls = []

    def __call__(self, updates):
        self.calls.append(dict(updates))
        if self.fake is not None:
            self.fake.log.append("persist")

    def keys(self):
        merged = {}
        for c in self.calls:
            merged.update(c)
        return merged


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def read(self, name):
        return self.store.get(name, "")

    def write(self, name, value):
        if value:
            self.store[name] = value
        else:
            self.store.pop(name, None)
        return True


class NetCase(unittest.TestCase):
    """A fake Notion, the transport pointed at it, module state reset, an empty keyring."""

    reply = "json"
    hold_open = False
    ttl = 28800
    site = "ok"            # what our website's return page does ("ok" = it is up; see FakeNotion.on_get)

    def setUp(self):
        del SECRET_WORDS[:]
        reset_module()
        self.fake = FakeNotion(self.reply, self.ttl, self.hold_open)
        self.addCleanup(self.fake.stop)
        self.addCleanup(reset_module)
        self.keyring = FakeKeyring()
        self.site_url = self.fake.base + "/notion/callback"      # "gusan.aibuben.xyz/notion/callback" for these tests
        self.fake.site = self.site
        for p in (patch.object(nm, "_request", stdlib_request),
                  patch.object(nm, "MCP_BASE", self.fake.base),
                  patch.object(nm, "RETURN_URL", self.site_url),
                  patch.object(nm, "_sleep", lambda s: self.sleeps.append(s)),
                  patch.object(storage, "read_secret", self.keyring.read),
                  patch.object(storage, "write_secret", self.keyring.write)):
            p.start()
            self.addCleanup(p.stop)
        self.sleeps = []
        self.persist = Persist(self.fake)

    # a connection as the config would hold it after connecting
    def connect_now(self, **kw):
        res = self.browse_connect(**kw)
        return res

    def browse_connect(self, cfg=None, wrong_state_first=False, deny=None, hits=None, preferred_port=0,
                       cancel=None, on_url=None, on_code=None, timeout=10, error_after=None):
        """nm.connect with a "browser" that follows the authorize URL the way Notion's consent
        page would: registers a code, then calls the listener back."""
        seen = hits if hits is not None else []
        threads = []

        def browse(url):
            try:
                self.visit(url, seen, deny=deny, wrong_state_first=wrong_state_first)
            except Exception as e:          # a broken fake browser must fail the test, not hide in a thread
                seen.append({"visit_error": repr(e)})

        def open_browser(url):
            t = threading.Thread(target=browse, args=(url,), daemon=True)
            threads.append(t)
            t.start()
            return True

        try:
            return nm.connect(cfg or {}, open_browser=open_browser, timeout=timeout, cancel=cancel,
                              on_url=on_url, on_code=on_code, persist=self.persist, preferred_port=preferred_port)
        finally:
            for t in threads:               # the tab gets its answer when the connect has ended: wait for it
                t.join(10)
            self.assertEqual([h for h in seen if "visit_error" in h], [])

    def visit(self, url, seen, deny=None, wrong_state_first=False, query=None):
        """What the browser does with Notion's authorize address after the user says Continue: Notion's
        consent page sends it back to the return address with the code and the state; when that is our
        website's page, the page's script forwards the very same query to 127.0.0.1 on the port the state
        names (the real script has tests of its own in transcribe-site); then the listener answers the tab.
        Appends what happened to ``seen``: the authorize query, the website's answer, the listener's."""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        seen.append(q)
        if query is not None:
            back = dict(query)
        elif deny is not None:
            back = {"error": deny, "state": q["state"]}
        else:
            code = self.fake.issue_code(q["client_id"], q["code_challenge"], q["redirect_uri"])
            back = {"code": code, "state": q["state"], "iss": self.fake.base}
        target = urllib.parse.urlsplit(q["redirect_uri"])
        if q["redirect_uri"] == nm.RETURN_URL:
            page = http.client.HTTPConnection(target.hostname, target.port, timeout=5)
            page.request("GET", target.path + "?" + urllib.parse.urlencode(back))
            answer = page.getresponse()
            seen.append({"site_status": answer.status, "site_body": answer.read().decode("utf-8")})
            page.close()
            m = SITE_STATE.match(q["state"])
            self.assertIsNotNone(m, q["state"])
            port = int(m.group(1))
        else:
            port = target.port
        loopback = ("127.0.0.1", port)
        if wrong_state_first:
            bad = http.client.HTTPConnection(*loopback, timeout=5)
            bad.request("GET", nm.CALLBACK_PATH + "?" + urllib.parse.urlencode(dict(back, state="nope" * 6)))
            seen.append({"wrong_state_status": bad.getresponse().status})
            bad.close()
        c = http.client.HTTPConnection(*loopback, timeout=15)
        c.request("GET", nm.CALLBACK_PATH + "?" + urllib.parse.urlencode(back))
        r = c.getresponse()
        seen.append({"landing_status": r.status, "landing": r.read().decode("utf-8")})
        c.close()

    @staticmethod
    def listener_port(authorize_query):
        """The port the listener was on, whichever return address the authorize address named."""
        q = authorize_query
        if q["redirect_uri"] == nm.RETURN_URL:
            return int(q["state"].split(".")[0])
        return urllib.parse.urlsplit(q["redirect_uri"]).port

    @property
    def via_site(self):
        return self.site == "ok"

    def creds(self, **over):
        """A live connection on the fake: tokens it accepts, a registered client."""
        self.fake.registered["cid1"] = {}
        tok = self.fake.issue_tokens()
        base = dict(access=tok["access_token"], refresh=tok["refresh_token"],
                    expires_at=time.time() + self.ttl, client_id="cid1")
        base.update(over)
        nm._adopt_chain({"access_token": base["access"], "refresh_token": base["refresh"],
                         "expires_at": base["expires_at"]})
        return nm.Credentials(**base)

    def make_page(self, title="Parent"):
        pid = hex_id(len(self.fake.pages) + 1 + 0xabc000)
        self.fake.pages[pid] = {"title": title, "content": "", "parent": None, "icon": None}
        return pid


NOTES = (
    "## Summary\n"
    "We agreed to **ship on Friday** after the `May` review. Costs stay under $5,000 [budget] {approved}.\n"
    "\n"
    "## Key decisions\n"
    "1. Ship Friday\n"
    "2. Keep the *old* pricing until June\n"
    "   - Sara owns the announcement\n"
    "   - David checks the _pricing_ page\n"
    "\n"
    "## Action items\n"
    "- [ ] Sara: write the announcement (Owner: Sara)\n"
    "- [x] David: book the room\n"
    "- Plain bullet with a link [the plan](https://example.com/plan?a=1&b=2) and <b>tags</b>\n"
    "\n"
    "> Quote: \"keep it simple\" - David\n"
    "\n"
    "---\n"
    "| Owner | Task |\n"
    "|---|---|\n"
    "| Sara | Announce |\n"
    "\n"
    "Done."
)


def meeting_folder(tmp, notes=NOTES, transcript="Anna: We ship Friday.\n- David: yes\n1. Agreed", title="Weekly sync"):
    folder = str(Path(tmp) / "20261008_101500")
    os.mkdir(folder)
    Path(folder, "notes.md").write_text(notes, encoding="utf-8")
    Path(folder, "transcript.txt").write_text(transcript, encoding="utf-8")
    ms.update_meta(folder, title=title, started="2026-10-08 10:15:00", duration_sec=1500,
                   attendees_list=["Anna", "David"])
    return folder


# ── the Markdown the pages are written in ────────────────────────────────────
class TestMarkdown(unittest.TestCase):
    def test_escape_text_covers_the_documented_set_and_nothing_else(self):
        self.assertEqual(nm.escape_text(r"a\b*c~d`e$f[g]h<i>j{k}l|m^n"),
                         r"a\\b\*c\~d\`e\$f\[g\]h\<i\>j\{k\}l\|m\^n")
        self.assertEqual(nm.escape_text("plain: text, (parens) #hash -dash 1."), "plain: text, (parens) #hash -dash 1.")
        self.assertEqual(nm.escape_text("a\x00b\x07c"), "abc")                 # control characters go

    def test_inline_formatting_is_kept_and_everything_else_is_escaped(self):
        self.assertEqual(nm.inline_markdown("a **bold** b"), "a **bold** b")
        self.assertEqual(nm.inline_markdown("a *it* and _it2_ b"), "a *it* and *it2* b")
        self.assertEqual(nm.inline_markdown("~~gone~~"), "~~gone~~")
        self.assertEqual(nm.inline_markdown("run `a <b> * c` now"), "run `a <b> * c` now")        # code is literal
        self.assertEqual(nm.inline_markdown("[the plan](https://example.com/p?a=1&b=2)"),
                         "[the plan](https://example.com/p?a=1&b=2)")
        self.assertEqual(nm.inline_markdown("5 * 3 = 15, a_b_c and 2^3"), r"5 \* 3 = 15, a_b_c and 2\^3")
        self.assertEqual(nm.inline_markdown('<b>x</b> {color="red"} | y'),
                         '\\<b\\>x\\</b\\> \\{color="red"\\} \\| y')

    def test_a_link_to_anything_but_http_is_plain_text(self):
        out = nm.inline_markdown("[x](javascript:alert(1)) [y](file:///etc/passwd)")
        self.assertNotIn("[x](javascript", out)             # the brackets are escaped: text, not a link
        self.assertNotIn("[y](file", out)
        self.assertIn(r"\[x\]", out)
        self.assertIn(r"\[y\]", out)

    def test_unfinished_markers_are_just_text(self):
        self.assertEqual(nm.inline_markdown("**oops and `tick"), r"\*\*oops and \`tick")
        self.assertEqual(nm.inline_markdown("a * b"), r"a \* b")

    def test_the_real_notes_sample(self):
        units = nm.markdown_units(NOTES)
        kinds = [k for k, _ in units]
        self.assertEqual(kinds, ["h", "p", "h", "li", "li", "h", "li", "li", "li", "quote", "hr", "p", "p", "p"])
        text = nm.render(units)
        self.assertIn("## Summary\n\nWe agreed to **ship on Friday** after the `May` review.", text)
        self.assertIn(r"Costs stay under \$5,000 \[budget\] \{approved\}.", text)
        self.assertIn("## Key decisions\n\n1. Ship Friday\n2. Keep the *old* pricing until June\n"
                      "\t- Sara owns the announcement\n\t- David checks the *pricing* page\n\n## Action items", text)
        self.assertIn("- [ ] Sara: write the announcement (Owner: Sara)\n- [x] David: book the room\n- Plain bullet", text)
        self.assertIn("[the plan](https://example.com/plan?a=1&b=2) and \\<b\\>tags\\</b\\>", text)
        self.assertIn('> Quote: "keep it simple" - David', text)
        self.assertIn("\n\n---\n\n", text)
        self.assertIn("Owner · Task", text)                                    # a table is plain lines, nothing dropped
        self.assertIn("Sara · Announce", text)
        self.assertNotIn("|---|", text)
        self.assertTrue(text.endswith("\n\nDone."))

    def test_nested_items_stay_with_their_parent_in_one_unit(self):
        units = nm.markdown_units("- a\n  - b\n    - c\n- d\n")
        self.assertEqual(units, [("li", "- a\n\t- b\n\t\t- c"), ("li", "- d")])
        # a child with no parent is a top-level item (never an orphan indent)
        self.assertEqual(nm.markdown_units("  - lone"), [("li", "- lone")])

    def test_headings_dividers_fences_and_odd_input(self):
        self.assertEqual(nm.markdown_units("# One\n### C# ##"), [("h", "# One"), ("h", "### C#")])
        self.assertEqual([t for k, t in nm.markdown_units("#### Four\n##### Five\n###### Six")],
                         ["#### Four", "#### Five", "#### Six"])           # Notion has four levels
        self.assertEqual(nm.markdown_units("####### seven"), [("p", "####### seven")])
        self.assertEqual(nm.markdown_units("***\n- - -\n___"), [("hr", "---")] * 3)
        fenced = nm.markdown_units("```python\nx = '<a>' * 2  # {no}\n```\nafter")
        self.assertEqual(fenced[0], ("code", "```python\nx = '<a>' * 2  # {no}\n```"))
        self.assertEqual(fenced[1], ("p", "after"))
        self.assertEqual(nm.markdown_units(""), [])
        self.assertEqual(nm.markdown_units(None), [])
        self.assertEqual(nm.markdown_units("\n\n  \n"), [])

    def test_a_paragraph_that_would_read_as_a_list_gets_an_invisible_guard(self):
        units = nm.transcript_units("- David: yes\n1. Agreed\n# hash\n--- \nplain\n-5 degrees\n#tag")
        paras = [t for k, t in units if k == "p"]
        self.assertEqual(paras[0], nm.ZERO_WIDTH + "- David: yes")
        self.assertEqual(paras[1], nm.ZERO_WIDTH + "1. Agreed")
        self.assertEqual(paras[2], nm.ZERO_WIDTH + "# hash")
        self.assertEqual(paras[3], nm.ZERO_WIDTH + "---")
        self.assertEqual(paras[4:], ["plain", "-5 degrees", "#tag"])

    def test_the_transcript_is_literal_text(self):
        units = nm.transcript_units("Anna: **not bold** [x](https://a.b) <br> {c}\r\n\r\nDavid: ok")
        self.assertEqual(units[0], ("h", "## Transcript"))
        self.assertEqual(units[1][1], r"Anna: \*\*not bold\*\* \[x\](https://a.b) \<br\> \{c\}")
        self.assertEqual(units[2][1], "David: ok")
        self.assertEqual(nm.transcript_units("  \n "), [])

    def test_a_very_long_line_is_cut_at_spaces(self):
        line = ("word " * 1000).strip()
        units = nm.transcript_units(line)
        paras = [t for k, t in units if k == "p"]
        self.assertGreater(len(paras), 2)
        self.assertTrue(all(len(p) <= nm.MAX_LINE_CHARS for p in paras))
        self.assertEqual(" ".join(paras), line)
        unbroken = nm.transcript_units("x" * 4000)
        self.assertEqual("".join(t for k, t in unbroken if k == "p"), "x" * 4000)

    def test_the_grey_line_and_the_page_order(self):
        units = nm.page_units("## Summary\n- a", "Anna: hi", "Thu 8 Oct · 10:15 · Anna, David · Gusan")
        self.assertEqual(units[0], ("p", 'Thu 8 Oct · 10:15 · Anna, David · Gusan {color="gray"}'))
        self.assertEqual([k for k, _ in units], ["p", "h", "li", "h", "p"])
        self.assertEqual(nm.page_units("", "", ""), [])
        self.assertEqual(nm.page_units("x", "", "")[0], ("p", "x"))

    def test_render_and_chunk(self):
        units = [("h", "# A"), ("li", "- 1"), ("li", "- 2"), ("p", "text"), ("li", "- 3")]
        self.assertEqual(nm.render(units), "# A\n\n- 1\n- 2\n\ntext\n\n- 3")
        self.assertEqual(nm.render([]), "")
        chunks = nm.chunk_units([("p", "x" * 100)] * 10, limit=250)
        self.assertEqual([len(c) for c in chunks], [2, 2, 2, 2, 2])
        self.assertEqual(nm.chunk_units([("p", "x" * 500)], limit=100), [[("p", "x" * 500)]])    # never cut
        self.assertEqual(nm.chunk_units([]), [])
        flat = [u for c in nm.chunk_units(units, limit=12) for u in c]
        self.assertEqual(flat, units)                                                          # order and content kept

    def test_titles(self):
        self.assertEqual(nm.clean_title("  Weekly\n sync\t\x07 "), "Weekly sync")
        self.assertEqual(nm.clean_title(""), nm.DEFAULT_TITLE)
        self.assertEqual(nm.clean_title(None), nm.DEFAULT_TITLE)
        self.assertEqual(len(nm.clean_title("t" * 500)), nm.MAX_TITLE_LEN)

    def test_hostile_input_can_not_stall_the_converter(self):
        """A run of spaces after a heading, or thousands of unfinished markers, used to take minutes."""
        cases = ["# a" + " " * 50000 + "b", "## " + " #" * 20000 + " x", "- " + "*a " * 6000, "> " + "_a " * 6000,
                 "**a " * 6000, "~~a " * 6000, "[a " * 6000, "- " + "`a " * 6000]
        started = time.monotonic()
        for text in cases:
            nm.render(nm.markdown_units(text))
        self.assertLess(time.monotonic() - started, 5.0)

    def test_closing_hashes_and_unfinished_markers_read_as_before(self):
        self.assertEqual(nm.markdown_units("## Title ##  \n### C# ##\n# ##\n#### x # y #"),
                         [("h", "## Title"), ("h", "### C#"), ("h", "# ##"),("h", "#### x # y")])
        self.assertEqual(nm.inline_markdown("*a b* then *c"), "*a b* then \\*c")
        self.assertEqual(nm.inline_markdown("*x **y** z* and _w_ and *lone"), "*x **y** z* and *w* and \\*lone")
        self.assertEqual(nm.inline_markdown("**a** then **b"), "**a** then \\*\\*b")
        self.assertEqual(nm.inline_markdown("~~a~~ then ~~b"), "~~a~~ then \\~\\~b")


# ── reading what a tool answered ─────────────────────────────────────────────
class TestFindPage(unittest.TestCase):
    URL = "https://www.notion.so/Weekly-0123456789abcdef0123456789abcdef"

    def r(self, data=None, text=""):
        return nm.ToolResult(text, data)

    def test_structured_pages(self):
        got = nm.find_page(self.r({"pages": [{"id": "01234567-89ab-cdef-0123-456789abcdef", "url": self.URL}]}))
        self.assertEqual(got, {"id": "01234567-89ab-cdef-0123-456789abcdef", "url": self.URL})

    def test_id_only_gets_an_address(self):
        got = nm.find_page(self.r({"pages": [{"id": "0123456789abcdef0123456789abcdef"}]}))
        self.assertEqual(got["url"], "https://www.notion.so/0123456789abcdef0123456789abcdef")

    def test_url_only_gets_its_id(self):
        got = nm.find_page(self.r({"results": [{"url": self.URL}]}))
        self.assertEqual(got["id"], "01234567-89ab-cdef-0123-456789abcdef")

    def test_plain_text_with_an_address(self):
        got = nm.find_page(self.r(text="Created 1 page: %s (parent: x)" % self.URL))
        self.assertEqual(got["url"], self.URL)
        self.assertEqual(got["id"], "01234567-89ab-cdef-0123-456789abcdef")

    def test_only_https_notion_addresses_are_ever_returned(self):
        for bad in ("http://www.notion.so/x-0123456789abcdef0123456789abcdef",
                    "javascript:alert(1)", "https://evil.example/notion.so/0123456789abcdef0123456789abcdef",
                    "https://notion.so.evil.example/p", "https://user:pw@www.notion.so/p", "file:///c:/x"):
            got = nm.find_page(self.r({"pages": [{"url": bad}]}))
            self.assertEqual(got["url"], "", bad)
        # a bad address with a good id: the address is rebuilt from the id, never taken from the answer
        got = nm.find_page(self.r({"pages": [{"id": "0123456789abcdef0123456789abcdef", "url": "http://evil.example/x"}]}))
        self.assertEqual(got["url"], "https://www.notion.so/0123456789abcdef0123456789abcdef")
        self.assertEqual(nm.safe_page_url("https://team.notion.site/Page-1"), "https://team.notion.site/Page-1")

    def test_nothing_found(self):
        self.assertEqual(nm.find_page(self.r(None, "no page here")), {"id": "", "url": ""})
        self.assertEqual(nm.find_page(self.r({"pages": []})), {"id": "", "url": ""})


# ── the default transport ────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status=200, headers=None, content=b"", lines=None):
        self.status_code, self.headers, self.content = status, headers or {}, content
        self._lines, self.closed = lines, False

    def iter_lines(self, chunk_size=512):
        self.chunk_size = chunk_size
        return iter(self._lines or [])

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), max(1, chunk_size)):
            yield self.content[i:i + chunk_size]

    def close(self):
        self.closed = True


class Gone(Exception):
    pass


class TestDefaultTransport(unittest.TestCase):
    def fake_requests(self, resp=None, raises=None):
        calls = []

        def request(method, url, **kw):
            calls.append((method, url, kw))
            if raises:
                raise raises
            return resp

        return SimpleNamespace(request=request, RequestException=Gone), calls

    def test_a_json_answer(self):
        resp = _Resp(200, {"Content-Type": "application/json", "Mcp-Session-Id": "s1"}, b'{"a": 1}')
        fake, calls = self.fake_requests(resp)
        with patch.object(nm, "requests", fake):
            r = nm._request("POST", "https://x.example/mcp", headers={"A": "b"}, json_body={"k": 1}, timeout=(1, 2))
        method, url, kw = calls[0]
        self.assertEqual((method, url), ("POST", "https://x.example/mcp"))
        self.assertEqual((kw["headers"], kw["json"], kw["timeout"]), ({"A": "b"}, {"k": 1}, (1, 2)))
        self.assertIs(kw["allow_redirects"], False)             # a token never follows a redirect
        self.assertEqual((r.status, r.json(), r.header("mcp-session-id"), r.is_stream), (200, {"a": 1}, "s1", False))
        self.assertTrue(resp.closed)

    def test_a_form_body(self):
        fake, calls = self.fake_requests(_Resp(200, {}, b"{}"))
        with patch.object(nm, "requests", fake):
            nm._request("POST", "https://x.example/token", form={"grant_type": "refresh_token"})
        self.assertEqual(calls[0][2]["data"], {"grant_type": "refresh_token"})
        self.assertNotIn("json", calls[0][2])

    def test_an_event_stream_is_read_lazily_a_byte_at_a_time(self):
        resp = _Resp(200, {"Content-Type": "text/event-stream; charset=utf-8"},
                     lines=[b"data: {\"x\": \"\xc3\xa9\"}", b""])
        fake, _ = self.fake_requests(resp)
        with patch.object(nm, "requests", fake):
            r = nm._request("POST", "https://x.example/mcp")
        self.assertTrue(r.is_stream)
        self.assertEqual(list(r.iter_lines()), ['data: {"x": "é"}', ""])
        self.assertEqual(resp.chunk_size, 1)                    # so a stream that stays open still hands lines over
        r.close()
        self.assertTrue(resp.closed)

    def test_an_error_status_with_an_event_stream_content_type_is_a_plain_body(self):
        resp = _Resp(500, {"Content-Type": "text/event-stream"}, b"oops")
        fake, _ = self.fake_requests(resp)
        with patch.object(nm, "requests", fake):
            r = nm._request("GET", "https://x.example/")
        self.assertEqual((r.status, r.is_stream, r.body), (500, False, b"oops"))

    def test_no_connection_is_a_network_error_without_the_address_in_the_log(self):
        fake, _ = self.fake_requests(raises=Gone("https://secret.example/?code=SECRET"))
        with patch.object(nm, "requests", fake), self.assertLogs("transcribe.notion", "INFO") as logs:
            with self.assertRaises(nm.NetworkError):
                nm._request("GET", "https://secret.example/?code=SECRET")
        self.assertNotIn("SECRET", "\n".join(logs.output))

    def test_a_dropped_stream_is_a_network_error(self):
        def broken():
            yield b"data: 1"
            raise Gone("reset")
        r = nm.Reply(200, {}, lines=broken())
        with self.assertRaises(nm.NetworkError):
            list(r.iter_lines())

    def test_a_reply_without_a_stream_reads_its_body(self):
        self.assertEqual(list(nm.Reply(200, {}, b"a\r\nb\n").iter_lines()), ["a", "b"])
        self.assertIsNone(nm.Reply(200, {}, b"\xff\xfe").json())
        self.assertIsNone(nm.Reply(200, {}, b"not json").json())

    def test_an_answer_larger_than_the_cap_is_dropped_not_held_in_memory(self):
        resp = _Resp(200, {"Content-Type": "application/json"}, b"x" * 100)
        fake, _ = self.fake_requests(resp)
        with patch.object(nm, "requests", fake), patch.object(nm, "MAX_BODY", 50):
            with self.assertRaises(nm.NetworkError):
                nm._request("GET", "https://mcp.notion.com/.well-known/oauth-protected-resource")
        self.assertTrue(resp.closed)
        with patch.object(nm, "requests", fake), patch.object(nm, "MAX_BODY", 100):
            self.assertEqual(len(nm._request("GET", "https://mcp.notion.com/x").body), 100)       # exactly the cap is fine
        self.assertEqual(nm.MAX_BODY, 8 * 1024 * 1024)


# ── discovery ────────────────────────────────────────────────────────────────
class TestDiscovery(NetCase):
    def test_reads_both_documents(self):
        ep = nm.discover()
        self.assertEqual(ep.mcp, self.fake.base + "/mcp")
        self.assertEqual((ep.issuer, ep.authorize, ep.token, ep.register, ep.revoke),
                         (self.fake.base, self.fake.base + "/authorize", self.fake.base + "/token",
                          self.fake.base + "/register", self.fake.base + "/token"))

    def test_it_is_cached(self):
        seen = []
        real = stdlib_request

        def counting(method, url, **kw):
            seen.append(url)
            return real(method, url, **kw)

        with patch.object(nm, "_request", counting):
            nm.discover()
            first = len(seen)
            nm.discover()
            self.assertEqual(len(seen), first)
            nm.discover(force=True)
            self.assertGreater(len(seen), first)

    def test_the_cache_expires(self):
        calls = []
        real = stdlib_request

        def counting(method, url, **kw):
            calls.append(url)
            return real(method, url, **kw)

        with patch.object(nm, "_request", counting):
            nm.discover()
            n = len(calls)
            with patch.object(nm, "_now", lambda: time.time() + nm.DISCOVERY_TTL + 5):
                nm.discover()
        self.assertGreater(len(calls), n)

    def test_the_root_form_of_the_resource_document_is_tried_second(self):
        def only_root(method, url, **kw):
            if url.endswith("/.well-known/oauth-protected-resource/mcp"):
                return nm.Reply(404)
            return stdlib_request(method, url, **kw)

        with patch.object(nm, "_request", only_root):
            ep = nm.discover()
        self.assertEqual(ep.token, self.fake.base + "/token")

    def test_when_nothing_is_published_the_known_paths_are_used(self):
        with patch.object(nm, "_request", lambda *a, **k: nm.Reply(404)):
            ep = nm.discover()
        self.assertEqual((ep.authorize, ep.token, ep.register, ep.mcp),
                         (self.fake.base + "/authorize", self.fake.base + "/token",
                          self.fake.base + "/register", self.fake.base + "/mcp"))

    def test_an_endpoint_that_is_not_https_is_never_followed(self):
        meta = {"issuer": "https://auth.notion.com", "authorization_endpoint": "http://evil.example/authorize",
                "token_endpoint": "ftp://evil.example/token", "registration_endpoint": "https://user:pw@evil.example/r",
                "code_challenge_methods_supported": ["S256"]}
        res = {"resource": "https://auth.notion.com/mcp", "authorization_servers": ["https://auth.notion.com"]}

        def fake_get(method, url, **kw):
            body = res if "protected-resource" in url else meta
            return nm.Reply(200, {}, json.dumps(body).encode())

        with patch.object(nm, "_request", fake_get):
            ep = nm.discover("https://mcp.notion.com")
        self.assertEqual(ep.issuer, "https://auth.notion.com")
        self.assertEqual(ep.authorize, "https://auth.notion.com/authorize")    # the bad ones fall back to the issuer's own
        self.assertEqual(ep.token, "https://auth.notion.com/token")
        self.assertEqual(ep.register, "https://auth.notion.com/register")

    def test_the_access_token_only_goes_to_the_host_discovery_started_from(self):
        res = {"resource": "https://elsewhere.notion.com/mcp", "authorization_servers": ["https://auth.notion.com"]}

        def fake_get(method, url, **kw):
            body = res if "protected-resource" in url else {"issuer": "https://auth.notion.com"}
            return nm.Reply(200, {}, json.dumps(body).encode())

        with patch.object(nm, "_request", fake_get):
            ep = nm.discover("https://mcp.notion.com")
        self.assertEqual(ep.mcp, "https://mcp.notion.com/mcp")
        self.assertEqual(ep.token, "https://auth.notion.com/token")                # the sign-in server may be another notion.com name

    def test_a_mismatched_issuer_is_not_trusted(self):
        meta = {"issuer": "https://other.notion.com", "token_endpoint": "https://other.notion.com/token"}

        def fake_get(method, url, **kw):
            body = {"authorization_servers": ["https://auth.notion.com"]} if "protected-resource" in url else meta
            return nm.Reply(200, {}, json.dumps(body).encode())

        with patch.object(nm, "_request", fake_get):
            ep = nm.discover("https://mcp.notion.com")
        self.assertEqual(ep.token, "https://auth.notion.com/token")

    def test_a_discovery_document_can_not_send_a_code_or_a_token_to_another_company(self):
        """The code and the verifier go to the token endpoint, the refresh token too: whatever the
        documents say, those addresses stay on notion.com (https, the standard port, no log-in in the url)."""
        evil = ["https://evil.example", "https://notion.com.evil.example", "https://evilnotion.com",
                "https://mcp.notion.com:8443", "http://mcp.notion.com", "http://127.0.0.1:9", "https://localhost",
                "https://user:pw@auth.notion.com", "https://auth.notion.com/x#frag", "ftp://auth.notion.com",
                "javascript:alert(1)", "//auth.notion.com/x", "https://[::1]/x", ""]
        for bad in evil:
            res = {"resource": bad + "/mcp", "authorization_servers": [bad]}
            meta = {"issuer": bad, "authorization_endpoint": bad + "/authorize", "token_endpoint": bad + "/token",
                    "registration_endpoint": bad + "/register", "revocation_endpoint": bad + "/revoke"}

            def fake_get(method, url, _r=res, _m=meta, **kw):
                return nm.Reply(200, {}, json.dumps(_r if "protected-resource" in url else _m).encode())

            nm.forget_discovery()
            with patch.object(nm, "_request", fake_get):
                ep = nm.discover("https://mcp.notion.com")
            for what in (ep.authorize, ep.token, ep.register, ep.revoke, ep.mcp, ep.issuer):
                self.assertTrue(what.startswith("https://mcp.notion.com/") or what == "https://mcp.notion.com", (bad, what))
        good = nm._safe_url("https://auth.notion.com/token", "https://mcp.notion.com")
        self.assertEqual(good, "https://auth.notion.com/token")
        self.assertEqual(nm._safe_url("https://mcp.notion.com:443/token", "https://mcp.notion.com"),
                         "https://mcp.notion.com:443/token")
        # a server that is not Notion's own may only name itself, and loopback http never leaks into Notion's case
        self.assertEqual(nm._safe_url("https://auth.example/t", "https://mcp.example.test"), "")
        self.assertEqual(nm._safe_url("https://mcp.example.test/t", "https://mcp.example.test"),
                         "https://mcp.example.test/t")
        self.assertEqual(nm._safe_url("http://127.0.0.1:5/t", "https://mcp.notion.com"), "")
        self.assertEqual(nm._safe_url("http://127.0.0.1:5/t", "http://127.0.0.1:7"), "http://127.0.0.1:5/t")

    def test_a_server_without_pkce_s256_is_refused(self):
        def fake_get(method, url, **kw):
            body = {} if "protected-resource" in url else {"issuer": "https://mcp.example.test",
                                                           "code_challenge_methods_supported": ["plain"]}
            return nm.Reply(200, {}, json.dumps(body).encode())

        with patch.object(nm, "_request", fake_get):
            with self.assertRaises(ConnectError) as cm:
                nm.discover("https://mcp.example.test")
        self.assertEqual(cm.exception.code, "server")

    def test_offline(self):
        def down(*a, **k):
            raise nm.NetworkError("offline")

        with patch.object(nm, "_request", down):
            with self.assertRaises(ConnectError) as cm:
                nm.discover()
        self.assertEqual(cm.exception.code, "offline")


# ── registration ─────────────────────────────────────────────────────────────
class TestRegistration(NetCase):
    def test_the_registration_body(self):
        ep = nm.discover()
        reg = nm.register_client(ep)
        body = self.fake.registered[reg["client_id"]]
        self.assertEqual(body["client_name"], "Gusan")
        self.assertEqual(body["client_uri"], "https://gusan.aibuben.xyz")
        # our site's page first, the loopback address second: either can be used at authorize time
        self.assertEqual(body["redirect_uris"], [self.site_url, "http://127.0.0.1:53682/notion/callback"])
        self.assertEqual(body["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(body["response_types"], ["code"])
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertEqual(reg["client_secret"], "")
        self.assertNotIn("dev check", json.dumps(body))

    def test_registered_once_and_saved_at_once(self):
        ep = nm.discover()
        cid = nm.client_id_for({}, ep, self.persist)
        self.assertEqual(self.persist.calls, [{"notion_mcp_client_id": cid, "notion_mcp_redirects": "2"}])
        self.assertEqual(self.fake.log.count("register"), 1)

    def test_the_hosted_client_document_door_is_closed_until_someone_hosts_one(self):
        self.assertEqual(nm.CLIENT_ID_URL, "")
        ep = nm.discover()
        with patch.object(nm, "CLIENT_ID_URL", "https://gusan.aibuben.xyz/notion-client.json"):
            self.assertEqual(nm.client_id_for({"notion_mcp_client_id": "old"}, ep, self.persist),
                             "https://gusan.aibuben.xyz/notion-client.json")
        self.assertEqual((self.fake.log, self.persist.calls), ([], []))           # nothing registered, nothing saved
        with patch.object(nm, "CLIENT_ID_URL", "http://not-https.example/x.json"):
            self.assertEqual(nm.client_id_for({"notion_mcp_client_id": "old"}, ep), "old")

    def test_a_saved_id_is_reused_with_no_request(self):
        ep = nm.discover()
        self.assertEqual(nm.client_id_for({"notion_mcp_client_id": "saved-id"}, ep, self.persist), "saved-id")
        self.assertEqual((self.fake.log, self.persist.calls), ([], []))

    def test_a_client_that_knows_only_the_loopback_address_is_registered_again_for_the_site(self):
        ep = nm.discover()
        old = {"notion_mcp_client_id": "old-id"}                       # registered before the site page: no redirects key
        # for a loopback return the old client is just as good: nothing is registered, nothing is orphaned
        self.assertEqual(nm.client_for(old, ep, self.persist, site=False), ("old-id", ""))
        self.assertEqual((self.fake.log, self.persist.calls), ([], []))
        # for the site's return it is not (Notion cannot add an address to a client): a new one, saved with its set
        cid, known = nm.client_for(old, ep, self.persist, site=True)
        self.assertEqual((cid, known), ("cid1", "2"))
        self.assertEqual(self.persist.calls, [{"notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}])
        self.assertEqual(self.fake.registered["cid1"]["redirect_uris"], [self.site_url, nm.REGISTERED_REDIRECT])
        # the next connect finds that set in the config and reuses the client for both returns
        now = {"notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}
        self.assertEqual(nm.client_for(now, ep, self.persist, site=True), ("cid1", "2"))
        self.assertEqual(nm.client_for(now, ep, self.persist, site=False), ("cid1", "2"))
        self.assertEqual(self.fake.log.count("register"), 1)

    def test_a_stale_config_does_not_register_twice_in_one_run(self):
        ep = nm.discover()
        old = {"notion_mcp_client_id": "old-id"}
        first = nm.client_for(old, ep, self.persist, site=True)
        second = nm.client_for(old, ep, self.persist, site=True)       # the save has not come back yet
        self.assertEqual(first, second)
        self.assertEqual(self.fake.log.count("register"), 1)

    def test_a_saved_id_that_is_not_valid_is_not_trusted_whatever_the_version(self):
        ep = nm.discover()
        cid, known = nm.client_for({"notion_mcp_client_id": "has space", "notion_mcp_redirects": "2"}, ep, self.persist)
        self.assertEqual((cid, known), ("cid1", "2"))

    def test_the_redirect_set_is_kept_across_a_disconnect_like_the_id(self):
        self.assertNotIn("notion_mcp_redirects", ne.BLANK_SETTINGS)

    def test_a_second_call_before_the_config_has_the_id_does_not_register_again(self):
        ep = nm.discover()
        first = nm.client_id_for({}, ep, self.persist)
        second = nm.client_id_for({}, ep, self.persist)               # the save has not come back yet
        self.assertEqual(first, second)
        self.assertEqual(self.fake.log.count("register"), 1)

    def test_threads_register_once(self):
        ep = nm.discover()
        out = []
        threads = [threading.Thread(target=lambda: out.append(nm.client_id_for({}, ep))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(out)), 1)
        self.assertEqual(self.fake.log.count("register"), 1)

    def test_forgetting_the_client_clears_the_saved_id_and_registers_again_next_time(self):
        ep = nm.discover()
        first = nm.client_id_for({}, ep)
        nm.forget_client(self.persist)
        self.assertEqual(self.persist.calls[-1], {"notion_mcp_client_id": "", "notion_mcp_redirects": ""})
        self.assertNotEqual(nm.client_id_for({}, ep), first)

    def test_a_client_secret_goes_to_the_keyring_not_the_config(self):
        ep = nm.discover()
        reg = {"client_id": "cid-s", "client_secret": "S3CRET-VALUE"}
        with patch.object(nm, "register_client", return_value=reg):
            nm.client_id_for({}, ep, self.persist)
        self.assertEqual(self.keyring.store, {storage.NOTION_MCP_SECRET: "S3CRET-VALUE"})
        self.assertNotIn("S3CRET-VALUE", json.dumps(self.persist.calls))
        self.assertEqual(nm.client_secret(), "S3CRET-VALUE")
        nm.forget_client()
        self.assertEqual(self.keyring.store, {})

    def test_a_secret_is_sent_with_token_calls_only_when_there_is_one(self):
        self.fake.registered["cid1"] = {}
        ep = nm.discover()
        self.fake.token_fail.append((503, {"error": "temporarily_unavailable"}))
        with self.assertRaises(ConnectError):
            nm._token_post(ep, {"grant_type": "refresh_token", "client_id": "cid1"})
        self.assertNotIn("client_secret", self.fake.token_requests[-1])
        self.keyring.store[storage.NOTION_MCP_SECRET] = "S3CRET-VALUE"
        self.fake.token_fail.append((503, {"error": "temporarily_unavailable"}))
        with self.assertRaises(ConnectError):
            nm._token_post(ep, {"grant_type": "refresh_token", "client_id": "cid1"})
        self.assertEqual(self.fake.token_requests[-1]["client_secret"], "S3CRET-VALUE")

    def test_bad_answers(self):
        ep = nm.discover()
        for status, body, code in ((500, b"{}", "server"), (429, b"{}", "server"), (400, b'{"error":"invalid_redirect_uri"}', "other"),
                                   (201, b"{}", "server"), (201, b'{"client_id": "has space"}', "server"),
                                   (201, b"[]", "other")):
            with patch.object(nm, "_request", lambda *a, _s=status, _b=body, **k: nm.Reply(_s, {}, _b)):
                with self.assertRaises(ConnectError) as cm:
                    nm.register_client(ep)
            self.assertEqual(cm.exception.code, code, (status, body))

    def test_offline(self):
        ep = nm.discover()

        def down(*a, **k):
            raise nm.NetworkError("offline")

        with patch.object(nm, "_request", down):
            with self.assertRaises(ConnectError) as cm:
                nm.register_client(ep)
        self.assertEqual(cm.exception.code, "offline")


# ── PKCE and the authorize address ───────────────────────────────────────────
class TestPkce(unittest.TestCase):
    def test_the_rfc_7636_vector(self):
        self.assertEqual(nm.challenge_for("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_verifier_and_challenge(self):
        v, c = nm.make_pkce()
        self.assertRegex(v, r"^[A-Za-z0-9_-]{43,128}$")
        self.assertEqual(c, b64_s256(v))
        self.assertNotIn("=", c)
        self.assertNotEqual(nm.make_pkce()[0], v)

    def test_the_authorize_url(self):
        ep = nm.Endpoints("https://mcp.notion.com")
        url = nm.authorize_url(ep, "cid1", "http://127.0.0.1:53682/notion/callback", "st-1", "chal-1")
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.scheme + "://" + parts.netloc + parts.path, "https://mcp.notion.com/authorize")
        q = dict(urllib.parse.parse_qsl(parts.query))
        self.assertEqual(q, {"response_type": "code", "client_id": "cid1",
                             "redirect_uri": "http://127.0.0.1:53682/notion/callback", "state": "st-1",
                             "code_challenge": "chal-1", "code_challenge_method": "S256", "prompt": "consent"})
        self.assertNotIn("scope", q)
        self.assertNotIn("resource", q)

    def test_the_known_endpoints_of_notion(self):
        ep = nm.Endpoints("https://mcp.notion.com")
        self.assertEqual((ep.authorize, ep.token, ep.register, ep.mcp),
                         ("https://mcp.notion.com/authorize", "https://mcp.notion.com/token",
                          "https://mcp.notion.com/register", "https://mcp.notion.com/mcp"))
        self.assertEqual(nm.REGISTERED_REDIRECT, "http://127.0.0.1:53682/notion/callback")


# ── connecting: the browser, the listener, the code, the first page ──────────
class TestConnect(NetCase):
    def test_the_whole_connect(self):
        events, hits = [], []
        res = self.browse_connect(hits=hits, on_url=lambda u: events.append("url"), on_code=lambda: events.append("code"))
        self.assertEqual(events, ["url", "code"])
        self.assertEqual((res["mode"], res["workspace"], res["client_id"], res["pages"]), ("mcp", "Aram's workspace", "cid1", None))
        self.assertEqual(res["dest"]["kind"], "hub")
        self.assertEqual(res["dest"]["title"], "Gusan meetings")
        self.assertEqual(res["dest"]["url"], "https://www.notion.so/" + res["dest"]["id"].replace("-", ""))
        self.assertTrue(res["token"].startswith("at-") and res["refresh"].startswith("rt-"))
        self.assertGreater(res["expires_at"], time.time() + 28000)
        # the consent page was asked with PKCE S256 and consent, for this client
        q = hits[0]
        self.assertEqual((q["response_type"], q["code_challenge_method"], q["prompt"]), ("code", "S256", "consent"))
        redirect = urllib.parse.urlsplit(q["redirect_uri"])
        if self.via_site:
            # ...coming back to our site's page (it hands the answer to this computer)...
            self.assertEqual(q["redirect_uri"], nm.RETURN_URL)
            self.assertRegex(q["state"], SITE_STATE)
            self.assertEqual([h["site_status"] for h in hits if "site_status" in h], [200])
        else:
            # ...or straight back to the listener on a loopback address, as before the site page existed
            self.assertEqual((redirect.scheme, redirect.hostname, redirect.path), ("http", "127.0.0.1", "/notion/callback"))
            self.assertNotIn(".", q["state"])
            self.assertEqual([h for h in hits if "site_status" in h], [])
        # the browser tab said so, once the page was made
        landing = [h for h in hits if "landing_status" in h][0]
        self.assertEqual(landing["landing_status"], 200)
        self.assertIn("close this tab", landing["landing"])
        self.assertIn("Connected.", landing["landing"])
        # the one page that was made: private, no parent, no sharing, synchronous
        creates = self.fake.calls("notion-create-pages")
        self.assertEqual(len(creates), 1)
        args = creates[0]
        self.assertEqual(args["creation_mode"], "draft")
        self.assertNotIn("parent", args)
        self.assertIs(args["allow_async"], False)
        self.assertEqual(args["pages"][0]["properties"], {"title": "Gusan meetings"})
        self.assertEqual(args["pages"][0]["icon"], nm.HUB_ICON)
        self.assertIn("Meeting notes sent from Gusan land here", args["pages"][0]["content"])
        # saved: the client id (and the return addresses it knows) at once, and everything of the connection in one dict
        self.assertEqual(self.persist.calls, [{"notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}])
        self.assertEqual(res["redirects"], "2")
        s = res["settings"]
        self.assertEqual(s["notion_mcp_redirects"], "2")
        self.assertEqual((s["notion_auth_mode"], s["notion_api_key"], s["notion_refresh_token"]), ("mcp", res["token"], res["refresh"]))
        self.assertEqual((s["notion_parent_page"], s["notion_hub_page"]), (res["dest"]["id"], res["dest"]["id"]))
        self.assertEqual((s["notion_workspace_name"], s["notion_parent_title"], s["notion_reconnect"], s["notion_mcp_client_id"]),
                         ("Aram's workspace", "Gusan meetings", False, "cid1"))
        self.assertEqual(s["notion_token_expires"], res["expires_at"])
        # the pair is the process's newest, the exchange used the same redirect as the authorize
        self.assertEqual(nm.latest_tokens()["access_token"], res["token"])
        self.assertEqual(self.fake.token_requests[0]["redirect_uri"], q["redirect_uri"])
        self.assertNotIn("client_secret", self.fake.token_requests[0])
        self.assertTrue(ne.is_connected(s))
        self.assertEqual(ne.connection_mode(s), "mcp")

    def test_the_registered_port_is_used_when_it_is_free(self):
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        hits = []
        self.browse_connect(hits=hits, preferred_port=port)
        self.assertEqual(self.listener_port(hits[0]), port)

    def test_another_port_is_used_when_the_registered_one_is_busy(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        hits = []
        res = self.browse_connect(hits=hits, preferred_port=busy.getsockname()[1])
        port = self.listener_port(hits[0])
        self.assertNotEqual(port, busy.getsockname()[1])
        self.assertTrue(res["token"])                                 # the exchange used the same address as the authorize

    def test_the_listener_is_on_loopback_only(self):
        servers = []
        real = nm._bind_listener

        def spy(port, state=""):
            s = real(port, state)
            servers.append(s)
            return s

        with patch.object(nm, "_bind_listener", spy):
            self.browse_connect()
        self.assertEqual(servers[0].server_address[0], "127.0.0.1")

    def test_a_wrong_state_is_refused_and_the_real_answer_is_still_waited_for(self):
        hits = []
        res = self.browse_connect(hits=hits, wrong_state_first=True)
        self.assertEqual([h["wrong_state_status"] for h in hits if "wrong_state_status" in h], [400])
        self.assertTrue(res["token"])

    def test_the_user_says_no(self):
        with self.assertRaises(ConnectError) as cm:
            self.browse_connect(deny="access_denied")
        self.assertEqual(cm.exception.code, "denied")
        self.assertEqual(str(cm.exception), nc.MESSAGES["denied"])
        self.assertEqual(self.fake.calls("notion-create-pages"), [])
        self.assertNotIn("token:authorization_code", self.fake.log)

    def test_another_error_from_notion(self):
        with self.assertRaises(ConnectError) as cm:
            self.browse_connect(deny="server_error")
        self.assertEqual(cm.exception.code, "other")

    def test_notion_not_knowing_our_client_drops_the_saved_id(self):
        with self.assertRaises(ConnectError) as cm:
            self.browse_connect(deny="invalid_client", cfg={"notion_mcp_client_id": "old-id", "notion_mcp_redirects": "2"})
        self.assertEqual(cm.exception.code, "expired")
        self.assertEqual(self.persist.calls[-1], {"notion_mcp_client_id": "", "notion_mcp_redirects": ""})

    def test_cancel_while_waiting(self):
        cancel = threading.Event()
        started = time.monotonic()
        with self.assertRaises(ConnectCancelled):
            nm.connect({}, open_browser=lambda url: threading.Timer(0.2, cancel.set).start() or True,
                       timeout=30, cancel=cancel, persist=self.persist, preferred_port=0)
        self.assertLess(time.monotonic() - started, 3)

    def test_cancel_before_anything(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ConnectCancelled):
            nm.connect({}, open_browser=lambda url: True, cancel=cancel, preferred_port=0)
        self.assertEqual(self.fake.log, [])                            # not even a registration

    def test_timeout(self):
        started = time.monotonic()
        with self.assertRaises(ConnectError) as cm:
            nm.connect({}, open_browser=lambda url: True, timeout=0.4, persist=self.persist, preferred_port=0)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - started, 3)

    def test_the_default_timeout_is_180_seconds(self):
        self.assertEqual(nm.LOGIN_TIMEOUT, 180)

    def test_no_browser(self):
        for opener in (lambda url: False, MagicMock(side_effect=RuntimeError("boom"))):
            with self.assertRaises(ConnectError) as cm:
                nm.connect({}, open_browser=opener, timeout=2, persist=self.persist, preferred_port=0)
            self.assertEqual(cm.exception.code, "browser")

    def test_the_listener_is_closed_afterwards(self):
        hits = []
        self.browse_connect(hits=hits)
        port = self.listener_port(hits[0])
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_a_code_the_server_refuses(self):
        self.fake.token_fail.append((400, {"error": "invalid_grant"}))
        with self.assertRaises(ConnectError) as cm:
            self.browse_connect()
        self.assertEqual(cm.exception.code, "expired")

    def test_the_server_is_busy_at_the_exchange(self):
        self.fake.token_fail.append((503, {"error": "temporarily_unavailable"}))
        with self.assertRaises(ConnectError) as cm:
            self.browse_connect()
        self.assertEqual(cm.exception.code, "server")

    def test_a_token_answer_without_a_token(self):
        self.fake.token_fail.append((200, {"token_type": "bearer"}))
        with self.assertRaises(ConnectError) as cm:
            self.browse_connect()
        self.assertEqual(cm.exception.code, "server")

    def test_connected_but_the_first_page_fails_gives_the_grant_back(self):
        self.fake.rpc_error.append("something broke")
        with self.assertRaises(ConnectError) as cm:
            self.browse_connect()
        self.assertEqual(cm.exception.code, "other")
        self.assertTrue(self.fake.revoked)
        self.assertEqual(nm.latest_tokens(), {})

    def test_connected_but_offline_for_the_first_page(self):
        real = nm._request

        def drop_mcp(method, url, **kw):
            if url == self.fake.base + "/mcp":
                raise nm.NetworkError("offline")
            return real(method, url, **kw)

        with patch.object(nm, "_request", drop_mcp):
            with self.assertRaises(ConnectError) as cm:
                self.browse_connect()
        self.assertEqual(cm.exception.code, "offline")
        self.assertTrue(self.fake.revoked)

    def test_cancelled_while_the_first_page_is_made_gives_the_grant_back(self):
        cancel = threading.Event()
        real = self.fake.tools["notion-create-pages"]
        self.fake.tools["notion-create-pages"] = lambda args: (cancel.set(), real(args))[1]
        with self.assertRaises(ConnectCancelled):
            self.browse_connect(cancel=cancel)
        self.assertTrue(self.fake.revoked)

    def test_the_authorize_address_is_asked_once_before_the_browser_opens(self):
        hits = []
        self.browse_connect(hits=hits)
        self.assertEqual(self.fake.authorize_calls, [hits[0]])                # the very address the browser got

    def test_a_registration_notion_has_forgotten_is_replaced_and_connecting_goes_on(self):
        hits = []
        res = self.browse_connect(cfg={"notion_mcp_client_id": "gone-id", "notion_mcp_redirects": "2"}, hits=hits)
        self.assertEqual([c["client_id"] for c in self.fake.authorize_calls], ["gone-id", "cid1"])
        self.assertEqual(hits[0]["client_id"], "cid1")                        # the browser was sent with the new one
        self.assertEqual(self.persist.calls, [{"notion_mcp_client_id": "", "notion_mcp_redirects": ""},
                                              {"notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}])
        self.assertEqual((res["client_id"], res["settings"]["notion_mcp_client_id"]), ("cid1", "cid1"))
        self.assertEqual(self.fake.log.count("register"), 1)
        self.assertTrue(res["token"])

    def test_a_client_that_is_refused_twice_still_gets_the_browser_and_never_a_loop(self):
        """A server that answers a program differently from a browser must not lock anyone out."""
        self.fake.authorize_status = 400
        opened = []
        with self.assertRaises(ConnectError) as cm:
            nm.connect({"notion_mcp_client_id": "gone-id", "notion_mcp_redirects": "2"}, open_browser=lambda url: opened.append(url) or True,
                       timeout=0.5, persist=self.persist, preferred_port=0)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertEqual(len(opened), 1)
        self.assertEqual(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(opened[0]).query))["client_id"], "cid1")
        self.assertEqual(self.fake.log.count("register"), 1)                  # one new registration, never a loop
        # asked: the saved client, the new one - and, on the site's return, the loopback return as the last try
        self.assertEqual(len(self.fake.authorize_calls), 3 if self.via_site else 2)
        if self.via_site:
            self.assertEqual(urllib.parse.urlsplit(opened[0]).query.count("127.0.0.1"), 1)   # the browser got the loopback one

    def test_anything_but_a_plain_refusal_goes_on_to_the_browser(self):
        self.fake.registered["cid1"] = {}
        for status in (302, 403, 429, 503):
            self.fake.authorize_status = status if status != 302 else 0
            self.persist.calls[:] = []
            hits = []
            res = self.browse_connect(cfg=SAVED, hits=hits)
            self.assertTrue(res["token"], status)
            self.assertEqual(hits[0]["client_id"], "cid1", status)
            self.assertEqual(self.persist.calls, [], status)                  # the saved id was kept
        real = nm._request

        def no_authorize(method, url, **kw):
            if "/authorize" in url:
                raise nm.NetworkError("offline")
            return real(method, url, **kw)

        self.fake.authorize_status = 0
        with patch.object(nm, "_request", no_authorize):
            self.assertTrue(self.browse_connect(cfg=SAVED)["token"])

    def test_a_bug_after_the_exchange_gives_the_grant_back_too(self):
        with patch.object(nm, "ensure_hub", MagicMock(side_effect=RuntimeError("bug"))):
            with self.assertRaises(RuntimeError):
                self.browse_connect()
        self.assertTrue(self.fake.revoked)
        self.assertEqual(nm.latest_tokens(), {})

    def test_connecting_again_keeps_the_page_notes_were_going_to_in_the_same_workspace(self):
        old = self.make_page("Gusan meetings")
        cfg = {"notion_hub_page": old, "notion_workspace_name": "Aram's workspace", "notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}
        self.fake.registered["cid1"] = {}
        res = self.browse_connect(cfg=cfg)
        self.assertEqual(res["dest"]["id"], ne.extract_page_id(old))
        self.assertEqual(self.fake.calls("notion-create-pages"), [])   # no second "Gusan meetings"
        self.assertEqual(self.persist.calls, [])                       # the id was already saved
        self.assertEqual(self.fake.calls("notion-fetch"), [{"id": ne.extract_page_id(old)}])   # checked, not trusted

    def test_a_saved_page_that_is_in_the_trash_is_not_reused(self):
        for marker in (False, True):
            self.fake.trash_marker = marker
            old = self.make_page("Gusan meetings")
            self.fake.trashed.add(old)
            cfg = {"notion_hub_page": old, "notion_workspace_name": "Aram's workspace", "notion_mcp_client_id": "cid1",
                   "notion_mcp_redirects": "2"}
            self.fake.registered["cid1"] = {}
            res = self.browse_connect(cfg=cfg)
            self.assertNotEqual(res["dest"]["id"], ne.extract_page_id(old), marker)
            self.assertEqual(self.fake.calls("notion-create-pages")[-1]["creation_mode"], "draft")

    def test_a_saved_page_that_cannot_be_checked_just_now_is_kept(self):
        # A hiccup while connecting again (busy, a 5xx, words we cannot read) must not make a second
        # "Gusan meetings" page; a send that finds the page really gone makes it again then.
        old = self.make_page("Gusan meetings")
        cfg = {"notion_hub_page": old, "notion_workspace_name": "Aram's workspace", "notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}
        self.fake.registered["cid1"] = {}
        for text in ("rate_limited", "Internal server error"):
            self.fake.tools["notion-fetch"] = lambda args, _t=text: (_ for _ in ()).throw(ToolFail(_t))
            res = self.browse_connect(cfg=cfg)
            self.assertEqual(res["dest"]["id"], ne.extract_page_id(old), text)
            self.assertEqual(res["settings"]["notion_hub_page"], ne.extract_page_id(old), text)
        self.assertEqual(self.fake.calls("notion-create-pages"), [])           # no second "Gusan meetings"
        self.assertEqual(len(self.fake.calls("notion-fetch")), 2)               # it did ask

    def test_another_workspace_gets_its_own_page(self):
        cfg = {"notion_hub_page": hex_id(0x55), "notion_workspace_name": "Somebody else's", "notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}
        self.fake.registered["cid1"] = {}
        res = self.browse_connect(cfg=cfg)
        self.assertNotEqual(res["dest"]["id"], ne.extract_page_id(hex_id(0x55)))
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)

    def test_a_workspace_name_missing_from_the_token_answer_is_asked_for(self):
        real = self.fake.issue_tokens
        self.fake.issue_tokens = lambda extra=None: real({})            # no workspace_name in the answer
        res = self.browse_connect()
        self.assertEqual(res["workspace"], "Fake Workspace")

    def test_a_long_or_hostile_workspace_name_is_cleaned(self):
        real = self.fake.issue_tokens
        self.fake.issue_tokens = lambda extra=None: real({"workspace_name": "<b>X</b>\n" + "y" * 500})
        res = self.browse_connect()
        self.assertNotIn("<", res["workspace"])
        self.assertLessEqual(len(res["workspace"]), 200)

    def test_connect_through_notion_connect(self):
        res = nc.connect_mcp({}, open_browser=lambda url: self._follow(url), timeout=10, persist=self.persist)
        self.assertEqual(res["mode"], "mcp")

    def _follow(self, url):
        threading.Thread(target=self.visit, args=(url, []), daemon=True).start()
        return True


class TestConnectLoopback(TestConnect):
    """Every connect test again, with our website's return page not there (a 404): the browser comes
    back straight to the listener on 127.0.0.1, exactly as it did before the page existed."""
    site = "404"


class TestReturnPage(NetCase):
    """Is our website's page up? One GET, no redirect, 4 s, and only OUR page counts."""

    def ask(self, mode, **kw):
        self.fake.site = mode
        del self.fake.site_requests[:]
        return nm.return_page_ready(**kw)

    def test_only_a_200_with_our_mark_counts(self):
        self.assertTrue(self.ask("ok"))
        self.assertEqual(self.fake.site_requests, ["/notion/callback"])         # a plain GET: no query, nothing sent along
        for mode in ("404", "nomarker", "redirect"):
            self.assertFalse(self.ask(mode), mode)
            self.assertEqual(self.fake.site_requests, ["/notion/callback"], mode)   # a 302 is not followed

    def test_a_catch_all_page_is_not_ours(self):
        """A host that answers 200 to everything (a site builder's fallback) must not make the return page "up"."""
        self.assertFalse(self.ask("nomarker"))

    def test_the_mark_in_a_big_page_and_in_either_attribute_order(self):
        for page in ('<html><head><meta name="gusan-return" content="1"></head></html>',
                     "<html><head><meta name='gusan-return' content='1'/></head></html>",
                     '<head><META  NAME="gusan-return"   CONTENT="1" ></head>',
                     '<head><meta content="1" name="gusan-return"></head>',
                     # a minifier that drops the quotes, or the closing slash, must not switch the site return off
                     "<head><meta charset=utf-8><meta name=gusan-return content=1><title>x</title></head>",
                     '<head><meta content=1 name=gusan-return/></head>',
                     '<head><meta name="gusan-return" content="1" data-x="y" /></head>'):
            reply = nm.Reply(200, {"Content-Type": "text/html"}, ("<p>x</p>" * 100 + page).encode("utf-8"))
            with patch.object(nm, "_request", lambda *a, _r=reply, **k: _r):
                self.assertTrue(nm.return_page_ready(), page)
        for page in ('<meta name="gusan-return" content="0">', '<meta name="gusan-return">', "gusan-return",
                     '<meta name="other" content="1">', '<!-- <meta name="gusan-return" -->',
                     '<meta name="gusan-return" content="10">', '<meta name="gusan-returned" content="1">',
                     '<meta name=gusan-return content=11>'):
            reply = nm.Reply(200, {}, page.encode("utf-8"))
            with patch.object(nm, "_request", lambda *a, _r=reply, **k: _r):
                self.assertFalse(nm.return_page_ready(), page)

    def test_a_site_that_is_down_is_not_ready_and_nothing_raises(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        busy.close()                                                              # nothing listens there any more
        self.assertFalse(nm.return_page_ready("http://127.0.0.1:%d/notion/callback" % port))
        with patch.object(nm, "_request", MagicMock(side_effect=nm.NetworkError("offline"))):
            self.assertFalse(nm.return_page_ready())
        with patch.object(nm, "_request", MagicMock(side_effect=RuntimeError("a bug in the transport"))):
            self.assertFalse(nm.return_page_ready())

    def test_a_slow_site_is_given_its_four_seconds_and_no_more(self):
        self.assertEqual(nm.RETURN_TIMEOUT, 4)
        self.fake.site_delay = 1.5
        started = time.monotonic()
        self.assertFalse(self.ask("slow", timeout=0.3))
        self.assertLess(time.monotonic() - started, 1.2)                          # it did not wait for the answer
        self.fake.site_delay = 0.0
        self.assertTrue(self.ask("slow", timeout=3))

    def test_an_answer_that_trickles_in_is_cut_off_by_the_clock_not_by_the_socket(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def drip(*a, **k):
            gate.wait(5)                          # every socket read "works", the whole answer never finishes
            return nm.Reply(200, {}, SITE_PAGE.encode("utf-8"))

        started = time.monotonic()
        with patch.object(nm, "_request", drip):
            self.assertFalse(nm.return_page_ready(timeout=0.3))
        self.assertLess(time.monotonic() - started, 2)

    def test_a_huge_page_is_only_looked_at_in_part_and_a_big_answer_with_the_mark_far_in_is_not_ours(self):
        self.assertFalse(self.ask("huge"))                                         # the mark is past the first 64 KB
        self.assertEqual(nm.RETURN_PEEK, 65536)

    def test_the_real_address(self):
        self.assertEqual(REAL_RETURN_URL, "https://gusan.aibuben.xyz/notion/callback")
        self.assertEqual(urllib.parse.urlsplit(REAL_RETURN_URL).path, nm.CALLBACK_PATH)
        self.assertEqual(urllib.parse.urlsplit(REAL_RETURN_URL).netloc, urllib.parse.urlsplit(nm.CLIENT_URI).netloc)
        self.assertEqual(nm.REGISTERED_REDIRECT, "http://127.0.0.1:53682/notion/callback")

    def test_nothing_about_the_answer_is_logged_but_a_status_number(self):
        with captured_logs() as lines:
            self.ask("nomarker")
            self.ask("404")
            self.ask("ok")
        text = "\n".join(lines)
        self.assertNotIn("Welcome", text)
        self.assertNotIn(self.site_url, text)


class TestSiteReturn(NetCase):
    """The two ways back, and what decides between them."""

    def test_the_state_of_the_site_return_is_the_port_and_a_random_part(self):
        for port in (1024, 53682, 49152, 65535):
            redirect, state = nm.return_for(port, True)
            self.assertEqual(redirect, nm.RETURN_URL)
            m = SITE_STATE.match(state)
            self.assertIsNotNone(m, state)
            self.assertEqual(int(m.group(1)), port)
            self.assertGreaterEqual(len(state.split(".", 1)[1]), 24)
            self.assertEqual(state.count("."), 1)
        self.assertNotEqual(nm.return_for(53682, True)[1], nm.return_for(53682, True)[1])

    def test_the_loopback_return_has_a_plain_random_state(self):
        redirect, state = nm.return_for(53682, False)
        self.assertEqual(redirect, "http://127.0.0.1:53682/notion/callback")
        self.assertRegex(state, r"^[A-Za-z0-9_-]{24,}$")
        self.assertNotIn(".", state)

    def test_a_port_that_does_not_fit_the_sites_check_never_uses_the_site(self):
        for port in (0, 80, 1023, 65536, 100000):
            redirect, state = nm.return_for(port, True)
            self.assertEqual(redirect, "http://127.0.0.1:%d/notion/callback" % port, port)
            self.assertNotIn(".", state)

    def test_the_site_is_used_when_it_is_up_and_the_whole_flow_works(self):
        hits = []
        res = self.browse_connect(hits=hits)
        q = hits[0]
        self.assertEqual(q["redirect_uri"], nm.RETURN_URL)
        self.assertEqual(int(q["state"].split(".")[0]), self.listener_port(q))
        # the code was swapped under the SAME return address the browser was sent to
        self.assertEqual(self.fake.token_requests[0]["redirect_uri"], nm.RETURN_URL)
        self.assertTrue(res["token"])
        # the site was asked once before the browser (a bare GET), then visited by the "browser" with the answer
        self.assertEqual(len(self.fake.site_requests), 2)
        self.assertEqual(self.fake.site_requests[0], "/notion/callback")
        visited = urllib.parse.parse_qs(urllib.parse.urlsplit(self.fake.site_requests[1]).query)
        self.assertEqual(visited["state"], [q["state"]])
        self.assertIn("code", visited)

    def test_the_site_is_asked_before_anything_is_registered_and_only_once(self):
        self.browse_connect()
        bare = [r for r in self.fake.site_requests if "?" not in r]
        self.assertEqual(bare, ["/notion/callback"])
        self.assertEqual(self.fake.site_seen_clients[0], 0)           # nothing was registered yet when it was asked

    def test_a_state_with_the_right_port_but_the_wrong_rest_is_refused_by_the_listener(self):
        statuses = []

        def open_browser(url):
            def go():
                q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                port, rest = int(q["state"].split(".")[0]), q["state"].split(".")[1]
                last = "A" if rest[-1] != "A" else "B"
                for state in ("%d.%s" % (port, "x" * 32), "%d.%s" % (port, rest[:-1] + last), rest, str(port), ""):
                    bad = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    bad.request("GET", "/notion/callback?" + urllib.parse.urlencode({"code": "stolen", "state": state}))
                    statuses.append(bad.getresponse().status)
                    bad.close()
                self.visit(url, [])
            threading.Thread(target=go, daemon=True).start()
            return True

        res = nm.connect({}, open_browser=open_browser, timeout=10, persist=self.persist, preferred_port=0)
        self.assertEqual(statuses, [400] * 5)
        self.assertTrue(res["token"])
        self.assertNotIn("stolen", [r.get("code") for r in self.fake.token_requests])

    def test_the_listener_takes_what_the_site_forwards_whole(self):
        """code, state, iss, error and error_description: the page passes the query on as it came."""
        def run(error):
            def open_browser(url):
                def go():
                    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
                    if error:
                        back = {"error": "access_denied", "error_description": "The user said no", "state": q["state"],
                                "iss": self.fake.base}
                    else:
                        code = self.fake.issue_code(q["client_id"], q["code_challenge"], q["redirect_uri"])
                        back = {"code": code, "state": q["state"], "iss": self.fake.base,
                                "error_description": "never read"}
                    self.visit(url, [], query=back)
                threading.Thread(target=go, daemon=True).start()
                return True
            return nm.connect({}, open_browser=open_browser, timeout=10, persist=self.persist, preferred_port=0)

        self.assertTrue(run(False)["token"])
        with self.assertRaises(ConnectError) as cm:
            run(True)
        self.assertEqual(cm.exception.code, "denied")

    def test_when_notion_refuses_the_sites_address_the_loopback_one_is_used(self):
        self.fake.refuse_redirects.add(nm.RETURN_URL)
        hits = []
        res = self.browse_connect(hits=hits)
        self.assertTrue(res["token"])
        self.assertTrue(hits[0]["redirect_uri"].startswith("http://127.0.0.1:"), hits[0]["redirect_uri"])
        self.assertNotIn(".", hits[0]["state"])
        self.assertEqual(self.fake.token_requests[0]["redirect_uri"], hits[0]["redirect_uri"])
        self.assertEqual(self.fake.log.count("register"), 2)               # the first, and the replacement
        self.assertEqual([c["redirect_uri"] for c in self.fake.authorize_calls],
                         [nm.RETURN_URL, nm.RETURN_URL, hits[0]["redirect_uri"]])

    def test_a_client_of_before_the_site_page_is_replaced_when_the_site_is_up_and_kept_when_it_is_not(self):
        old = {"notion_mcp_client_id": "old-id", "notion_mcp_hub": "x"}
        self.fake.registered["old-id"] = {"redirect_uris": [nm.REGISTERED_REDIRECT]}     # as 2.0.0 registered it
        hits = []
        res = self.browse_connect(cfg=old, hits=hits)                                   # the site is up
        self.assertEqual(hits[0]["client_id"], "cid2")
        self.assertEqual(hits[0]["redirect_uri"], nm.RETURN_URL)
        self.assertEqual(self.persist.calls, [{"notion_mcp_client_id": "cid2", "notion_mcp_redirects": "2"}])
        self.assertEqual((res["client_id"], res["redirects"]), ("cid2", "2"))
        # the very same install with the site down: the old client does the loopback return, nothing is registered
        self.fake.site = "404"
        del self.persist.calls[:]
        hits = []
        res = self.browse_connect(cfg=old, hits=hits)
        self.assertEqual(hits[0]["client_id"], "old-id")
        self.assertTrue(hits[0]["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertEqual(self.persist.calls, [])
        self.assertEqual(self.fake.log.count("register"), 1)
        self.assertNotIn("notion_mcp_redirects", res["settings"])                    # it still knows the loopback one only

    def test_the_site_down_with_a_client_that_knows_both_reuses_it(self):
        self.fake.site = "404"
        self.fake.registered["cid1"] = {"redirect_uris": [nm.RETURN_URL, nm.REGISTERED_REDIRECT]}
        res = self.browse_connect(cfg=SAVED)
        self.assertEqual((res["client_id"], res["redirects"]), ("cid1", "2"))
        self.assertEqual(self.fake.log.count("register"), 0)

    def test_every_kind_of_trouble_with_the_site_means_the_loopback_return_and_a_working_connect(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        closed = "http://127.0.0.1:%d/notion/callback" % busy.getsockname()[1]
        busy.close()                                                       # nothing answers there: "the site is down"
        self.fake.site_delay = 1.5
        for mode in ("404", "nomarker", "redirect", "slow", "down"):
            self.fake.site = mode
            del self.fake.site_requests[:]
            hits = []
            started = time.monotonic()
            site = closed if mode == "down" else self.site_url
            with patch.object(nm, "RETURN_URL", site), patch.object(nm, "RETURN_TIMEOUT", 0.4):
                res = self.browse_connect(hits=hits)
            self.assertTrue(res["token"], mode)
            self.assertTrue(hits[0]["redirect_uri"].startswith("http://127.0.0.1:"), mode)
            self.assertNotEqual(hits[0]["redirect_uri"], site, mode)
            self.assertNotIn(".", hits[0]["state"], mode)                      # a plain state: no port in it
            self.assertEqual(self.fake.token_requests[-1]["redirect_uri"], hits[0]["redirect_uri"], mode)
            self.assertLess(time.monotonic() - started, 6, mode)                # a slow or silent site never held it up long
            self.assertEqual([h for h in hits if "site_status" in h], [], mode)  # the browser never went to the site
            nm.forget_tokens()

    def test_the_token_call_uses_the_authorize_address_whichever_one_that_was(self):
        for mode in ("ok", "nomarker"):
            self.fake.site = mode
            del self.fake.token_requests[:]
            hits = []
            self.browse_connect(hits=hits)
            self.assertEqual(self.fake.token_requests[0]["redirect_uri"], hits[0]["redirect_uri"], mode)


# ── credentials and the token chain ──────────────────────────────────────────
class TestCredentials(NetCase):
    def test_from_the_config(self):
        cfg = {"notion_api_key": " at ", "notion_refresh_token": "rt", "notion_token_expires": "123.5",
               "notion_mcp_client_id": "c"}
        c = nm.Credentials.from_cfg(cfg)
        self.assertEqual((c.access, c.refresh, c.expires_at, c.client_id), ("at", "rt", 123.5, "c"))
        self.assertEqual(c.updates(), {"notion_api_key": "at", "notion_refresh_token": "rt",
                                       "notion_token_expires": 123.5})
        empty = nm.Credentials.from_cfg({"notion_token_expires": "soon"})
        self.assertEqual((empty.access, empty.refresh, empty.expires_at, empty.client_id), ("", "", 0.0, ""))
        self.assertEqual(nm.Credentials.from_cfg(None).access, "")

    def test_expiring_means_under_five_minutes_left(self):
        with patch.object(nm, "_now", lambda: 1000.0):
            self.assertFalse(nm.Credentials("a", "r", 0).expiring())                  # unknown: never proactive
            self.assertFalse(nm.Credentials("a", "r", 1000 + 301).expiring())
            self.assertTrue(nm.Credentials("a", "r", 1000 + 299).expiring())
            self.assertTrue(nm.Credentials("a", "r", 999).expiring())
            self.assertFalse(nm.Credentials("a", "r", 1000 + 299).expired())
            self.assertTrue(nm.Credentials("a", "r", 999).expired())
        self.assertEqual(nm.REFRESH_EARLY, 300)

    def test_an_older_link_of_the_chain_takes_the_newest_pair(self):
        c = self.creds()
        old = nm.Credentials(c.access, c.refresh, c.expires_at, c.client_id)
        nm.renew(c)
        self.assertNotEqual(old.refresh, c.refresh)
        same = nm.Credentials.from_cfg({"notion_api_key": old.access, "notion_refresh_token": old.refresh,
                                        "notion_mcp_client_id": "cid1"})
        self.assertEqual((same.access, same.refresh), (c.access, c.refresh))         # never the replayed one
        other = nm.Credentials.from_cfg({"notion_api_key": "x", "notion_refresh_token": "unrelated"})
        self.assertEqual((other.access, other.refresh), ("x", "unrelated"))


class TestRenew(NetCase):
    def test_it_swaps_the_pair_and_hands_it_over(self):
        c = self.creds(expires_at=time.time() + 10)
        old_access, old_refresh = c.access, c.refresh
        nm.renew(c, self.persist)
        self.assertNotEqual((c.access, c.refresh), (old_access, old_refresh))
        sent = self.fake.token_requests[-1]
        self.assertEqual((sent["grant_type"], sent["refresh_token"], sent["client_id"]),
                         ("refresh_token", old_refresh, "cid1"))
        self.assertNotIn("client_secret", sent)
        self.assertEqual(self.persist.calls, [{"notion_api_key": c.access, "notion_refresh_token": c.refresh,
                                               "notion_token_expires": c.expires_at, "notion_reconnect": False}])
        self.assertGreater(c.expires_at, time.time() + 28000)
        self.assertEqual(nm.latest_tokens()["refresh_token"], c.refresh)
        self.assertEqual(nm._SUPERSEDED, [old_refresh])

    def test_an_old_refresh_token_is_never_replayed(self):
        c = self.creds()
        stale = nm.Credentials(c.access, c.refresh, c.expires_at, c.client_id)       # read from the config before the swap
        nm.renew(c, self.persist)
        before = len(self.fake.token_requests)
        nm.renew(stale, self.persist)
        self.assertEqual(len(self.fake.token_requests), before)                      # no second POST
        self.assertEqual((stale.access, stale.refresh), (c.access, c.refresh))
        self.assertFalse(self.fake.revoked)

    def test_a_pair_that_was_not_rotated_keeps_the_refresh_token(self):
        c = self.creds()
        real = self.fake.issue_tokens

        def no_refresh(extra=None):
            out = real(extra)
            out.pop("refresh_token")
            return out

        self.fake.issue_tokens = no_refresh
        old = c.refresh
        nm.renew(c, self.persist)
        self.assertEqual(c.refresh, old)
        self.assertEqual(nm._SUPERSEDED, [])

    def test_threads_make_one_refresh(self):
        c = self.creds()
        clones = [nm.Credentials(c.access, c.refresh, c.expires_at, c.client_id) for _ in range(10)]
        barrier = threading.Barrier(10)

        def go(creds):
            barrier.wait()
            nm.renew(creds, self.persist)

        threads = [threading.Thread(target=go, args=(x,)) for x in clones]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        refreshes = [r for r in self.fake.token_requests if r["grant_type"] == "refresh_token"]
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(len({(x.access, x.refresh) for x in clones}), 1)
        self.assertEqual(len(self.persist.calls), 1)
        self.assertFalse(self.fake.revoked)

    def test_the_new_pair_is_handed_over_before_the_next_request(self):
        c = self.creds(expires_at=time.time() + 30)                 # under five minutes left
        client = nm.McpClient(c, self.persist)
        client.call_tool("notion-get-self", {})
        self.assertEqual(self.fake.log[:3], ["token:refresh_token", "persist", "mcp:initialize"])
        self.assertEqual(self.fake.mcp_headers[0]["authorization"], "Bearer " + c.access)   # the NEW token

    def test_a_token_with_plenty_of_time_left_is_not_refreshed(self):
        c = self.creds(expires_at=time.time() + 4000)
        nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        self.assertNotIn("token:refresh_token", self.fake.log)

    def test_an_unknown_expiry_is_only_handled_by_a_401(self):
        c = self.creds(expires_at=0)
        nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        self.assertNotIn("token:refresh_token", self.fake.log)

    def test_invalid_grant_is_final(self):
        c = self.creds(expires_at=time.time() + 30)
        self.fake.revoked = True                                     # Notion no longer honours this grant
        with self.assertRaises(ConnectError) as cm:
            nm.renew(c, self.persist)
        self.assertEqual(cm.exception.code, "expired")
        self.assertEqual(self.persist.calls, [{"notion_reconnect": True}])
        self.assertEqual(len(self.fake.token_requests), 1)
        client = nm.McpClient(c, self.persist)
        with self.assertRaises(NotionAuthError):
            client.call_tool("notion-get-self", {})
        self.assertEqual(len(self.fake.token_requests), 2)           # one try per call, never a loop
        self.assertEqual(self.fake.calls("notion-get-self"), [])

    def test_a_refresh_token_older_than_one_step_revokes_the_grant_at_notion(self):
        # What the fake models, and why the module never replays: a token two steps old kills the grant.
        c = self.creds()
        first = c.refresh
        nm.renew(c)
        second = c.refresh
        nm.renew(c)
        self.assertEqual(self.fake.chain, [first, second, c.refresh])
        self.assertIn(first, nm._SUPERSEDED)
        self.assertIn(second, nm._SUPERSEDED)

    def test_a_busy_server_is_temporary(self):
        c = self.creds(expires_at=time.time() + 30)
        self.fake.token_fail.append((503, {"error": "temporarily_unavailable"}))
        with self.assertRaises(ConnectError) as cm:
            nm.renew(c, self.persist)
        self.assertEqual(cm.exception.code, "server")
        self.assertEqual(self.persist.calls, [])                     # nothing is lost, nothing flagged
        # the token still has time, so the call just goes ahead with it
        self.fake.token_fail.append((503, {"error": "temporarily_unavailable"}))
        res = nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        self.assertEqual(res.data["workspace"]["name"], "Fake Workspace")

    def test_offline_is_temporary_too(self):
        c = self.creds(expires_at=time.time() + 30)
        real = nm._request

        def no_token(method, url, **kw):
            if url.endswith("/token"):
                raise nm.NetworkError("offline")
            return real(method, url, **kw)

        with patch.object(nm, "_request", no_token):
            with self.assertRaises(ConnectError) as cm:
                nm.renew(c, self.persist)
            self.assertEqual(cm.exception.code, "offline")
            nm.McpClient(c, self.persist).call_tool("notion-get-self", {})        # still valid: carries on
            gone = self.creds(expires_at=time.time() - 5)
            with self.assertRaises(NotionError) as cm2:
                nm.McpClient(gone, self.persist).call_tool("notion-get-self", {})
        self.assertEqual(cm2.exception.code, "offline")
        self.assertEqual(self.persist.calls, [])

    def test_an_expired_token_and_a_busy_server_is_an_error(self):
        c = self.creds(expires_at=time.time() - 5)
        self.fake.token_fail.append((503, {"error": "temporarily_unavailable"}))
        with self.assertRaises(NotionError) as cm:
            nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        self.assertEqual(cm.exception.code, "other")
        self.assertEqual(self.fake.calls("notion-get-self"), [])

    def test_no_refresh_token_or_no_client_id_needs_a_reconnect(self):
        for kw in ({"refresh": ""}, {"client_id": ""}, {"client_id": "bad id"}):
            self.persist.calls[:] = []
            c = self.creds(**kw)
            with self.assertRaises(ConnectError) as cm:
                nm.renew(c, self.persist)
            self.assertEqual(cm.exception.code, "expired", kw)
            self.assertEqual(self.persist.calls, [{"notion_reconnect": True}], kw)

    def test_notion_forgetting_our_registration_drops_the_saved_id(self):
        c = self.creds()
        self.fake.token_fail.append((401, {"error": "invalid_client"}))
        with self.assertRaises(ConnectError) as cm:
            nm.renew(c, self.persist)
        self.assertEqual(cm.exception.code, "expired")
        self.assertEqual(self.persist.calls, [{"notion_mcp_client_id": "", "notion_mcp_redirects": ""},
                                              {"notion_reconnect": True}])

    def test_other_token_answers(self):
        c = self.creds()
        for status, body, code in ((400, {"error": "invalid_request"}, "other"), (429, {}, "server"),
                                   (500, {"error": "server_error"}, "server"), (200, {"nothing": 1}, "server"),
                                   (200, {"access_token": "has space"}, "server")):
            self.fake.token_fail.append((status, body))
            with self.assertRaises(ConnectError) as cm:
                nm.renew(c, self.persist)
            self.assertEqual(cm.exception.code, code, (status, body))
        self.assertNotIn({"notion_reconnect": True}, self.persist.calls)

    def test_a_401_gets_one_refresh_and_one_retry(self):
        c = self.creds()
        self.fake.expire(c.access)                                   # revoked behind our back
        res = nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        self.assertEqual(res.data["workspace"]["id"], "ws-1")
        self.assertEqual(self.fake.log, ["mcp:401", "token:refresh_token", "persist", "mcp:initialize",
                                         "mcp:notifications/initialized", "mcp:tools/call:notion-get-self"])

    def test_two_401s_in_a_row_are_an_auth_error_not_a_loop(self):
        c = self.creds()
        self.fake.mcp_fail = [(401, {}), (401, {})]
        with self.assertRaises(NotionAuthError):
            nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        self.assertEqual(len([r for r in self.fake.token_requests if r["grant_type"] == "refresh_token"]), 1)

    def test_a_pair_another_caller_refreshed_is_adopted_after_a_401(self):
        a, b = self.creds(), None
        b = nm.Credentials(a.access, a.refresh, a.expires_at, a.client_id)
        self.fake.expire(a.access)
        nm.McpClient(a, self.persist).call_tool("notion-get-self", {})               # refreshes
        before = len([r for r in self.fake.token_requests if r["grant_type"] == "refresh_token"])
        nm.McpClient(b, self.persist).call_tool("notion-get-self", {})               # holds the old pair
        after = len([r for r in self.fake.token_requests if r["grant_type"] == "refresh_token"])
        self.assertEqual((before, after), (1, 1))
        self.assertEqual((b.access, b.refresh), (a.access, a.refresh))


class TestDisconnectDuringARefresh(NetCase):
    """The GUI thread forgets the connection (Disconnect, another account signing in) while a worker
    is waiting for Notion to answer a refresh."""

    def slow_refresh(self):
        started, release = threading.Event(), threading.Event()
        real = nm._request

        def slow(method, url, **kw):
            if url.endswith("/token") and (kw.get("form") or {}).get("grant_type") == "refresh_token":
                started.set()
                release.wait(5)
            return real(method, url, **kw)

        return started, release, slow

    def run_renew(self, creds, errors):
        def go():
            try:
                nm.renew(creds, self.persist)
            except Exception as e:
                errors.append(e)
        t = threading.Thread(target=go)
        t.start()
        return t

    def test_forgetting_never_waits_for_the_refresh_and_the_late_pair_is_not_kept(self):
        c = self.creds()
        started, release, slow = self.slow_refresh()
        errors = []
        with patch.object(nm, "_request", slow):
            t = self.run_renew(c, errors)
            self.assertTrue(started.wait(5))
            began = time.monotonic()
            nc.forget_tokens()                                       # what Disconnect calls, on the GUI thread
            self.assertLess(time.monotonic() - began, 1.0)
            self.assertEqual(nc.latest_tokens(), {})
            release.set()
            t.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(self.persist.calls, [])                     # nothing written back after the disconnect
        self.assertEqual(nm.latest_tokens(), {})
        self.assertEqual(nm._SUPERSEDED, [])

    def test_a_new_connection_in_the_meantime_is_not_overwritten_by_the_old_grants_refresh(self):
        c = self.creds()
        started, release, slow = self.slow_refresh()
        errors = []
        with patch.object(nm, "_request", slow):
            t = self.run_renew(c, errors)
            self.assertTrue(started.wait(5))
            nm._adopt_chain({"access_token": "at-new", "refresh_token": "rt-new", "expires_at": time.time() + 9999})
            release.set()
            t.join(5)
        self.assertEqual(self.persist.calls, [])
        self.assertEqual(nm.latest_tokens()["access_token"], "at-new")

    def test_a_refusal_after_the_disconnect_flags_nothing(self):
        c = self.creds()
        started, release, slow = self.slow_refresh()
        errors = []
        self.fake.revoked = True
        with patch.object(nm, "_request", slow):
            t = self.run_renew(c, errors)
            self.assertTrue(started.wait(5))
            nm.forget_tokens()
            release.set()
            t.join(5)
        self.assertEqual([getattr(e, "code", "") for e in errors], ["expired"])
        self.assertEqual(self.persist.calls, [])                     # no "Reconnect" for a connection that is gone

    def test_credentials_read_before_a_disconnect_are_never_refreshed_afterwards(self):
        c = self.creds(expires_at=time.time() + 10)
        cfg = {"notion_api_key": c.access, "notion_refresh_token": c.refresh, "notion_mcp_client_id": "cid1",
               "notion_token_expires": c.expires_at}
        held = nm.Credentials.from_cfg(cfg)                          # what a send that is running holds
        running = nm.Credentials.from_cfg(cfg)
        running.expires_at = 0                                       # a send whose token has time left
        self.assertTrue(held.current())
        nc.forget_tokens()                                           # Disconnect, on the GUI thread
        self.assertFalse(held.current())
        before = len(self.fake.token_requests)
        with self.assertRaises(ConnectError) as cm:
            nm.renew(held, self.persist)
        self.assertEqual(cm.exception.code, "expired")
        self.assertEqual(len(self.fake.token_requests), before)      # nothing was sent: that refresh token is revoked anyway
        self.assertEqual(self.persist.calls, [])                     # and no "Reconnect" was flagged
        with self.assertRaises(NotionAuthError):
            nm.McpClient(held, self.persist).call_tool("notion-get-self", {})
        self.assertEqual(self.persist.calls, [])
        # a hub made by that send is not written into the settings either
        nm.ensure_hub(nm.McpClient(running, self.persist), "", self.persist)
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)
        self.assertEqual(self.persist.calls, [])
        # credentials of the new connection are fine, and so are ones that are tied to no connection
        fresh = nm.Credentials.from_cfg(cfg)
        self.assertTrue(fresh.current() and nm.Credentials("a", "r").current())

    def test_connecting_again_makes_the_older_credentials_stale_too(self):
        c = self.creds()
        held = nm.Credentials.from_cfg({"notion_api_key": c.access, "notion_refresh_token": c.refresh})
        nm._adopt_chain({"access_token": "at-new", "refresh_token": "rt-new", "expires_at": time.time() + 9999})
        self.assertFalse(held.current())


class TestRevoke(NetCase):
    def test_the_refresh_token_is_given_back_with_the_client_id(self):
        self.assertTrue(nm.revoke("at-x", "rt-x", "cid1"))
        sent = self.fake.token_requests[-1]
        self.assertEqual(sent, {"token": "rt-x", "token_type_hint": "refresh_token", "client_id": "cid1"})
        self.assertTrue(self.fake.revoked)

    def test_without_a_refresh_token_the_access_token_is(self):
        self.assertTrue(nm.revoke("at-x", "", "cid1"))
        self.assertEqual(self.fake.token_requests[-1]["token_type_hint"], "access_token")

    def test_nothing_to_give_back(self):
        self.assertTrue(nm.revoke("", "", "cid1"))
        self.assertEqual(self.fake.log, [])

    def test_it_never_raises_and_says_when_it_could_not(self):
        def down(*a, **k):
            raise nm.NetworkError("offline")

        with patch.object(nm, "_request", down):
            self.assertFalse(nm.revoke("at", "rt", "cid1"))
        with patch.object(nm, "_request", lambda *a, **k: nm.Reply(500, {}, b"{}")):
            self.assertFalse(nm.revoke("at", "rt", "cid1"))
        with patch.object(nm, "_request", MagicMock(side_effect=RuntimeError("boom"))):
            self.assertFalse(nm.revoke("at", "rt", "cid1"))

    def test_the_tokens_in_memory_are_forgotten_either_way(self):
        self.creds()
        self.assertTrue(nm.latest_tokens())
        with patch.object(nm, "_request", MagicMock(side_effect=nm.NetworkError("x"))):
            nm.revoke("at", "rt", "cid1")
        self.assertEqual(nm.latest_tokens(), {})


# ── the MCP client ───────────────────────────────────────────────────────────
class ClientCases:
    """The protocol, run once with JSON answers and once with event streams."""

    def client(self, **kw):
        return nm.McpClient(self.creds(**kw), self.persist)

    def test_initialize_then_initialized_then_the_call(self):
        res = self.client().call_tool("notion-get-self", {})
        self.assertEqual(res.data["workspace"]["name"], "Fake Workspace")
        self.assertEqual(self.fake.log, ["mcp:initialize", "mcp:notifications/initialized",
                                         "mcp:tools/call:notion-get-self"])

    def test_the_headers(self):
        c = self.creds()
        nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        first, second, third = self.fake.mcp_headers
        self.assertEqual(first["authorization"], "Bearer " + c.access)
        self.assertEqual(first["accept"], "application/json, text/event-stream")
        self.assertEqual(first["content-type"], "application/json")
        self.assertEqual(first["user-agent"], "Gusan/" + nm.APP_VERSION)
        self.assertNotIn("mcp-session-id", first)                    # not known yet
        self.assertNotIn("mcp-protocol-version", first)
        for later in (second, third):
            self.assertEqual(later["mcp-session-id"], "sess-1")      # echoed
            self.assertEqual(later["mcp-protocol-version"], nm.PROTOCOL_VERSION)
            self.assertEqual(later["authorization"], "Bearer " + c.access)

    def test_one_session_for_many_calls(self):
        client = self.client()
        client.call_tool("notion-get-self", {})
        client.call_tool("notion-get-self", {})
        self.assertEqual(self.fake.log.count("mcp:initialize"), 1)
        self.assertEqual(self.fake.log.count("mcp:tools/call:notion-get-self"), 2)

    def test_the_request_body(self):
        seen = []
        real = stdlib_request

        def spy(method, url, **kw):
            if kw.get("json_body"):
                seen.append(kw["json_body"])
            return real(method, url, **kw)

        with patch.object(nm, "_request", spy):
            self.client().call_tool("notion-get-self", {"a": 1})
        init, note, call = [s for s in seen if "jsonrpc" in s]
        self.assertEqual(init["method"], "initialize")
        self.assertEqual(init["params"]["clientInfo"], {"name": "Gusan", "version": nm.APP_VERSION})
        self.assertEqual(init["params"]["protocolVersion"], nm.PROTOCOL_VERSION)
        self.assertEqual(note, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(call["method"], "tools/call")
        self.assertEqual(call["params"], {"name": "notion-get-self", "arguments": {"a": 1}})
        self.assertEqual([init["id"], call["id"]], [1, 2])

    def test_structured_content_and_text_json_both_come_back_as_data(self):
        self.fake.tools["a"] = lambda args: tool_result({"x": [1, 2]}, structured=True)
        self.fake.tools["b"] = lambda args: tool_result({"y": 3})
        self.fake.tools["c"] = lambda args: tool_result(text="just words")
        client = self.client()
        self.assertEqual(client.call_tool("a", {}).data, {"x": [1, 2]})
        self.assertEqual(client.call_tool("b", {}).data, {"y": 3})
        c = client.call_tool("c", {})
        self.assertEqual((c.text, c.data), ("just words", None))

    def test_tool_errors_say_what_happened_in_the_apps_own_codes(self):
        cases = (
            ("object_not_found: Could not find page with ID abc", NotionNotFoundError, "no_access", "gone"),
            ("Can't edit block that is archived.", NotionNotFoundError, "no_access", "gone"),
            ("restricted_resource: you do not have access", NotionNotFoundError, "no_access", "forbidden"),
            ("unauthorized: token expired", NotionAuthError, "auth", ""),
            ("validation_error: content is too large", NotionError, "other", "validation"),
            ("rate_limited: retry_after_seconds: 7", NotionError, "rate", ""),
            ("something nobody planned for", NotionError, "other", ""),
        )
        client = self.client()
        for text, cls, code, reason in cases:
            def boom(args, _t=text):
                raise ToolFail(_t)
            self.fake.tools["t"] = boom
            with self.assertRaises(cls) as cm:
                client.call_tool("t", {})
            e = cm.exception
            self.assertEqual((e.code, getattr(e, "reason", "")), (code, reason), text)
            self.assertNotIn(text, str(e))                           # a server's words never become ours
            if code == "rate":
                self.assertEqual(e.retry_after, 7.0)

    def test_the_sentences_are_the_fixed_ones(self):
        self.assertEqual(str(nm._tool_error("unauthorized")), ne.AUTH_AGAIN_TEXT)
        self.assertEqual(str(nm._tool_error("object_not_found")), "Gusan can't reach that Notion page anymore.")
        self.assertEqual(str(nm._tool_error("rate_limited")), "Notion rate limit hit - wait a moment and try again.")
        self.assertEqual(str(nm._http_error(500)), "Notion had a problem (error 500) - try again in a minute.")

    def test_a_json_rpc_error(self):
        self.fake.rpc_error.append((-32601, "Method not found"))     # the protocol's own error: not a missing page
        client = self.client()
        with self.assertRaises(NotionError) as cm:
            client.call_tool("notion-get-self", {})
        self.assertEqual(cm.exception.code, "other")
        self.fake.rpc_error.append("unauthorized request")
        with self.assertRaises(NotionAuthError):
            client.call_tool("notion-get-self", {})

    def test_http_failures(self):
        for status, code in ((403, "no_access"), (500, "other"), (502, "other"), (404, "other"), (400, "other"),
                             (405, "other")):
            self.fake.mcp_fail = [(status, {})]
            with self.assertRaises(NotionError) as cm:
                self.client().call_tool("notion-get-self", {})
            self.assertEqual(cm.exception.code, code, status)
            self.assertEqual(cm.exception.status, status)

    def test_a_short_429_is_waited_out_once(self):
        self.fake.mcp_fail = [(429, {"Retry-After": "1"})]
        res = self.client().call_tool("notion-get-self", {})
        self.assertEqual(res.data["workspace"]["id"], "ws-1")
        self.assertEqual(self.sleeps, [1.0])

    def test_a_long_429_is_reported_not_waited_out(self):
        self.fake.mcp_fail = [(429, {"Retry-After": "99"})]
        with self.assertRaises(NotionError) as cm:
            self.client().call_tool("notion-get-self", {})
        self.assertEqual((cm.exception.code, cm.exception.retry_after, self.sleeps), ("rate", 99.0, []))

    def test_a_second_429_is_reported(self):
        self.fake.mcp_fail = [(429, {"Retry-After": "1"}), (529, {"Retry-After": "1"})]
        with self.assertRaises(NotionError) as cm:
            self.client().call_tool("notion-get-self", {})
        self.assertEqual((cm.exception.code, self.sleeps), ("rate", [1.0]))

    def test_no_internet(self):
        def down(*a, **k):
            raise nm.NetworkError("offline")

        c = self.creds()
        with patch.object(nm, "_request", down):
            with self.assertRaises(NotionError) as cm:
                nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        self.assertEqual((cm.exception.code, str(cm.exception)), ("offline", ne.OFFLINE_TEXT))

    def test_discovery_offline_is_offline(self):
        def down(*a, **k):
            raise nm.NetworkError("offline")

        c = self.creds()
        nm.forget_discovery()
        with patch.object(nm, "_request", down):
            with self.assertRaises(NotionError) as cm:
                nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
        self.assertEqual(cm.exception.code, "offline")


class TestClientJson(ClientCases, NetCase):
    reply = "json"

    def test_a_refused_initialized_notification_does_not_end_the_call(self):
        real = stdlib_request

        def refuse_notification(method, url, **kw):
            body = kw.get("json_body") or {}
            if url.endswith("/mcp") and body.get("method") == "notifications/initialized":
                return nm.Reply(400, {}, b"")
            return real(method, url, **kw)

        with patch.object(nm, "_request", refuse_notification):
            res = self.client().call_tool("notion-get-self", {})
        self.assertEqual(res.data["workspace"]["id"], "ws-1")

        def rate_limit_notification(method, url, **kw):
            body = kw.get("json_body") or {}
            if url.endswith("/mcp") and body.get("method") == "notifications/initialized":
                return nm.Reply(429, {"Retry-After": "99"}, b"")
            return real(method, url, **kw)

        with patch.object(nm, "_request", rate_limit_notification):
            with self.assertRaises(NotionError) as cm:          # an answer that means something else still ends it
                self.client().call_tool("notion-get-self", {})
        self.assertEqual(cm.exception.code, "rate")

    def test_a_json_list_with_the_answer_in_it(self):
        ok = {"jsonrpc": "2.0", "id": 7, "result": {"v": 1}}
        r = nm.Reply(200, {"content-type": "application/json"}, json.dumps([{"jsonrpc": "2.0", "method": "x"}, ok]).encode())
        self.assertEqual(nm.McpClient._message(r, 7), ok)

    def test_an_answer_for_another_request_is_not_ours(self):
        r = nm.Reply(200, {"content-type": "application/json"}, b'{"jsonrpc":"2.0","id":99,"result":{}}')
        with self.assertRaises(NotionError):
            nm.McpClient._message(r, 1)
        with self.assertRaises(NotionError):
            nm.McpClient._message(nm.Reply(200, {}, b"not json"), 1)


class TestClientSse(ClientCases, NetCase):
    reply = "sse"

    def test_the_progress_message_before_the_answer_is_skipped(self):
        res = self.client().call_tool("notion-get-self", {})
        self.assertEqual(res.data["workspace"]["id"], "ws-1")


class TestClientSseHeldOpen(ClientCases, NetCase):
    """A server that keeps the stream open after its answer must not make us wait."""
    reply = "sse"
    hold_open = True

    def test_it_returns_as_soon_as_the_answer_is_in(self):
        started = time.monotonic()
        self.client().call_tool("notion-get-self", {})
        self.assertLess(time.monotonic() - started, 2.0)       # the fake holds each stream for up to 3 s


class TestEventStreamParsing(unittest.TestCase):
    OK = {"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}

    def msg(self, lines, rid=1):
        return nm.McpClient._message(
            nm.Reply(200, {"content-type": "text/event-stream"}, lines=iter(lines)), rid)

    def d(self, obj=None):
        return b"data: " + json.dumps(obj or self.OK).encode()

    def test_the_plain_event(self):
        self.assertEqual(self.msg([b"event: message", self.d(), b""]), self.OK)

    def test_no_space_after_the_colon_and_comments_and_other_fields(self):
        self.assertEqual(self.msg([b": ping", b"id: 3", b"retry: 10", b"data:" + json.dumps(self.OK).encode(), b""]),
                         self.OK)

    def test_data_split_over_lines(self):
        self.assertEqual(self.msg([b'data: {"jsonrpc": "2.0", "id": 1,', b'data: "result": {"x": 1}}', b""]), self.OK)

    def test_a_stream_that_ends_without_a_blank_line(self):
        self.assertEqual(self.msg([self.d()]), self.OK)

    def test_extra_blank_lines_from_crlf(self):
        self.assertEqual(self.msg([b"", b"", self.d(), b"", b"", b""]), self.OK)

    def test_other_messages_first(self):
        note = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
        other = {"jsonrpc": "2.0", "id": 5, "result": {}}
        self.assertEqual(self.msg([self.d(note), b"", self.d(other), b"", self.d(), b""]), self.OK)

    def test_not_json_is_ignored_then_nothing_is_an_error(self):
        with self.assertRaises(NotionError) as cm:
            self.msg([b"data: not json", b"", b"data: [DONE]", b""])
        self.assertEqual(cm.exception.code, "other")
        with self.assertRaises(NotionError):
            self.msg([])

    def test_a_stream_that_drops_is_offline(self):
        def broken():
            yield self.d(dict(self.OK, id=9))
            raise OSError("reset")
        with self.assertRaises(NotionError) as cm:
            nm.McpClient._message(nm.Reply(200, {"content-type": "text/event-stream"}, lines=broken()), 1)
        self.assertEqual(cm.exception.code, "offline")

    def test_an_event_stream_in_a_finished_body(self):
        body = ("event: message\ndata: " + json.dumps(self.OK) + "\n\n").encode()
        self.assertEqual(nm.McpClient._message(nm.Reply(200, {"content-type": "text/event-stream"}, body), 1), self.OK)

    def test_a_stream_that_never_answers_is_given_up_on(self):
        def keepalive():
            while True:
                yield b": ping"
                yield b""
        with patch.object(nm, "STREAM_SECONDS", -1):
            with self.assertRaises(NotionError) as cm:
                nm.McpClient._message(nm.Reply(200, {"content-type": "text/event-stream"}, lines=keepalive()), 1)
        self.assertEqual(cm.exception.code, "other")
        self.assertEqual(nm.STREAM_SECONDS, 300)

    def test_an_event_that_never_ends_is_given_up_on(self):
        def endless():
            while True:
                yield b"data: " + b"x" * 40
        with patch.object(nm, "MAX_BODY", 1000):
            with self.assertRaises(NotionError) as cm:
                nm.McpClient._message(nm.Reply(200, {"content-type": "text/event-stream"}, lines=endless()), 1)
        self.assertEqual(cm.exception.code, "other")


# ── the private page, and one page per meeting ───────────────────────────────
class TestHub(NetCase):
    def client(self):
        return nm.McpClient(self.creds(), self.persist)

    def test_a_saved_hub_is_returned_with_no_call(self):
        hub = self.make_page("Gusan meetings")
        got = nm.ensure_hub(self.client(), hub, self.persist)
        self.assertEqual((got["id"], got["created"], got["title"]), (ne.extract_page_id(hub), False, "Gusan meetings"))
        self.assertEqual(got["url"], "https://www.notion.so/" + hub)
        self.assertEqual(self.fake.mcp_calls, [])
        self.assertEqual(self.persist.calls, [])

    def test_it_is_made_once(self):
        client = self.client()
        first = nm.ensure_hub(client, "", self.persist)
        self.assertTrue(first["created"])
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)
        args = self.fake.calls("notion-create-pages")[0]
        self.assertEqual((args["creation_mode"], args["allow_async"]), ("draft", False))
        self.assertNotIn("parent", args)
        page = args["pages"][0]
        self.assertEqual((page["properties"]["title"], page["icon"]), ("Gusan meetings", nm.HUB_ICON))
        self.assertEqual(self.persist.keys(), {"notion_hub_page": first["id"], "notion_parent_page": first["id"],
                                               "notion_parent_title": "Gusan meetings"})
        again = nm.ensure_hub(client, "", self.persist)            # the config has not caught up yet
        self.assertEqual((again["id"], again["created"]), (first["id"], False))
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)

    def test_a_gone_hub_is_replaced_and_two_senders_make_one_page_not_two(self):
        gone = self.make_page("Gusan meetings")
        self.fake.trashed.add(gone)
        out = []
        threads = [threading.Thread(target=lambda: out.append(nm.ensure_hub(self.client(), gone, self.persist, gone=True)))
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({h["id"] for h in out}), 1)
        self.assertNotEqual(out[0]["id"], ne.extract_page_id(gone))
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)
        self.assertEqual(sum(1 for h in out if h["created"]), 1)

    def test_hub_alive(self):
        client = self.client()
        page = self.make_page("Gusan meetings")
        self.assertIs(nm.hub_alive(client, page), True)
        self.assertIs(nm.hub_alive(client, hex_id(0x999)), False)                  # not found
        self.fake.trashed.add(page)
        self.assertIs(nm.hub_alive(client, page), False)                           # an error that says so
        self.fake.trash_marker = True
        self.assertIs(nm.hub_alive(client, page), False)                           # an answer that says so
        self.fake.tools["notion-fetch"] = lambda args: tool_result(text="<page>fine</page>")
        self.assertIs(nm.hub_alive(client, page), True)

    def test_hub_alive_does_not_guess_when_it_could_not_ask(self):
        client = self.client()
        for text in ("unauthorized", "rate_limited: retry_after_seconds: 1", "something odd", "Internal server error"):
            self.fake.tools["notion-fetch"] = lambda args, _t=text: (_ for _ in ()).throw(ToolFail(_t))
            self.assertIsNone(nm.hub_alive(client, hex_id(1)), text)
        self.fake.tools["notion-fetch"] = lambda args: (_ for _ in ()).throw(ToolFail("Forbidden: no access"))
        self.assertIs(nm.hub_alive(client, hex_id(1)), False)                      # not ours to reach: unusable
        for status in (500, 502, 503):                                              # Notion having a bad minute
            self.fake.mcp_fail.append((status, {}))
            self.assertIsNone(nm.hub_alive(client, hex_id(1)), status)

        def down(*a, **k):
            raise nm.NetworkError("offline")

        with patch.object(nm, "_request", down):
            self.assertIsNone(nm.hub_alive(nm.McpClient(self.creds(), None), hex_id(1)))

    def test_only_the_pages_own_head_says_it_is_in_the_trash(self):
        # notion-fetch lists the pages under the hub by title in its <content>: a meeting called
        # "Sorting what is in the trash" is not the hub being in the trash.
        client = self.client()
        page = self.make_page("Gusan meetings")
        head = '<page url="https://www.notion.so/%s"%s>\n<properties>\n{"title":"Gusan meetings"}\n</properties>\n'
        child = ('<content>\nNotes from your meetings.\n<page url="https://www.notion.so/%s">Sorting what is in the trash'
                 '</page>\n<page url="https://www.notion.so/%s">Old notes in_trash: true</page>\n</content>\n</page>'
                 % (hex_id(0x77), hex_id(0x78)))
        self.fake.tools["notion-fetch"] = lambda args: tool_result(text=head % (page, "") + child)
        self.assertIs(nm.hub_alive(client, page), True)
        self.fake.tools["notion-fetch"] = lambda args: tool_result(text=head % (page, ' in_trash="true"') + child)
        self.assertIs(nm.hub_alive(client, page), False)                           # the page's own tag still counts
        # a yes/no field in the answer's data decides when there is one
        self.fake.tools["notion-fetch"] = lambda args: tool_result(
            {"in_trash": False, "text": head % (page, "") + child}, structured=True)
        self.assertIs(nm.hub_alive(client, page), True)
        self.fake.tools["notion-fetch"] = lambda args: tool_result({"archived": True, "title": "Gusan meetings"})
        self.assertIs(nm.hub_alive(client, page), False)

    def test_forgetting_the_tokens_forgets_the_memo(self):
        client = self.client()
        first = nm.ensure_hub(client, "", None)
        nm.forget_tokens()
        second = nm.ensure_hub(client, "", None)
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue(second["created"])


class TestSend(NetCase):
    TITLE = "Weekly sync"
    LINE = "Thu 8 Oct · 10:15 · 25 min 00 s · Anna, David · Gusan"

    def send(self, creds=None, notes=NOTES, transcript="Anna: We ship Friday.", **kw):
        return nm.send_meeting_mcp(creds or self.creds(), self.TITLE, notes, transcript, self.LINE,
                                   persist=self.persist, **kw)

    def test_the_first_send_makes_the_private_page_then_the_meeting_under_it(self):
        out = self.send()
        creates = self.fake.calls("notion-create-pages")
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0]["creation_mode"], "draft")
        hub_id = ne.extract_page_id(self.persist.keys()["notion_hub_page"])
        meeting = creates[1]
        self.assertEqual(meeting["parent"], {"type": "page_id", "page_id": hub_id})
        self.assertNotIn("creation_mode", meeting)
        self.assertIs(meeting["allow_async"], False)
        page = meeting["pages"][0]
        self.assertEqual((page["properties"], page["icon"]), ({"title": "Weekly sync"}, nm.MEETING_ICON))
        content = page["content"]
        self.assertTrue(content.startswith(self.LINE + ' {color="gray"}\n\n## Summary'))
        self.assertIn("## Key decisions", content)
        self.assertIn("- [ ] Sara: write the announcement", content)
        self.assertIn("## Transcript\n\nAnna: We ship Friday.", content)
        self.assertRegex(out["url"], r"^https://www\.notion\.so/[0-9a-f]{32}$")
        self.assertEqual(out["page_id"], ne.extract_page_id(out["url"]))
        self.assertEqual(out["hub_id"], hub_id)
        self.assertEqual(self.fake.pages[out["url"].rsplit("/", 1)[1]]["title"], "Weekly sync")

    def test_with_a_hub_the_page_goes_straight_under_it(self):
        hub = self.make_page("Gusan meetings")
        out = self.send(hub=hub, parent=hub)
        creates = self.fake.calls("notion-create-pages")
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["parent"]["page_id"], ne.extract_page_id(hub))
        self.assertEqual(out["hub_id"], ne.extract_page_id(hub))
        self.assertEqual(self.persist.calls, [])

    def test_the_hub_alone_is_enough(self):
        hub = self.make_page()
        self.send(hub=hub)
        self.assertEqual(self.fake.calls("notion-create-pages")[0]["parent"]["page_id"], ne.extract_page_id(hub))

    def test_a_page_the_user_picked_gets_the_notes(self):
        picked, hub = self.make_page("Picked"), self.make_page("Gusan meetings")
        out = self.send(hub=hub, parent=picked)
        self.assertEqual(self.fake.calls("notion-create-pages")[0]["parent"]["page_id"], ne.extract_page_id(picked))
        self.assertEqual(out["hub_id"], "")

    def test_only_the_notes_when_there_is_no_transcript(self):
        self.send(hub=self.make_page(), transcript="")
        self.assertNotIn("Transcript", self.fake.calls("notion-create-pages")[0]["pages"][0]["content"])

    def test_a_gone_hub_is_made_again_once_and_the_send_goes_on(self):
        hub = self.make_page("Gusan meetings")
        self.fake.trashed.add(hub)
        out = self.send(hub=hub, parent=hub)
        creates = self.fake.calls("notion-create-pages")
        self.assertEqual(len(creates), 3)                              # the failed try, the new hub, the page
        self.assertEqual(creates[1]["creation_mode"], "draft")
        new_hub = ne.extract_page_id(self.persist.keys()["notion_hub_page"])
        self.assertNotEqual(new_hub, ne.extract_page_id(hub))
        self.assertEqual(creates[2]["parent"]["page_id"], new_hub)
        self.assertEqual(out["hub_id"], new_hub)
        self.assertEqual(self.persist.keys()["notion_parent_page"], new_hub)

    def test_a_gone_hub_is_found_out_even_when_notion_words_it_differently(self):
        self.fake.trash_marker = True                                           # the fetch shows it in the trash
        self.fake.gone_text = "request failed (code 7)"                          # nothing our classifier knows
        hub = self.make_page("Gusan meetings")
        self.fake.trashed.add(hub)
        out = self.send(hub=hub, parent=hub)
        creates = self.fake.calls("notion-create-pages")
        self.assertEqual([c.get("creation_mode") for c in creates], [None, "draft", None])
        self.assertEqual(len(self.fake.calls("notion-fetch")), 1)
        self.assertNotEqual(out["hub_id"], ne.extract_page_id(hub))

    def test_a_hub_nobody_can_say_is_gone_is_never_swapped(self):
        # The create and the check both answer something we cannot read: that may be an outage,
        # so the meeting fails with the error and the user's hub stays the one notes go to.
        self.fake.gone_text = "request failed (code 7)"
        hub = self.make_page("Gusan meetings")
        self.fake.trashed.add(hub)
        with self.assertRaises(NotionError) as cm:
            self.send(hub=hub, parent=hub)
        self.assertEqual(cm.exception.code, "other")
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)
        self.assertEqual(len(self.fake.calls("notion-fetch")), 1)
        self.assertEqual(self.persist.calls, [])

    def test_a_passing_outage_never_makes_a_second_hub(self):
        # Notion answers 5xx to the create and to the check: before, the check's failure read as
        # "the page is gone", so a second "Gusan meetings" page was made and every later meeting went there.
        hub = self.make_page("Gusan meetings")
        creds = self.creds()
        client = nm.McpClient(creds, self.persist)
        self.assertIs(nm.hub_alive(client, hub), True)                     # the session is up and the hub is fine
        del self.fake.mcp_calls[:]
        self.fake.mcp_fail.extend([(503, {}), (503, {})])
        with self.assertRaises(NotionError) as cm:
            self.send(creds, hub=hub, parent=hub, client=client)
        self.assertEqual(cm.exception.code, "other")
        self.assertIn("Notion had a problem", str(cm.exception))
        self.assertEqual(self.fake.log.count("mcp:503"), 2)                 # the create, then the check
        self.assertEqual(self.fake.calls("notion-create-pages"), [])        # nothing after them: no second hub
        self.assertEqual(self.persist.calls, [])                            # ...and none saved
        self.assertEqual(len(self.fake.pages), 1)
        # Notion is back: the next send goes under the same hub
        out = self.send(creds, hub=hub, parent=hub, client=client)
        self.assertEqual(out["hub_id"], ne.extract_page_id(hub))
        self.assertEqual(self.fake.pages[out["page_id"].replace("-", "")]["parent"], ne.extract_page_id(hub))

    def test_an_unknown_failure_with_a_hub_that_is_fine_is_reported_not_swapped(self):
        hub = self.make_page("Gusan meetings")
        self.fake.rpc_error.append("request failed (code 7)")
        with self.assertRaises(NotionError) as cm:
            self.send(hub=hub, parent=hub)
        self.assertEqual(cm.exception.code, "other")
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)
        self.assertEqual(len(self.fake.calls("notion-fetch")), 1)                    # it asked, was told "fine", and stopped

    def test_a_validation_problem_is_never_mistaken_for_a_gone_hub(self):
        hub = self.make_page("Gusan meetings")
        self.fake.max_content = 10
        with patch.object(nm, "MAX_SHRINKS", 0):
            with self.assertRaises(NotionError):
                self.send(hub=hub, parent=hub)
        self.assertEqual(self.fake.calls("notion-fetch"), [])

    def test_a_page_the_user_picked_is_never_swapped(self):
        picked, hub = self.make_page("Picked"), self.make_page("Gusan meetings")
        self.fake.trashed.add(picked)
        with self.assertRaises(NotionNotFoundError) as cm:
            self.send(hub=hub, parent=picked)
        self.assertEqual(cm.exception.code, "no_access")
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)   # no new hub
        self.assertEqual(self.persist.calls, [])

    def test_a_hub_that_is_gone_again_is_not_chased_forever(self):
        hub = self.make_page("Gusan meetings")
        calls = []

        def always_gone(args):
            calls.append(args)
            if args.get("creation_mode") == "draft":
                return tool_result({"pages": [{"id": hex_id(0xfeed), "url": "https://www.notion.so/" + hex_id(0xfeed)}]})
            raise ToolFail("object_not_found: Could not find page")

        self.fake.tools["notion-create-pages"] = always_gone
        with self.assertRaises(NotionNotFoundError):
            self.send(hub=hub, parent=hub)
        self.assertEqual(len(calls), 3)                                    # fail, new hub, fail: then it says so

    def test_a_long_page_is_made_with_the_first_group_and_the_rest_is_appended_in_order(self):
        lines = ["- bullet number %03d with some words in it" % i for i in range(300)]
        with patch.object(nm, "CHUNK_CHARS", 3000):
            out = self.send(hub=self.make_page(), notes="## Summary\n" + "\n".join(lines), transcript="")
        appends = self.fake.calls("notion-update-page")
        self.assertGreater(len(appends), 2)
        for a in appends:
            self.assertEqual((a["command"], a["position"], a["allow_async"]), ("insert_content", {"type": "end"}, False))
            self.assertEqual(a["page_id"], out["page_id"])
            self.assertLessEqual(len(a["content"]), 3200)
        stored = self.fake.pages[out["url"].rsplit("/", 1)[1]]["content"]
        found = re.findall(r"bullet number (\d{3})", stored)
        self.assertEqual(found, ["%03d" % i for i in range(300)])         # nothing lost, nothing out of order

    def test_a_group_notion_calls_too_big_makes_nothing_and_is_tried_smaller(self):
        self.fake.max_content = 2500
        hub = self.make_page()
        words = "\n".join("Anna: line %03d of what was said in the meeting today." % i for i in range(150))
        out = self.send(hub=hub, notes="", transcript=words)
        creates = self.fake.calls("notion-create-pages")
        self.assertGreaterEqual(len(creates), 4)                          # 12000, 6000, 3000, 1500
        self.assertEqual(len([p for p in self.fake.pages.values() if p["title"] == "Weekly sync"]), 1)   # no half pages
        stored = self.fake.pages[out["url"].rsplit("/", 1)[1]]["content"]
        self.assertEqual(re.findall(r"line (\d{3}) of", stored), ["%03d" % i for i in range(150)])

    def test_a_group_that_is_too_big_to_append_is_split_in_two(self):
        self.fake.max_append = 4000
        lines = ["- bullet number %03d with some words in it" % i for i in range(300)]
        out = self.send(hub=self.make_page(), notes="\n".join(lines), transcript="")
        appends = self.fake.calls("notion-update-page")
        self.assertTrue(all(len(a["content"]) <= 4000 for a in appends))
        stored = self.fake.pages[out["url"].rsplit("/", 1)[1]]["content"]
        self.assertEqual(re.findall(r"bullet number (\d{3})", stored), ["%03d" % i for i in range(300)])

    def test_a_failure_after_the_page_exists_carries_its_link(self):
        self.fake.tools["notion-update-page"] = lambda args: (_ for _ in ()).throw(ToolFail("rate_limited: retry_after_seconds: 3"))
        lines = ["- bullet number %03d with some words in it" % i for i in range(300)]
        with patch.object(nm, "CHUNK_CHARS", 3000):
            with self.assertRaises(NotionError) as cm:
                self.send(hub=self.make_page(), notes="\n".join(lines), transcript="")
        e = cm.exception
        self.assertEqual(e.code, "rate")
        self.assertRegex(e.url, r"^https://www\.notion\.so/[0-9a-f]{32}$")
        self.assertIn("created", str(e))

    def test_an_auth_error_after_the_page_exists_is_not_an_auth_error_class(self):
        # A caller that refreshes and sends again on NotionAuthError would make a second page.
        self.fake.tools["notion-update-page"] = lambda args: (_ for _ in ()).throw(ToolFail("unauthorized"))
        lines = ["- bullet number %03d with some words in it" % i for i in range(300)]
        with patch.object(nm, "CHUNK_CHARS", 3000):
            with self.assertRaises(NotionError) as cm:
                self.send(hub=self.make_page(), notes="\n".join(lines), transcript="")
        self.assertNotIsInstance(cm.exception, NotionAuthError)
        self.assertEqual(cm.exception.code, "auth")
        self.assertTrue(cm.exception.url)

    # ── a workspace that has used up its free blocks ──
    def full_workspace(self, args):
        raise ToolFail(FULL_TOOL_TEXT)

    def test_a_full_workspace_is_its_own_error_for_the_page_and_for_the_hub(self):
        self.fake.tools["notion-create-pages"] = self.full_workspace
        for hub, parent in ((self.make_page("Gusan meetings"),) * 2, ("", "")):
            with self.assertRaises(NotionError) as cm:
                self.send(hub=hub, parent=parent)
            e = cm.exception
            self.assertEqual((e.code, e.reason, str(e)), ("full", "full", ne.FULL_TEXT))
            self.assertFalse(e.url)
        self.assertEqual(self.persist.calls, [])                           # no half-made hub is remembered
        # not mistaken for a page that is gone, so no new hub is chased
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 2)
        self.assertEqual(self.fake.calls("notion-fetch"), [])

    def test_the_tail_of_a_long_page_that_does_not_fit_keeps_the_link_and_says_full(self):
        self.fake.tools["notion-update-page"] = self.full_workspace
        lines = ["- bullet number %03d with some words in it" % i for i in range(300)]
        with patch.object(nm, "CHUNK_CHARS", 3000):
            with self.assertRaises(NotionError) as cm:
                self.send(hub=self.make_page(), notes="\n".join(lines), transcript="")
        e = cm.exception
        self.assertEqual(e.code, "full")
        self.assertRegex(e.url, r"^https://www\.notion\.so/[0-9a-f]{32}$")     # the page exists: its link is kept
        self.assertNotIsInstance(e, NotionAuthError)

    def test_nothing_to_send(self):
        with self.assertRaises(NotionError) as cm:
            nm.send_meeting_mcp(self.creds(), "t", "", "", "", persist=self.persist)
        self.assertIn("Nothing to send", str(cm.exception))
        self.assertEqual(self.fake.log, [])

    def test_the_title_is_cleaned(self):
        nm.send_meeting_mcp(self.creds(), "  Q3\n review\x07  ", "x", "", "", hub=self.make_page())
        self.assertEqual(self.fake.calls("notion-create-pages")[0]["pages"][0]["properties"]["title"], "Q3 review")
        nm.send_meeting_mcp(self.creds(), "", "x", "", "", hub=self.make_page())
        self.assertEqual(self.fake.calls("notion-create-pages")[1]["pages"][0]["properties"]["title"], "Meeting notes")

    def test_the_address_is_read_from_whatever_the_tool_says(self):
        hub = self.make_page()
        pid = hex_id(0xbeef)
        url = "https://www.notion.so/Weekly-sync-" + pid
        answers = [tool_result(text="Created 1 page: %s." % url),
                   tool_result({"pages": [{"id": pid}]}),
                   tool_result({"pages": [{"id": pid, "url": "http://evil.example/p"}]}),
                   tool_result({"pages": [{"url": url}]}, structured=True)]
        for answer in answers:
            self.fake.tools["notion-create-pages"] = lambda args, _a=answer: _a
            out = nm.send_meeting_mcp(self.creds(), "t", "x", "", "", hub=hub)
            self.assertEqual(out["page_id"], ne.extract_page_id(pid))
            self.assertTrue(out["url"].startswith("https://www.notion.so/"), out["url"])
            self.assertNotIn("evil", out["url"])

    def test_an_answer_with_no_page_in_it_is_an_error_not_a_guess(self):
        self.fake.tools["notion-create-pages"] = lambda args: tool_result(text="done")
        with self.assertRaises(NotionError) as cm:
            nm.send_meeting_mcp(self.creds(), "t", "x", "", "", hub=self.make_page())
        self.assertEqual(cm.exception.code, "other")

    def test_a_queued_answer_is_waited_for(self):
        pid = hex_id(0xcafe)
        polls = []

        def create(args):
            return tool_result({"async_task": {"task_id": "t1", "status": "queued"}})

        def poll(args):
            polls.append(args)
            if len(polls) < 3:
                return tool_result({"status": "running"})
            return tool_result({"status": "succeeded", "pages": [{"id": pid, "url": "https://www.notion.so/" + pid}]})

        self.fake.tools.update({"notion-create-pages": create, "notion-get-async-task": poll})
        out = nm.send_meeting_mcp(self.creds(), "t", "x", "", "", hub=self.make_page())
        self.assertEqual(out["page_id"], ne.extract_page_id(pid))
        self.assertEqual(polls, [{"task_id": "t1"}] * 3)
        self.assertEqual(self.sleeps, [nm.ASYNC_WAIT] * 3)

    def test_a_task_that_fails_or_never_ends_is_an_error(self):
        self.fake.tools["notion-create-pages"] = lambda args: tool_result({"async_task": {"task_id": "t1"}})
        self.fake.tools["notion-get-async-task"] = lambda args: tool_result({"status": "failed"})
        with self.assertRaises(NotionError):
            nm.send_meeting_mcp(self.creds(), "t", "x", "", "", hub=self.make_page())
        self.fake.tools["notion-get-async-task"] = lambda args: tool_result({"status": "running"})
        with self.assertRaises(NotionError):
            nm.send_meeting_mcp(self.creds(), "t", "x", "", "", hub=self.make_page())
        self.assertEqual(len(self.sleeps), 1 + nm.ASYNC_POLLS)

    def test_a_refresh_during_a_send_is_saved_before_the_page_is_made(self):
        c = self.creds(expires_at=time.time() + 30)
        nm.send_meeting_mcp(c, "t", "x", "", "", hub=self.make_page(), persist=self.persist)
        self.assertEqual(self.fake.log[:3], ["token:refresh_token", "persist", "mcp:initialize"])
        self.assertEqual(self.persist.calls[0]["notion_api_key"], c.access)

    def test_a_401_in_the_middle_of_a_send_is_refreshed_and_carried_on(self):
        c = self.creds()
        hub = self.make_page()
        self.fake.expire(c.access)
        out = nm.send_meeting_mcp(c, "t", "x", "", "", hub=hub, persist=self.persist)
        self.assertTrue(out["url"])
        self.assertEqual(self.persist.calls[0]["notion_reconnect"], False)
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)           # one page, not two


# ── notion_sync routes "mcp" and leaves the others alone ─────────────────────
class TestSyncRouting(NetCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = meeting_folder(self._tmp.name)
        self.updates = []

    def cfg(self, creds=None, **over):
        c = creds or self.creds()
        hub = self.make_page("Gusan meetings")
        cfg = {"notion_api_key": c.access, "notion_refresh_token": c.refresh, "notion_auth_mode": "mcp",
               "notion_token_expires": c.expires_at, "notion_mcp_client_id": "cid1",
               "notion_hub_page": hub, "notion_parent_page": hub, "notion_workspace_name": "W"}
        cfg.update(over)
        return cfg

    def meta(self):
        return ms.load_meta(self.folder)

    def test_a_manual_send_end_to_end(self):
        res = notion_sync.send_meeting(self.folder, self.cfg(), persist=self.persist)
        self.assertEqual((res["ok"], res["code"], res["sentence"]), (True, "sent", ""))
        self.assertRegex(res["url"], r"^https://www\.notion\.so/[0-9a-f]{32}$")
        meta = self.meta()
        self.assertEqual((meta["notion_url"], meta["notion_auto"]), (res["url"], False))
        self.assertEqual(meta["notion_page_id"], ne.extract_page_id(res["url"]))
        self.assertNotIn("notion_error", meta)
        page = self.fake.calls("notion-create-pages")[0]["pages"][0]
        self.assertEqual(page["properties"]["title"], "Weekly sync")
        self.assertIn("Thu 8 Oct · 10:15 · 25 min 00 s · Anna, David · Gusan", page["content"])
        self.assertIn("## Transcript", page["content"])
        self.assertIn(r"\- David: yes".replace("\\-", nm.ZERO_WIDTH + "-"), page["content"])

    def test_an_automatic_send_is_the_notes_only(self):
        res = notion_sync.send_meeting(self.folder, self.cfg(), auto=True, persist=self.persist)
        self.assertTrue(res["ok"])
        content = self.fake.calls("notion-create-pages")[0]["pages"][0]["content"]
        self.assertNotIn("Transcript", content)
        self.assertNotIn("Anna: We ship Friday", content)
        self.assertEqual(self.meta()["notion_auto"], True)
        again = notion_sync.send_meeting(self.folder, self.cfg(), auto=True, persist=self.persist)
        self.assertEqual((again["ok"], again["code"]), (False, "sent"))              # never twice
        self.assertEqual(len(self.fake.calls("notion-create-pages")), 1)

    def test_no_parent_page_is_needed(self):
        cfg = self.cfg(notion_parent_page="", notion_hub_page="")
        res = notion_sync.send_meeting(self.folder, cfg, persist=self.persist)
        self.assertTrue(res["ok"])
        self.assertEqual([c.get("creation_mode") for c in self.fake.calls("notion-create-pages")], ["draft", None])
        self.assertIn("notion_hub_page", self.persist.keys())

    def test_mcp_never_touches_the_rest_api_senders(self):
        boom = MagicMock(side_effect=AssertionError("the REST path was used"))
        res = notion_sync.send_meeting(self.folder, self.cfg(), publish=boom, refresh=boom, create_hub=boom,
                                       persist=self.persist)
        self.assertTrue(res["ok"])
        boom.assert_not_called()

    def test_the_sender_is_given_what_it_needs(self):
        seen = {}

        def mcp_send(creds, title, notes, transcript, line, *, parent, hub, persist):
            seen.update(creds=creds, title=title, notes=notes, transcript=transcript, line=line,
                        parent=parent, hub=hub, persist=persist)
            return {"url": "https://www.notion.so/" + hex_id(7), "page_id": ne.extract_page_id(hex_id(7)), "hub_id": ""}

        cfg = self.cfg(notion_parent_page="", notion_hub_page="")
        res = notion_sync.send_meeting(self.folder, cfg, persist=self.persist, mcp_send=mcp_send)
        self.assertTrue(res["ok"])
        self.assertEqual((seen["creds"].access, seen["creds"].refresh, seen["creds"].client_id),
                         (cfg["notion_api_key"], cfg["notion_refresh_token"], "cid1"))
        self.assertEqual((seen["title"], seen["parent"], seen["hub"]), ("Weekly sync", "", ""))
        self.assertEqual(seen["notes"], NOTES)
        self.assertIn("Anna: We ship Friday.", seen["transcript"])
        seen["persist"]({"notion_hub_page": "x"})                                       # forwarded to the caller's persist
        self.assertEqual(self.persist.calls[-1], {"notion_hub_page": "x"})

    def test_a_connection_notion_refuses_for_good(self):
        cfg = self.cfg()
        self.fake.valid.clear()
        self.fake.revoked = True
        res = notion_sync.send_meeting(self.folder, cfg, persist=self.persist)
        self.assertEqual((res["ok"], res["code"]), (False, "auth"))
        self.assertEqual(res["sentence"], notion_sync.AUTH_OAUTH_TEXT)              # never mentions a secret
        self.assertTrue(self.persist.keys()["notion_reconnect"])
        self.assertEqual(self.meta()["notion_error"], "auth")
        self.assertNotIn("notion_url", self.meta())

    def test_a_send_that_was_running_when_the_user_disconnected_does_not_flag_a_reconnect(self):
        cfg = self.cfg()

        def mcp_send(creds, *args, **kw):
            nc.forget_tokens()                                      # Disconnect is pressed while this send runs
            raise NotionAuthError(ne.AUTH_AGAIN_TEXT)               # ... and Notion no longer honours the token

        res = notion_sync.send_meeting(self.folder, cfg, persist=self.persist, mcp_send=mcp_send)
        self.assertEqual((res["ok"], res["code"]), (False, "auth"))
        self.assertNotIn("notion_reconnect", self.persist.keys())
        self.assertIs(res["stale"], True)                           # so main's tray hint does not ask for a reconnect
        self.assertEqual(self.meta()["notion_error"], "auth")       # the meeting still says what happened to it

    def test_a_send_that_was_running_when_the_user_connected_again_does_not_flag_the_new_connection(self):
        # Disconnect + Connect while an automatic send is in flight: the old token is refused, and the
        # fresh connection must not read "Reconnect" (that would stop every automatic send after it).
        cfg = self.cfg()

        def mcp_send(creds, *args, **kw):
            nc.forget_tokens()                                      # Disconnect...
            nm._adopt_chain({"access_token": "at-new", "refresh_token": "rt-new", "expires_at": time.time() + 9999})
            raise NotionAuthError(ne.AUTH_AGAIN_TEXT)               # ...Connect, and the old token is refused

        res = notion_sync.send_meeting(self.folder, cfg, auto=True, persist=self.persist, mcp_send=mcp_send)
        self.assertEqual((res["ok"], res["code"], res["stale"]), (False, "auth", True))
        self.assertEqual(self.persist.calls, [])

    def test_a_refused_token_flags_a_reconnect_when_nobody_disconnected(self):
        def mcp_send(creds, *args, **kw):
            raise NotionAuthError(ne.AUTH_AGAIN_TEXT)

        res = notion_sync.send_meeting(self.folder, self.cfg(), persist=self.persist, mcp_send=mcp_send)
        self.assertTrue(self.persist.keys()["notion_reconnect"])
        self.assertIs(res["stale"], False)

    def test_only_a_refused_token_says_whether_its_connection_is_stale(self):
        def raiser(*a, **k):
            raise NotionError("x", code="rate")

        res = notion_sync.send_meeting(self.folder, self.cfg(), persist=self.persist, mcp_send=raiser)
        self.assertEqual(set(res), {"ok", "url", "code", "sentence"})

    def test_the_other_failures_read_the_same_as_before(self):
        cases = ((NotionNotFoundError(ne.AUTH_AGAIN_TEXT), "no_access"), (NotionError("x", code="rate"), "rate"),
                 (NotionError("x", code="offline"), "offline"), (NotionError("x"), "other"), (RuntimeError("bug"), "other"))
        for exc, code in cases:
            def raiser(*a, _e=exc, **k):
                raise _e
            res = notion_sync.send_meeting(self.folder, self.cfg(), persist=self.persist, mcp_send=raiser)
            self.assertEqual((res["ok"], res["code"]), (False, code), exc)
            # (a page Notion won't let us use: "Pick another page" would send the user looking for
            # a picker this mode doesn't have, so it says to disconnect and connect again)
            self.assertEqual(res["sentence"], notion_sync.NO_ACCESS_MCP_TEXT if code == "no_access"
                             else notion_sync.SENTENCES[code])
            self.assertEqual(self.meta()["notion_error"], code)

    def test_a_partly_sent_page_keeps_its_link(self):
        def raiser(*a, **k):
            raise NotionError("later part failed", code="rate", url="https://www.notion.so/" + hex_id(9))

        res = notion_sync.send_meeting(self.folder, self.cfg(), persist=self.persist, mcp_send=raiser)
        self.assertEqual((res["ok"], res["url"], res["code"]), (False, "https://www.notion.so/" + hex_id(9), "rate"))
        self.assertEqual(self.meta()["notion_url"], "https://www.notion.so/" + hex_id(9))

    def test_a_full_workspace_says_so_in_one_calm_sentence_and_flags_nothing(self):
        def full(args):
            raise ToolFail(FULL_TOOL_TEXT)

        working = self.fake.tools["notion-create-pages"]
        self.fake.tools["notion-create-pages"] = full
        res = notion_sync.send_meeting(self.folder, self.cfg(), persist=self.persist)
        self.assertEqual((res["ok"], res["url"], res["code"], res["sentence"]), (False, "", "full", ne.FULL_TEXT))
        self.assertEqual(self.meta()["notion_error"], "full")
        self.assertNotIn("notion_url", self.meta())
        self.assertNotIn("notion_reconnect", self.persist.keys())           # the connection is fine: nothing says "Reconnect"
        for word in ("Upgrade", "http", "free blocks", "Learn more"):         # Notion's own words and links never reach the user
            self.assertNotIn(word, res["sentence"])
        # after the user made room (or picked another workspace) the same click goes through and clears the error
        self.fake.tools["notion-create-pages"] = working
        again = notion_sync.send_meeting(self.folder, self.cfg(), persist=self.persist)
        self.assertTrue(again["ok"])
        self.assertNotIn("notion_error", self.meta())

    def test_a_full_workspace_when_even_the_first_page_cannot_be_made(self):
        def full(args):
            raise ToolFail(FULL_TOOL_TEXT)

        self.fake.tools["notion-create-pages"] = full
        res = notion_sync.send_meeting(self.folder, self.cfg(notion_parent_page="", notion_hub_page=""),
                                       persist=self.persist)
        self.assertEqual((res["code"], res["sentence"]), ("full", ne.FULL_TEXT))
        self.assertNotIn("notion_hub_page", self.persist.keys())

    def test_a_page_that_lost_its_tail_to_a_full_workspace_keeps_its_link_and_says_full(self):
        def full(args):
            raise ToolFail(FULL_TOOL_TEXT)

        self.fake.tools["notion-update-page"] = full
        lines = ["- bullet number %03d with some words in it" % i for i in range(300)]
        folder = meeting_folder(tempfile.mkdtemp(dir=self._tmp.name), notes="\n".join(lines), transcript="")
        with patch.object(nm, "CHUNK_CHARS", 3000):
            res = notion_sync.send_meeting(folder, self.cfg(), persist=self.persist)
        self.assertEqual((res["ok"], res["code"], res["sentence"]), (False, "full", ne.FULL_TEXT))
        self.assertRegex(res["url"], r"^https://www\.notion\.so/[0-9a-f]{32}$")
        self.assertEqual(ms.load_meta(folder)["notion_url"], res["url"])        # so an automatic retry cannot make a second page

    def test_a_persist_that_raises_does_not_break_the_send(self):
        def bad(updates):
            raise RuntimeError("settings are locked")

        c = self.creds(expires_at=time.time() + 30)
        res = notion_sync.send_meeting(self.folder, self.cfg(creds=c), persist=bad)
        self.assertTrue(res["ok"])

    def test_privacy_free_telemetry_shape_is_unchanged(self):
        res = notion_sync.send_meeting(self.folder, self.cfg(), persist=self.persist)
        self.assertEqual(set(res), {"ok", "url", "code", "sentence"})

    def test_the_old_modes_still_use_the_old_path(self):
        calls = []

        def publish(token, parent, title, notes, transcript="", meta_line="", *, oauth=False):
            calls.append((token, oauth))
            return "https://www.notion.so/Weekly-0123456789abcdef0123456789abcdef"

        boom = MagicMock(side_effect=AssertionError("the MCP path was used"))
        for mode, oauth in (("oauth", True), ("own", False), ("", False)):
            cfg = {"notion_api_key": "tok", "notion_auth_mode": mode, "notion_parent_page": hex_id(5)}
            folder = meeting_folder(tempfile.mkdtemp(dir=self._tmp.name))
            res = notion_sync.send_meeting(folder, cfg, publish=publish, mcp_send=boom)
            self.assertTrue(res["ok"], mode)
            self.assertEqual(calls[-1], ("tok", oauth))
        boom.assert_not_called()

    def test_a_page_notion_will_not_let_us_use_says_to_connect_again_in_mcp_mode(self):
        # nothing to pick in this mode: the old "Pick another page" would send the user looking for a picker;
        # and the token is fine, so Settings has no "Reconnect" button to point at - only Disconnect, then Connect
        exc = NotionNotFoundError("x")
        self.assertEqual(notion_sync.friendly(exc, "mcp"), ("no_access", notion_sync.NO_ACCESS_MCP_TEXT))
        self.assertEqual(notion_sync.NO_ACCESS_MCP_TEXT,
                         "Couldn't send to Notion. Disconnect and connect it in Meeting settings.")
        self.assertNotIn("Reconnect", notion_sync.NO_ACCESS_MCP_TEXT)
        self.assertEqual(notion_sync.friendly(exc, "oauth"), ("no_access", notion_sync.SENTENCES["no_access"]))
        self.assertEqual(notion_sync.friendly(exc, "own"), ("no_access", notion_sync.SENTENCES["no_access"]))
        self.assertIn("Pick another page", notion_sync.SENTENCES["no_access"])

    def test_friendly_wording_for_a_refused_mcp_token(self):
        self.assertEqual(notion_sync.friendly(NotionAuthError("x"), "mcp"), ("auth", notion_sync.AUTH_OAUTH_TEXT))
        self.assertEqual(notion_sync.friendly(NotionAuthError("x"), "own"), ("auth", notion_sync.AUTH_OWN_TEXT))
        self.assertNotIn("secret", notion_sync.AUTH_OAUTH_TEXT)

    def test_two_sends_at_once_make_one_hub_and_two_pages(self):
        other = meeting_folder(tempfile.mkdtemp(dir=self._tmp.name))
        cfg = self.cfg(notion_parent_page="", notion_hub_page="")
        results = []
        threads = [threading.Thread(target=lambda f=f: results.append(
            notion_sync.send_meeting(f, dict(cfg), persist=self.persist))) for f in (self.folder, other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertTrue(all(r["ok"] for r in results), results)
        modes = [c.get("creation_mode") for c in self.fake.calls("notion-create-pages")]
        self.assertEqual(sorted(m or "" for m in modes), ["", "", "draft"])           # one hub, two meetings


# ── what the sheet and main.py call ──────────────────────────────────────────
class TestExportHelpers(unittest.TestCase):
    def test_mcp_is_connected_with_a_token_alone(self):
        self.assertTrue(ne.is_connected({"notion_api_key": "t", "notion_auth_mode": "mcp"}))
        self.assertFalse(ne.is_connected({"notion_api_key": "", "notion_auth_mode": "mcp"}))
        self.assertFalse(ne.is_connected({"notion_api_key": "  ", "notion_auth_mode": "mcp"}))
        # the old modes still need a page
        for mode in ("oauth", "own", ""):
            self.assertFalse(ne.is_connected({"notion_api_key": "t", "notion_auth_mode": mode}), mode)
            self.assertTrue(ne.is_connected({"notion_api_key": "t", "notion_auth_mode": mode,
                                             "notion_parent_page": hex_id(5)}), mode)

    def test_connection_mode(self):
        self.assertEqual(ne.connection_mode({"notion_api_key": "t", "notion_auth_mode": "mcp"}), "mcp")
        self.assertEqual(ne.connection_mode({"notion_api_key": "t", "notion_auth_mode": "oauth"}), "oauth")
        self.assertEqual(ne.connection_mode({"notion_api_key": "t", "notion_auth_mode": "own"}), "own")
        self.assertEqual(ne.connection_mode({"notion_api_key": "t"}), "own")
        self.assertEqual(ne.connection_mode({"notion_api_key": "t", "notion_auth_mode": "weird"}), "own")
        self.assertEqual(ne.connection_mode({"notion_auth_mode": "mcp"}), "")
        self.assertEqual(ne.GRANT_MODES, ("oauth", "mcp"))

    def test_what_disconnect_blanks(self):
        blank = ne.BLANK_SETTINGS
        self.assertEqual(blank["notion_token_expires"], 0)
        self.assertEqual(blank["notion_auth_mode"], "")
        self.assertIs(blank["notion_auto_send"], False)
        self.assertNotIn("notion_mcp_client_id", blank)            # kept: registering again orphans earlier grants

    def test_a_page_address_from_an_id(self):
        self.assertEqual(ne.page_url("01234567-89ab-cdef-0123-456789abcdef"),
                         "https://www.notion.so/0123456789abcdef0123456789abcdef")
        self.assertEqual(ne.page_url("nonsense"), "")
        self.assertEqual(ne.page_url(""), "")

    def test_the_new_keys_are_known_settings(self):
        import main
        self.assertEqual(main.DEFAULT["notion_mcp_client_id"], "")
        self.assertEqual(main.DEFAULT["notion_mcp_redirects"], "")             # "" = a client that knows only the loopback address
        self.assertEqual(main.DEFAULT["notion_token_expires"], 0)
        self.assertEqual(set(main._NOTION_SECRETS), {"notion_api_key", "notion_refresh_token"})   # no new secret in the config


class TestConnectApi(NetCase):
    def test_the_old_public_integration_is_untouched(self):
        self.assertEqual(nc.NOTION_CLIENT_ID, "")
        self.assertFalse(nc.configured())
        self.assertTrue(nc.mcp_available())

    def test_latest_tokens_and_forget_cover_both_chains(self):
        self.assertEqual(nc.latest_tokens(), {})
        c = self.creds()
        self.assertEqual(nc.latest_tokens()["access_token"], c.access)
        nc.forget_tokens()
        self.assertEqual(nc.latest_tokens(), {})
        self.assertEqual(nm.latest_tokens(), {})

    def test_disconnect_an_mcp_connection(self):
        cfg = {"notion_api_key": "at-old", "notion_refresh_token": "rt-old", "notion_auth_mode": "mcp",
               "notion_mcp_client_id": "cid1"}
        self.assertTrue(nc.disconnect(cfg))
        self.assertEqual(self.fake.token_requests[-1],
                         {"token": "rt-old", "token_type_hint": "refresh_token", "client_id": "cid1"})
        self.assertTrue(self.fake.revoked)

    def test_disconnect_gives_back_the_newest_pair(self):
        c = self.creds()
        cfg = {"notion_api_key": c.access, "notion_refresh_token": c.refresh, "notion_auth_mode": "mcp",
               "notion_mcp_client_id": "cid1"}
        nm.renew(c)                                                   # the config still holds the old pair
        nc.disconnect(cfg)
        self.assertEqual(self.fake.token_requests[-1]["token"], c.refresh)
        self.assertEqual(nc.latest_tokens(), {})

    def test_disconnect_a_pasted_secret_goes_nowhere(self):
        self.assertTrue(nc.disconnect({"notion_api_key": "ntn_x", "notion_auth_mode": "own"}))
        self.assertTrue(nc.disconnect({"notion_api_key": "ntn_x"}))
        self.assertTrue(nc.disconnect({}))
        self.assertEqual(self.fake.log, [])

    def test_disconnect_an_oauth_connection_uses_the_relay(self):
        with patch.object(nc, "revoke", return_value=True) as relay:
            self.assertTrue(nc.disconnect({"notion_api_key": "tok", "notion_auth_mode": "oauth"}))
        relay.assert_called_once_with("tok")
        self.assertEqual(self.fake.log, [])

    def test_disconnect_says_when_notion_could_not_be_told(self):
        cfg = {"notion_api_key": "at", "notion_refresh_token": "rt", "notion_auth_mode": "mcp", "notion_mcp_client_id": "cid1"}
        with patch.object(nm, "_request", MagicMock(side_effect=nm.NetworkError("x"))):
            self.assertFalse(nc.disconnect(cfg))
        with patch.object(nm, "revoke", MagicMock(side_effect=RuntimeError("boom"))):
            self.assertFalse(nc.disconnect(cfg))

    def test_revoke_mcp(self):
        self.assertTrue(nc.revoke_mcp("at", "rt", "cid1"))
        self.assertTrue(self.fake.revoked)

    def test_the_relay_code_still_forgets_the_mcp_pair_when_it_starts_a_new_chain(self):
        self.creds()
        nc.forget_tokens()
        self.assertEqual(nm.latest_tokens(), {})


# ── nothing secret is ever logged or said ────────────────────────────────────
@contextlib.contextmanager
def captured_logs():
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            lines.append(text)

    handler = Grab(level=logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        root.setLevel(old)


class TestNothingSecretIsLogged(NetCase):
    def test_a_whole_life_of_a_connection(self):
        said = []
        with captured_logs() as lines:
            res = self.browse_connect()
            c = nm.Credentials(res["token"], res["refresh"], res["expires_at"], res["client_id"])
            nm.renew(c, self.persist)
            old_refresh = res["refresh"]
            hub = self.make_page()
            nm.send_meeting_mcp(c, "Quarterly numbers for Sara", "## Summary\n- budget is $5,000 secret-notes", "Anna: secret-transcript",
                                "line", hub=hub, persist=self.persist)
            # failures, with the server saying things we must never repeat
            self.fake.token_fail.append((400, {"error": "invalid_grant", "error_description": "SECRET-DESC " + c.refresh}))
            try:
                nm.renew(c, self.persist)
            except ConnectError as e:
                said.append(str(e))

            def boom(args):
                raise ToolFail("object_not_found SECRET-TOOL-TEXT " + c.access)

            self.fake.tools["notion-update-page"] = boom
            client = nm.McpClient(c, self.persist)
            try:
                client.call_tool("notion-update-page", {"page_id": hub, "command": "insert_content", "content": "x"})
            except NotionError as e:
                said.append(str(e))
            self.fake.mcp_fail = [(500, {})]
            try:
                nm.McpClient(c, self.persist).call_tool("notion-get-self", {})
            except NotionError as e:
                said.append(str(e))
            nm.revoke(c.access, c.refresh, c.client_id)
            with patch.object(nm, "_request", MagicMock(side_effect=nm.NetworkError("https://x/?code=SECRET-URL"))):
                nm.revoke(c.access, c.refresh, c.client_id)
        text = "\n".join(lines + said)
        for word in list(SECRET_WORDS) + ["SECRET-DESC", c.access, c.refresh, old_refresh,
                                          "secret-notes", "secret-transcript", "Quarterly numbers", "SECRET-URL"]:
            self.assertNotIn(word, text, word)
        self.assertTrue(lines)                                   # it does log, in fixed words and numbers
        # ONE thing of the server's is logged on purpose: the start of a tool's own error message (what the
        # founder reads to learn why a page was not made) - with the tokens in it masked (see TestSnippet).
        # It is never part of what the user is told.
        self.assertIn("SECRET-TOOL-TEXT", "\n".join(lines))
        self.assertNotIn("SECRET-TOOL-TEXT", "\n".join(said))

    def test_the_url_the_user_opens_is_not_logged(self):
        urls = []
        with captured_logs() as lines:
            self.browse_connect(on_url=urls.append)
        self.assertTrue(urls)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(urls[0]).query))
        text = "\n".join(lines)
        self.assertNotIn(query["state"], text)
        self.assertNotIn(query["state"].split(".")[-1], text)             # nor the random part of a site return's
        self.assertNotIn(query["code_challenge"], text)
        for word in SECRET_WORDS:                                          # no code, no token, no verifier
            self.assertNotIn(word, text, word)

    def test_a_return_that_carries_a_code_is_never_logged_in_either_mode(self):
        for mode in ("ok", "404"):
            self.fake.site = mode
            del SECRET_WORDS[:]
            with captured_logs() as lines:
                self.browse_connect()
            text = "\n".join(lines)
            for word in SECRET_WORDS:
                self.assertNotIn(word, text, (mode, word))
            self.assertNotIn("code=", text)
            self.assertNotIn("state=", text)


# ── a workspace that has used up its free blocks ─────────────────────────────
class TestFullWorkspace(NetCase):
    """A free Notion workspace with more than one member has a total limit on blocks; when it is used up
    every create answers "Your workspace has used all of its free blocks. Upgrade your plan...". Nothing is
    wrong with the connection - the user has to make room, upgrade or pick another workspace."""

    def full_workspace(self, args):
        raise ToolFail(FULL_TOOL_TEXT)

    def test_notions_message_is_its_own_class_and_the_wording_may_vary(self):
        self.assertEqual(nm.classify_text(FULL_TOOL_TEXT), "full")
        for text in ("Your workspace has used all of its free blocks.", "Upgrade your plan to create more content.",
                     "You have reached the block limit for this workspace.", "UPGRADE PLAN", "The workspace is full",
                     "This workspace is out of blocks", "blocks limit reached"):
            self.assertEqual(nm.classify_text(text), "full", text)

    def test_the_other_classes_are_what_they_were(self):
        for text, kind in (("object_not_found: Could not find page with ID x", "gone"),
                           ("validation_error: content is too large", "validation"),
                           ("rate_limited: retry_after_seconds: 3", "rate"), ("restricted_resource", "forbidden"),
                           ("unauthorized", "auth"), ("something odd", "other"), ("", "other"), (None, "other"),
                           ("Page was moved to the trash", "other"), ("free of charge", "other")):
            self.assertEqual(nm.classify_text(text), kind, text)

    def test_the_error_is_the_apps_own_sentence_and_none_of_notions_words(self):
        err = nm._tool_error(FULL_TOOL_TEXT)
        self.assertIsInstance(err, nm.McpError)
        self.assertEqual((err.code, err.reason, str(err)), ("full", "full", ne.FULL_TEXT))
        self.assertNotIsInstance(err, (nm.McpNotFound, NotionAuthError))
        for word in ("Upgrade", "http", "blocks", "Learn more", "plan"):
            self.assertNotIn(word, str(err), word)
        # the JSON-RPC route says the same
        self.fake.rpc_error.append(FULL_TOOL_TEXT)
        client = nm.McpClient(self.creds(), self.persist)
        with self.assertRaises(NotionError) as cm:
            client.call_tool("notion-create-pages", {"pages": []})
        self.assertEqual((cm.exception.code, str(cm.exception)), ("full", ne.FULL_TEXT))

    def test_a_full_workspace_ends_the_connect_with_its_own_sentence_and_gives_the_grant_back(self):
        self.fake.tools["notion-create-pages"] = self.full_workspace
        hits = []
        with self.assertRaises(ConnectError) as cm:
            self.browse_connect(hits=hits)
        self.assertEqual(cm.exception.code, "full")
        self.assertEqual(str(cm.exception), nc.MESSAGES["full"])
        self.assertEqual(str(cm.exception), ne.FULL_TEXT)
        self.assertTrue(self.fake.revoked)                                    # exactly as for any failed connect
        self.assertEqual(nm.latest_tokens(), {})
        self.assertEqual(self.persist.calls, [{"notion_mcp_client_id": "cid1", "notion_mcp_redirects": "2"}])
        landing = [h for h in hits if "landing_status" in h][0]["landing"]
        self.assertIn("That didn't work.", landing)                            # the tab says so too, in plain words
        self.assertNotIn("Connected", landing)

    def test_the_connect_log_names_the_class_and_the_start_of_what_notion_said(self):
        self.fake.tools["notion-create-pages"] = self.full_workspace
        with captured_logs() as lines:
            with self.assertRaises(ConnectError):
                self.browse_connect()
        text = "\n".join(lines)
        self.assertIn("answered an error (full)", text)
        self.assertIn("used all of its free blocks", text)                      # what the founder reads in the log
        self.assertNotIn("source=", text)                                       # ...but no address' query string
        self.assertNotIn("create_pages", text.replace("notion-create-pages", ""))

    def test_the_telemetry_words_are_the_existing_ones(self):
        """The analytics function does not list reason values (only event names and keys), so "full" is a
        value like any other string: nothing there to change, and it fits the 80-character cap."""
        self.assertLessEqual(len("full"), 80)
        self.assertEqual(ne.FULL_HINT, "That Notion workspace is full")

    def test_no_technical_word_and_no_blame(self):
        text = ne.FULL_TEXT
        for word in ("MCP", "OAuth", "token", "integration", "block", "error", "failed", "Upgrade plan"):
            self.assertNotIn(word, text)
        self.assertNotIn("you", text.lower())
        self.assertEqual(text, "That Notion workspace is full. Free up space or upgrade it.")
        self.assertLessEqual(len(text), 62)               # two lines of the notes toolbar's flash at the app's own size


class TestSnippet(NetCase):
    """What of a tool's error message reaches the log: one masked line, never a secret."""

    def test_an_address_loses_its_query_and_fragment(self):
        out = nm._snippet(FULL_TOOL_TEXT)
        self.assertIn("https://app.notion.com/notion-mcp", out)
        self.assertNotIn("?", out)
        self.assertNotIn("source=", out)
        self.assertNotIn("mcp_tool_upsell", out)
        self.assertEqual(nm._snippet("see http://x.example/a/b?code=SECRET#frag now"), "see http://x.example/a/b now")
        self.assertEqual(nm._snippet("go to https://x.example/?token=SECRET&x=1 now"), "go to https://x.example/ now")

    def test_a_query_with_a_quote_a_bar_or_a_bracket_in_it_is_cut_whole(self):
        """Whatever follows the first "?" of an address, up to the next space, is gone - also what comes
        after a character that used to end the cut (a quote, a bar, a bracket)."""
        for text in ("see https://x.example/cb?code=ab'cd|state=SECRET-TAIL end",
                     "see https://x.example/cb?a=(b)&state=SECRET-TAIL end",
                     "see https://x.example/cb#a[1]=SECRET-TAIL end",
                     'see https://x.example/cb?q="a"&state=SECRET-TAIL end'):
            out = nm._snippet(text)
            self.assertNotIn("SECRET-TAIL", out, text)
            self.assertNotIn("state=", out, text)
            self.assertTrue(out.startswith("see https://x.example/cb"), out)
            self.assertTrue(out.endswith(" end"), out)

    def test_secrets_by_name_by_shape_and_by_value_are_masked(self):
        for text in ("Authorization: Bearer abc123DEF", "access_token=abc123DEF", 'refresh_token: "abc123DEF"',
                     "client_secret = abc123DEF", "{'password': 'abc123DEF'}", "code_verifier=abc123DEF",
                     "bearer abc123DEF"):
            self.assertNotIn("abc123DEF", nm._snippet(text), text)
        self.assertNotIn("SHORT", nm._snippet("it said SHORT here", hide=("SHORT",)))
        self.assertEqual(nm._snippet("the pair at-4 and rt-3", hide=("at-4", "rt-3")), "the pair ... and ...")
        self.assertNotIn("a" * 30, nm._snippet("id " + "a" * 30))
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", nm._snippet("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K"))

    def test_masking_comes_before_the_cut(self):
        token = "T" * 40
        text = ("a " * 118) + token + " tail"                 # the token starts at 236: the cut falls inside it
        out = nm._snippet(text)
        self.assertLessEqual(len(out), 240)
        self.assertNotIn("TTTT", out)
        hidden = ("b " * 118) + "SECRETVALUE9"
        self.assertNotIn("SECRET", nm._snippet(hidden, hide=("SECRETVALUE9",)))

    def test_a_huge_message_never_keeps_the_worker_busy(self):
        """The masking passes once took minutes on a single run of a few tens of thousands of characters
        (a JWT-shaped pattern that started at every position): this is on the error path of a connect and
        of every send, so a long answer must cost next to nothing, and still be masked."""
        for text in ("a" * 1000000, "ab " * 400000, "x1.y2." * 200000, ("word " * 1000 + "k" * 5000 + " ") * 400):
            started = time.monotonic()
            out = nm._snippet(text, hide=("the-access-token", "the-refresh-token"))
            self.assertLess(time.monotonic() - started, 1.5, text[:12])
            self.assertLessEqual(len(out), 240)
            self.assertNotIn("a" * 24, out)
        self.assertEqual(nm._snippet("a" * 1000000), "...")
        # a held token is hidden wherever it sits in a big text, also past the part that is looked at
        big = "w " * 30000 + "the-access-token tail"
        self.assertNotIn("the-access", nm._snippet(big, hide=("the-access-token",)))

    def test_a_secret_cut_by_the_end_of_what_is_looked_at_is_not_left_half_visible(self):
        """Only the start of a long message is read, in whole words: a secret that straddles the end is dropped
        with its word, not shown in part."""
        secret = "SECRETVALUE-ABCDEFGHIJ-0123456789"
        for pad in (nm._SNIPPET_RAW - 12, nm._SNIPPET_RAW - 3, nm._SNIPPET_SCAN - 12, nm._SNIPPET_SCAN - 3):
            text = ("p " * (pad // 2)) + secret + " tail " * 50
            self.assertNotIn("SECRET", nm._snippet(text, limit=nm._SNIPPET_RAW), pad)
            self.assertNotIn("SECRET", nm._snippet(text), pad)

    def test_a_jwt_shaped_run_is_still_masked_where_it_starts_a_word(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dBjftJeZ4CVPmB92K"
        for text in ("jwt " + jwt + " end", "token:" + jwt, '"' + jwt + '"', "x." + jwt):
            self.assertNotIn("eyJzdWIi", nm._snippet(text), text)

    def test_one_line_and_never_an_error(self):
        self.assertEqual(nm._snippet("a\n\n  b\t c"), "a b c")
        self.assertEqual(nm._snippet(None), "")
        self.assertEqual(nm._snippet(5), "")
        self.assertEqual(nm._snippet("ab " * 2000, limit=10), "ab ab ab a")
        self.assertEqual(nm._snippet("ok", hide=(None, 5, "", "ab")), "ok")        # junk in ``hide`` is ignored

    def test_a_tool_that_echoes_the_token_and_a_code_logs_neither(self):
        c = self.creds()
        urlcode = "SECRET-CODE-IN-A-URL"

        def echo(args):
            raise ToolFail("object_not_found: see https://x.example/cb?code=%s&state=s1 for %s and %s" % (
                urlcode, c.access, c.refresh))

        self.fake.tools["notion-fetch"] = echo
        with captured_logs() as lines:
            with self.assertRaises(NotionError):
                nm.McpClient(c, self.persist).call_tool("notion-fetch", {"id": "x"})
        text = "\n".join(lines)
        for word in (urlcode, c.access, c.refresh, "state=s1"):
            self.assertNotIn(word, text, word)
        self.assertIn("object_not_found", text)


class TestConnectedTab(NetCase):
    """The browser tab is kept waiting until the connect is over, then says how it went."""

    def landing(self, hits):
        return [h for h in hits if "landing_status" in h][0]

    def test_it_waits_for_the_page_and_then_says_connected(self):
        hits, seen = [], []
        real = self.fake.tools["notion-create-pages"]

        def create(args):
            seen.append(any("landing_status" in h for h in hits))        # has the tab been answered yet?
            return real(args)

        self.fake.tools["notion-create-pages"] = create
        res = self.browse_connect(hits=hits)
        self.assertEqual(seen, [False])                                  # no: it is still waiting while the page is made
        page = self.landing(hits)
        self.assertEqual(page["landing_status"], 200)
        self.assertIn("Connected.", page["landing"])
        self.assertIn("You can close this tab and go back to Gusan.", page["landing"])
        self.assertNotIn("Almost done", page["landing"])
        self.assertTrue(res["token"])

    def test_every_way_it_can_fail_says_it_did_not_work(self):
        cases = []

        def rpc():
            self.fake.rpc_error.append("something broke")

        def refused():
            self.fake.token_fail.append((400, {"error": "invalid_grant"}))

        def busy():
            self.fake.token_fail.append((503, {"error": "temporarily_unavailable"}))

        def full():
            self.fake.tools["notion-create-pages"] = lambda args: (_ for _ in ()).throw(ToolFail(FULL_TOOL_TEXT))

        for name, arrange in (("the first page", rpc), ("a refused code", refused), ("a busy server", busy),
                              ("a full workspace", full)):
            hits = []
            arrange()
            with self.assertRaises(ConnectError):
                self.browse_connect(hits=hits)
            page = self.landing(hits)["landing"]
            self.assertIn("That didn't work.", page, name)
            self.assertIn("Go back to Gusan and try again.", page, name)
            self.assertNotIn("Connected", page, name)
            self.fake.tools["notion-create-pages"] = self.fake._create_pages
            self.fake.token_fail[:] = []
            self.fake.rpc_error[:] = []
            nm.forget_tokens()

    def test_a_cancel_while_the_page_is_made_says_it_did_not_work_too(self):
        cancel, hits = threading.Event(), []
        real = self.fake.tools["notion-create-pages"]
        self.fake.tools["notion-create-pages"] = lambda args: (cancel.set(), real(args))[1]
        with self.assertRaises(ConnectCancelled):
            self.browse_connect(cancel=cancel, hits=hits)
        self.assertIn("That didn't work.", self.landing(hits)["landing"])

    def test_a_bug_ends_the_tab_too(self):
        hits = []
        with patch.object(nm, "ensure_hub", MagicMock(side_effect=RuntimeError("bug"))):
            with self.assertRaises(RuntimeError):
                self.browse_connect(hits=hits)
        self.assertIn("That didn't work.", self.landing(hits)["landing"])

    def test_the_user_saying_no_in_notion_is_answered_at_once_as_before(self):
        hits = []
        with self.assertRaises(ConnectError):
            self.browse_connect(deny="access_denied", hits=hits)
        page = self.landing(hits)["landing"]
        self.assertIn("Cancelled.", page)
        self.assertIn("Nothing was connected.", page)

    def test_the_wait_has_a_cap_after_which_the_old_page_is_shown(self):
        hits = []
        real = self.fake.tools["notion-create-pages"]
        self.fake.tools["notion-create-pages"] = lambda args: (time.sleep(1.2), real(args))[1]
        with patch.object(nc, "HOLD_SECONDS", 0.3):
            res = self.browse_connect(hits=hits)
        self.assertTrue(res["token"])                                    # the connect itself went on and finished
        page = self.landing(hits)["landing"]
        self.assertIn("Almost done.", page)
        self.assertNotIn("Connected", page)
        self.assertEqual(nc.HOLD_SECONDS, 100)

    def test_the_listener_keeps_the_first_answer_waiting_and_answers_a_second_one_at_once(self):
        """A reload or a second tab: the first valid answer is the one that counts, and only it waits."""
        server = nm._bind_listener(0, "state-1")
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        worker.start()

        def stop():
            server.shutdown()
            server.server_close()
        self.addCleanup(stop)
        port, out = server.server_address[1], {}

        def get(name, query):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/notion/callback?" + urllib.parse.urlencode(query))
            r = c.getresponse()
            out[name] = (r.status, r.read().decode("utf-8"))
            c.close()

        first = threading.Thread(target=get, args=("first", {"code": "c1", "state": "state-1"}), daemon=True)
        first.start()
        self.assertTrue(server.done.wait(5))                              # the answer is taken at once...
        self.assertEqual(server.result, {"code": "c1"})
        time.sleep(0.2)
        self.assertNotIn("first", out)                                    # ...but its tab is still waiting
        get("second", {"code": "c2", "state": "state-1"})
        self.assertEqual(out["second"][0], 200)
        self.assertIn("Almost done.", out["second"][1])                   # it does not wait, and does not decide
        self.assertEqual(server.result, {"code": "c1"})
        nm._finish_tab(server, "connected")
        first.join(5)
        self.assertIn("Connected.", out["first"][1])
        get("third", {"code": "c3", "state": "state-1"})                  # after the end, a late tab hears how it went
        self.assertIn("Connected.", out["third"][1])
        get("wrong", {"code": "c4", "state": "other"})
        self.assertEqual(out["wrong"][0], 400)

    def test_the_tab_pages_carry_no_secret_and_ask_for_nothing(self):
        hits = []
        self.browse_connect(hits=hits)
        page = self.landing(hits)["landing"]
        for needle in ("http", "<script", "<link", "<img", "@import", "url(", "src=", "code", "state", "token"):
            self.assertNotIn(needle, page, needle)
        for word in SECRET_WORDS:
            self.assertNotIn(word, page)

    def test_the_old_relay_listener_has_no_held_request(self):
        server = nc._LoopbackServer(("127.0.0.1", 0), nc._Callback)
        self.addCleanup(server.server_close)
        self.assertFalse(getattr(server, "hold_tab", False))
        bound = nm._bind_listener(0, "s")
        self.addCleanup(bound.server_close)
        self.assertTrue(bound.hold_tab)
        self.assertEqual((bound.expected_state, bound.callback_path, bound.server_address[0]), ("s", "/notion/callback", "127.0.0.1"))


class TestConnectedTabLoopback(TestConnectedTab):
    site = "404"


# ── main.py's two small hunks ────────────────────────────────────────────────
class TestMainWiring(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        try:
            from tests import test_notion_owner as owner
        except ImportError:
            import test_notion_owner as owner
        self.owner = owner
        owner._Threads.started = []

    CONNECTION = {"notion_api_key": "tok-M", "notion_refresh_token": "ref-M", "notion_auth_mode": "mcp",
                  "notion_mcp_client_id": "cid-M", "notion_workspace_name": "M's workspace",
                  "notion_parent_page": hex_id(5), "notion_hub_page": hex_id(5), "notion_token_expires": 123.0,
                  "notion_auto_send": True}

    def test_another_account_signing_in_gives_the_mcp_grant_back_and_keeps_the_client_id(self):
        me = self.owner.controller("user-B", **self.CONNECTION, notion_owner="user-A")
        with patch.object(self.main.threading, "Thread", self.owner._Threads), \
                patch.object(nc, "forget_tokens"), patch.object(nc, "latest_tokens", return_value={}):
            me._reconcile_notion_owner()
        self.assertEqual(self.owner._Threads.started,
                         [(nc.revoke_mcp, ("tok-M", "ref-M", "cid-M"), "gusan-notion-revoke")])
        for key, blank in ne.BLANK_SETTINGS.items():
            self.assertEqual(me.cfg[key], blank, key)
        self.assertEqual(me.cfg["notion_mcp_client_id"], "cid-M")             # public, reused: never re-register
        self.assertFalse(ne.is_connected(me.cfg))

    def test_the_tokens_of_the_button_live_in_the_keyring_and_never_in_config_json(self):
        written = {}

        def fake_write(name, value):
            if value:
                written[name] = value
            else:
                written.pop(name, None)
            return True

        cfg = {**self.main.DEFAULT, **self.CONNECTION, "notion_api_key": "AT-secret-1", "notion_refresh_token": "RT-secret-1"}
        with patch.object(self.main.storage, "write_secret", side_effect=fake_write), \
                patch.object(self.main.storage, "atomic_write_json") as aw:
            self.main.save_config(cfg)
            disk = aw.call_args[0][1]
            self.assertEqual((disk["notion_api_key"], disk["notion_refresh_token"]), ("", ""))
            self.assertNotIn("secret-1", json.dumps(disk))
            names = (self.main.storage.NOTION_TOKEN_SECRET, self.main.storage.NOTION_REFRESH_SECRET)
            self.assertEqual([written.get(n) for n in names], ["AT-secret-1", "RT-secret-1"])
            # what is not a secret stays on disk: the mode, the client id, the expiry
            self.assertEqual((disk["notion_auth_mode"], disk["notion_mcp_client_id"], disk["notion_token_expires"]),
                             ("mcp", "cid-M", 123.0))
            self.assertEqual(cfg["notion_api_key"], "AT-secret-1")                 # the app keeps using the real value
            # Disconnect: the keyring is emptied, the client id stays
            cfg.update(ne.BLANK_SETTINGS)
            self.main.save_config(cfg)
            self.assertEqual([written.get(n) for n in names], [None, None])
            self.assertEqual(aw.call_args[0][1]["notion_mcp_client_id"], "cid-M")

    def test_a_config_from_1_9_loads_with_the_new_keys_and_no_connection(self):
        old = {"notion_api_key": "", "notion_auth_mode": "", "notion_parent_page": ""}
        loaded = {**self.main.DEFAULT, **old}
        self.assertEqual((loaded["notion_mcp_client_id"], loaded["notion_token_expires"]), ("", 0))
        self.assertFalse(ne.is_connected(loaded))
        self.assertEqual(ne.connection_mode(loaded), "")
        pasted = {**self.main.DEFAULT, "notion_api_key": "ntn_x", "notion_parent_page": hex_id(9)}
        self.assertEqual(ne.connection_mode(pasted), "own")                       # a setup from before OAuth still reads as "own"
        self.assertTrue(ne.is_connected(pasted))

    def test_it_revokes_the_newest_pair_when_the_saved_one_is_stale(self):
        me = self.owner.controller("user-B", **self.CONNECTION, notion_owner="user-A")
        newest = {"access_token": "tok-new", "refresh_token": "ref-new"}
        with patch.object(self.main.threading, "Thread", self.owner._Threads), \
                patch.object(nc, "forget_tokens"), patch.object(nc, "latest_tokens", return_value=newest):
            me._reconcile_notion_owner()
        self.assertEqual(self.owner._Threads.started[0][1], ("tok-new", "ref-new", "cid-M"))

    def test_a_pasted_secret_is_still_never_sent_anywhere(self):
        me = self.owner.controller("user-B", notion_api_key="ntn_x", notion_auth_mode="own",
                                   notion_parent_page=hex_id(5), notion_owner="user-A")
        with patch.object(self.main.threading, "Thread", self.owner._Threads), patch.object(nc, "forget_tokens"):
            me._reconcile_notion_owner()
        self.assertEqual(self.owner._Threads.started, [])

    def _app(self, **cfg):
        main = self.main
        settings = {"notion_api_key": "tok", "notion_auth_mode": "mcp", "notify_when_ready": True, **cfg}
        a = SimpleNamespace(cfg=settings, hints=[], applied=[], tracked=[], opened=[])
        a.notify_history_changed = lambda: None
        a.show_tray_hint = lambda t, m, on_click=None: a.hints.append((t, m))
        a.show_main = lambda route=None, **o: a.opened.append((route, o))
        a.main_win = SimpleNamespace(isVisible=lambda: False, isMinimized=lambda: False, current_route=lambda: "home")
        a.track = lambda event, props=None: a.tracked.append((event, props))
        a.set_setting = lambda k, v: a.applied.append((k, v)) or True
        a._quitting = False
        a._notion_auto_done = main.AppController._notion_auto_done.__get__(a)
        return a

    def test_a_refused_mcp_connection_asks_for_a_reconnect_with_the_same_words_as_oauth(self):
        a = self._app()
        a._notion_auto_done("/m", "Weekly sync", {"ok": False, "url": "", "code": "auth", "sentence": "", "stale": False})
        self.assertEqual(a.hints, [("Couldn't send to Notion", "Reconnect it in Meeting settings · Open")])
        self.assertEqual(a.tracked[0][1]["mode"], "mcp")
        self.assertEqual(a.tracked[0][1]["reason"], "auth")
        # The flag itself is notion_sync's to set, through persist (TestSyncRouting
        # .test_a_refused_token_flags_a_reconnect_when_nobody_disconnected): main never sets it.
        self.assertEqual(a.applied, [])

    def test_a_refused_token_of_a_connection_given_up_meanwhile_flags_nothing_and_asks_nothing(self):
        # Disconnect + Connect while an automatic send ran: notion_sync answers "auth" with "stale" and
        # persists nothing. Before, main set notion_reconnect itself - on the fresh, valid connection.
        for cfg in ({"notion_auth_mode": "mcp"},                                  # connected again
                    {"notion_api_key": "", "notion_auth_mode": ""}):               # just disconnected
            a = self._app(**cfg)
            a._notion_auto_done("/m", "Weekly sync", {"ok": False, "url": "", "code": "auth", "sentence": "",
                                                      "stale": True})
            self.assertEqual(a.applied, [], cfg)
            self.assertEqual(a.hints, [("Couldn't send to Notion", "Weekly sync · Open")], cfg)
            self.assertEqual(a.tracked[0][1]["reason"], "auth", cfg)

    def test_the_hint_for_a_page_notion_will_not_take_is_mode_aware(self):
        a = self._app()
        a._notion_auto_done("/m", "Weekly sync", {"ok": False, "url": "", "code": "no_access", "sentence": ""})
        # no "Reconnect it": with a token that works, Settings shows Manage and Disconnect, no Reconnect button
        self.assertEqual(a.hints, [("Couldn't send to Notion", "Disconnect and connect it in Meeting settings · Open")])
        self.assertEqual(a.applied, [])                                        # nothing here sets the flag
        a = self._app(notion_auth_mode="oauth")
        a._notion_auto_done("/m", "Weekly sync", {"ok": False, "url": "", "code": "no_access", "sentence": ""})
        self.assertEqual(a.hints, [("Couldn't send to Notion", "Pick another page in Meeting settings · Open")])

    def test_a_full_workspace_gets_its_own_calm_hint_and_asks_for_no_reconnect(self):
        a = self._app()
        a._notion_auto_done("/m", "Weekly sync", {"ok": False, "url": "", "code": "full", "sentence": ne.FULL_TEXT})
        self.assertEqual(a.hints, [("Couldn't send to Notion", "That Notion workspace is full · Open")])
        self.assertEqual(a.applied, [])                                        # the connection is fine
        self.assertEqual(a.tracked[0][1]["reason"], "full")
        self.assertEqual((a.tracked[0][0], a.tracked[0][1]["ok"]), ("meeting_exported", False))
        for mode in ("oauth", "own"):
            other = self._app(notion_auth_mode=mode)
            other._notion_auto_done("/m", "Weekly sync", {"ok": False, "url": "", "code": "full", "sentence": ""})
            self.assertEqual(other.hints, [("Couldn't send to Notion", "That Notion workspace is full · Open")], mode)
            self.assertEqual(other.applied, [], mode)

    def test_the_pasted_secret_wording_is_unchanged(self):
        a = self._app(notion_auth_mode="own")
        a._notion_auto_done("/m", "Weekly sync", {"ok": False, "url": "", "code": "auth", "sentence": ""})
        self.assertEqual(a.applied, [])
        self.assertEqual(a.hints[0][1], "Notion rejected the secret · Open")


# ── the module's shape ───────────────────────────────────────────────────────
class TestShape(unittest.TestCase):
    def imports(self, name):
        import ast
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                out.add(node.module or "")
        return out

    def test_notion_mcp_is_qt_free_and_has_no_windows_only_import(self):
        names = self.imports("notion_mcp.py")
        self.assertFalse({n for n in names if n.startswith(("PySide", "PyQt", "ui", "winreg", "ctypes", "main"))}, names)

    def test_it_is_reached_by_plain_import_statements(self):
        self.assertIn("notion_mcp", self.imports("notion_connect.py"))
        self.assertIn("notion_mcp", self.imports("notion_sync.py"))

    def test_the_public_names_the_sheet_builds_on(self):
        for name in ("connect", "revoke", "send_meeting_mcp", "ensure_hub", "renew", "Credentials", "McpClient",
                     "connection_settings", "latest_tokens", "forget_tokens", "discover", "classify_text"):
            self.assertTrue(callable(getattr(nm, name)), name)
        for name in ("mcp_available", "connect_mcp", "disconnect", "revoke_mcp", "latest_tokens", "forget_tokens"):
            self.assertTrue(callable(getattr(nc, name)), name)


if __name__ == "__main__":
    unittest.main()
