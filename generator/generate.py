"""Generate one equirectangular panorama per room in a manifest.

    python -m generator.generate --manifest out/manifest.json --backend mock
    python -m generator.generate --manifest out/manifest.json --backend dit360 --s3-bucket my-bucket

Rooms whose panorama already exists are skipped, so an interrupted run can be resumed.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

WIDTH, HEIGHT = 2048, 1024  # 2:1 equirectangular, matching DiT360's 1024 x 2048 default


def render_mock(room: dict, out_path: Path) -> None:
    """Placeholder panorama: sky/floor gradient with a grid, so the sphere mapping is visible."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT))
    px = img.load()
    seed = room["diffusion_parameters"]["seed"]
    tint = ((seed >> 0) % 60, (seed >> 8) % 60, (seed >> 16) % 60)
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        base = (200 - int(110 * t), 205 - int(120 * t), 215 - int(130 * t)) if t < 0.5 else (
            90 + int(70 * (t - 0.5)), 80 + int(60 * (t - 0.5)), 70 + int(50 * (t - 0.5)))
        row = tuple(min(255, c + tint[i]) for i, c in enumerate(base))
        for x in range(WIDTH):
            px[x, y] = row
    draw = ImageDraw.Draw(img)
    for x in range(0, WIDTH, WIDTH // 16):
        draw.line([(x, 0), (x, HEIGHT)], fill=(255, 255, 255), width=2)
    for y in range(0, HEIGHT, HEIGHT // 8):
        draw.line([(0, y), (WIDTH, y)], fill=(255, 255, 255), width=2)
    for i in range(4):  # the room name at four headings
        draw.text((WIDTH * i // 4 + 80, HEIGHT // 2 - 40), room["room_name"].upper(), fill=(20, 20, 20))
    img.save(out_path, format="JPEG", quality=90)


class Dit360Backend:
    """DiT360 on FLUX.1-dev. Needs about 37 GB of GPU memory; use a g6e.xlarge (L40S).

    Follows the DiT360 repo's inference.py (see generator/dit360.py). Not yet run end to end: use
    generator.benchmark on a GPU instance first.
    """

    def __init__(self, repo_dir: str) -> None:
        from . import dit360

        self._dit360 = dit360
        self._pipe = dit360.load(repo_dir)

    def __call__(self, room: dict, out_path: Path) -> None:
        params = room["diffusion_parameters"]
        image = self._dit360.generate(self._pipe, params["positive_prompt"], params["seed"])
        image.save(out_path, format="JPEG", quality=95)


def upload(path: Path, bucket: str, key: str) -> None:
    import boto3

    boto3.client("s3").upload_file(
        str(path), bucket, key,
        ExtraArgs={"ContentType": "image/jpeg", "CacheControl": "public, max-age=31536000"},
    )


def run(manifest_path: Path, backend: str, s3_bucket: str | None, prefix: str, repo_dir: str | None = None,
        seam_repair: bool = True) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out_dir = manifest_path.parent
    if backend == "dit360" and not repo_dir:
        raise SystemExit("--dit360-repo is required for the dit360 backend")
    render = render_mock if backend == "mock" else Dit360Backend(repo_dir)

    generated = 0
    for room in manifest["topology"]["nodes"]:
        out_path = out_dir / room["asset_url"]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            print(f"skip   {room['room_id']} (exists)")
            continue
        started = time.time()
        render(room, out_path)
        generated += 1
        print(f"done   {room['room_id']} in {time.time() - started:.1f}s -> {out_path}")
        if seam_repair:
            from .seam import repair_file

            result = repair_file(out_path)
            note = "repaired" if result["repaired"] else "clean"
            print(f"seam   {room['room_id']} {note}: score {result['before']} -> {result['after']}")
        if s3_bucket:
            upload(out_path, s3_bucket, f"{prefix}/{manifest['project_id']}/{room['asset_url']}")
    if s3_bucket:
        key = f"{prefix}/{manifest['project_id']}/manifest.json"
        import boto3
        boto3.client("s3").put_object(Bucket=s3_bucket, Key=key, Body=manifest_path.read_bytes(),
                                      ContentType="application/json")
    return generated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--backend", choices=["mock", "dit360"], default="mock")
    parser.add_argument("--dit360-repo", help="path to a clone of the DiT360 repo (dit360 backend)")
    parser.add_argument("--s3-bucket", help="also upload each panorama and the manifest to this bucket")
    parser.add_argument("--s3-prefix", default="projects")
    parser.add_argument("--no-seam-repair", action="store_true", help="keep the raw left-right seam")
    args = parser.parse_args()
    count = run(args.manifest, args.backend, args.s3_bucket, args.s3_prefix, args.dit360_repo,
                seam_repair=not args.no_seam_repair)
    print(f"generated {count} panorama(s)")


if __name__ == "__main__":
    main()
