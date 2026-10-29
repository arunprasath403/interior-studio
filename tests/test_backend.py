import base64
import json
import unittest
from unittest import mock

from backend import handler
from backend.core import Backend, Config, friendly_error, phase_for
from backend.store import MemoryStore
from engine.manifest import build_manifest
from infra.launch import NoCapacity

CODE = "open-sesame"
AUTH = {"x-access-code": CODE}


class FakeAws:
    def __init__(self):
        self.panos, self.launches, self.terminated = set(), [], []
        self.fail_launches, self.state, self.log = 0, "running", ""

    def list_panos(self, project_id):
        return [k for k in self.panos if k.startswith(f"renders/{project_id}/panos/")]

    def presign(self, key, seconds):
        return f"https://files.example/{key}?sig=1&exp={seconds}"

    def launch(self, manifest_bytes, instance_type, max_minutes):
        self.launches.append(instance_type)
        if len(self.launches) <= self.fail_launches:
            raise NoCapacity("no g6e capacity in any zone")
        return {"instance_id": "i-test", "run": "run-1", "zone": "us-east-1b"}

    def instance_state(self, instance_id):
        return self.state

    def terminate(self, instance_id):
        self.terminated.append(instance_id)

    def log_text(self, run):
        return self.log


class FakePlanner:
    def __init__(self):
        self.fail = None

    def plan(self, request):
        if self.fail:
            raise RuntimeError(self.fail)
        if "brief" in request:
            return build_manifest(3_000_000, 90, "2BHK", "scandinavian", "INR")
        return build_manifest(request["budget"], request["area_sqm"], request["bhk"], request["style"], request["currency"])


class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def seed_project(store, project_id="proj_gallery01", featured=True, bhk="2BHK"):
    manifest = build_manifest(40_000, 90, bhk, "japandi")
    manifest["project_id"] = project_id
    store.put_project({"id": project_id, "manifest": json.dumps(manifest), "featured": featured, "order": 1, "created": 1})
    return manifest


def add_panos(aws, manifest, count=None):
    nodes = manifest["topology"]["nodes"][:count]
    for n in nodes:
        aws.panos.add(f"renders/{manifest['project_id']}/{n['asset_url']}")


class BackendCase(unittest.TestCase):
    def setUp(self):
        self.store, self.aws, self.planner, self.clock = MemoryStore(), FakeAws(), FakePlanner(), Clock()
        self.triggered = []
        self.config = Config(access_code=CODE, daily_render_cap=2, daily_plan_cap=3)
        self.api = Backend(self.store, self.aws, self.planner, self.config, trigger=lambda: self.triggered.append(1),
                           clock=self.clock)

    def call(self, method, path, body=None, headers=None):
        payload = json.dumps(body).encode() if body is not None else b""
        return self.api.handle(method, path, headers or {}, payload)

    def plan(self, **overrides):
        body = {"budget": 40000, "currency": "USD", "area_sqm": 90, "bhk": "2", "style": "japandi", **overrides}
        status, data = self.call("POST", "/api/plan", body, AUTH)
        self.assertEqual(status, 200, data)
        return data

    def render(self, project_id):
        return self.call("POST", f"/api/projects/{project_id}/render", {"confirm": True}, AUTH)


