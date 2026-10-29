"""The AWS calls the cloud backend makes (inside Lambda, using the function's own role)."""
from __future__ import annotations

import os

import boto3
from botocore.config import Config

from infra.common import REGION
from infra.launch import launch_instance


class CloudAws:
    def __init__(self, bucket: str, code_key: str, region: str = REGION):
        self.bucket, self.code_key, self.region = bucket, code_key, region
        self.s3 = boto3.client("s3", region_name=region, config=Config(signature_version="s3v4"))
        self.ec2 = boto3.client("ec2", region_name=region)

    def launch(self, manifest_bytes: bytes, instance_type: str, max_minutes: int) -> dict:
        return launch_instance(job="render", manifest_bytes=manifest_bytes, code_key=self.code_key, check_token=False,
                               instance_type=instance_type, region=self.region, max_minutes=max_minutes)

    def instance_state(self, instance_id: str) -> str:
        reply = self.ec2.describe_instances(InstanceIds=[instance_id])
        return reply["Reservations"][0]["Instances"][0]["State"]["Name"]

    def terminate(self, instance_id: str) -> None:
        self.ec2.terminate_instances(InstanceIds=[instance_id])

    def list_panos(self, project_id: str) -> list[str]:
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket,
                                                                       Prefix=f"renders/{project_id}/panos/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys

    def presign(self, key: str, seconds: int) -> str:
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=seconds)

    def log_text(self, run: str) -> str:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=f"benchmarks/{run}/benchmark.log")["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return ""
        return body[-40_000:].decode("utf-8", errors="replace")


class BedrockPlanner:
    """Free-text briefs go to the Bedrock agents; structured requests use the plain rules."""

    def __init__(self):
        self._llm = None

    def plan(self, request: dict) -> dict:
        from engine.agents import AgentError, bedrock_llm, plan_from_brief
        from engine.manifest import build_manifest

        if "brief" not in request:
            return build_manifest(request["budget"], request["area_sqm"], request["bhk"], request["style"],
                                  request["currency"])
        if self._llm is None:
            self._llm = bedrock_llm(None, region=os.environ.get("BEDROCK_REGION", REGION))
        try:
            return plan_from_brief(self._llm, request["brief"])
        except AgentError as err:
            raise RuntimeError(str(err)) from err
