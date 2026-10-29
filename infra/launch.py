"""Launch one GPU instance that runs a job, uploads results to S3, then shuts down.

    python -m infra.launch --profile personal --dry-run                  # validate with EC2 DryRun, launches nothing
    python -m infra.launch --profile personal                            # benchmark 4 rooms of a sample home
    python -m infra.launch --profile personal --job render --manifest out/manifest.json

Safety: the instance terminates when the script ends (success or failure), and a dead-man switch shuts it
down after --max-minutes regardless. Benchmark results land in s3://<bucket>/benchmarks/<run>/ and rendered
panoramas in s3://<bucket>/renders/<project_id>/.
"""
from __future__ import annotations

import argparse
import io
import json
import tarfile
import time
import uuid
from pathlib import Path

from botocore.config import Config
from botocore.exceptions import ClientError

from .common import HF_TOKEN_KEY, INSTANCE_TYPE, REGION, ROLE_NAME, bucket_name, session

ROOT = Path(__file__).resolve().parent.parent
AMI_PARAM = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id"
PACKAGES = ("engine", "generator")


class NoCapacity(Exception):
    """No GPU capacity (or vCPU quota headroom) right now. Trying again later may work."""


USER_DATA = """#!/bin/bash
# Dead-man switch: power off after {max_minutes} minutes no matter what, so a stuck run cannot keep billing.
shutdown -h +{max_minutes}
exec >> /var/log/benchmark.log 2>&1  # append mode, so the monitoring loop below cannot be overwritten
cleanup() {{
  aws s3 cp /var/log/benchmark.log "s3://{bucket}/benchmarks/{run}/benchmark.log" --region {region}
  shutdown -h now
}}
trap cleanup EXIT
# Every minute: record memory, load and GPU use in the log, then upload it, so a stall is diagnosable.
( while true; do sleep 60
  {{ echo "== $(date -u +%T) =="; free -m | sed -n 2p; uptime; nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader; }} >> /var/log/benchmark.log 2>&1
  aws s3 cp /var/log/benchmark.log "s3://{bucket}/benchmarks/{run}/benchmark.log" --region {region} --only-show-errors
done ) &
set -ex
export HOME=/root
cd /opt
# Safety net: 32 GB of swap so a memory spike slows the run instead of freezing or killing it.
(fallocate -l 32G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile) || echo "swap setup failed, continuing without it"
aws s3 cp "s3://{bucket}/{code_key}" code.tgz --region {region}
mkdir app && tar xzf code.tgz -C app
git clone --depth 1 https://github.com/Insta360-Research-Team/DiT360.git
command -v pip3 || (apt-get update && apt-get install -y python3-pip)
pip3 install uv
uv venv --python 3.12 /opt/venv
uv pip install --python /opt/venv/bin/python torch==2.6.0 torchvision==0.21.0
uv pip install --python /opt/venv/bin/python accelerate sentencepiece protobuf transformers==4.52.4 peft \\
  opencv-python-headless py360convert pillow boto3 huggingface_hub psutil pyyaml tqdm \\
  diffusers==0.35.2
/opt/venv/bin/python -c "import sys; sys.path.insert(0, '/opt/DiT360'); from src.pipeline import DiT360Pipeline; print('DiT360 import OK')"
set +x
export HF_TOKEN="$(aws s3 cp s3://{bucket}/{token_key} - --region {region} | tr -d '[:space:]')"
set -x
cd /opt/app
{job_commands}
"""

BENCHMARK_JOB = """/opt/venv/bin/python -m engine.plan --budget 40000 --area 90 --bhk 2BHK --style "dark academia" --out out/manifest.json
/opt/venv/bin/python -m generator.benchmark --manifest out/manifest.json --backend dit360 \\
  --repo /opt/DiT360 --rooms {rooms} --out out/benchmark
aws s3 cp --recursive out/benchmark "s3://{bucket}/benchmarks/{run}/" --region {region}"""

# Renders the uploaded manifest. generate.py repairs seams and uploads each panorama as it finishes, so an
# interrupted run keeps what it already made (and skips those rooms on a rerun).
RENDER_JOB = """mkdir -p out
aws s3 cp "s3://{bucket}/{manifest_key}" out/manifest.json --region {region}
/opt/venv/bin/python -m generator.generate --manifest out/manifest.json --backend dit360 \\
  --dit360-repo /opt/DiT360 --s3-bucket {bucket} --s3-prefix renders"""