class PublicTests(BackendCase):
    def test_health(self):
        self.assertEqual(self.call("GET", "/api/health"), (200, {"ok": True}))

    def test_gallery_is_public_and_lists_only_featured_homes(self):
        manifest = seed_project(self.store)
        seed_project(self.store, "proj_private01", featured=False)
        add_panos(self.aws, manifest)
        status, data = self.call("GET", "/api/gallery")
        self.assertEqual(status, 200)
        self.assertEqual([h["id"] for h in data["homes"]], ["proj_gallery01"])
        home = data["homes"][0]
        self.assertEqual(home["rooms_ready"], 4)
        self.assertTrue(home["cover_url"].startswith("https://files.example/renders/proj_gallery01/"))

    def test_titles_use_everyday_words(self):
        manifest = seed_project(self.store)
        manifest["metadata"]["aesthetic_theme"] = "scandinavian"
        self.store.put_project({"id": "proj_gallery01", "manifest": json.dumps(manifest), "featured": True, "order": 1})
        title = self.call("GET", "/api/gallery")[1]["homes"][0]["title"]
        self.assertEqual(title, "Scandinavian 2-bedroom home")
        self.assertNotIn("BHK", title)

    def test_a_design_can_be_opened_by_its_link_and_only_ready_rooms_get_images(self):
        manifest = seed_project(self.store, featured=False)
        add_panos(self.aws, manifest, count=2)
        status, data = self.call("GET", "/api/projects/proj_gallery01")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["images"]), 2)
        self.assertEqual(data["summary"]["rooms_ready"], 2)
        self.assertEqual(data["manifest"]["project_id"], "proj_gallery01")

    def test_unknown_or_malformed_ids_are_404(self):
        for path in ("/api/projects/nope", "/api/projects/..", "/api/projects/a b", "/api/jobs/zzzzzzzzzz"):
            self.assertEqual(self.call("GET", path)[0], 404, path)
        self.assertEqual(self.call("GET", "/api/unknown")[0], 404)


class AccessTests(BackendCase):
    def test_creating_needs_the_code(self):
        body = {"budget": 40000, "currency": "USD", "area_sqm": 90, "bhk": "2", "style": "japandi"}
        for headers in ({}, {"x-access-code": "wrong"}, {"x-access-code": CODE + "x"}):
            self.assertEqual(self.call("POST", "/api/plan", body, headers)[0], 403, headers)
        self.assertEqual(self.call("POST", "/api/plan", body, AUTH)[0], 200)

    def test_it_fails_closed_when_no_code_is_configured(self):
        self.api.config.access_code = ""
        self.assertEqual(self.call("POST", "/api/access", {}, {"x-access-code": ""})[0], 403)
        self.assertEqual(self.call("POST", "/api/plan", {}, {})[0], 403)

    def test_render_and_cancel_need_the_code_too(self):
        project = self.plan()["summary"]["id"]
        self.assertEqual(self.call("POST", f"/api/projects/{project}/render", {"confirm": True})[0], 403)
        self.assertEqual(self.call("POST", "/api/jobs/0123456789/cancel", {})[0], 403)
        self.assertEqual(self.store.jobs, {})  # nothing was started

    def test_access_check_endpoint(self):
        self.assertEqual(self.call("POST", "/api/access", {}, AUTH)[0], 200)
        self.assertEqual(self.call("POST", "/api/access", {}, {"x-access-code": "no"})[0], 403)


class PlanTests(BackendCase):
    def test_plans_get_unique_unlisted_ids(self):
        first, second = self.plan()["summary"]["id"], self.plan()["summary"]["id"]
        self.assertNotEqual(first, second)
        self.assertFalse(self.store.get_project(first)["featured"])
        self.assertEqual(self.call("GET", "/api/gallery")[1]["homes"], [])

    def test_bad_requests_and_planner_failures_get_readable_errors(self):
        self.assertEqual(self.call("POST", "/api/plan", {"brief": "short"}, AUTH)[0], 400)
        self.assertEqual(self.call("POST", "/api/plan", {"budget": -1}, AUTH)[0], 400)
        self.planner.fail = "the brief does not say: budget"
        status, data = self.call("POST", "/api/plan", {"brief": "a lovely home with warm light"}, AUTH)
        self.assertEqual((status, data["error"]), (422, "the brief does not say: budget"))

    def test_daily_plan_limit(self):
        for _ in range(3):
            self.plan()
        status, data = self.call("POST", "/api/plan", {"brief": "a lovely home with warm light"}, AUTH)
        self.assertEqual(status, 429)
        self.assertIn("tomorrow", data["error"])
        self.clock.advance(24 * 3600)
        self.plan()  # a new day starts a new allowance

    def test_invalid_json_and_big_bodies(self):
        self.assertEqual(self.api.handle("POST", "/api/plan", AUTH, b"{nope")[0], 400)
        self.assertEqual(self.api.handle("POST", "/api/plan", AUTH, b"x" * 30_000)[0], 413)


