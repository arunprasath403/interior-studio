"""Deploy the public site: backend (CloudFormation), website (Amplify Hosting), image access (bucket CORS), gallery.

    python -m infra.deploy --profile personal all        # everything, in the right order
    python -m infra.deploy --profile personal backend    # just the API (after changing backend/ or engine/)
    python -m infra.deploy --profile personal site       # just the website (after changing site/)
    python -m infra.deploy --profile personal gallery    # (re)load the featured homes from out/projects

The invitation code is generated on the first deploy and printed once; pass --access-code to set your own, or to
change it later. It is stored only as a CloudFormation NoEcho parameter and a Lambda environment variable.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import secrets
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from .common import REGION, ROLE_NAME, bucket_name, session
from .launch import package_code

ROOT = Path(__file__).resolve().parent.parent
STACK = "interior-studio"
APP_NAME = "interior-studio"
BRANCH = "main"
LAMBDA_DIRS = ("backend", "engine")
LAMBDA_FILES = ("infra/__init__.py", "infra/common.py", "infra/launch.py", "app/__init__.py", "app/jobs.py")
HEADERS = """customHeaders:
  - pattern: '**'
    headers:
      - key: Strict-Transport-Security
        value: max-age=31536000; includeSubDomains
      - key: X-Content-Type-Options
        value: nosniff
      - key: X-Frame-Options
        value: DENY
      - key: Referrer-Policy
        value: strict-origin-when-cross-origin
      - key: Permissions-Policy
        value: camera=(), microphone=(), geolocation=()