def build_user_data(*, bucket: str, run: str, code_key: str, max_minutes: int, job: str = "benchmark",
                    rooms: int = 4, manifest_key: str = "") -> str:
    template = {"benchmark": BENCHMARK_JOB, "render": RENDER_JOB}[job]
    job_commands = template.format(bucket=bucket, run=run, region=REGION, rooms=rooms, manifest_key=manifest_key)
    return USER_DATA.format(bucket=bucket, run=run, region=REGION, code_key=code_key, token_key=HF_TOKEN_KEY,
                            max_minutes=max_minutes, job_commands=job_commands)


def package_code() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for package in PACKAGES:
            tar.add(ROOT / package, arcname=package, filter=lambda t: None if "__pycache__" in t.name else t)
    return buffer.getvalue()


def pick_subnets(ec2, instance_type: str) -> list[tuple[str, str]]:
    """Default-VPC subnets (id, zone) in availability zones that offer the instance type."""
    offered = {
        o["Location"] for o in ec2.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [instance_type]}],
        )["InstanceTypeOfferings"]
    }
    subnets = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
    found = [(s["SubnetId"], s["AvailabilityZone"]) for s in sorted(subnets, key=lambda s: s["AvailabilityZone"])
             if s["AvailabilityZone"] in offered]
    if not found:
        raise SystemExit(f"no default subnet in an AZ that offers {instance_type}")
    return found


