"""Run the whole site locally with no AWS: the real API logic, in-memory data and a simulated render.

    python -m backend.dev                # then open http://127.0.0.1:8770  (invitation code: demo-code)

The gallery shows the real panoramas from out/projects. A new design "renders" in about a minute: it waits for a
free space (two failed attempts, like a busy AWS), sets up, then produces one room every few seconds by copying a
real panorama. Nothing here touches AWS, so it costs nothing and is only for trying the site.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.mock_planner import MockPlanner
from infra.launch import NoCapacity

from .core import Backend, Config
from .store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
STORE = ROOT / "out" / "dev-store"
FEATURED = (("proj_fcb8467b", "Warm Scandinavian family home"), ("proj_3e3de056-261029091311", "Calm Japandi studio"))


class DevAws:
    """Stands in for backend.aws_io.CloudAws. Time since launch decides which stage a job is in."""

    SETUP, PER_ROOM = 14.0, 6.0

    def __init__(self, fail_launches: int = 2):
        self.fail_launches, self.attempts, self.runs = fail_launches, 0, {}
        self.sources = sorted((ROOT / "out" / "projects" / FEATURED[0][0] / "panos").glob("*.jpg"))
        self.lock = threading.Lock()

    def launch(self, manifest_bytes: bytes, instance_type: str, max_minutes: int) -> dict:
        self.attempts += 1
        if self.attempts <= self.fail_launches:
            raise NoCapacity("no capacity right now")
        manifest = json.loads(manifest_bytes)
        run = f"dev-{int(time.time())}"
        self.runs[run] = {"t0": time.time(), "manifest": manifest, "cancelled": False}
        return {"instance_id": f"i-{run}", "run": run, "zone": "dev"}

    def _run(self, instance_id: str) -> dict:
        return self.runs[instance_id.removeprefix("i-")]

    def _rooms_done(self, run: dict) -> int:
        nodes = run["manifest"]["topology"]["nodes"]
        return max(0, min(len(nodes), int((time.time() - run["t0"] - self.SETUP) // self.PER_ROOM) + 1)) if time.time() - run["t0"] >= self.SETUP else 0

    def _materialise(self, run: dict) -> None:
        manifest = run["manifest"]
        for i, node in enumerate(manifest["topology"]["nodes"][: self._rooms_done(run)]):
            target = STORE / "renders" / manifest["project_id"] / node["asset_url"]
            if not target.exists() and self.sources:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.sources[i % len(self.sources)], target)

    def list_panos(self, project_id: str) -> list[str]:
        for run in self.runs.values():
            if run["manifest"]["project_id"] == project_id and not run["cancelled"]:
                self._materialise(run)
        folder = STORE / "renders" / project_id / "panos"
        return sorted(f"renders/{project_id}/panos/{p.name}" for p in folder.glob("*.jpg")) if folder.exists() else []

    def presign(self, key: str, seconds: int) -> str:
        return f"/files/{key}"

    def instance_state(self, instance_id: str) -> str:
        run = self._run(instance_id)
        done = self._rooms_done(run) >= len(run["manifest"]["topology"]["nodes"])
        return "terminated" if run["cancelled"] or (done and time.time() - run["t0"] > self.SETUP + self.PER_ROOM * len(run["manifest"]["topology"]["nodes"]) + 3) else "running"

    def terminate(self, instance_id: str) -> None:
        self._run(instance_id)["cancelled"] = True

    def log_text(self, run_id: str) -> str:
        elapsed = time.time() - self.runs[run_id]["t0"]
        if elapsed < 3:
            return ""
        if elapsed < 6:
            return "Collecting uv"
        if elapsed < 9:
            return "DiT360 import OK\nFetching 23 files"
        if elapsed < self.SETUP:
            return "Loading pipeline components"
        return " 10/28 [00:13<00:24, 1.34s/it]"


def build_backend(access_code: str = "demo-code") -> Backend:
    store, aws = MemoryStore(), DevAws()
    for order, (folder, title) in enumerate(FEATURED, start=1):
        manifest_path = ROOT / "out" / "projects" / folder / "manifest.json"
        if not manifest_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        store.put_project({"id": manifest["project_id"], "manifest": json.dumps(manifest), "featured": True,
                           "order": order, "title": title, "created": 1})
        for node in manifest["topology"]["nodes"]:
            source = ROOT / "out" / "projects" / folder / node["asset_url"]
            target = STORE / "renders" / manifest["project_id"] / node["asset_url"]
            if source.exists() and not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
    return Backend(store, aws, MockPlanner(), Config(access_code=access_code, daily_render_cap=50, daily_plan_cap=500))


def make_handler(backend: Backend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _api(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload = backend.handle(method, urlsplit(self.path).path, headers, body)
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _file(self, base: Path, relative: str) -> None:
            path = (base / unquote(relative)).resolve()
            if base.resolve() not in path.parents or not path.is_file():
                return self._send(404, b"not found", "text/plain")
            kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if path.suffix in (".js", ".mjs"):
                kind = "text/javascript"
            self._send(200, path.read_bytes(), kind)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                return self._api("GET")
            if path.startswith("/files/"):
                return self._file(STORE, path.removeprefix("/files/"))
            if path == "/config.js":
                return self._send(200, b'window.STUDIO_API = "";\n', "text/javascript")
            self._file(SITE, "index.html" if path == "/" else path.lstrip("/"))

        def do_POST(self):
            self._api("POST") if urlsplit(self.path).path.startswith("/api/") else self._send(404, b"not found", "text/plain")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--code", default="demo-code")
    args = parser.parse_args()
    backend = build_backend(args.code)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(backend))

    def scheduler() -> None:  # stands in for the once-a-minute scheduler
        while True:
            time.sleep(2)
            try:
                backend.tick()
            except Exception as err:  # noqa: BLE001
                print("tick failed:", err)

    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Studio (local preview) at http://127.0.0.1:{args.port}   invitation code: {args.code}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
