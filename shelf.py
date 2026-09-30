"""GitHub OPDS catalog: list, add, and remove books in one commit."""

import base64
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import HTTPException

from txt2epub import txt_to_epub

load_dotenv()

OWNER = os.environ["GH_OWNER"]
REPO = os.environ["GH_REPO"]
BRANCH = os.getenv("GH_BRANCH", "main")
TOKEN = os.environ["GH_TOKEN"]
CATALOG_PATH = os.getenv("CATALOG_PATH", "catalog.xml")
FILES_DIR = os.getenv("FILES_DIR", "books").strip("/")
FILE_BASE_URL = os.getenv("FILE_BASE_URL") or (
    f"https://raw.githubusercontent.com/{OWNER}/{REPO}/{BRANCH}/"
)
# Vercel rejects request bodies over about 4.5 MB. Stay under that when deployed.
MAX_BYTES = 4 * 1024 * 1024 if os.getenv("VERCEL") else 25 * 1024 * 1024

MIME = {
    ".epub": "application/epub+zip",
    ".txt": "text/plain",
    ".xtc": "application/octet-stream",
}

ATOM = "http://www.w3.org/2005/Atom"
ACQ = "http://opds-spec.org/acquisition"
ET.register_namespace("", ATOM)
ET.register_namespace("dc", "http://purl.org/dc/terms/")
ET.register_namespace("opds", "http://opds-spec.org/2010/catalog")
A = lambda tag: f"{{{ATOM}}}{tag}"  # noqa: E731

API = f"https://api.github.com/repos/{OWNER}/{REPO}"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def gh(method: str, path: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(headers=HEADERS, timeout=60) as c:
        r = await c.request(method, API + path, **kw)
    if r.status_code >= 400:
        raise HTTPException(502, f"GitHub {r.status_code}: {r.text[:300]}")
    return r


async def head_sha() -> str:
    r = await gh("GET", f"/git/ref/heads/{BRANCH}")
    return r.json()["object"]["sha"]


async def read_catalog(ref: str) -> str:
    r = await gh(
        "GET",
        f"/contents/{CATALOG_PATH}",
        params={"ref": ref},
        headers={"Accept": "application/vnd.github.raw+json"},
    )
    return r.text


async def commit(changes: dict[str, bytes | None], message: str, parent: str):
    """Write several files in ONE commit. None means delete that path."""
    base_tree = (await gh("GET", f"/git/commits/{parent}")).json()["tree"]["sha"]
    tree = []
    for path, data in changes.items():
        if data is None:
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        blob = await gh(
            "POST",
            "/git/blobs",
            json={"content": base64.b64encode(data).decode(), "encoding": "base64"},
        )
        tree.append(
            {"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]}
        )
    new_tree = await gh("POST", "/git/trees", json={"base_tree": base_tree, "tree": tree})
    new_commit = await gh(
        "POST",
        "/git/commits",
        json={"message": message, "tree": new_tree.json()["sha"], "parents": [parent]},
    )
    # Not forced: if someone pushed in between, this fails instead of overwriting.
    await gh("PATCH", f"/git/refs/heads/{BRANCH}", json={"sha": new_commit.json()["sha"]})


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def acq_link(entry):
    for link in entry.findall(A("link")):
        if link.get("rel", "").startswith(ACQ):
            return link
    return None


def list_entries(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for e in root.findall(A("entry")):
        link = acq_link(e)
        items.append(
            {
                "id": e.findtext(A("id")),
                "title": e.findtext(A("title")),
                "updated": e.findtext(A("updated")),
                "href": link.get("href") if link is not None else None,
            }
        )
    return items


def add_entry(xml: str, title: str, filename: str, mime: str) -> bytes:
    root = ET.fromstring(xml)
    entry_id = f"urn:manual:{filename}"

    # Replace an older upload with the same file name.
    for e in root.findall(A("entry")):
        if e.findtext(A("id")) == entry_id:
            root.remove(e)

    ts = now()
    e = ET.Element(A("entry"))
    ET.SubElement(e, A("title")).text = title
    ET.SubElement(e, A("id")).text = entry_id
    ET.SubElement(e, A("updated")).text = ts
    ET.SubElement(
        e,
        A("link"),
        attrib={"rel": ACQ, "href": f"{FILE_BASE_URL}{FILES_DIR}/{filename}", "type": mime},
    )

    # Put new items at the top of the list.
    children = list(root)
    first_entry = next((i for i, c in enumerate(children) if c.tag == A("entry")), len(children))
    root.insert(first_entry, e)

    feed_updated = root.find(A("updated"))
    if feed_updated is not None:
        feed_updated.text = ts
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def remove_entry(xml: str, entry_id: str) -> tuple[bytes, str | None]:
    root = ET.fromstring(xml)
    repo_path = None
    for e in root.findall(A("entry")):
        if e.findtext(A("id")) == entry_id:
            link = acq_link(e)
            href = link.get("href", "") if link is not None else ""
            if href.startswith(FILE_BASE_URL):
                repo_path = href[len(FILE_BASE_URL):]
            root.remove(e)
            break
    else:
        raise HTTPException(404, "No item with that id")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), repo_path


async def list_items() -> list[dict]:
    return list_entries(await read_catalog(await head_sha()))


async def publish_upload(filename: str, data: bytes, title: str) -> dict:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "")
    ext = os.path.splitext(name)[1].lower()
    if ext not in MIME:
        raise HTTPException(400, f"Only {', '.join(MIME)} files are allowed")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"File is larger than {MAX_BYTES // (1024 * 1024)} MB")

    # E-ink readers like the Xteink skip .txt entries, so publish those as EPUB.
    if ext == ".txt":
        stem = os.path.splitext(name)[0]
        data = txt_to_epub(data, title.strip() or stem.replace("_", " "))
        name, ext = f"{stem}.epub", ".epub"

    parent = await head_sha()
    catalog = add_entry(await read_catalog(parent), title.strip() or name, name, MIME[ext])
    await commit(
        {f"{FILES_DIR}/{name}": data, CATALOG_PATH: catalog},
        f"Add {name} from uploader",
        parent,
    )
    return {
        "ok": True,
        "file": name,
        "id": f"urn:manual:{name}",
        "href": f"{FILE_BASE_URL}{FILES_DIR}/{name}",
    }


async def remove_upload(entry_id: str) -> dict:
    parent = await head_sha()
    catalog, repo_path = remove_entry(await read_catalog(parent), entry_id)
    changes: dict[str, bytes | None] = {CATALOG_PATH: catalog}
    # Only delete files that live in our uploads folder.
    if repo_path and repo_path.startswith(f"{FILES_DIR}/") and ".." not in repo_path:
        changes[repo_path] = None
    await commit(changes, f"Remove {entry_id} from uploader", parent)
    return {"ok": True, "id": entry_id}