class RenderStartTests(BackendCase):
    def test_needs_confirmation(self):
        project = self.plan()["summary"]["id"]
        self.assertEqual(self.call("POST", f"/api/projects/{project}/render", {}, AUTH)[0], 400)
        self.assertEqual(self.store.jobs, {})

    def test_starts_a_waiting_job_and_triggers_the_worker(self):
        project = self.plan()["summary"]["id"]
        status, data = self.render(project)
        self.assertEqual(status, 202)
        self.assertEqual((data["state"], data["phase"], data["rooms_total"]), ("waiting", "queue", 4))
        self.assertEqual([r["name"] for r in data["rooms"]][:2], ["Living", "Kitchen"])
        self.assertEqual(self.triggered, [1])

    def test_only_one_render_at_a_time(self):
        first, second = self.plan()["summary"]["id"], self.plan(budget=50000)["summary"]["id"]
        self.assertEqual(self.render(first)[0], 202)
        status, data = self.render(second)
        self.assertEqual(status, 409)
        self.assertIn("busy", data["error"])

    def test_daily_render_limit_releases_the_lock(self):
        ids = [self.plan(budget=40000 + n)["summary"]["id"] for n in range(3)]
        for project in ids[:2]:
            status, data = self.render(project)
            self.assertEqual(status, 202)
            self.api.tick()
            self.api._cancel(self.store.get_job(data["id"]))  # free the studio for the next one
        status, data = self.render(ids[2])
        self.assertEqual(status, 429)
        self.assertIn("tomorrow", data["error"])
        self.assertEqual(self.store.locks, {})  # the failed attempt did not leave the studio locked

    def test_finished_and_gallery_designs_cannot_be_rendered_again(self):
        manifest = seed_project(self.store, featured=False)
        add_panos(self.aws, manifest)
        self.assertEqual(self.render("proj_gallery01")[0], 409)
        seed_project(self.store, "proj_featured01", featured=True)
        self.assertEqual(self.render("proj_featured01")[0], 409)


