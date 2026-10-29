from __future__ import annotations

import boto3

REGION = "us-east-1"
ROLE_NAME = "ai-interior-gpu"  # also the instance profile name
INSTANCE_TYPE = "g6e.xlarge"  # NVIDIA L40S, 48 GB: DiT360 needs about 37 GB
HF_TOKEN_KEY = "secrets/hf-token"


def session(profile: str | None, region: str = REGION) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


def account_id(sess: boto3.Session) -> str:
    return sess.client("sts").get_caller_identity()["Account"]


def bucket_name(sess: boto3.Session) -> str:
    return f"ai-interior-3d-{account_id(sess)}"
