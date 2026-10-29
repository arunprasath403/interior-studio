"""Create the three things the GPU run needs: a private S3 bucket, and an IAM role + instance profile.

    python -m infra.setup --profile personal            # print the plan only
    python -m infra.setup --profile personal --apply    # create (safe to re-run)

The role can only read and write this one bucket. Nothing here costs money by itself.
"""
from __future__ import annotations

import argparse
import json
import time

from botocore.exceptions import ClientError

from .common import REGION, ROLE_NAME, account_id, bucket_name, session

TRUST = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}],
}


def bucket_policy(bucket: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": f"arn:aws:s3:::{bucket}/*"},
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{bucket}"},
        ],
    }


def ensure_bucket(s3, bucket: str) -> str:
    try:
        s3.head_bucket(Bucket=bucket)
        return "exists"
    except ClientError as err:
        if err.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
    s3.create_bucket(Bucket=bucket)  # us-east-1 takes no LocationConstraint
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )
    return "created"


def ensure_role(iam, bucket: str) -> str:
    status = "exists"
    try:
        iam.get_role(RoleName=ROLE_NAME)
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST),
                        Description="AI Interior 3D GPU instance: access to one bucket")
        status = "created"
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="bucket-access", PolicyDocument=json.dumps(bucket_policy(bucket)))
    try:
        iam.get_instance_profile(InstanceProfileName=ROLE_NAME)
    except iam.exceptions.NoSuchEntityException:
        iam.create_instance_profile(InstanceProfileName=ROLE_NAME)
        iam.add_role_to_instance_profile(InstanceProfileName=ROLE_NAME, RoleName=ROLE_NAME)
        status = "created"
        time.sleep(10)  # a new instance profile takes a few seconds to become usable
    return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="AWS CLI profile name")
    parser.add_argument("--apply", action="store_true", help="actually create resources")
    args = parser.parse_args()

    sess = session(args.profile)
    bucket = bucket_name(sess)
    print(f"account {account_id(sess)}, region {REGION}")
    print(f"plan: S3 bucket {bucket} (private)")
    print(f"      IAM role + instance profile {ROLE_NAME} (read/write that bucket only)")
    if not args.apply:
        print("dry run: nothing created. Re-run with --apply.")
        return
    print(f"bucket: {ensure_bucket(sess.client('s3'), bucket)}")
    print(f"role:   {ensure_role(sess.client('iam'), bucket)}")
    print(f"\nNext: put your Hugging Face token (with FLUX.1-dev access) in the bucket, from a file you create:\n"
          f"  aws s3 cp hf-token.txt s3://{bucket}/secrets/hf-token" + (f" --profile {args.profile}" if args.profile else ""))


if __name__ == "__main__":
    main()
