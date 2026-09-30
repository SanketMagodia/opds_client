import os
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_server import HOST, PORT, install_mcp, mcp
from shelf import list_items, publish_upload, remove_upload

APP_USER = os.environ["APP_USER"]
APP_PASS = os.environ["APP_PASS"]

security = HTTPBasic()


def auth(c: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest(c.username, APP_USER) and secrets.compare_digest(
        c.password, APP_PASS
    )
    if not ok:
        raise HTTPException(401, "Wrong login", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(_app):
    # The MCP session manager has to be running before /mcp can answer.
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan, dependencies=[Depends(auth)])


@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


@app.get("/api/items")
async def items():
    return await list_items()


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), title: str = Form("")):
    data = await file.read()
    result = await publish_upload(file.filename or "", data, title)
    return {"ok": True, "file": result["file"]}


@app.delete("/api/items")
async def delete(id: str):
    await remove_upload(id)
    return {"ok": True}


async def health(_request):
    return JSONResponse({"ok": True, "service": "shelf"})


# Plain Starlette route so this stays public. The shelf pages use the login above.
app.router.routes.append(Route("/health", health, methods=["GET"]))
install_mcp(app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
