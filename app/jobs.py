"""Background render jobs: one at a time, with a human-readable stage the UI can show."""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

GPU_HOURLY = 2.242          # g6e.2xlarge on-demand, us-east-1 (AWS Pricing API); the xlarge is cheaper (about 1.86)
# Tried in turn when AWS has no capacity. Both have rendered end to end: with the low-RAM loader the 64 GiB 2xlarge
# peaked at 12.6 GB of RAM and the 32 GiB xlarge (cheaper, about 1.86/hour) at 6.6 GB.
INSTANCE_TYPES = ("g6e.2xlarge", "g6e.xlarge")
SETUP_MINUTES = 5.0         # boot, install, weights download and model load: the first room landed 4.5 minutes in
SECONDS_PER_ROOM = 39.0     # measured on an L40S
MAX_RUN_MINUTES = 45        # dead-man switch for render instances: they power off by then regardless


class Busy(Exception):
    """A render is already running."""


class Cancelled(Exception):
    pass


def estimate_cost(rooms: int, hourly: float = GPU_HOURLY) -> float:
    """Rough on-demand GPU cost of rendering ``rooms`` panoramas from a cold start."""
    return round((SETUP_MINUTES + rooms * SECONDS_PER_ROOM / 60) / 60 * hourly, 2)


def estimate_minutes(rooms: int) -> int:
    return round(SETUP_MINUTES + rooms * SECONDS_PER_ROOM / 60)


def derive_stage(log: str, rooms_done: int, rooms_total: int) -> tuple[str, str]:
    """Turn the GPU instance's log into a stage name and detail for the progress view."""
    if rooms_total and rooms_done >= rooms_total:
        return "Finishing up", "All rooms are rendered; the machine is shutting down."
    if rooms_done:
        return f"Rendering rooms ({rooms_done}/{rooms_total})", "About 40 seconds per room."
    if re.search(r"/28 \[", log):
        return "Rendering the first room", "The model is loaded and generating."
    if "Loading pipeline components" in log:
        return "Loading the model", "Moving the model onto the GPU."
    if "Fetching" in log or "DiT360 import OK" in log:
        return "Downloading model weights", "About 33 GB, usually a minute or two."
    if log.strip():
        return "Installing software", "Setting up Python and the model libraries."
    return "Starting the GPU machine", "Booting a fresh instance."


@dataclass
class Job:
    id: str
    project_id: str
    state: str = "queued"               # queued | running | done | failed | cancelled
    stage: str = "Queued"
    detail: str = ""
    attempts: int = 0
    rooms_total: int = 0
    rooms_done: int = 0
    instance_id: str | None = None
    run: str | None = None
    error: str | None = None
    started: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    def to_dict(self) -> dict:
        return {**asdict(self), "elapsed": round(self.updated - self.started)}


@dataclass
class Project:
    id: str
    dir: Path
    manifest: dict

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    def pano_paths(self) -> dict[str, Path]:
        return {n["room_id"]: self.dir / n["asset_url"] for n in self.manifest["topology"]["nodes"]}