def launch_instance(*, profile: str | None = None, job: str = "benchmark", manifest: Path | None = None,
                    manifest_bytes: bytes | None = None, code_key: str | None = None, check_token: bool = True,
                    rooms: int = 4, instance_type: str = INSTANCE_TYPE, region: str = REGION, spot: bool = False,
                    max_minutes: int = 90, volume_gb: int = 200, dry_run: bool = False, log=print) -> dict:
    """Start one job instance and return {"instance_id", "zone", "run", "region", "bucket"}.

    Raises NoCapacity when no zone has capacity or the vCPU quota is in use, so callers can retry later.
    In dry-run mode nothing is launched and {"dry_run": True, ...} is returned.

    ``manifest_bytes`` replaces the ``manifest`` file (used by the cloud backend), ``code_key`` points at code
    already uploaded to the bucket (so the caller need not package it), and ``check_token=False`` skips reading the
    Hugging Face token object, which the cloud backend's role is deliberately not allowed to read.
    """
    manifest_data = manifest_bytes
    if job == "render":
        if manifest_data is None:
            if not manifest or not Path(manifest).exists():
                raise ValueError("job 'render' needs a manifest.json that exists")
            manifest_data = Path(manifest).read_bytes()
        if not dry_run:
            json.loads(manifest_data.decode("utf-8"))  # fail here, not on a billed GPU

    sess = session(profile)  # the S3 bucket lives in us-east-1
    s3 = sess.client("s3")
    ec2_sess = session(profile, region)
    # No automatic retries: AWS answers "no capacity" instantly, and retrying made each attempt take about a minute.
    ec2 = ec2_sess.client("ec2", config=Config(retries={"max_attempts": 1, "mode": "standard"}))
    bucket = bucket_name(sess)

    if check_token:
        try:
            s3.head_object(Bucket=bucket, Key=HF_TOKEN_KEY)
        except ClientError:
            raise SystemExit(f"missing s3://{bucket}/{HF_TOKEN_KEY}. Run infra.setup --apply, then upload your token.")

    ami = ec2_sess.client("ssm").get_parameter(Name=AMI_PARAM)["Parameter"]["Value"]
    candidates = pick_subnets(ec2, instance_type)
    run = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]  # unique even for two launches in one second
    manifest_key = f"jobs/{run}/manifest.json"

    if not dry_run:
        if code_key is None:
            code_key = f"code/{run}.tgz"
            s3.put_object(Bucket=bucket, Key=code_key, Body=package_code())
        if job == "render":
            s3.put_object(Bucket=bucket, Key=manifest_key, Body=manifest_data, ContentType="application/json")
    code_key = code_key or f"code/{run}.tgz"

    params = dict(
        ImageId=ami,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        IamInstanceProfile={"Name": ROLE_NAME},
        InstanceInitiatedShutdownBehavior="terminate",
        BlockDeviceMappings=[{"DeviceName": "/dev/sda1",
                              # The default gp3 speed (125 MB/s) made loading the 33 GB of weights take 15+ minutes.
                              "Ebs": {"VolumeSize": volume_gb, "VolumeType": "gp3", "DeleteOnTermination": True,
                                      "Iops": 6000, "Throughput": 500}}],
        MetadataOptions={"HttpTokens": "required"},
        UserData=build_user_data(bucket=bucket, run=run, code_key=code_key, max_minutes=max_minutes,
                                 job=job, rooms=rooms, manifest_key=manifest_key),
        TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "ai-interior-benchmark"}]}],
        DryRun=dry_run,
    )
    if spot:
        params["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": {
            "SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}}

    for subnet, zone in candidates:
        network = [{"DeviceIndex": 0, "SubnetId": subnet, "AssociatePublicIpAddress": True}]
        try:
            instance = ec2.run_instances(**params, NetworkInterfaces=network)["Instances"][0]
        except ClientError as err:
            code = err.response["Error"]["Code"]
            if dry_run and code == "DryRunOperation":
                return {"dry_run": True, "zone": zone, "ami": ami, "run": run, "region": region, "bucket": bucket}
            if code == "InsufficientInstanceCapacity":
                log(f"no {instance_type} capacity in {zone}, trying the next zone")
                continue
            if code == "VcpuLimitExceeded":
                raise NoCapacity("the GPU vCPU quota is in use (another instance may still be shutting down)") from err
            raise
        return {"instance_id": instance["InstanceId"], "zone": zone, "run": run, "region": region, "bucket": bucket}
    raise NoCapacity(f"no {instance_type} capacity in any zone ({', '.join(z for _, z in candidates)})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="AWS CLI profile name")
    parser.add_argument("--rooms", type=int, default=4, help="rooms to generate in the benchmark")
    parser.add_argument("--max-minutes", type=int, default=90, help="dead-man switch")
    parser.add_argument("--volume-gb", type=int, default=200)
    parser.add_argument("--spot", action="store_true", help="use Spot (needs Spot quota; may be interrupted)")
    parser.add_argument("--instance-type", default=INSTANCE_TYPE, help="needs a 48 GB GPU, e.g. g6e.xlarge/2xlarge")
    parser.add_argument("--region", default=REGION, help="EC2 region (the bucket stays in us-east-1)")
    parser.add_argument("--dry-run", action="store_true", help="validate only; launch nothing")
    parser.add_argument("--job", choices=["benchmark", "render"], default="benchmark",
                        help="benchmark: time 4 rooms of a sample home. render: generate a manifest's panoramas")
    parser.add_argument("--manifest", type=Path, help="manifest.json to render (with --job render)")
    args = parser.parse_args()
    if args.job == "render" and (not args.manifest or not args.manifest.exists()):
        parser.error("--job render needs --manifest pointing at an existing manifest.json")

    try:
        result = launch_instance(profile=args.profile, job=args.job, manifest=args.manifest, rooms=args.rooms,
                                 instance_type=args.instance_type, region=args.region, spot=args.spot,
                                 max_minutes=args.max_minutes, volume_gb=args.volume_gb, dry_run=args.dry_run)
    except NoCapacity as err:
        raise SystemExit(f"{err}. Try again later.")

    if result.get("dry_run"):
        print(f"dry run OK: would launch {args.instance_type} ({result['ami']}) in {result['zone']}, "
              f"{'spot' if args.spot else 'on-demand'}")
        return
    bucket, run = result["bucket"], result["run"]
    flag = f" --profile {args.profile}" if args.profile else ""
    print(f"launched {result['instance_id']} in {result['zone']}. Run id: {run}")
    if args.job == "render":
        print(f"panoramas (about 10-15 minutes):  aws s3 ls s3://{bucket}/renders/ --recursive{flag}")
    else:
        print(f"results (about 15-25 minutes):  aws s3 ls s3://{bucket}/benchmarks/{run}/{flag}")
    print(f"log:  aws s3 cp s3://{bucket}/benchmarks/{run}/benchmark.log -{flag}")
    print(f"stop early:  aws ec2 terminate-instances --instance-ids {result['instance_id']} --region {args.region}{flag}")


if __name__ == "__main__":
    main()
