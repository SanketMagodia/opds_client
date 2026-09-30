"""MCP tools for the OPDS shelf.

Vercel does not run this file as a separate process. It loads the FastAPI app
in main.py, and install_mcp() attaches these tools at /mcp on that same app.

Locally, `python main.py` or `python mcp_server.py` starts both the website
and /mcp on one port.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from shelf import MAX_BYTES, list_items, publish_upload, remove_upload

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
ON_VERCEL = bool(os.getenv("VERCEL"))
UPLOAD_LINK_TTL = 15 * 60

mcp = FastMCP(
    "Shelf",
    instructions=(
        "Personal OPDS bookshelf stored in a GitHub repository. "
        "list_books shows the catalog. "
        "This server runs on a different machine than you, so it cannot open "
        "file paths from your sandbox or the user's chat attachments. To add a "
        "book the user attached: call create_upload_link, then run the returned "
        "curl command in your own sandbox to send the file. If the book is "
        "available at a public http(s) URL, call add_book_from_url instead. "
        "Allowed types are .epub, .txt, and .xtc. "
        "remove_book deletes a catalog entry by the id from list_books and, when "
        "the file lives in the uploads folder, deletes that file too."
    ),
    host=HOST,
    port=PORT,
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    # Vercel sends Host: <project>.vercel.app. The localhost-only host check
    # would reject that. The bearer token is what guards /mcp.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _fail(exc: HTTPException) -> dict:
    return {"ok": False, "error": str(exc.detail)}


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "shelf"})


@mcp.tool()
async def list_books() -> dict:
    """List every book currently in the OPDS catalog."""
    try:
        return {"ok": True, "books": await list_items()}
    except HTTPException as exc:
        return _fail(exc)


async def add_book(
    file_path: Annotated[
        str,
        Field(description="Full path of the book file on the computer running this server."),
    ],
    title: Annotated[
        str,
        Field(description="Title to show in the catalog. Empty uses the file name."),
    ] = "",
) -> dict:
    """Add a book file to the OPDS catalog.

    If the server is remote, this returns a curl command to run where the file lives.
    """
    raw = file_path.strip().strip('"').strip("'")
    if ON_VERCEL:
        # The path is on the caller's machine, not ours. Hand back a command that
        # sends the file here, so clients with a stale tool list still succeed.
        link = await create_upload_link(Path(raw).name, title)
        if not link["ok"]:
            return link
        return {
            "ok": False,
            "error": (
                "This server cannot read files from your machine. Run the command "
                "below where the file lives to upload it, then call list_books."
            ),
            "run_this": f'curl -sS -F "file=@{raw}" "{link["upload_url"]}"',
            "expires_in_seconds": link["expires_in_seconds"],
        }
    path = Path(raw).expanduser()
    if not path.is_file():
        return {"ok": False, "error": f"No file at {path}"}
    try:
        return await publish_upload(path.name, path.read_bytes(), title)
    except HTTPException as exc:
        return _fail(exc)


# Kept on Vercel too: clients that cached the old tool list keep calling it.
mcp.tool()(add_book)


@mcp.tool()
async def add_book_from_url(
    url: Annotated[str, Field(description="Public http(s) URL of a .epub, .txt, or .xtc file.")],
    title: Annotated[
        str,
        Field(description="Title to show in the catalog. Empty uses the file name."),
    ] = "",
    filename: Annotated[
        str,
        Field(description="File name to store it as. Empty uses the last part of the URL."),
    ] = "",
) -> dict:
    """Download a book from a public URL and add it to the OPDS catalog."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "error": "URL must start with http:// or https://"}
    name = filename.strip() or unquote(Path(parsed.path).name)
    chunks, size = [], 0
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60) as c:
            async with c.stream("GET", url.strip()) as r:
                if r.status_code >= 400:
                    return {"ok": False, "error": f"Download failed: HTTP {r.status_code}"}
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        return {
                            "ok": False,
                            "error": f"File is larger than {MAX_BYTES // (1024 * 1024)} MB",
                        }
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Download failed: {exc}"}
    try:
        return await publish_upload(name, b"".join(chunks), title)
    except HTTPException as exc:
        return _fail(exc)


