"""MCP tools for the OPDS shelf.

Vercel does not run this file as a separate process. It loads the FastAPI app
in main.py, and install_mcp() attaches these tools at /mcp on that same app.

Locally, `python main.py` or `python mcp_server.py` starts both the website
and /mcp on one port.
"""

import os
import secrets
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from shelf import list_items, publish_upload, remove_upload

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

mcp = FastMCP(
    "Shelf",
    instructions=(
        "Personal OPDS bookshelf stored in a GitHub repository. "
        "list_books shows the catalog. "
        "add_book uploads a .epub, .txt, or .xtc file that already exists on the "
        "machine running this server (pass its full path) and updates catalog.xml "
        "in the same commit. On Vercel there is no local disk of books, so add "
        "files through the website uploader instead. "
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


@mcp.tool()
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
    """Upload a local book file and add it to the OPDS catalog."""
    raw = file_path.strip().strip('"').strip("'")
    path = Path(raw).expanduser()
    if not path.is_file():
        return {"ok": False, "error": f"No file at {path}"}
    try:
        return await publish_upload(path.name, path.read_bytes(), title)
    except HTTPException as exc:
        return _fail(exc)


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
    app.mount("/mcp", BearerGate(inner, token))


def main():
    import uvicorn

    from main import app

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
