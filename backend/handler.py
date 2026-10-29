"""AWS Lambda entry point: HTTP API requests and the once-a-minute scheduler both arrive here."""
from __future__ import annotations

import base64
import json
import os

from .core import Backend, Config

_backend: Backend | None = None


def _build() -> Backend:
    import boto3

    from .aws_io import BedrockPlanner, CloudAws
    from .store import DynamoDBStore

    config = Config(access_code=os.environ.get("ACCESS_CODE", ""),
                    daily_render_cap=int(os.environ.get("DAILY_RENDER_CAP", "6")),
                    daily_plan_cap=int(os.environ.get("DAILY_PLAN_CAP", "100")))
    self_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
    client = boto3.client("lambda")

    def trigger() -> None:
        if self_name:
            client.invoke(FunctionName=self_name, InvocationType="Event", Payload=json.dumps({"tick": True}).encode())

    return Backend(DynamoDBStore(os.environ["TABLE_NAME"]),
                   CloudAws(os.environ["BUCKET"], os.environ["CODE_KEY"]), BedrockPlanner(), config, trigger)


def lambda_handler(event, context):
    global _backend
    if _backend is None:
        _backend = _build()
    if event.get("source") == "aws.events" or event.get("tick"):
        _backend.tick()
        return {"ok": True}

    http = event["requestContext"]["http"]
    if http["method"] == "OPTIONS":
        # The browser's pre-flight check before a POST. API Gateway adds the CORS headers from its own settings; the
        # catch-all route sends this request here, so it must succeed instead of falling through to "not found".
        return {"statusCode": 204}
    raw = event.get("body") or ""
    body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    try:
        status, payload = _backend.handle(http["method"], event.get("rawPath", "/"), headers, body)
    except Exception:  # noqa: BLE001 - log the cause for the owner, show the visitor nothing technical
        import traceback

        traceback.print_exc()
        status, payload = 500, {"error": "Something went wrong. Please try again."}
    return {"statusCode": status, "body": json.dumps(payload),
            "headers": {"Content-Type": "application/json", "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff"}}
