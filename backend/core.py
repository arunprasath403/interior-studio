"""API logic for the public site, independent of Lambda so it can be tested with fakes.

Public (no code):      gallery, opening a design by its link, checking a render's progress.
Needs the access code: planning a new home, starting a render, cancelling one.

Protections for the paid part (rendering starts a GPU on the account owner's AWS bill): the access code, one render
at a time, a daily limit on renders and on plans, and a hard time limit per render. All are enforced here, not in the
browser.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass

from app.jobs import MAX_RUN_MINUTES, SETUP_MINUTES, derive_stage
from engine.validation import BadRequest, clean_request
from infra.launch import NoCapacity

ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,80}$")
FINAL_STATES = ("done", "failed", "cancelled")
STUDIO_STAGES = ("Starting the GPU machine", "Installing software", "Downloading model weights", "Loading the model")
EXPIRED_GRACE = 25  # seconds to let a last upload land after the machine stops


@dataclass
class Config:
    access_code: str = ""
    daily_render_cap: int = 6
    daily_plan_cap: int = 100
    instance_types: tuple = ("g6e.2xlarge", "g6e.xlarge")
    max_job_seconds: int = 2400      # a render that has not finished after 40 minutes is stopped
    max_wait_seconds: int = 1200     # give up waiting for free hardware after 20 minutes
    presign_seconds: int = 3600


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def friendly_error(raw: str | None) -> str:
    """What a visitor sees when a render fails. The raw reason stays in the job record for the owner."""
    text = (raw or "").lower()
    if "capacity" in text or "waiting" in text or "busy" in text:
        return "Our design studio is very busy right now. Please try again in a little while."
    if "too long" in text:
        return "This one is taking longer than expected, so we stopped it. Please try again."
    return "Something went wrong while creating your walkthrough. Please try again."


def phase_for(job: dict) -> str:
    """queue | studio | rooms | finishing | done | stopped: the stages the site shows, in plain terms."""
    state = job["state"]
    if state == "waiting":
        return "queue"
    if state in ("done",):
        return "done"
    if state in ("failed", "cancelled"):
        return "stopped"
    stage = job.get("stage", "")
    if stage in STUDIO_STAGES:
        return "studio"
    if stage.startswith("Rendering"):
        return "rooms"
    if stage == "Finishing up":
        return "finishing"
    return "studio"


class Backend:
    def __init__(self, store, aws, planner, config: Config, trigger=lambda: None, clock=time.time):
        self.store, self.aws, self.planner, self.config = store, aws, planner, config
        self.trigger, self.clock = trigger, clock

    # --- entry point -------------------------------------------------------------------------------------------
    def handle(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
        try:
            return self._route(method, path.rstrip("/") or "/", headers, body)
        except ApiError as err:
            return err.status, {"error": err.message}
        except BadRequest as err:
            return 400, {"error": str(err)}

    def _require_code(self, headers: dict) -> None:
        expected = self.config.access_code
        given = headers.get("x-access-code", "")
        if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
            raise ApiError(403, "That invitation code is not valid.")

    @staticmethod
    def _json(body: bytes) -> dict:
        if len(body) > 20_000:
            raise ApiError(413, "That request is too large.")
        if not body:
            return {}
        try:
            data = json.loads(body)
        except ValueError as err:
            raise ApiError(400, "That request could not be read.") from err
        if not isinstance(data, dict):
            raise ApiError(400, "That request could not be read.")
        return data

    def _route(self, method: str, path: str, headers: dict, body: bytes) -> tuple[int, dict]:
        if method == "GET":
            if path == "/api/health":
                return 200, {"ok": True}
            if path == "/api/gallery":
                return 200, {"homes": [self._gallery_card(p) for p in self.store.list_featured()]}
            if m := re.fullmatch(r"/api/projects/([^/]+)", path):
                return 200, self._project_view(self._project(m.group(1)))
            if m := re.fullmatch(r"/api/jobs/([a-f0-9]{10})", path):
                return 200, self._job_view(self._job(m.group(1)), refresh=True)
        if method == "POST":
            if path == "/api/access":
                self._require_code(headers)
                return 200, {"ok": True}
            if path == "/api/plan":
                self._require_code(headers)
                return 200, self._plan(self._json(body))
            if m := re.fullmatch(r"/api/projects/([^/]+)/render", path):
                self._require_code(headers)
                return 202, self._start_render(self._project(m.group(1)), self._json(body))
            if m := re.fullmatch(r"/api/jobs/([a-f0-9]{10})/cancel", path):
                self._require_code(headers)
                return 200, self._cancel(self._job(m.group(1)))
        raise ApiError(404, "Not found.")

    # --- projects ----------------------------------------------------------------------------------------------
    def _project(self, project_id: str) -> dict:
        project = self.store.get_project(project_id) if ID_RE.match(project_id) else None
        if not project:
            raise ApiError(404, "We couldn't find that design.")
        return project

    @staticmethod
    def _manifest(project: dict) -> dict:
        return json.loads(project["manifest"])

    def _rendered_rooms(self, project: dict) -> dict[str, str]:
        """room_id -> S3 key, for rooms whose panorama already exists."""
        manifest = self._manifest(project)
        present = set(self.aws.list_panos(project["id"]))
        found = {}
        for node in manifest["topology"]["nodes"]:
            key = f"renders/{project['id']}/{node['asset_url']}"
            if key in present:
                found[node["room_id"]] = key
        return found

    def _summary(self, project: dict, rendered: dict) -> dict:
        manifest = self._manifest(project)
        meta, nodes = manifest["metadata"], manifest["topology"]["nodes"]
        bedrooms = int(re.sub(r"\D", "", str(meta["bhk"])) or 1)
        return {"id": project["id"],
                "title": project.get("title") or f"{meta['aesthetic_theme'].title()} {bedrooms}-bedroom home",
                "style": meta["aesthetic_theme"], "bedrooms": meta["bhk"], "area_sqm": meta["total_area_sqm"],
                "budget": meta["total_budget"], "currency": meta["currency"], "spent": meta["spent"],
                "level": meta["tier"], "rooms": len(nodes), "rooms_ready": len(rendered),
                "estimated_minutes": round(SETUP_MINUTES + len(nodes) * 39 / 60)}

    def _gallery_card(self, project: dict) -> dict:
        rendered = self._rendered_rooms(project)
        card = self._summary(project, rendered)
        first = next(iter(rendered.values()), None)
        card["cover_url"] = self.aws.presign(first, self.config.presign_seconds) if first else None
        return card

    def _project_view(self, project: dict) -> dict:
        rendered = self._rendered_rooms(project)
        return {"manifest": self._manifest(project), "summary": self._summary(project, rendered),
                "images": {room: self.aws.presign(key, self.config.presign_seconds) for room, key in rendered.items()}}

    def _plan(self, data: dict) -> dict:
        request = clean_request(data)
        now = self.clock()
        if not self.store.bump("plan", time.strftime("%Y%m%d", time.gmtime(now)), self.config.daily_plan_cap, now):
            raise ApiError(429, "We've reached today's limit for new designs. Please come back tomorrow.")
        try:
            manifest = self.planner.plan(request)
        except Exception as err:  # noqa: BLE001 - the planner's messages are written for the user
            raise ApiError(422, str(err) or "We couldn't plan that home.") from err
        project_id = f"{manifest['project_id']}-{time.strftime('%y%m%d%H%M%S', time.gmtime(now))}-{secrets.token_hex(3)}"
        manifest["project_id"] = project_id
        project = {"id": project_id, "manifest": json.dumps(manifest), "created": now, "featured": False,
                   "ttl": int(now + 30 * 24 * 3600)}
        self.store.put_project(project)
        return self._project_view(project)

    # --- jobs --------------------------------------------------------------------------------------------------
    def _job(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            raise ApiError(404, "We couldn't find that job.")
        return job

    def _start_render(self, project: dict, data: dict) -> dict:
        if data.get("confirm") is not True:
            raise ApiError(400, "Please confirm to start.")
        if project.get("featured"):
            raise ApiError(409, "This design is already finished.")
        if len(self._rendered_rooms(project)) == len(self._manifest(project)["topology"]["nodes"]):
            raise ApiError(409, "This design is already finished.")
        now = self.clock()
        job_id = secrets.token_hex(5)
        if not self.store.acquire_lock("gpu", job_id, self.config.max_job_seconds + 300, now):
            raise ApiError(409, "Our design studio is busy with another home right now. Please try again in a few minutes.")
        if not self.store.bump("render", time.strftime("%Y%m%d", time.gmtime(now)), self.config.daily_render_cap, now):
            self.store.release_lock("gpu", job_id)
            raise ApiError(429, "We've reached today's limit for new walkthroughs. Please come back tomorrow.")
        total = len(self._manifest(project)["topology"]["nodes"])
        job = {"id": job_id, "project_id": project["id"], "state": "waiting", "stage": "Waiting for a GPU",
               "detail": "", "attempts": 0, "rooms_total": total, "rooms_done": 0, "created": now, "updated": now,
               "ttl": int(now + 7 * 24 * 3600)}
        self.store.put_job(job)
        self.trigger()  # start the first attempt now instead of at the next scheduled check
        return self._job_view(job)

    def _cancel(self, job: dict) -> dict:
        if job["state"] in FINAL_STATES:
            raise ApiError(409, "That walkthrough has already finished.")
        self.store.update_job(job["id"], {"cancel": True})
        self.advance(self._job(job["id"]), allow_launch=False)  # stop the machine now, not at the next check
        return {"ok": True}

    def _job_view(self, job: dict, refresh: bool = False) -> dict:
        if refresh and job["state"] == "running" and self.clock() - job["updated"] >= 3:
            job = self.advance(job, allow_launch=False)
        project = self.store.get_project(job["project_id"])
        rooms = [{"id": n["room_id"], "name": n["room_name"], "kind": n["kind"]}
                 for n in self._manifest(project)["topology"]["nodes"]] if project else []
        state = job["state"]
        return {"id": job["id"], "project_id": job["project_id"], "state": state, "phase": phase_for(job),
                "rooms_done": job["rooms_done"], "rooms_total": job["rooms_total"], "rooms": rooms,
                "elapsed": round(job["updated"] - job["created"]),
                "error": friendly_error(job.get("error")) if state == "failed" else None}

    # --- the state machine, advanced by the scheduler and by status checks -----------------------------------
    def tick(self) -> None:
        for job in self.store.active_jobs():
            try:
                self.advance(job, allow_launch=True)
            except Exception as err:  # noqa: BLE001 - one broken job must not stop the others
                self._finish(job, "failed", error=f"internal error: {err.__class__.__name__}: {err}")

    def _finish(self, job: dict, state: str, error: str | None = None, instance: str | None = None) -> dict:
        if instance:
            try:
                self.aws.terminate(instance)
            except Exception:  # noqa: BLE001 - the machine also shuts itself down; do not mask the real outcome
                pass
        fields = {"state": state, "updated": self.clock()}
        if error:
            fields["error"] = error
        if state == "done":
            fields["rooms_done"] = job["rooms_total"]
        self.store.update_job(job["id"], fields)
        self.store.release_lock("gpu", job["id"])
        return {**job, **fields}

    def advance(self, job: dict, allow_launch: bool) -> dict:
        if job["state"] in FINAL_STATES:
            return job
        now = self.clock()
        instance = job.get("instance_id")
        if job.get("cancel"):
            return self._finish(job, "cancelled", instance=instance)
        if now - job["created"] > self.config.max_job_seconds:
            return self._finish(job, "failed", error="took too long", instance=instance)
        project = self.store.get_project(job["project_id"])
        if job["state"] == "waiting":
            return self._try_launch(job, project, now) if allow_launch else job
        return self._follow(job, project, now)

    def _try_launch(self, job: dict, project: dict, now: float) -> dict:
        attempts = job["attempts"] + 1
        itype = self.config.instance_types[(attempts - 1) % len(self.config.instance_types)]
        try:
            launched = self.aws.launch(project["manifest"].encode(), itype, MAX_RUN_MINUTES)
        except NoCapacity as err:
            if now - job["created"] > self.config.max_wait_seconds:
                return self._finish(job, "failed", error=f"no capacity after {self.config.max_wait_seconds // 60} minutes: {err}")
            fields = {"attempts": attempts, "detail": str(err), "updated": now}
            self.store.update_job(job["id"], fields)
            return {**job, **fields}
        fields = {"state": "running", "attempts": attempts, "instance_id": launched["instance_id"],
                  "run": launched["run"], "instance_type": itype, "stage": "Starting the GPU machine",
                  "detail": "", "updated": now}
        self.store.update_job(job["id"], fields)
        return {**job, **fields}

    def _follow(self, job: dict, project: dict, now: float) -> dict:
        done = len(self._rendered_rooms(project))
        stage, detail = derive_stage(self.aws.log_text(job["run"]), done, job["rooms_total"])
        if done >= job["rooms_total"]:
            return self._finish({**job, "stage": stage}, "done")
        fields = {"rooms_done": done, "stage": stage, "detail": detail, "updated": now}
        state = self.aws.instance_state(job["instance_id"])
        if state in ("shutting-down", "terminated", "stopping", "stopped"):
            stopped_at = job.get("stopped_at") or now
            fields["stopped_at"] = stopped_at
            if now - stopped_at >= EXPIRED_GRACE:
                tail = [ln for ln in re.split(r"[\r\n]+", self.aws.log_text(job["run"])) if ln.strip()][-3:]
                return self._finish({**job, **fields}, "failed",
                                    error=f"machine stopped after {done}/{job['rooms_total']} rooms: {' | '.join(tail)[:300]}")
        self.store.update_job(job["id"], fields)
        return {**job, **fields}