"""


def say(message: str) -> None:
    print(message, flush=True)


def lambda_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in LAMBDA_DIRS:
            for path in sorted((ROOT / folder).rglob("*.py")):
                if path.name != "dev.py":  # the local preview stays out of the cloud package
                    archive.write(path, path.relative_to(ROOT).as_posix())
        for name in LAMBDA_FILES:
            archive.write(ROOT / name, name)
    return buffer.getvalue()


def upload_versioned(s3, bucket: str, prefix: str, suffix: str, data: bytes) -> str:
    key = f"{prefix}/{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
    s3.put_object(Bucket=bucket, Key=key, Body=data)
    return key


# --- Amplify ----------------------------------------------------------------------------------------------------
def ensure_amplify(amplify) -> dict:
    for app in amplify.list_apps()["apps"]:
        if app["name"] == APP_NAME:
            amplify.update_app(appId=app["appId"], customHeaders=HEADERS)
            break
    else:
        app = amplify.create_app(name=APP_NAME, platform="WEB", customHeaders=HEADERS,
                                 description="Interior design studio website")["app"]
    try:
        amplify.create_branch(appId=app["appId"], branchName=BRANCH, stage="PRODUCTION")
    except amplify.exceptions.BadRequestException:
        pass  # the branch already exists
    return {"app_id": app["appId"], "origin": f"https://{BRANCH}.{app['defaultDomain']}"}


def site_zip(api_url: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((ROOT / "site").rglob("*")):
            if path.is_file() and path.name != "config.js":
                archive.write(path, path.relative_to(ROOT / "site").as_posix())
        archive.writestr("config.js", f'window.STUDIO_API = {json.dumps(api_url)};\n')
    return buffer.getvalue()


def deploy_site(amplify, app_id: str, api_url: str) -> None:
    deployment = amplify.create_deployment(appId=app_id, branchName=BRANCH)
    request = urllib.request.Request(deployment["zipUploadUrl"], data=site_zip(api_url), method="PUT",
                                     headers={"Content-Type": "application/zip"})
    urllib.request.urlopen(request, timeout=120).read()
    amplify.start_deployment(appId=app_id, branchName=BRANCH, jobId=deployment["jobId"])
    for _ in range(60):
        status = amplify.get_job(appId=app_id, branchName=BRANCH, jobId=deployment["jobId"])["job"]["summary"]["status"]
        if status in ("SUCCEED", "FAILED", "CANCELLED"):
            if status != "SUCCEED":
                raise SystemExit(f"site deployment {status.lower()}")
            return
        time.sleep(3)
    raise SystemExit("site deployment did not finish in time")


# --- backend ----------------------------------------------------------------------------------------------------
def stack_outputs(cfn) -> dict | None:
    try:
        stack = cfn.describe_stacks(StackName=STACK)["Stacks"][0]
    except ClientError:
        return None
    return {"status": stack["StackStatus"], "outputs": {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])},
            "parameters": {p["ParameterKey"]: p.get("ParameterValue") for p in stack["Parameters"]}}


def deploy_backend(sess, bucket: str, origin: str, access_code: str | None) -> dict:
    s3, cfn = sess.client("s3"), sess.client("cloudformation")
    lambda_key = upload_versioned(s3, bucket, "lambda/backend", ".zip", lambda_zip())
    code_key = upload_versioned(s3, bucket, "code/studio", ".tgz", package_code())
    existing = stack_outputs(cfn)
    generated = None
    if access_code is None and not existing:
        access_code = generated = secrets.token_urlsafe(9)
    params = [{"ParameterKey": "BucketName", "ParameterValue": bucket},
              {"ParameterKey": "AllowedOrigin", "ParameterValue": origin},
              {"ParameterKey": "LambdaKey", "ParameterValue": lambda_key},
              {"ParameterKey": "CodeKey", "ParameterValue": code_key},
              {"ParameterKey": "GpuRoleName", "ParameterValue": ROLE_NAME}]
    params.append({"ParameterKey": "AccessCode", "ParameterValue": access_code} if access_code
                  else {"ParameterKey": "AccessCode", "UsePreviousValue": True})
    template = (ROOT / "infra" / "backend.yaml").read_text(encoding="utf-8")
    action = "update" if existing else "create"
    say(f"{action} stack {STACK} ...")
    try:
        if existing:
            cfn.update_stack(StackName=STACK, TemplateBody=template, Parameters=params, Capabilities=["CAPABILITY_IAM"])
        else:
            cfn.create_stack(StackName=STACK, TemplateBody=template, Parameters=params, Capabilities=["CAPABILITY_IAM"])
        cfn.get_waiter(f"stack_{action}_complete").wait(StackName=STACK, WaiterConfig={"Delay": 5, "MaxAttempts": 120})
    except ClientError as err:
        if "No updates are to be performed" not in str(err):
            raise
        say("stack already up to date")
    result = stack_outputs(cfn)["outputs"]
    result["generated_code"] = generated
    return result


def set_bucket_cors(s3, bucket: str, origin: str) -> None:
    s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={"CORSRules": [{
        "AllowedOrigins": [origin], "AllowedMethods": ["GET", "HEAD"], "AllowedHeaders": ["*"], "MaxAgeSeconds": 3000}]})


# --- gallery ----------------------------------------------------------------------------------------------------
FEATURED = (  # (project folder under out/projects, title shown on the site)
    ("proj_fcb8467b", "Warm Scandinavian family home"),
    ("proj_3e3de056-261029091311", "Calm Japandi studio"),
)


def seed_gallery(sess, table: str) -> None:
    from backend.store import DynamoDBStore

    store = DynamoDBStore(table, sess)  # the chosen profile and region, not whatever the default happens to be
    for order, (folder, title) in enumerate(FEATURED, start=1):
        path = ROOT / "out" / "projects" / folder / "manifest.json"
        if not path.exists():
            say(f"skip {folder}: no local manifest")
            continue
        manifest = json.loads(path.read_text(encoding="utf-8"))
        store.put_project({"id": manifest["project_id"], "manifest": json.dumps(manifest), "featured": True,
                           "order": order, "title": title, "created": int(time.time())})
        say(f"gallery: {title} ({manifest['project_id']})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("part", choices=["all", "backend", "site", "gallery"])
    parser.add_argument("--profile", help="AWS CLI profile")
    parser.add_argument("--access-code", help="invitation code (generated on first deploy if omitted)")
    args = parser.parse_args()

    sess = session(args.profile)
    bucket = bucket_name(sess)
    amplify = sess.client("amplify")
    site = ensure_amplify(amplify)
    say(f"site address: {site['origin']}")
    outputs = stack_outputs(sess.client("cloudformation"))

    if args.part in ("all", "backend"):
        outputs = {"outputs": deploy_backend(sess, bucket, site["origin"], args.access_code)}
        set_bucket_cors(sess.client("s3"), bucket, site["origin"])
    elif not outputs:
        raise SystemExit("deploy the backend first (python -m infra.deploy backend)")
    api_url = outputs["outputs"]["ApiUrl"]
    say(f"api address:  {api_url}")

    if args.part in ("all", "gallery"):
        seed_gallery(sess, outputs["outputs"]["TableName"])
    if args.part in ("all", "site"):
        deploy_site(amplify, site["app_id"], api_url)
        say(f"site is live: {site['origin']}")
    if outputs["outputs"].get("generated_code"):
        say(f"\nINVITATION CODE (shown once): {outputs['outputs']['generated_code']}")


if __name__ == "__main__":
    sys.exit(main())
