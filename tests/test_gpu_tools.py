import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from types import SimpleNamespace as types_namespace

from engine.manifest import build_manifest
from generator import benchmark
from infra import launch


class BenchmarkMathTests(unittest.TestCase):
    def test_steady_state_excludes_warmup(self):
        summary = benchmark.summarize([60.0, 40.0, 44.0])
        self.assertEqual(summary["first_room_s"], 60.0)
        self.assertEqual(summary["steady_mean_s"], 42.0)

    def test_single_room_falls_back_to_that_room(self):
        self.assertEqual(benchmark.summarize([50.0])["steady_mean_s"], 50.0)

    def test_projection_grows_with_room_time(self):
        self.assertLess(benchmark.project_cost(30), benchmark.project_cost(120))

    def test_projection_matches_hand_calculation(self):
        # 120 s * 4 rooms * 10 homes = 4800 s = 1.333 h, plus 15 min = 1.583 h, at 1.861/h
        self.assertAlmostEqual(benchmark.project_cost(120), 2.95, places=2)


class BenchmarkHarnessTests(unittest.TestCase):
    def test_mock_run_writes_images_and_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = tmp / "manifest.json"
            manifest.write_text(json.dumps(build_manifest(40_000, 90, "2BHK", "japandi")))
            results = benchmark.run(manifest, "mock", None, rooms=3, steps=None, out=tmp / "bench")
            self.assertEqual(results["rooms_run"], 3)
            self.assertEqual(len(list((tmp / "bench").glob("*.jpg"))), 3)
            self.assertTrue((tmp / "bench" / "results.json").exists())

    def test_more_rooms_than_the_home_has_cycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            manifest = tmp / "manifest.json"
            manifest.write_text(json.dumps(build_manifest(40_000, 90, "2BHK", "japandi")))
            results = benchmark.run(manifest, "mock", None, rooms=6, steps=None, out=tmp / "bench")
            self.assertEqual(results["rooms_run"], 6)


class LowRamLoadingTests(unittest.TestCase):
    """dit360.load is checked against stand-ins for torch and the DiT360 repo; no GPU is needed."""

    def setUp(self):
        import sys
        import types
        from unittest import mock

        calls = {"from_pretrained": [], "lora": [], "to": []}

        class FakePipeline:
            @classmethod
            def from_pretrained(cls, name, **kwargs):
                calls["from_pretrained"].append((name, kwargs))
                return cls()

            def to(self, device):
                calls["to"].append(device)
                return self

            def load_lora_weights(self, name):
                calls["lora"].append(name)

        fake_torch = types.SimpleNamespace(float16="fp16", device=lambda name: f"device:{name}")
        fake_pipeline_module = types.SimpleNamespace(DiT360Pipeline=FakePipeline)
        patcher = mock.patch.dict(sys.modules, {"torch": fake_torch, "src": types.ModuleType("src"),
                                                "src.pipeline": fake_pipeline_module})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = calls

    def test_low_ram_loads_straight_onto_the_gpu(self):
        from generator import dit360

        dit360.load("repo")
        (name, kwargs), = self.calls["from_pretrained"]
        self.assertEqual(name, dit360.BASE_MODEL)
        self.assertEqual(kwargs["device_map"], "cuda")
        self.assertEqual(kwargs["torch_dtype"], "fp16")
        self.assertEqual(self.calls["to"], [])  # never staged through system RAM and moved afterwards
        self.assertEqual(self.calls["lora"], [dit360.LORA])

    def test_falls_back_to_the_original_loader_if_low_ram_loading_fails(self):
        import sys

        from generator import dit360

        pipeline_cls = sys.modules["src.pipeline"].DiT360Pipeline
        real = pipeline_cls.from_pretrained.__func__
        attempts = []

        def flaky(cls, name, **kwargs):
            attempts.append(kwargs)
            if "device_map" in kwargs:
                raise RuntimeError("device_map is not supported here")
            return real(cls, name, **kwargs)

        sys.modules["torch"].cuda = types_namespace(empty_cache=lambda: None)
        pipeline_cls.from_pretrained = classmethod(flaky)
        pipe = dit360.load("repo")
        self.assertEqual(len(attempts), 2)
        self.assertIn("device_map", attempts[0])
        self.assertNotIn("device_map", attempts[1])
        self.assertEqual(self.calls["to"], ["device:cuda:0"])  # the fallback path was used
        self.assertEqual(self.calls["lora"], [dit360.LORA])
        self.assertIsNotNone(pipe)

    def test_original_path_is_still_available(self):
        from generator import dit360

        dit360.load("repo", low_ram=False)
        (_, kwargs), = self.calls["from_pretrained"]
        self.assertNotIn("device_map", kwargs)
        self.assertEqual(self.calls["to"], ["device:cuda:0"])


