"""Local web app server. Serves the UI and a small JSON API; binds to 127.0.0.1 only.

    python -m app.server --profile personal            # plan with Bedrock, render on an AWS GPU
    python -m app.server --mode mock                   # no AWS at all: placeholder panoramas, for trying the UI

Safety: only the local machine can connect, requests must carry the Host header of this server (blocks DNS
rebinding), and every POST must carry the per-run token the page was served with (blocks other websites from
making your browser start GPU jobs). Starting a GPU render also needs an explicit confirm and only one runs at a time.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
import secrets
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from engine.currency import PER_USD
from engine.manifest import build_manifest
from engine.validation import BadRequest, clean_request as validate_request

from .jobs import Busy, GPU_HOURLY, INSTANCE_TYPES, JobManager, Project, estimate_cost, estimate_minutes

ROOT = Path(__file__).resolve().parent.parent
ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,80}$")
MAX_BODY = 20_000
DRAIN_LIMIT = 1_000_000  # larger bodies are not read at all; the connection is simply closed
STATIC_DIRS = {"web": ROOT / "web", "viewer": ROOT / "viewer"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Planner:
    """Turns a request into a manifest. Free-text briefs go to the Bedrock agents, structured inputs to the rules."""

    def __init__(self, profile: str | None = None):
        self.profile = profile
        self._llm = None

    def plan(self, request: dict) -> dict:
        brief = request.get("brief")
        if brief is not None:
            from .agents_bridge import plan_brief

            return plan_brief(self._get_llm(), brief)
        return build_manifest(request["budget"], request["area_sqm"], request["bhk"], request["style"],
                              request["currency"])

    def _get_llm(self):
        if self._llm is None:
            from engine.agents import bedrock_llm

            self._llm = bedrock_llm(self.profile)
        return self._llm


@dataclass
class Context:
    projects_dir: Path
    planner: Planner
    jobs: JobManager
    aws: object | None
    mode: str
    token: str
    profile: str | None = None
    port: int = 0


def clean_request(data: dict) -> dict:
    """Validate a /api/plan body, turning problems into a 400 the UI can show."""
    try:
        return validate_request(data)
    except BadRequest as err:
        raise ApiError(400, str(err)) from err


def list_projects(ctx: Context) -> list[dict]:
    rows = []
    for manifest_path in sorted(ctx.projects_dir.glob("*/manifest.json"), reverse=True):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows.append(summarise(manifest_path.parent, manifest))
        except (OSError, ValueError, KeyError):
            continue  # a half-written or foreign folder must not break the list
    return rows


def summarise(directory: Path, manifest: dict) -> dict:
    nodes = manifest["topology"]["nodes"]
    present = [n["room_id"] for n in nodes if (directory / n["asset_url"]).exists()]
    meta = manifest["metadata"]
    return {"id": manifest["project_id"], "rooms": len(nodes), "rooms_rendered": len(present), "rendered": present,
            "bhk": meta["bhk"], "area": meta["total_area_sqm"], "budget": meta["total_budget"],
            "currency": meta["currency"], "style": meta["aesthetic_theme"], "tier": meta["tier"],
            "design": meta.get("design_source", "rules"),
            "gpu_estimate": {"usd": estimate_cost(len(nodes)), "minutes": estimate_minutes(len(nodes))}}


def create_project(ctx: Context, manifest: dict) -> Project:
    manifest = json.loads(json.dumps(manifest))  # own copy
    project_id = f"{manifest['project_id']}-{time.strftime('%y%m%d%H%M%S')}"
    manifest["project_id"] = project_id
    directory = ctx.projects_dir / project_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return Project(project_id, directory, manifest)


def load_project(ctx: Context, project_id: str) -> Project:
    if not ID_RE.match(project_id):
        raise ApiError(404, "no such project")
    path = ctx.projects_dir / project_id / "manifest.json"
    if not path.is_file():
        raise ApiError(404, "no such project")
    return Project(project_id, path.parent, json.loads(path.read_text(encoding="utf-8")))


def import_project(ctx: Context, project_id: str) -> Project:
    """Copy a project that was rendered on AWS (for example by the command line) into the local app."""
    if ctx.aws is None:
        raise ApiError(400, "importing needs AWS (not available in mock mode)")
    if not ID_RE.match(project_id):
        raise ApiError(400, "invalid project id")
    manifest = json.loads(ctx.aws.get_manifest(project_id))
    directory = ctx.projects_dir / project_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    project = Project(project_id, directory, manifest)
    wanted = {f"renders/{project_id}/{p.relative_to(directory).as_posix()}": p for p in project.pano_paths().values()}
    for key in ctx.aws.list_panos(project_id):
        if key in wanted and not wanted[key].exists():
            ctx.aws.download(key, wanted[key])
    return project


class Handler(BaseHTTPRequestHandler):
    server_version = "InteriorApp/1.0"
    ctx: Context  # set on the subclass by make_server

    # --- plumbing ---------------------------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.ctx.port}", f"localhost:{self.ctx.port}")

    def _body(self) -> dict:
        if not self._raw:
            return {}
        try:
            return json.loads(self._raw)
        except ValueError as err:
            raise ApiError(400, "the body is not valid JSON") from err

    def _dispatch(self, method: str) -> None:
        self._raw = b""
        try:
            if method == "POST":
                # Read the body before answering anything, or an early rejection can reset the connection.
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY:
                    self.close_connection = True
                    if MAX_BODY < length <= DRAIN_LIMIT:
                        remaining = length  # read and discard it, or the client may see a reset instead of our answer
                        while remaining > 0:
                            chunk = self.rfile.read(min(65536, remaining))
                            if not chunk:
                                break
                            remaining -= len(chunk)
                    raise ApiError(413 if length > MAX_BODY else 400, "request too large or malformed")
                self._raw = self.rfile.read(length) if length else b""
            if not self._host_ok():
                raise ApiError(403, "unexpected Host header")
            if method == "POST" and not secrets.compare_digest(self.headers.get("X-App-Token", ""), self.ctx.token):
                raise ApiError(403, "missing or wrong app token; reload the page")
            path = urlsplit(self.path).path
            (self._post if method == "POST" else self._get)(path)
        except ApiError as err:
            self._json(err.status, {"error": err.message})
        except Exception as err:  # noqa: BLE001 - never leak a traceback or hang the connection
            self._json(500, {"error": f"{err.__class__.__name__}: {err}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # --- GET --------------------------------------------------------------------------------------------------
    def _get(self, path: str) -> None:
        ctx = self.ctx
        if path == "/":
            html = (STATIC_DIRS["web"] / "index.html").read_text(encoding="utf-8").replace("__APP_TOKEN__", ctx.token)
            return self._send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/api/config":
            return self._json(200, {"mode": ctx.mode, "profile": ctx.profile, "instance_types": list(INSTANCE_TYPES),
                                    "gpu_hourly": GPU_HOURLY, "currencies": list(PER_USD)})
        if path == "/api/projects":
            return self._json(200, {"projects": list_projects(ctx)})
        if path == "/api/remote":
            if ctx.aws is None:
                return self._json(200, {"projects": []})
            local = {p["id"] for p in list_projects(ctx)}
            return self._json(200, {"projects": [i for i in ctx.aws.remote_projects() if i not in local]})
        if path == "/api/jobs/current":
            job = ctx.jobs.active()
            return self._json(200, {"job": job.to_dict() if job else None})
        if m := re.fullmatch(r"/api/jobs/([a-f0-9]{10})", path):
            job = ctx.jobs.get(m.group(1))
            if not job:
                raise ApiError(404, "no such job")
            return self._json(200, {"job": job.to_dict()})
        if m := re.fullmatch(r"/api/projects/([^/]+)", path):
            project = load_project(ctx, m.group(1))
            return self._json(200, {"manifest": project.manifest, "summary": summarise(project.dir, project.manifest)})
        if m := re.fullmatch(r"/p/([^/]+)/(manifest\.json|panos/[A-Za-z0-9_.-]+\.jpg)", path):
            project = load_project(ctx, m.group(1))
            return self._file(project.dir / m.group(2), project.dir)
        if m := re.fullmatch(r"/(web|viewer)/([A-Za-z0-9_.-]+)", path):
            return self._file(STATIC_DIRS[m.group(1)] / m.group(2), STATIC_DIRS[m.group(1)])
        raise ApiError(404, "not found")

    def _file(self, path: Path, base: Path) -> None:
        resolved = path.resolve()
        if base.resolve() not in resolved.parents or not resolved.is_file():
            raise ApiError(404, "not found")
        kind = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        if resolved.suffix in (".js", ".mjs"):
            kind = "text/javascript"
        self._send(200, resolved.read_bytes(), kind if not kind.startswith("text/") else f"{kind}; charset=utf-8")

    # --- POST -------------------------------------------------------------------------------------------------
    def _post(self, path: str) -> None:
        ctx = self.ctx
        if path == "/api/plan":
            request = clean_request(self._body())
            try:
                manifest = ctx.planner.plan(request)
            except Exception as err:  # agent or rule failures are shown to the user, not hidden
                raise ApiError(422, str(err) or err.__class__.__name__) from err
            project = create_project(ctx, manifest)
            return self._json(200, {"manifest": project.manifest, "summary": summarise(project.dir, project.manifest)})
        if m := re.fullmatch(r"/api/projects/([^/]+)/render", path):
            project = load_project(ctx, m.group(1))
            if self._body().get("confirm") is not True:
                raise ApiError(400, "confirm the GPU cost first")
            if all(p.exists() for p in project.pano_paths().values()):
                raise ApiError(409, "this project is already rendered")
            try:
                job = ctx.jobs.start(project)
            except Busy as err:
                raise ApiError(409, str(err)) from err
            return self._json(202, {"job": job.to_dict()})
        if m := re.fullmatch(r"/api/jobs/([a-f0-9]{10})/cancel", path):
            if not ctx.jobs.cancel(m.group(1)):
                raise ApiError(409, "that job is not running")
            return self._json(200, {"ok": True})
        if path == "/api/import":
            project_id = self._body().get("project_id", "")
            project = import_project(ctx, str(project_id))
            return self._json(200, {"summary": summarise(project.dir, project.manifest)})
        raise ApiError(404, "not found")


def make_server(ctx: Context, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctx": ctx})
    httpd = ThreadingHTTPServer((host, port), handler)
    ctx.port = httpd.server_address[1]
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="AWS CLI profile (Bedrock, EC2, S3)")
    parser.add_argument("--mode", choices=["gpu", "mock"], default="gpu",
                        help="gpu: real renders on AWS. mock: placeholder panoramas, no AWS")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    from .jobs import GpuRunner, MockRunner

    if args.mode == "gpu":
        from .aws import Aws

        aws = Aws(args.profile)
        runner, planner = GpuRunner(aws), Planner(args.profile)
    else:
        from .mock_planner import MockPlanner

        aws, runner, planner = None, MockRunner(), MockPlanner()
    ctx = Context(projects_dir=ROOT / "out" / "projects", planner=planner, jobs=JobManager(runner), aws=aws,
                  mode=args.mode, token=secrets.token_urlsafe(24), profile=args.profile)
    ctx.projects_dir.mkdir(parents=True, exist_ok=True)
    httpd = make_server(ctx, args.port)
    print(f"Interior 3D running at http://127.0.0.1:{ctx.port}  (mode: {args.mode})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
