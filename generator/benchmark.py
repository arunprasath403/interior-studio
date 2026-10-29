"""Time DiT360 panorama generation on the current machine.

    python -m generator.benchmark --manifest out/manifest.json --backend dit360 --repo ~/DiT360 --out out/benchmark

Writes the panoramas and results.json (timings, GPU memory, versions, cost projection). The first room
includes CUDA warm-up, so the steady-state figure uses the remaining rooms.
"""
from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path

ON_DEMAND_PER_HOUR = 1.861  # g6e.xlarge, us-east-1 (checked against third-party price pages)
ROOMS_PER_HOME = 4


def summarize(seconds: list[float]) -> dict:
    """First-room and steady-state timing. Steady state excludes the first (warm-up) room."""
    steady = seconds[1:] or seconds
    return {
        "first_room_s": round(seconds[0], 2),
        "steady_mean_s": round(sum(steady) / len(steady), 2),
        "steady_min_s": round(min(steady), 2),
        "steady_max_s": round(max(steady), 2),
    }


def project_cost(seconds_per_room: float, homes: int = 10, overhead_minutes: float = 15.0) -> float:
    """Rough on-demand cost of generating ``homes`` homes in one session, including start-up overhead."""
    hours = (seconds_per_room * ROOMS_PER_HOME * homes) / 3600 + overhead_minutes / 60
    return round(hours * ON_DEMAND_PER_HOUR, 2)


def _versions() -> dict:
    versions = {"python": platform.python_version()}
    for name in ("torch", "diffusers", "transformers", "peft"):
        try:
            versions[name] = __import__(name).__version__
        except Exception:  # noqa: BLE001 - absent on the mock path
            versions[name] = None
    return versions


def _gpu_info() -> dict:
    try:
        import torch

        props = torch.cuda.get_device_properties(0)
        return {"name": props.name, "vram_gb": round(props.total_memory / 1e9, 1)}
    except Exception:  # noqa: BLE001
        return {"name": None, "vram_gb": None}


def run(manifest_path: Path, backend: str, repo: str | None, rooms: int, steps: int | None, out: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    nodes = manifest["topology"]["nodes"]
    chosen = [nodes[i % len(nodes)] for i in range(rooms)]
    out.mkdir(parents=True, exist_ok=True)

    peak_gb = None
    load_s = 0.0
    if backend == "mock":
        from .generate import render_mock

        def render(room: dict, path: Path) -> None:
            render_mock(room, path)
    else:
        import torch

        from . import dit360

        started = time.perf_counter()
        pipe = dit360.load(repo)
        torch.cuda.synchronize()
        load_s = time.perf_counter() - started
        print(f"model loaded in {load_s:.1f}s", flush=True)
        torch.cuda.reset_peak_memory_stats()

        def render(room: dict, path: Path) -> None:
            params = room["diffusion_parameters"]
            image = dit360.generate(pipe, params["positive_prompt"], params["seed"], steps or dit360.STEPS)
            torch.cuda.synchronize()
            image.save(path, format="JPEG", quality=95)

    timings = []
    for n, room in enumerate(chosen):
        path = out / f"{n:02d}_{room['room_id']}.jpg"
        started = time.perf_counter()
        render(room, path)
        timings.append(time.perf_counter() - started)
        print(f"room {n + 1}/{len(chosen)} {room['room_id']}: {timings[-1]:.1f}s", flush=True)

    if backend != "mock":
        peak_gb = round(torch.cuda.max_memory_allocated() / 1e9, 1)

    summary = summarize(timings)
    results = {
        "backend": backend,
        "steps": steps,
        "rooms_run": len(timings),
        "model_load_s": round(load_s, 1),
        "peak_vram_gb": peak_gb,
        "per_room_s": [round(t, 2) for t in timings],
        **summary,
        "projected_cost_10_homes_usd_on_demand": project_cost(summary["steady_mean_s"]),
        "gpu": _gpu_info(),
        "versions": _versions(),
    }
    (out / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--backend", choices=["mock", "dit360"], default="mock")
    parser.add_argument("--repo", help="path to a clone of the DiT360 repo (dit360 backend)")
    parser.add_argument("--rooms", type=int, default=4, help="how many rooms to generate")
    parser.add_argument("--steps", type=int, help="override inference steps (default 28)")
    parser.add_argument("--out", type=Path, default=Path("out/benchmark"))
    args = parser.parse_args()
    if args.backend == "dit360" and not args.repo:
        raise SystemExit("--repo is required for the dit360 backend")
    results = run(args.manifest, args.backend, args.repo, args.rooms, args.steps, args.out)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