class LaunchScriptTests(unittest.TestCase):
    def rendered(self, job="benchmark") -> str:
        return launch.build_user_data(bucket="b", run="r", code_key="code/r.tgz", max_minutes=90, job=job,
                                      rooms=4, manifest_key="jobs/r/manifest.json")

    def test_benchmark_job_runs_the_benchmark(self):
        script = self.rendered("benchmark")
        self.assertIn("generator.benchmark", script)
        self.assertNotIn("generator.generate", script)

    def test_render_job_generates_from_the_uploaded_manifest(self):
        script = self.rendered("render")
        self.assertIn('aws s3 cp "s3://b/jobs/r/manifest.json" out/manifest.json', script)
        self.assertIn("generator.generate", script)
        self.assertIn("--backend dit360", script)
        self.assertIn("--s3-bucket b --s3-prefix renders", script)
        self.assertNotIn("generator.benchmark", script)

    def test_render_job_is_valid_bash_and_keeps_the_safety_nets(self):
        script = self.rendered("render")
        self.assertIn("trap cleanup EXIT", script)
        self.assertIn("shutdown -h +90", script)
        self.assertLess(script.index("DiT360 import OK"), script.index("generator.generate"))

    def test_user_data_is_valid_bash(self):
        probe = subprocess.run(["bash", "-c", "echo ok"], capture_output=True, text=True)
        if probe.returncode != 0 or "ok" not in probe.stdout:  # e.g. Windows' WSL launcher with no distro
            self.skipTest("no working bash on PATH")
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n") as handle:
            handle.write(self.rendered())
        result = subprocess.run(["bash", "-n", handle.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_token_is_never_echoed_by_xtrace(self):
        script = self.rendered()
        before = script.index("set +x")
        after = script.index("set -x", before)
        self.assertLess(before, script.index("HF_TOKEN"))
        self.assertLess(script.index("HF_TOKEN"), after)

    def test_dependencies_are_pinned_to_a_compatible_set(self):
        # diffusers from GitHub HEAD needs huggingface-hub>=1.32, transformers 4.52.4 needs <1.0: unsatisfiable.
        script = self.rendered()
        self.assertIn("transformers==4.52.4", script)
        self.assertIn("diffusers==0.35.2", script)
        self.assertNotIn("git+https://github.com/huggingface/diffusers", script)

    def test_import_smoke_test_runs_before_model_download(self):
        script = self.rendered()
        self.assertLess(script.index("DiT360 import OK"), script.index("HF_TOKEN"))

    def test_log_is_uploaded_while_running(self):
        self.assertIn("while true; do sleep 60", self.rendered())

    def test_main_script_appends_so_monitoring_lines_survive(self):
        # Without O_APPEND the main shell overwrites lines the monitoring loop appended to the same file.
        self.assertIn("exec >> /var/log/benchmark.log", self.rendered())

    def test_swap_is_set_up_before_the_heavy_steps(self):
        script = self.rendered()
        self.assertIn("swapon /swapfile", script)
        self.assertLess(script.index("swapon /swapfile"), script.index("pip3 install uv"))
        self.assertIn("|| echo", script)  # a swap failure must not abort the run under `set -e`

    def test_each_minute_records_memory_and_gpu_state(self):
        script = self.rendered()
        self.assertIn("free -m", script)
        self.assertIn("nvidia-smi", script)

    def test_instance_always_shuts_down(self):
        script = self.rendered()
        self.assertIn("trap cleanup EXIT", script)
        self.assertIn("shutdown -h +90", script)

    def test_code_package_has_both_packages_and_no_bytecode(self):
        with tarfile.open(fileobj=io.BytesIO(launch.package_code()), mode="r:gz") as tar:
            names = tar.getnames()
        self.assertTrue(any(n.startswith("engine/") for n in names))
        self.assertTrue(any(n.startswith("generator/benchmark.py") for n in names))
        self.assertFalse(any("__pycache__" in n for n in names))


if __name__ == "__main__":
    unittest.main()
