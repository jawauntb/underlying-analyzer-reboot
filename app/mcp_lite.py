"""mcp_lite: a site is its own MCP server in one call, and can reach the other
sites' MCPs. Standard library only (Python 3.9+); Flask and FastAPI/Starlette
adapters import their framework when you ask for them. The canonical copy is
lattice-animal's lib/mcp_lite.py; the other sites in the constellation carry
it verbatim and pass the same vectors (tests/fixtures/constellation-vectors.json).
It mirrors lib/mcp-lite.mjs, and the spec is docs/constellation.md.

    from mcp_lite import create_mcp, lattice_tool

    mcp = create_mcp(
        name="mysite",
        tools=[
            {"name": "hello", "description": "Say hello", "run": lambda args, ctx: "hello"},
            lattice_tool(),
        ],
    )
    mcp.flask(app)      # Flask
    mcp.fastapi(app)    # FastAPI or any Starlette app

That mounts POST /mcp (this site's own tools), POST /mcp/<peer> (another
site's MCP, relayed one hop deeper) and GET /.well-known/mcp.json (what a
client, or the lattice animal's embed, finds to learn the address).

Transport: Streamable HTTP, stateless. Every POST carries one JSON-RPC message
and gets one JSON reply; notifications get 202; there are no sessions and no
server-initiated stream, so GET and DELETE are 405.

The hop rule: every call between sites carries x-mcp-hop (how many sites have
already relayed it) and x-mcp-path (their names). A site makes an outbound
call only while the hop is under MAX_HOP, and sends hop + 1; at the limit its
pure tools still answer and anything that would call out says so. A call can go
around a loop (A to B to A) and is stopped at the limit; each site checks the
counter, and nothing here signs it.
"""

import asyncio
import inspect
import json
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

MCP_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_HOP = 2
HUB_URL = "https://latticeanimal-production.up.railway.app/mcp"

_PATH_MAX = 8
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_RESERVED = frozenset({"f", "meta", "peers", "well-known"})
_TEXT_MAX = 8000
_BODY_MAX = 64 * 1024
_REPLY_MAX = 256 * 1024
_INT_RE = re.compile(r"^\s*([+-]?\d+)")


def _clip(s, n):
    return ("" if s is None else str(s))[:n]


def _one_line(s, n):
    return _clip(re.sub(r"\s+", " ", "" if s is None else str(s)).strip(), n)


def _text(t, is_error=False):
    out = {"content": [{"type": "text", "text": _clip(t, _TEXT_MAX)}]}
    if is_error:
        out["isError"] = True
    return out