class JobFlowTests(BackendCase):
    def start(self):
        project = self.plan()["summary"]["id"]
        status, data = self.render(project)
        self.assertEqual(status, 202)
        self.manifest = json.loads(self.store.get_project(project)["manifest"])
        return data["id"], project

    def job(self, job_id):
        return self.call("GET", f"/api/jobs/{job_id}")[1]

    def test_full_happy_path_with_friendly_phases(self):
        job_id, project = self.start()
        self.aws.fail_launches = 2
        phases = []
        for _ in range(2):  # two attempts find no free hardware, alternating sizes
            self.api.tick()
            phases.append(self.job(job_id)["phase"])
        self.assertEqual(phases, ["queue", "queue"])
        self.api.tick()  # the third attempt succeeds
        self.assertEqual(self.aws.launches, ["g6e.2xlarge", "g6e.xlarge", "g6e.2xlarge"])
        self.assertEqual(self.job(job_id)["phase"], "studio")

        self.aws.log = "Loading pipeline components"
        self.clock.advance(5)
        self.assertEqual(self.job(job_id)["phase"], "studio")
        self.aws.log = " 10/28 [00:13<00:24, 1.34s/it]"
        self.clock.advance(5)
        self.assertEqual(self.job(job_id)["phase"], "rooms")

        for done in (1, 2, 3):
            add_panos(self.aws, {"project_id": project, "topology": {"nodes": self.manifest["topology"]["nodes"]}}, done)
            self.clock.advance(5)
            view = self.job(job_id)
            self.assertEqual((view["phase"], view["rooms_done"]), ("rooms", done))
        add_panos(self.aws, {"project_id": project, "topology": {"nodes": self.manifest["topology"]["nodes"]}}, 4)
        self.clock.advance(5)
        view = self.job(job_id)
        self.assertEqual((view["state"], view["phase"], view["rooms_done"]), ("done", "done", 4))
        self.assertEqual(self.store.locks, {})  # the studio is free again
        self.assertEqual(self.aws.terminated, [])  # the machine shuts itself down; nothing to kill

    def test_status_checks_are_throttled(self):
        job_id, _ = self.start()
        self.api.tick()
        calls = []
        original = self.aws.log_text
        self.aws.log_text = lambda run: (calls.append(1), original(run))[1]
        self.job(job_id)
        self.job(job_id)
        self.assertEqual(len(calls), 0)  # within 3 seconds of the last update: served from the record
        self.clock.advance(4)
        self.job(job_id)
        self.assertEqual(len(calls), 1)

    def test_gives_up_waiting_for_hardware_with_a_kind_message(self):
        job_id, _ = self.start()
        self.aws.fail_launches = 10_000
        self.api.tick()
        self.clock.advance(self.config.max_wait_seconds + 1)
        self.api.tick()
        view = self.job(job_id)
        self.assertEqual((view["state"], view["phase"]), ("failed", "stopped"))
        self.assertIn("very busy", view["error"])
        self.assertNotIn("capacity", view["error"])  # no technical words reach the visitor
        self.assertEqual(self.store.locks, {})

    def test_a_machine_that_stops_early_fails_after_a_short_grace(self):
        job_id, _ = self.start()
        self.api.tick()
        self.aws.state = "terminated"
        self.clock.advance(5)
        self.job(job_id)
        self.assertEqual(self.store.get_job(job_id)["state"], "running")  # still within the grace period
        self.clock.advance(30)
        self.api.tick()
        view = self.job(job_id)
        self.assertEqual(view["state"], "failed")
        self.assertIn("Something went wrong", view["error"])
        self.assertIn("stopped after 0/4", self.store.get_job(job_id)["error"])  # the owner keeps the real reason
        self.assertEqual(self.store.locks, {})

    def test_a_render_that_takes_too_long_is_stopped_and_the_machine_terminated(self):
        job_id, _ = self.start()
        self.api.tick()
        self.clock.advance(self.config.max_job_seconds + 1)
        self.api.tick()
        self.assertEqual(self.job(job_id)["state"], "failed")
        self.assertEqual(self.aws.terminated, ["i-test"])
        self.assertIn("longer than expected", self.job(job_id)["error"])

    def test_cancelling_a_running_job_terminates_the_machine(self):
        job_id, _ = self.start()
        self.api.tick()
        self.assertEqual(self.call("POST", f"/api/jobs/{job_id}/cancel", {}, AUTH)[0], 200)
        self.assertEqual(self.job(job_id)["state"], "cancelled")
        self.assertEqual(self.aws.terminated, ["i-test"])
        self.assertEqual(self.store.locks, {})
        self.assertEqual(self.call("POST", f"/api/jobs/{job_id}/cancel", {}, AUTH)[0], 409)

    def test_cancelling_while_still_waiting_starts_no_machine(self):
        job_id, _ = self.start()
        self.assertEqual(self.call("POST", f"/api/jobs/{job_id}/cancel", {}, AUTH)[0], 200)
        self.api.tick()
        self.assertEqual(self.aws.launches, [])
        self.assertEqual(self.job(job_id)["state"], "cancelled")

    def test_one_broken_job_does_not_stop_the_scheduler(self):
        job_id, _ = self.start()
        with mock.patch.object(self.aws, "launch", side_effect=KeyError("boom")):
            self.api.tick()
        self.assertEqual(self.job(job_id)["state"], "failed")
        self.assertEqual(self.store.locks, {})

    def test_job_view_never_leaks_machine_details(self):
        job_id, _ = self.start()
        self.api.tick()
        text = json.dumps(self.job(job_id))
        for secret in ("i-test", "run-1", "g6e", "instance"):
            self.assertNotIn(secret, text)