class JobManager:
    def __init__(self, runner):
        self.runner = runner
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._cancel: dict[str, threading.Event] = {}

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.active), None)

    def start(self, project: Project) -> Job:
        with self._lock:
            if any(j.active for j in self._jobs.values()):
                raise Busy("a render is already running")
            job = Job(id=uuid.uuid4().hex[:10], project_id=project.id,
                      rooms_total=len(project.manifest["topology"]["nodes"]))
            self._jobs[job.id] = job
            self._cancel[job.id] = threading.Event()
        threading.Thread(target=self._work, args=(job, project), daemon=True).start()
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            event, job = self._cancel.get(job_id), self._jobs.get(job_id)
        if not event or not job or not job.active:
            return False
        event.set()
        return True

    def wait(self, job_id: str, timeout: float = 30.0) -> Job:
        """Block until the job finishes (used by tests)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.get(job_id)
            if job and not job.active:
                return job
            time.sleep(0.02)
        raise TimeoutError(f"job {job_id} did not finish within {timeout}s")

    def _update(self, job: Job, **fields) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(job, key, value)
            job.updated = time.time()

    def _work(self, job: Job, project: Project) -> None:
        cancelled = self._cancel[job.id]
        self._update(job, state="running", stage="Starting")
        try:
            self.runner.run(job, project, lambda **f: self._update(job, **f), cancelled.is_set)
        except Cancelled:
            self._update(job, state="cancelled", stage="Cancelled", detail="The GPU machine was shut down.")
        except Exception as err:  # noqa: BLE001 - any failure must end the job visibly, never hang it
            self._update(job, state="failed", stage="Failed", error=str(err) or err.__class__.__name__)
        else:
            self._update(job, state="done", stage="Done", detail="Your tour is ready.")


class MockRunner:
    """Renders placeholder panoramas locally, with realistic stages. No GPU and no cost."""

    def __init__(self, delay: float = 0.4):
        self.delay = delay

    def run(self, job, project, update, is_cancelled) -> None:
        from generator.generate import render_mock
        from generator.seam import repair_file

        for stage in ("Starting the GPU machine", "Installing software", "Downloading model weights", "Loading the model"):
            update(stage=stage, detail="(mock mode: nothing is running on AWS)")
            time.sleep(self.delay)
            if is_cancelled():
                raise Cancelled()
        for n, node in enumerate(project.manifest["topology"]["nodes"], start=1):
            update(stage=f"Rendering rooms ({n - 1}/{job.rooms_total})", detail="(mock panoramas)")
            path = project.dir / node["asset_url"]
            path.parent.mkdir(parents=True, exist_ok=True)
            render_mock(node, path)
            repair_file(path)
            update(rooms_done=n)
            time.sleep(self.delay)
            if is_cancelled():
                raise Cancelled()


class GpuRunner:
    """Launches a render instance on AWS, follows its log, and downloads panoramas as they appear."""

    def __init__(self, aws, instance_types: tuple[str, ...] = INSTANCE_TYPES, max_wait: float = 1200.0,
                 poll: float = 8.0, retry_every: float = 15.0, sleep=time.sleep, clock=time.time):
        self.aws, self.instance_types = aws, instance_types
        self.max_wait, self.poll, self.retry_every = max_wait, poll, retry_every
        self.sleep, self.clock = sleep, clock

    def run(self, job, project, update, is_cancelled) -> None:
        from infra.launch import NoCapacity

        busy = self.aws.running_gpu_instances()
        if busy:
            raise RuntimeError(f"a GPU instance is already running ({busy[0]}); wait for it to finish first")

        deadline = self.clock() + self.max_wait
        launched = None
        while launched is None:
            if is_cancelled():
                raise Cancelled()
            update(attempts=job.attempts + 1, stage="Waiting for a GPU",
                   detail="AWS often has no GPU free; retrying every few seconds.")
            instance_type = self.instance_types[(job.attempts - 1) % len(self.instance_types)]
            try:
                launched = self.aws.launch(project.manifest_path, instance_type, MAX_RUN_MINUTES)
            except NoCapacity as err:
                if self.clock() >= deadline:
                    raise RuntimeError(f"no GPU capacity after {round(self.max_wait / 60)} minutes ({err}). "
                                       "Try again later.") from err
                update(detail=f"{err}. Retrying...")
                self.sleep(self.retry_every)
        instance_id = launched["instance_id"]
        update(instance_id=instance_id, run=launched["run"], stage="Starting the GPU machine",
               detail=f"{instance_type} instance {instance_id} in {launched['zone']}.")

        wanted = project.pano_paths()
        try:
            self._follow(job, project, launched, wanted, update, is_cancelled)
        except Cancelled:
            self.aws.terminate(instance_id)
            raise

    def _follow(self, job, project, launched, wanted, update, is_cancelled) -> None:
        instance_id, run = launched["instance_id"], launched["run"]
        by_key = {f"renders/{project.id}/{path.relative_to(project.dir).as_posix()}": path for path in wanted.values()}
        grace_until = None
        while True:
            if is_cancelled():
                raise Cancelled()
            for key in self.aws.list_panos(project.id):
                local = by_key.get(key)
                if local is not None and not local.exists():
                    self.aws.download(key, local)
            done = sum(1 for p in wanted.values() if p.exists())
            stage, detail = derive_stage(self.aws.log_text(run), done, job.rooms_total)
            update(rooms_done=done, stage=stage, detail=detail)
            if done >= job.rooms_total:
                return
            state = self.aws.instance_state(instance_id)
            if state in ("shutting-down", "terminated", "stopping", "stopped"):
                grace_until = grace_until or self.clock() + 20  # allow one last upload to land
                if self.clock() >= grace_until:
                    tail = [ln for ln in re.split(r"[\r\n]+", self.aws.log_text(run)) if ln.strip()][-3:]
                    raise RuntimeError(f"the GPU machine stopped after {done}/{job.rooms_total} rooms. "
                                       f"Last log lines: {' | '.join(tail)[:400]}")
            self.sleep(self.poll)