@mcp.tool()
async def create_upload_link(
    filename: Annotated[
        str,
        Field(description="File name to store, such as Alice.epub. Must end in .epub, .txt, or .xtc."),
    ],
    title: Annotated[
        str,
        Field(description="Title to show in the catalog. Empty uses the file name."),
    ] = "",
) -> dict:
    """Get a one-time upload URL for sending a book file from your own sandbox.

    Run the returned curl command where the file lives. The link expires in 15 minutes.
    """
    if not os.getenv("MCP_TOKEN", "").strip():
        return {"ok": False, "error": "MCP_TOKEN is not set on the server"}
    name = Path(filename.strip().strip('"').strip("'")).name
    url = f"{public_base_url()}/upload/{make_upload_token(name, title.strip())}"
    return {
        "ok": True,
        "upload_url": url,
        "expires_in_seconds": UPLOAD_LINK_TTL,
        "max_mb": MAX_BYTES // (1024 * 1024),
        "curl": f'curl -sS -F "file=@<path to {name}>" "{url}"',
    }


def public_base_url() -> str:
    explicit = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    # Set automatically by Vercel, without the scheme.
    host = os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL")
    if host:
        return f"https://{host}"
    return f"http://{HOST}:{PORT}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sign(body: str) -> str:
    key = os.getenv("MCP_TOKEN", "").strip().encode()
    return _b64(hmac.new(key, b"upload:" + body.encode(), hashlib.sha256).digest())


def make_upload_token(filename: str, title: str) -> str:
    claims = {"f": filename, "t": title, "e": int(time.time()) + UPLOAD_LINK_TTL}
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def read_upload_token(token: str) -> dict | None:
    if not os.getenv("MCP_TOKEN", "").strip() or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not secrets.compare_digest(sig, _sign(body)):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if claims.get("e", 0) < time.time():
        return None
    return claims


async def upload_with_link(request: Request) -> JSONResponse:
    """Receive a file for a link made by create_upload_link. Multipart or raw body."""
    claims = read_upload_token(request.path_params["token"])
    if claims is None:
        return JSONResponse({"ok": False, "error": "Upload link is invalid or expired"}, 401)
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            return JSONResponse({"ok": False, "error": "Send the book in a 'file' field"}, 400)
        data = await upload.read()
    else:
        data = await request.body()
    try:
        result = await publish_upload(claims["f"], data, claims["t"])
    except HTTPException as exc:
        return JSONResponse({"ok": False, "error": str(exc.detail)}, exc.status_code)
    return JSONResponse(result)


@mcp.tool()
async def remove_book(
    entry_id: Annotated[
        str,
        Field(description="Catalog entry id from list_books, such as urn:manual:Book.epub."),
    ],
) -> dict:
    """Remove a book from the OPDS catalog and delete its uploaded file."""
    try:
        return await remove_upload(entry_id.strip())
    except HTTPException as exc:
        return _fail(exc)


class BearerGate:
    """Require MCP_TOKEN on every request except the health check.

    Accepts Authorization: Bearer <token>, a raw Authorization value equal to
    the token, or ?token=<token> for connector UIs that only have a URL field.
    """

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if get_route_path(scope) == "/health":
            await self.app(scope, receive, send)
            return
        if not _authorized(scope, self.token):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _authorized(scope, expected: str) -> bool:
    if not expected:
        return False
    headers = {
        key.decode("latin1").lower(): value.decode("latin1")
        for key, value in scope.get("headers", [])
    }
    auth = headers.get("authorization", "").strip()
    if auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    else:
        supplied = auth
    if supplied and secrets.compare_digest(supplied, expected):
        return True
    query = parse_qs(scope.get("query_string", b"").decode("latin1"), keep_blank_values=False)
    supplied = (query.get("token") or [""])[0]
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def install_mcp(app) -> None:
    """Serve the MCP endpoint at /mcp on an existing FastAPI app."""
    token = os.getenv("MCP_TOKEN", "").strip()
    inner = mcp.streamable_http_app()
    stream = next(route.endpoint for route in inner.routes if getattr(route, "path", None) == "/")
    app.router.routes.append(Route("/mcp", endpoint=BearerGate(stream, token)))
    # Public route: the signed link itself is the credential.
    app.router.routes.append(Route("/upload/{token}", upload_with_link, methods=["POST", "PUT"]))
    app.mount("/mcp", BearerGate(inner, token))


def main():
    import uvicorn

    from main import app

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