def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _fail(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def dumps(value):
    """Compact JSON, the way the other members write it."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# ---- the hop rule ----------------------------------------------------------


def _header(headers, key):
    if headers is None:
        return ""
    for k, v in headers.items():
        if str(k).lower() == key:
            if isinstance(v, (list, tuple)):
                v = v[0] if v else ""
            return "" if v is None else str(v)
    return ""


def _int(s):
    m = _INT_RE.match(s or "")
    return int(m.group(1)) if m else 0


def read_hop(headers):
    """The depth a call arrived at and the sites it passed through. The older
    x-lattice-hop header counts too, so the field-to-field rule keeps working."""
    n = max(
        _int(_header(headers, "x-mcp-hop")), _int(_header(headers, "x-lattice-hop"))
    )
    path = []
    for part in _header(headers, "x-mcp-path").split(">"):
        part = part.strip().lower()
        if _NAME_RE.match(part):
            path.append(part)
    return {"hop": max(0, min(9, n)), "path": path[:_PATH_MAX]}


def hop_headers(state=None, self_name=""):
    """The headers for a call this site makes one hop deeper."""
    state = state or {}
    nxt = max(0, min(9, int(state.get("hop", 0)))) + 1
    trail = [p for p in [*state.get("path", []), self_name] if p][-_PATH_MAX:]
    out = {"x-mcp-hop": str(nxt), "x-lattice-hop": str(nxt)}
    if trail:
        out["x-mcp-path"] = ">".join(trail)
    return out


# ---- replies ---------------------------------------------------------------


def parse_reply(res, id_=None):
    """One JSON-RPC reply out of a JSON or event-stream body: the matching id."""
    headers = res.get("headers") or {}
    kind = str(headers.get("content-type") or headers.get("Content-Type") or "").lower()

    def pick(m):
        if (
            isinstance(m, dict)
            and m.get("jsonrpc") == "2.0"
            and (id_ is None or m.get("id") == id_)
            and ("result" in m or "error" in m)
        ):
            return m
        return None

    body = res.get("text") or ""
    if "text/event-stream" in kind:
        for block in re.split(r"\r?\n\r?\n", body):
            data = "\n".join(
                line[5:].lstrip()
                for line in re.split(r"\r?\n", block)
                if line.startswith("data:")
            )
            if not data:
                continue
            try:
                m = pick(json.loads(data))
            except (ValueError, RecursionError):
                continue
            if m:
                return m
        return None
    try:
        j = json.loads(body or "null")
    except (ValueError, RecursionError):
        return None
    if isinstance(j, list):
        for m in j:
            x = pick(m)
            if x:
                return x
        return None
    return pick(j)


def result_text(result, max_len=_TEXT_MAX):
    """A tool result as plain text: text parts joined, anything else named."""
    if not isinstance(result, dict):
        return ""
    parts = result.get("content") if isinstance(result.get("content"), list) else []
    out = "\n".join(
        (str(p.get("text") or "") if p.get("type") == "text" else f"[{p['type']}]")
        for p in parts
        if isinstance(p, dict)
        and p.get("type")
        and (p.get("type") != "text" or p.get("text"))
    )
    structured = (
        dumps(result["structuredContent"])
        if not out and result.get("structuredContent")
        else ""
    )
    return _clip(out or structured, max_len)


def relay_result(r, who="the other site"):
    """A peer call's answer as a tool result: what came back, or why it did not."""
    if r and r.get("ok"):
        return r.get("text") or "(no text)"
    if r and r.get("isError"):
        return {
            "text": f"{who} reported an error: {r.get('text') or ''}",
            "isError": True,
        }
    return {
        "text": f"{who} did not answer ({(r or {}).get('reason') or 'failed'})",
        "isError": True,
    }


# ---- peers: the sites this one can reach -----------------------------------


def _loopback(host):
    return host in ("localhost", "127.0.0.1", "[::1]", "::1")


def vet_peer_url(raw, allow_local=False):
    """A peer URL is https (http only for loopback when allow_local, for a
    laptop or a test), with no credentials and no fragment; else None."""
    try:
        u = urlsplit(str(raw or "").strip())
        host = u.hostname or ""
        _ = u.port  # raises on a bad port
    except ValueError:
        return None
    if not host or u.username or u.password:
        return None
    if u.scheme == "http":
        if not (allow_local and _loopback(host)):
            return None
    elif u.scheme != "https":
        return None
    path = u.path or "/"
    return urlunsplit((u.scheme, u.netloc, path, u.query, ""))


def normalize_peers(peers, self_name="", allow_local=False):
    """{name: url} | {name: {url, about}} | [{name, url, about}] -> {name: row}.
    A peer with no url is listed as not configured, never guessed."""
    if isinstance(peers, (list, tuple)):
        rows = list(peers)
    else:
        rows = []
        for name, v in (peers or {}).items():
            rows.append(
                {"name": name, **v} if isinstance(v, dict) else {"name": name, "url": v}
            )
    out = {}
    for r in rows[:16]:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").lower()
        if (
            not _NAME_RE.match(name)
            or name in _RESERVED
            or name == self_name
            or name in out
        ):
            continue
        url = vet_peer_url(r["url"], allow_local) if r.get("url") else None
        out[name] = {
            "name": name,
            "url": url,
            "about": _one_line(r.get("about"), 240),
            "host": urlsplit(url).netloc if url else "",
        }
    return out


def peers_from_env(defaults, env=None):
    """Peer URLs from the environment: NAME_MCP_URL for each default (name
    upper-cased, dashes to underscores), and MCP_PEERS as a JSON array that
    adds or replaces entries."""
    env = env or {}
    rows = {}
    for d in defaults or []:
        rows[d["name"]] = dict(d)
    for name, r in rows.items():
        v = env.get(name.upper().replace("-", "_") + "_MCP_URL")
        if v:
            r["url"] = v
    try:
        extra = json.loads(env.get("MCP_PEERS") or "[]")
        for r in extra:
            if isinstance(r, dict) and isinstance(r.get("name"), str):
                key = r["name"].lower()
                rows[key] = {**rows.get(key, {}), **r}
    except (ValueError, TypeError, RecursionError):
        pass  # a bad MCP_PEERS is ignored
    return list(rows.values())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


async def plain_post(url, *, body, headers, timeout_ms):
    """A plain POST for peers the operator named: no redirects, capped reply, a
    deadline. Sites with a stricter guard pass their own (see create_peers)."""

    def run():
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.build_opener(_NoRedirect).open(
                req, timeout=timeout_ms / 1000
            ) as res:
                text = res.read(_REPLY_MAX + 1)[:_REPLY_MAX].decode("utf-8", "replace")
                return {
                    "ok": 200 <= res.status < 300,
                    "status": res.status,
                    "headers": {k.lower(): v for k, v in res.headers.items()},
                    "text": text,
                }
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                return {"ok": False, "reason": "redirect-refused", "status": e.code}
            return {
                "ok": False,
                "status": e.code,
                "headers": {k.lower(): v for k, v in e.headers.items()},
                "text": e.read(_REPLY_MAX).decode("utf-8", "replace"),
            }
        except (socket.timeout, TimeoutError):
            return {"ok": False, "reason": "timeout"}
        except urllib.error.URLError as e:
            return {
                "ok": False,
                "reason": "timeout"
                if isinstance(e.reason, (socket.timeout, TimeoutError))
                else "unreachable",
            }
        except Exception:
            return {"ok": False, "reason": "unreachable"}

    return await asyncio.to_thread(run)


class Peers:
    """The registry and the way out. `post(url, body=, headers=, timeout_ms=)`
    resolves {ok, status, headers, text} or {ok: False, reason}."""

    def __init__(
        self,
        self_name="",
        peers=None,
        post=None,
        allow_local=False,
        timeout_ms=25000,
        tools_ttl=300.0,
        daily_cap=500,
        now=time.time,
    ):
        self.self = self_name
        self._reg = normalize_peers(peers or {}, self_name, allow_local)
        self._post = post or plain_post
        self._timeout_ms = timeout_ms
        self._ttl = tools_ttl
        self._cap = daily_cap
        self._now = now
        self._seen = {}
        self._day = ["", 0]
        self._seq = 0

    def has(self, name):
        return str(name or "").lower() in self._reg

    def get(self, name):
        return self._reg.get(str(name or "").lower())

    def list(self):
        return [
            {
                "name": p["name"],
                "about": p["about"],
                "host": p["host"],
                "configured": bool(p["url"]),
            }
            for p in self._reg.values()
        ]

    def _budget(self):
        d = datetime.fromtimestamp(self._now(), tz=timezone.utc).strftime("%Y-%m-%d")
        if self._day[0] != d:
            self._day = [d, 0]
        if self._day[1] >= self._cap:
            return False
        self._day[1] += 1
        return True

    def _headers(self, ctx):
        return {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": MCP_VERSIONS[1],
            **hop_headers(ctx, self.self),
        }

    async def _rpc(self, peer, method, params, ctx):
        self._seq += 1
        id_ = self._seq
        res = await self._post(
            peer["url"],
            body=dumps(
                {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
            ),
            headers=self._headers(ctx),
            timeout_ms=self._timeout_ms,
        )
        m = parse_reply(res, id_)
        if not m:
            raise RuntimeError(
                res.get("reason")
                or (
                    f"http {res.get('status')}"
                    if res.get("ok") is False
                    else "no JSON-RPC reply"
                )
            )
        if m.get("error"):
            raise RuntimeError(
                _one_line((m["error"] or {}).get("message") or "error", 200)
            )
        return m.get("result")

    async def tools(self, name, ctx=None):
        """The tools one peer offers, short and cached."""
        ctx = ctx or {}
        p = self.get(name)
        if not p:
            return {"ok": False, "reason": "no-such-peer"}
        if not p["url"]:
            return {"ok": False, "reason": "not-configured"}
        c = self._seen.get(p["name"])
        if c and self._now() - c[1] < self._ttl:
            return {"ok": True, "tools": c[0]}
        if ctx.get("hop", 0) >= MAX_HOP:
            return {"ok": False, "reason": "too-deep"}
        if not self._budget():
            return {"ok": False, "reason": "daily-cap"}
        try:
            r = await self._rpc(p, "tools/list", {}, ctx)
            rows = (
                (r or {}).get("tools")
                if isinstance((r or {}).get("tools"), list)
                else []
            )
            tools = []
            for t in rows[:40]:
                t = t if isinstance(t, dict) else {}
                schema = (
                    t.get("inputSchema")
                    if isinstance(t.get("inputSchema"), dict)
                    else {}
                )
                req = (
                    schema.get("required")
                    if isinstance(schema.get("required"), list)
                    else []
                )
                tools.append(
                    {
                        "name": _clip(t.get("name"), 64),
                        "description": _one_line(t.get("description"), 300),
                        "required": req[:8],
                        "properties": list((schema.get("properties") or {}).keys())[
                            :12
                        ],
                    }
                )
            self._seen[p["name"]] = (tools, self._now())
            return {"ok": True, "tools": tools}
        except Exception as e:
            return {"ok": False, "reason": _one_line(str(e), 120) or "failed"}

    async def call(self, name, tool, args=None, ctx=None):
        """Call one tool on one peer, one hop deeper. Never raises."""
        ctx = ctx or {}
        p = self.get(name)
        if not p:
            return {"ok": False, "reason": "no-such-peer"}
        if not p["url"]:
            return {"ok": False, "reason": "not-configured"}
        if ctx.get("hop", 0) >= MAX_HOP:
            return {"ok": False, "reason": "too-deep"}
        if not self._budget():
            return {"ok": False, "reason": "daily-cap"}
        try:
            r = await self._rpc(
                p,
                "tools/call",
                {
                    "name": _clip(tool, 64),
                    "arguments": args if isinstance(args, dict) else {},
                },
                ctx,
            )
            r = r if isinstance(r, dict) else {}
            meta = (r.get("_meta") or {}).get("constellation")
            out = {
                "ok": not r.get("isError"),
                "isError": bool(r.get("isError")),
                "text": result_text(r),
            }
            if meta:
                out["meta"] = meta
            return out
        except Exception as e:
            return {"ok": False, "reason": _one_line(str(e), 120) or "failed"}

    async def relay(self, name, message, ctx=None):
        """Forward one JSON-RPC message to a peer as it is, one hop deeper.
        Returns (status, body) where body is the peer's own JSON-RPC reply."""
        ctx = ctx or {}
        p = self.get(name)
        id_ = message.get("id") if isinstance(message, dict) else None
        if not p:
            return 404, _fail(id_, -32000, "no such peer")
        if not p["url"]:
            return 200, _fail(
                id_, -32000, f"{p['name']} is not configured on this site"
            )
        if ctx.get("hop", 0) >= MAX_HOP:
            return 200, _fail(
                id_,
                -32001,
                f"too deep: this call has already been relayed {ctx.get('hop', 0)} times",
            )
        if not self._budget():
            return 200, _fail(id_, -32002, "too many relayed calls today")
        res = await self._post(
            p["url"],
            body=dumps(message),
            headers=self._headers(ctx),
            timeout_ms=self._timeout_ms,
        )
        if id_ is None:
            return 202, None
        m = parse_reply(res, id_)
        if m:
            return 200, m
        return 200, _fail(
            id_,
            -32003,
            f"{p['name']} did not answer ({res.get('reason') or 'http ' + str(res.get('status'))})",
        )


def create_peers(**kwargs):
    return Peers(**kwargs)


# ---- the manifest ----------------------------------------------------------


def build_manifest(
    name,
    version,
    title=None,
    description="",
    origin="",
    base="/mcp",
    tools=(),
    peers=(),
):
    return {
        "name": name,
        "title": title or name,
        "description": description or "",
        "version": version,
        "endpoint": f"{origin}{base}",
        "transport": "streamable-http",
        "stateless": True,
        "auth": "none",
        "protocolVersions": list(MCP_VERSIONS),
        "tools": [
            {
                "name": t["name"],
                "description": _one_line(t.get("description"), 300),
                "readOnly": t.get("readOnly", True) is not False,
            }
            for t in tools
        ],
        "peers": [
            {
                "name": p["name"],
                "endpoint": f"{origin}{base}/{p['name']}",
                "about": p.get("about") or "",
            }
            for p in peers
            if p.get("configured") is not False
        ],
        "hop": {"max": MAX_HOP, "headers": ["x-mcp-hop", "x-mcp-path"]},
    }


# ---- the server ------------------------------------------------------------


class ToolContext:
    """What a tool's run(args, ctx) gets: where it sits on the chain, and a way
    to call a peer one hop deeper (await ctx.call(peer, tool, args))."""

    __slots__ = ("hop", "path", "ip", "call")

    def __init__(self, hop, path, ip, call):
        self.hop = hop
        self.path = path
        self.ip = ip
        self.call = call


def _client_ip(headers, remote=""):
    """The address the platform's proxy saw: the last x-forwarded-for entry
    (one trusted hop, as Express's `trust proxy: 1`), else the socket's."""
    xff = [
        p.strip() for p in _header(headers, "x-forwarded-for").split(",") if p.strip()
    ]
    return xff[-1] if xff else (remote or "")


_NOT_HERE = "POST JSON-RPC to {base}; this server keeps no stream or session"


def _declared_too_big(headers):
    """True if the request says its body is over the cap: refuse it unread."""
    try:
        return int(_header(headers, "content-length") or 0) > _BODY_MAX
    except ValueError:
        return False


def _read_capped(read):
    """A body read through read(n), or None once it is over the cap (the rest
    is left unread)."""
    chunks, size = [], 0
    while True:
        chunk = read(min(65536, _BODY_MAX + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > _BODY_MAX:
            return None


def _parse_body(raw):
    """A request body as JSON, or None if it is missing, is not JSON, or is
    nested too deeply to read."""
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


class Mcp:
    def __init__(
        self,
        name,
        title=None,
        description="",
        version="1.0.0",
        instructions="",
        tools=(),
        peers=None,
        base="/mcp",
        origin="",
        post=None,
        allow_local=False,
        per_ip_per_min=40,
        now=time.time,
        timeout_ms=None,
        daily_cap=None,
        cors=False,
    ):
        if not _NAME_RE.match(str(name or "")):
            raise ValueError("create_mcp: name must be a short slug (a-z, 0-9, dashes)")
        self.name = name
        self.title = title or name
        self.description = description
        self.version = version
        self.instructions = instructions or description
        self.base = base
        self.origin = origin
        self.cors = cors
        self._now = now
        self._per_ip = per_ip_per_min
        self._seen_ip = {}
        self.tools_list = []
        for t in tools:
            self.tools_list.append(
                {
                    "name": str(t["name"]),
                    "description": str(t.get("description") or ""),
                    "inputSchema": t["inputSchema"]
                    if isinstance(t.get("inputSchema"), dict)
                    else {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "readOnly": t.get("readOnly", True) is not False,
                    "relay": bool(t.get("relay")),
                    "run": t["run"],
                }
            )
        self._by_name = {t["name"]: t for t in self.tools_list}
        opts = {
            "self_name": name,
            "peers": peers or {},
            "post": post,
            "allow_local": allow_local,
            "now": now,
        }
        if timeout_ms:
            opts["timeout_ms"] = timeout_ms
        if daily_cap:
            opts["daily_cap"] = daily_cap
        self.peers = Peers(**opts)
        self._public_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
                "annotations": {
                    "readOnlyHint": t["readOnly"],
                    "openWorldHint": t["relay"],
                },
            }
            for t in self.tools_list
        ]

    # -- pieces --

    def tools(self):
        return [dict(t) for t in self._public_tools]

    def manifest(self, origin=None):
        return build_manifest(
            self.name,
            self.version,
            self.title,
            self.description,
            self.origin if origin is None else origin,
            self.base,
            self.tools_list,
            self.peers.list(),
        )

    def _limited(self, ip):
        if not ip:
            return False
        t = self._now()
        seen = [x for x in self._seen_ip.get(ip, []) if t - x < 60]
        if len(seen) >= self._per_ip:
            self._seen_ip[ip] = seen
            return True
        seen.append(t)
        self._seen_ip[ip] = seen
        if len(self._seen_ip) > 5000:
            self._seen_ip.clear()
        return False

    @staticmethod
    def _normalize(out):
        if isinstance(out, dict) and isinstance(out.get("content"), list):
            return out
        if isinstance(out, str):
            return _text(out)
        if isinstance(out, dict) and isinstance(out.get("text"), str):
            return _text(out["text"], bool(out.get("isError")))
        return _text(dumps(out))

    async def _run_tool(self, t, args, ctx):
        run = t["run"]
        if inspect.iscoroutinefunction(run):
            out = await run(args, ctx)
        else:
            out = await asyncio.to_thread(run, args, ctx)
            if inspect.isawaitable(out):
                out = await out
        return out

    async def _rpc(self, m, ctx):
        if isinstance(m, list):
            return 400, _fail(None, -32600, "batches are not supported")
        if (
            not isinstance(m, dict)
            or m.get("jsonrpc") != "2.0"
            or not isinstance(m.get("method"), str)
        ):
            return 400, _fail(
                m.get("id") if isinstance(m, dict) else None, -32600, "invalid request"
            )
        if m.get("id") is None:
            return 202, None  # a notification
        p = m.get("params") if isinstance(m.get("params"), dict) else {}
        try:
            method = m["method"]
            if method == "initialize":
                v = p.get("protocolVersion")
                return 200, _ok(
                    m["id"],
                    {
                        "protocolVersion": v if v in MCP_VERSIONS else MCP_VERSIONS[0],
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": self.name,
                            "title": self.title,
                            "version": self.version,
                        },
                        "instructions": self.instructions,
                    },
                )
            if method == "ping":
                return 200, _ok(m["id"], {})
            if method == "tools/list":
                return 200, _ok(m["id"], {"tools": self._public_tools})
            if method == "tools/call":
                t = self._by_name.get(p.get("name"))
                if not t:
                    return 200, _fail(m["id"], -32602, f"unknown tool: {p.get('name')}")
                if self._limited(ctx["ip"]):
                    return 200, _ok(
                        m["id"],
                        _text(
                            "rate-limited: too many calls from here this minute; wait a little",
                            True,
                        ),
                    )
                if t["relay"] and ctx["hop"] >= MAX_HOP:
                    return 200, _ok(
                        m["id"],
                        _text(
                            f"too-deep: this call has already been relayed {ctx['hop']} times, so {self.name} will not call out again (limit {MAX_HOP})",
                            True,
                        ),
                    )
                hop_state = {"hop": ctx["hop"], "path": ctx["path"]}

                async def call(peer, tool, args=None):
                    return await self.peers.call(peer, tool, args, hop_state)

                tctx = ToolContext(ctx["hop"], ctx["path"], ctx["ip"], call)
                args = (
                    p.get("arguments") if isinstance(p.get("arguments"), dict) else {}
                )
                res = self._normalize(await self._run_tool(t, args, tctx))
                meta = {
                    **(res.get("_meta") or {}),
                    "constellation": {
                        "server": self.name,
                        "hop": ctx["hop"],
                        "path": [*ctx["path"], self.name],
                    },
                }
                return 200, _ok(m["id"], {**res, "_meta": meta})
            return 200, _fail(m["id"], -32601, f"method not found: {method}")
        except Exception:
            return 200, _ok(m["id"], _text("the tool failed", True))

    async def handle(
        self, method="GET", path="/", headers=None, body=None, ip="", origin=None
    ):
        """The whole server as one function of a plain request: a dict of
        status, headers and json; None when the path is not ours."""
        base = self.base
        p = re.sub(r"/+$", "", str(path)) or "/"
        if p in ("/.well-known/mcp.json", "/.well-known/mcp/server-card.json"):
            if method not in ("GET", "HEAD"):
                return {
                    "status": 405,
                    "headers": {"allow": "GET, HEAD"},
                    "json": _fail(None, -32000, "GET the manifest"),
                }
            return {
                "status": 200,
                "headers": {
                    "access-control-allow-origin": "*",
                    "cache-control": "public, max-age=300",
                },
                "json": self.manifest(self.origin if origin is None else origin),
            }
        cors = (
            {
                "access-control-allow-origin": "*",
                "access-control-allow-headers": "content-type, mcp-protocol-version, x-mcp-hop, x-mcp-path",
                "access-control-allow-methods": "POST, OPTIONS",
            }
            if self.cors
            else {}
        )
        if p == base:
            peer = ""
        elif p.startswith(base + "/") and "/" not in p[len(base) + 1 :]:
            peer = p[len(base) + 1 :].lower()
        else:
            return None
        if method == "OPTIONS" and self.cors:
            return {"status": 204, "headers": cors, "json": None}
        if method != "POST":
            return {
                "status": 405,
                "headers": {"allow": "POST"},
                "json": _fail(None, -32000, _NOT_HERE.format(base=base)),
            }
        ctx = {**read_hop(headers), "ip": ip}
        if peer == "":
            status, out = await self._rpc(body, ctx)
            return {"status": status, "headers": cors, "json": out}
        if not self.peers.has(peer):
            return {
                "status": 404,
                "headers": cors,
                "json": _fail(None, -32000, "no such peer"),
            }
        mid = body.get("id") if isinstance(body, dict) else None
        if self._limited(ip):
            return {
                "status": 200,
                "headers": cors,
                "json": _fail(
                    mid,
                    -32002,
                    "rate-limited: too many calls from here this minute; wait a little",
                ),
            }
        if (
            not isinstance(body, dict)
            or body.get("jsonrpc") != "2.0"
            or not isinstance(body.get("method"), str)
        ):
            return {
                "status": 400,
                "headers": cors,
                "json": _fail(mid, -32600, "invalid request"),
            }
        status, out = await self.peers.relay(
            peer, body, {"hop": ctx["hop"], "path": ctx["path"]}
        )
        return {"status": status, "headers": cors, "json": out}

    # -- adapters --

    def flask(self, app):
        """Mount on a Flask app. Register before any catch-all route."""
        from flask import Response, request

        mcp = self

        def view(peer=None):
            body = None
            if request.method == "POST" and not _declared_too_big(request.headers):
                body = _parse_body(_read_capped(request.stream.read))
            origin = (
                mcp.origin
                or f"{(request.headers.get('x-forwarded-proto') or request.scheme).split(',')[0].strip()}://{request.headers.get('x-forwarded-host') or request.host}"
            )
            out = asyncio.run(
                mcp.handle(
                    request.method,
                    request.path,
                    request.headers,
                    body,
                    _client_ip(request.headers, request.remote_addr or ""),
                    origin,
                )
            )
            if out is None:
                return Response(status=404)
            resp = Response(status=out["status"], headers=out.get("headers") or {})
            if out.get("json") is not None:
                resp.set_data(dumps(out["json"]))
                resp.headers["content-type"] = "application/json"
            return resp

        methods = ["GET", "POST", "DELETE", "OPTIONS"]
        slug = self.name.replace("-", "_")
        app.add_url_rule(
            self.base,
            endpoint=f"mcp_lite_{slug}_root",
            view_func=view,
            methods=methods,
            strict_slashes=False,
        )
        app.add_url_rule(
            self.base + "/<peer>",
            endpoint=f"mcp_lite_{slug}_peer",
            view_func=view,
            methods=methods,
            strict_slashes=False,
        )
        app.add_url_rule(
            "/.well-known/mcp.json",
            endpoint=f"mcp_lite_{slug}_manifest",
            view_func=view,
            methods=["GET"],
        )
        app.add_url_rule(
            "/.well-known/mcp/server-card.json",
            endpoint=f"mcp_lite_{slug}_card",
            view_func=view,
            methods=["GET"],
        )
        return self

    def fastapi(self, app):
        """Mount on a FastAPI or Starlette app. The routes go to the front of
        the table, so a catch-all static mount cannot shadow them."""
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        mcp = self

        async def view(request: Request):
            body = None
            if request.method == "POST" and not _declared_too_big(request.headers):
                chunks, size = [], 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > _BODY_MAX:
                        chunks = None
                        break
                    chunks.append(chunk)
                body = _parse_body(None if chunks is None else b"".join(chunks))
            origin = (
                mcp.origin
                or f"{(request.headers.get('x-forwarded-proto') or request.url.scheme).split(',')[0].strip()}://{request.headers.get('x-forwarded-host') or request.headers.get('host') or request.url.netloc}"
            )
            remote = request.client.host if request.client else ""
            out = await mcp.handle(
                request.method,
                request.url.path,
                request.headers,
                body,
                _client_ip(request.headers, remote),
                origin,
            )
            if out is None:
                return Response(status_code=404)
            if out.get("json") is None:
                return Response(
                    status_code=out["status"], headers=out.get("headers") or {}
                )
            return Response(
                content=dumps(out["json"]),
                status_code=out["status"],
                headers=out.get("headers") or {},
                media_type="application/json",
            )

        methods = ["GET", "POST", "DELETE", "OPTIONS"]
        routes = [
            Route(self.base, view, methods=methods),
            Route(self.base + "/{peer}", view, methods=methods),
            Route("/.well-known/mcp.json", view, methods=["GET"]),
            Route("/.well-known/mcp/server-card.json", view, methods=["GET"]),
        ]
        for r in reversed(routes):
            app.router.routes.insert(0, r)
        return self


def create_mcp(**kwargs):
    """create_mcp(name=, tools=[{name, description, inputSchema?, readOnly?,
    relay?, run(args, ctx)}], peers={name: url}, ...) -> Mcp. run may be sync
    or async and returns a string, a dict (sent as JSON text), {text, isError}
    or a full MCP result. A tool marked relay=True is one that calls a peer;
    the server refuses it at the hop limit before it runs."""
    return Mcp(**kwargs)


def lattice_tool(peer="lattice", name="ask_lattice_animals"):
    """The tool every site gets for reaching the lattice animals: ask them one
    question (they answer in their own voice, and may look things up)."""

    async def run(args, ctx):
        args = args if isinstance(args, dict) else {}
        question = args.get("question")
        question = question.strip() if isinstance(question, str) else ""
        if not question:
            return {
                "text": "ask something: a question of 1 to 600 characters",
                "isError": True,
            }
        if "to" in args and args["to"] not in ("field", "app", "connectome"):
            return {"text": "to must be field, app or connectome", "isError": True}
        payload = {"question": _clip(question, 600)}
        if args.get("to"):
            payload["to"] = args["to"]
        return relay_result(
            await ctx.call(peer, "ask_the_minds", payload), "the lattice animals"
        )

    return {
        "name": name,
        "description": "Ask the lattice animals (a field of small minds that become polyominoes by agreement; latticeanimal-production.up.railway.app) a question. They answer in their own voice and may search their repo and the web. One model call on their side; slow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "1 to 600 characters"},
                "to": {
                    "type": "string",
                    "enum": ["field", "app", "connectome"],
                    "description": "who answers (default: the field)",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        "readOnly": True,
        "relay": True,
        "run": run,
    }