class WordingTests(unittest.TestCase):
    def test_friendly_errors_hide_the_reason(self):
        for raw in ("no capacity after 20 minutes", "ClientError: VcpuLimit", "machine stopped after 1/4 rooms", None):
            text = friendly_error(raw)
            self.assertNotRegex(text.lower(), r"gpu|instance|capacity|vcpu|machine|aws|error:")

    def test_phase_mapping(self):
        self.assertEqual(phase_for({"state": "waiting"}), "queue")
        self.assertEqual(phase_for({"state": "running", "stage": "Installing software"}), "studio")
        self.assertEqual(phase_for({"state": "running", "stage": "Rendering rooms (2/4)"}), "rooms")
        self.assertEqual(phase_for({"state": "running", "stage": "Finishing up"}), "finishing")
        self.assertEqual(phase_for({"state": "done"}), "done")
        self.assertEqual(phase_for({"state": "cancelled"}), "stopped")


class LambdaGlueTests(unittest.TestCase):
    def setUp(self):
        self.store, self.aws = MemoryStore(), FakeAws()
        self.api = Backend(self.store, self.aws, FakePlanner(), Config(access_code=CODE))
        handler._backend = self.api
        self.addCleanup(setattr, handler, "_backend", None)

    def event(self, method, path, body=None, headers=None, base64_body=False):
        raw = json.dumps(body) if body is not None else None
        if raw and base64_body:
            raw = base64.b64encode(raw.encode()).decode()
        return {"requestContext": {"http": {"method": method}}, "rawPath": path, "body": raw,
                "isBase64Encoded": base64_body, "headers": headers or {}}

    def test_http_request_round_trip(self):
        response = handler.lambda_handler(self.event("GET", "/api/health"), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(json.loads(response["body"]), {"ok": True})
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")

    def test_header_names_are_case_insensitive_and_bodies_may_be_base64(self):
        body = {"budget": 40000, "currency": "USD", "area_sqm": 90, "bhk": "2", "style": "japandi"}
        for headers in ({"X-Access-Code": CODE}, {"x-access-code": CODE}):
            response = handler.lambda_handler(self.event("POST", "/api/plan", body, headers, base64_body=True), None)
            self.assertEqual(response["statusCode"], 200, response["body"])

    def test_browser_preflight_requests_succeed_without_a_body(self):
        # Without this a browser refuses to send any POST: the catch-all route would answer 404.
        response = handler.lambda_handler(self.event("OPTIONS", "/api/plan"), None)
        self.assertEqual(response, {"statusCode": 204})

    def test_scheduler_event_runs_the_tick(self):
        with mock.patch.object(self.api, "tick") as tick:
            self.assertEqual(handler.lambda_handler({"source": "aws.events"}, None), {"ok": True})
            handler.lambda_handler({"tick": True}, None)
        self.assertEqual(tick.call_count, 2)

    def test_unexpected_errors_become_a_plain_500_without_details(self):
        with mock.patch.object(self.api, "handle", side_effect=RuntimeError("secret table name")):
            response = handler.lambda_handler(self.event("GET", "/api/health"), None)
        self.assertEqual(response["statusCode"], 500)
        self.assertNotIn("secret", response["body"])


class StoreTests(unittest.TestCase):
    def test_lock_expires_and_only_the_owner_releases_it(self):
        store = MemoryStore()
        self.assertTrue(store.acquire_lock("gpu", "a", 100, now=1000))
        self.assertFalse(store.acquire_lock("gpu", "b", 100, now=1050))
        store.release_lock("gpu", "b")  # not the owner: ignored
        self.assertFalse(store.acquire_lock("gpu", "b", 100, now=1060))
        self.assertTrue(store.acquire_lock("gpu", "b", 100, now=1101))  # expired
        store.release_lock("gpu", "b")
        self.assertTrue(store.acquire_lock("gpu", "c", 100, now=1102))

    def test_counter_stops_at_the_limit(self):
        store = MemoryStore()
        self.assertEqual([store.bump("x", "d1", 2, 0) for _ in range(3)], [True, True, False])
        self.assertTrue(store.bump("x", "d2", 2, 0))


if __name__ == "__main__":
    unittest.main()
