"""The few AWS calls the app makes, in one place so the job logic can be tested with a fake."""
from __future__ import annotations

from pathlib import Path

from infra.common import REGION, bucket_name, session
from infra.launch import launch_instance

INSTANCE_NAME_TAG = "ai-interior-benchmark"  # launch.py tags every GPU job instance with this name


class Aws:
    def __init__(self, profile: str | None = None, region: str = REGION):
        self.profile, self.region = profile, region
        sess = session(profile)
        self.s3 = sess.client("s3")
        self.ec2 = session(profile, region).client("ec2")
        self.bucket = bucket_name(sess)

    def launch(self, manifest_path: Path, instance_type: str, max_minutes: int, log=print) -> dict:
        """Start a render job instance. Raises infra.launch.NoCapacity when AWS has no GPU free."""
        return launch_instance(profile=self.profile, job="render", manifest=manifest_path,
                               instance_type=instance_type, region=self.region, max_minutes=max_minutes, log=log)

    def instance_state(self, instance_id: str) -> str:
        reply = self.ec2.describe_instances(InstanceIds=[instance_id])
        return reply["Reservations"][0]["Instances"][0]["State"]["Name"]

    def running_gpu_instances(self) -> list[str]:
        reply = self.ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": [INSTANCE_NAME_TAG]},
            {"Name": "instance-state-name", "Values": ["pending", "running"]},
        ])
        return [i["InstanceId"] for r in reply["Reservations"] for i in r["Instances"]]

    def terminate(self, instance_id: str) -> None:
        self.ec2.terminate_instances(InstanceIds=[instance_id])

    def list_panos(self, project_id: str) -> list[str]:
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,
                                                                       Prefix=f"renders/{project_id}/panos/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys

    def download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        self.s3.download_file(self.bucket, key, str(tmp))
        tmp.replace(dest)  # a half-downloaded image is never visible to the viewer

    def log_text(self, run: str) -> str:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=f"benchmarks/{run}/benchmark.log")["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return ""
        return body[-40_000:].decode("utf-8", errors="replace")

    def remote_projects(self) -> list[str]:
        ids: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="renders/",
                                                                       Delimiter="/"):
            ids += [p["Prefix"].split("/")[1] for p in page.get("CommonPrefixes", [])]
        return ids

    def get_manifest(self, project_id: str) -> bytes:
        return self.s3.get_object(Bucket=self.bucket, Key=f"renders/{project_id}/manifest.json")["Body"].read()
