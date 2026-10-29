import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app import jobs, server
from app.jobs import Cancelled, GpuRunner, JobManager, MockRunner, Project, derive_stage, estimate_cost
from app.mock_planner import MockPlanner
from engine.manifest import build_manifest
from infra.launch import NoCapacity

TOKEN = "test-token"


class AppCase(unittest.TestCase):
    """Runs the real HTTP server on a free port with a fast mock runner."""

    runner = None  # subclasses may override

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.jobs = JobManager(self.runner or MockRunner(delay=0))
        self.ctx = server.Context(projects_dir=Path(self.tmp.name), planner=MockPlanner(), jobs=self.jobs,
                                  aws=None, mode="mock", token=TOKEN)
        self.httpd = server.make_server(self.ctx, port=0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)  # cleanups run last-in-first-out: stop serving, then close
        self.addCleanup(self.httpd.shutdown)

    def call(self, method, path, body=None, token=TOKEN, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.ctx.port, timeout=10)
        headers = {"Host": host or f"127.0.0.1:{self.ctx.port}"}
        if token is not None and method == "POST":
            headers["X-App-Token"] = token
        payload = None
        if body is not None:
            payload = json.dumps(body) if not isinstance(body, (bytes, str)) else body
            headers["Content-Type"] = "application/json"
        conn.request(method, path, payload, headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw

    def plan(self, **overrides):
        body = {"budget": 40000, "currency": "USD", "area_sqm": 90, "bhk": "2", "style": "japandi", **overrides}
        status, data = self.call("POST", "/api/plan", body)
        self.assertEqual(status, 200, data)
        return data


class SafetyTests(AppCase):
    def test_page_gets_the_token_injected(self):
        status, html = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(TOKEN.encode(), html)
        self.assertNotIn(b"__APP_TOKEN__", html)

    def test_wrong_host_header_is_refused(self):
        for host in ("evil.example.com", f"evil.example.com:{self.ctx.port}", "127.0.0.1:1"):
            self.assertEqual(self.call("GET", "/api/config", host=host)[0], 403, host)

    def test_posts_need_the_token(self):
        self.assertEqual(self.call("POST", "/api/plan", {"brief": "x" * 20}, token=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/plan", {"brief": "x" * 20}, token="wrong")[0], 403)

    def test_no_cors_headers_are_sent(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.ctx.port)
        conn.request("GET", "/api/config", headers={"Host": f"127.0.0.1:{self.ctx.port}", "Origin": "http://evil.example"})
        resp = conn.getresponse()
        resp.read()
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

    def test_path_traversal_is_refused(self):
        for path in ("/web/..", "/p/../manifest.json", "/p/abc/panos/../../x.jpg", "/api/projects/..",
                     "/p/%2e%2e/manifest.json", "/viewer/../app/server.py"):
            self.assertEqual(self.call("GET", path)[0], 404, path)

    def test_server_only_listens_on_loopback(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")


class PlanTests(AppCase):
    def test_structured_plan_creates_a_project_on_disk(self):
        data = self.plan()
        project_id = data["summary"]["id"]
        self.assertRegex(project_id, r"^proj_[0-9a-f]{8}-\d{12}$")
        self.assertTrue((Path(self.tmp.name) / project_id / "manifest.json").is_file())
        self.assertEqual(data["manifest"]["project_id"], project_id)
        self.assertGreater(data["summary"]["gpu_estimate"]["usd"], 0)

    def test_two_plans_with_identical_inputs_do_not_collide(self):
        first = self.plan()["summary"]["id"]
        time.sleep(1.1)
        self.assertNotEqual(first, self.plan()["summary"]["id"])

    def test_bad_requests_get_a_readable_400(self):
        cases = [{"budget": -5}, {"budget": "lots"}, {"currency": "XYZ"}, {"area_sqm": 3}, {"bhk": "studio"},
                 {"style": ""}, {"style": "x" * 200}]
        for overrides in cases:
            body = {"budget": 40000, "currency": "USD", "area_sqm": 90, "bhk": "2", "style": "japandi", **overrides}
            status, data = self.call("POST", "/api/plan", body)
            self.assertEqual(status, 400, overrides)
            self.assertTrue(data["error"])

    def test_brief_is_length_checked(self):
        self.assertEqual(self.call("POST", "/api/plan", {"brief": "short"})[0], 400)
        self.assertEqual(self.call("POST", "/api/plan", {"brief": "x" * 2500})[0], 400)

    def test_invalid_json_and_huge_bodies_are_rejected(self):
        self.assertEqual(self.call("POST", "/api/plan", "{not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/plan", "x" * (server.MAX_BODY + 1))[0], 413)

    def test_planner_failures_are_shown_not_hidden(self):
        status, data = self.call("POST", "/api/plan", {"brief": "a nice home with a view and good light"})
        self.assertEqual(status, 422)
        self.assertIn("does not say", data["error"])

    def test_mock_planner_reads_a_simple_brief(self):
        status, data = self.call("POST", "/api/plan", {"brief": "2BHK, 90 sqm, 30 lakh, calm and warm"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["manifest"]["metadata"]["currency"], "INR")


class RenderTests(AppCase):
    def wait_done(self, job_id):
        return self.jobs.wait(job_id, timeout=20)

    def test_render_needs_confirmation(self):
        pid = self.plan()["summary"]["id"]
        status, data = self.call("POST", f"/api/projects/{pid}/render", {})
        self.assertEqual(status, 400)
        self.assertIn("confirm", data["error"])
        self.assertIsNone(self.jobs.active())

    def test_full_mock_render_produces_panoramas_and_is_served(self):
        data = self.plan()
        pid = data["summary"]["id"]
        status, started = self.call("POST", f"/api/projects/{pid}/render", {"confirm": True})
        self.assertEqual(status, 202, started)
        job = self.wait_done(started["job"]["id"])
        self.assertEqual((job.state, job.rooms_done, job.rooms_total), ("done", 4, 4))
        status, summary = self.call("GET", f"/api/projects/{pid}")
        self.assertEqual(summary["summary"]["rooms_rendered"], 4)
        first = data["manifest"]["topology"]["nodes"][0]["asset_url"]
        status, image = self.call("GET", f"/p/{pid}/{first}")
        self.assertEqual(status, 200)
        self.assertTrue(image.startswith(b"\xff\xd8"))  # a JPEG
        self.assertEqual(self.call("POST", f"/api/projects/{pid}/render", {"confirm": True})[0], 409)  # done already

    def test_unknown_job_and_project_are_404(self):
        self.assertEqual(self.call("GET", "/api/jobs/0123456789")[0], 404)
        self.assertEqual(self.call("GET", "/api/projects/proj_missing")[0], 404)
        self.assertEqual(self.call("POST", "/api/projects/proj_missing/render", {"confirm": True})[0], 404)

    def test_projects_list_shows_progress(self):
        self.plan()
        status, listing = self.call("GET", "/api/projects")
        self.assertEqual(len(listing["projects"]), 1)
        self.assertEqual(listing["projects"][0]["rooms_rendered"], 0)


class GateRunner:
    """Blocks until released or cancelled, so tests can look at a running job."""

    def __init__(self):
        self.release = threading.Event()
        self.started = threading.Event()

    def run(self, job, project, update, is_cancelled):
        self.started.set()
        while not self.release.is_set():
            if is_cancelled():
                raise Cancelled()
            time.sleep(0.01)


class ConcurrencyTests(AppCase):
    runner = GateRunner()

    def setUp(self):
        type(self).runner = GateRunner()
        super().setUp()

    def test_only_one_render_at_a_time(self):
        first, second = self.plan()["summary"]["id"], self.plan(budget=50000)["summary"]["id"]
        status, started = self.call("POST", f"/api/projects/{first}/render", {"confirm": True})
        self.assertEqual(status, 202)
        self.runner.started.wait(5)
        status, data = self.call("POST", f"/api/projects/{second}/render", {"confirm": True})
        self.assertEqual(status, 409)
        self.assertIn("already running", data["error"])
        self.assertEqual(self.call("GET", "/api/jobs/current")[1]["job"]["id"], started["job"]["id"])
        self.runner.release.set()
        self.jobs.wait(started["job"]["id"])

    def test_cancel_stops_the_job(self):
        pid = self.plan()["summary"]["id"]
        job_id = self.call("POST", f"/api/projects/{pid}/render", {"confirm": True})[1]["job"]["id"]
        self.runner.started.wait(5)
        self.assertEqual(self.call("POST", f"/api/jobs/{job_id}/cancel", {})[0], 200)
        self.assertEqual(self.jobs.wait(job_id).state, "cancelled")
        self.assertEqual(self.call("POST", f"/api/jobs/{job_id}/cancel", {})[0], 409)  # nothing left to cancel


class FailingRunner:
    def run(self, job, project, update, is_cancelled):
        raise RuntimeError("the GPU machine stopped after 1/4 rooms")


class FailureTests(AppCase):
    runner = FailingRunner()

    def test_failures_end_the_job_with_the_reason(self):
        pid = self.plan()["summary"]["id"]
        job_id = self.call("POST", f"/api/projects/{pid}/render", {"confirm": True})[1]["job"]["id"]
        job = self.jobs.wait(job_id)
        self.assertEqual(job.state, "failed")
        self.assertIn("stopped after 1/4", job.error)
        self.assertIsNone(self.jobs.active())  # a new render can start afterwards


class FakeAws:
    """Scripted stand-in for app.aws.Aws. Each runner sleep() advances the scenario by one step."""

    def __init__(self, fail_launches=0, running=(), stop_after=None):
        self.fail_launches, self.running, self.stop_after = fail_launches, list(running), stop_after
        self.attempts, self.terminated, self.keys, self.state, self.log = 0, [], [], "running", ""
        self.ticks = 0

    def running_gpu_instances(self):
        return self.running

    def launch(self, manifest_path, instance_type, max_minutes, log=print):
        self.attempts += 1
        if self.attempts <= self.fail_launches:
            raise NoCapacity("no g6e.2xlarge capacity in any zone")
        return {"instance_id": "i-test", "zone": "us-east-1b", "run": "run1"}

    def instance_state(self, instance_id):
        return self.state

    def list_panos(self, project_id):
        return list(self.keys)

    def download(self, key, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8fake")

    def log_text(self, run):
        return self.log

    def terminate(self, instance_id):
        self.terminated.append(instance_id)

    def tick(self, project):  # called from the runner's sleep()
        if self.attempts <= self.fail_launches or self.state == "terminated":
            return  # not launched yet, or the machine is gone: nothing progresses
        self.ticks += 1
        nodes = project.manifest["topology"]["nodes"]
        self.log = ["Fetching 23 files", "Loading pipeline components", " 10/28 [00:13<00:24, 1.34s/it]"][min(self.ticks - 1, 2)]
        done = max(0, self.ticks - 3)
        self.keys = [f"renders/{project.id}/{n['asset_url']}" for n in nodes[:done]]
        if self.stop_after is not None and self.ticks >= self.stop_after:
            self.state = "terminated"


def make_project(tmp):
    manifest = build_manifest(40_000, 90, "2BHK", "japandi")
    manifest["project_id"] = "proj_unit-250101000000"
    directory = Path(tmp) / manifest["project_id"]
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return Project(manifest["project_id"], directory, manifest)


class GpuRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = make_project(self.tmp.name)
        self.job = jobs.Job(id="j", project_id=self.project.id, rooms_total=4)
        self.updates = []

    def runner(self, aws, cancelled=lambda: False, **kwargs):
        clock = {"t": 0.0}

        def sleep(seconds):
            clock["t"] += seconds
            aws.tick(self.project)

        r = GpuRunner(aws, sleep=sleep, clock=lambda: clock["t"], poll=1, retry_every=1, max_wait=60, **kwargs)
        return lambda: r.run(self.job, self.project, lambda **f: (self.updates.append(f), self.job.__dict__.update(f)),
                             cancelled)

    def test_capacity_retries_alternate_between_instance_types(self):
        aws = FakeAws(fail_launches=3)
        types = []
        original = aws.launch

        def recording(manifest_path, instance_type, max_minutes, log=print):
            types.append(instance_type)
            return original(manifest_path, instance_type, max_minutes, log)

        aws.launch = recording
        self.runner(aws)()
        self.assertEqual(types, ["g6e.2xlarge", "g6e.xlarge", "g6e.2xlarge", "g6e.xlarge"])

    def test_retries_until_capacity_then_downloads_every_room(self):
        aws = FakeAws(fail_launches=3)
        self.runner(aws)()
        self.assertEqual(aws.attempts, 4)
        self.assertEqual(self.job.rooms_done, 4)
        self.assertTrue(all(p.exists() for p in self.project.pano_paths().values()))
        stages = [u["stage"] for u in self.updates if "stage" in u]
        self.assertIn("Waiting for a GPU", stages)
        self.assertIn("Downloading model weights", stages)
        self.assertIn("Loading the model", stages)

    def test_gives_up_when_no_capacity_ever_appears(self):
        with self.assertRaises(RuntimeError) as caught:
            self.runner(FakeAws(fail_launches=10_000))()
        self.assertIn("no GPU capacity", str(caught.exception))

    def test_refuses_to_start_while_another_gpu_instance_runs(self):
        aws = FakeAws(running=["i-other"])
        with self.assertRaises(RuntimeError) as caught:
            self.runner(aws)()
        self.assertIn("i-other", str(caught.exception))
        self.assertEqual(aws.attempts, 0)  # nothing was launched

    def test_cancel_terminates_the_instance(self):
        aws = FakeAws()
        state = {"calls": 0}

        def cancelled():
            state["calls"] += 1
            return state["calls"] > 4  # cancel a few polls after launch

        with self.assertRaises(Cancelled):
            self.runner(aws, cancelled)()
        self.assertEqual(aws.terminated, ["i-test"])

    def test_a_machine_that_stops_early_fails_with_the_log(self):
        aws = FakeAws(stop_after=5)
        with self.assertRaises(RuntimeError) as caught:
            self.runner(aws)()
        self.assertIn("stopped after", str(caught.exception))

    def test_previously_downloaded_rooms_are_not_downloaded_again(self):
        aws = FakeAws()
        first = next(iter(self.project.pano_paths().values()))
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"existing")
        self.runner(aws)()
        self.assertEqual(first.read_bytes(), b"existing")


class StageAndCostTests(unittest.TestCase):
    def test_stage_follows_the_log(self):
        self.assertEqual(derive_stage("", 0, 4)[0], "Starting the GPU machine")
        self.assertEqual(derive_stage("Collecting uv", 0, 4)[0], "Installing software")
        self.assertEqual(derive_stage("DiT360 import OK", 0, 4)[0], "Downloading model weights")
        self.assertEqual(derive_stage("Fetching 23 files", 0, 4)[0], "Downloading model weights")
        self.assertEqual(derive_stage("Loading pipeline components", 0, 4)[0], "Loading the model")
        self.assertEqual(derive_stage(" 10/28 [00:13", 0, 4)[0], "Rendering the first room")
        self.assertEqual(derive_stage("anything", 2, 4)[0], "Rendering rooms (2/4)")
        self.assertEqual(derive_stage("anything", 4, 4)[0], "Finishing up")

    def test_cost_estimate_scales_with_rooms(self):
        self.assertLess(estimate_cost(2), estimate_cost(8))
        self.assertAlmostEqual(estimate_cost(4), 0.4, delta=0.15)  # about 10 minutes at about $2.24/hour


if __name__ == "__main__":
    unittest.main()
