"""Storage for projects, render jobs, the one-GPU lock and daily counters.

DynamoDBStore is the real thing (one table, string key ``pk``). MemoryStore has the same interface for tests.
Key layout: PROJECT#<id>, JOB#<id>, LOCK#<name>, CAP#<name>#<yyyymmdd>.
"""
from __future__ import annotations

import json
import threading
from decimal import Decimal

ACTIVE_STATES = ("waiting", "running")
PROJECT_TTL = 30 * 24 * 3600  # user projects expire after 30 days; gallery projects never do


def _to_dynamo(value):
    return json.loads(json.dumps(value), parse_float=Decimal)


def _from_dynamo(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


class DynamoDBStore:
    def __init__(self, table_name: str, session=None):
        """``session`` is a boto3 Session (the deploy script passes the chosen profile); Lambda uses its own role."""
        import boto3

        self.table = (session or boto3).resource("dynamodb").Table(table_name)

    def _get(self, pk: str) -> dict | None:
        item = self.table.get_item(Key={"pk": pk}).get("Item")
        return _from_dynamo(item) if item else None

    def put_project(self, project: dict) -> None:
        self.table.put_item(Item=_to_dynamo({"pk": f"PROJECT#{project['id']}", **project}))

    def get_project(self, project_id: str) -> dict | None:
        return self._get(f"PROJECT#{project_id}")

    def list_featured(self) -> list[dict]:
        from boto3.dynamodb.conditions import Attr

        reply = self.table.scan(FilterExpression=Attr("pk").begins_with("PROJECT#") & Attr("featured").eq(True))
        return sorted((_from_dynamo(i) for i in reply.get("Items", [])), key=lambda p: p.get("order", 99))

    def put_job(self, job: dict) -> None:
        self.table.put_item(Item=_to_dynamo({"pk": f"JOB#{job['id']}", **job}))

    def get_job(self, job_id: str) -> dict | None:
        return self._get(f"JOB#{job_id}")

    def update_job(self, job_id: str, fields: dict) -> None:
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": v for i, v in enumerate(_to_dynamo(fields).values())}
        expression = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        self.table.update_item(Key={"pk": f"JOB#{job_id}"}, UpdateExpression=expression,
                               ExpressionAttributeNames=names, ExpressionAttributeValues=values,
                               ConditionExpression="attribute_exists(pk)")

    def active_jobs(self) -> list[dict]:
        from boto3.dynamodb.conditions import Attr

        reply = self.table.scan(FilterExpression=Attr("pk").begins_with("JOB#") & Attr("state").is_in(list(ACTIVE_STATES)))
        return [_from_dynamo(i) for i in reply.get("Items", [])]

    def acquire_lock(self, name: str, owner: str, ttl: float, now: float) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.table.put_item(
                Item={"pk": f"LOCK#{name}", "owner": owner, "expires": Decimal(str(now + ttl)), "ttl": int(now + ttl + 3600)},
                ConditionExpression="attribute_not_exists(pk) OR expires < :now",
                ExpressionAttributeValues={":now": Decimal(str(now))})
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def release_lock(self, name: str, owner: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self.table.delete_item(Key={"pk": f"LOCK#{name}"}, ConditionExpression="#o = :owner",
                                   ExpressionAttributeNames={"#o": "owner"}, ExpressionAttributeValues={":owner": owner})
        except ClientError as err:
            if err.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def bump(self, name: str, day: str, limit: int, now: float) -> bool:
        """Add one to today's counter unless it has reached ``limit``. Returns whether it was added."""
        from botocore.exceptions import ClientError

        try:
            self.table.update_item(
                Key={"pk": f"CAP#{name}#{day}"}, UpdateExpression="ADD n :one SET #t = :ttl",
                ConditionExpression="attribute_not_exists(n) OR n < :limit",
                ExpressionAttributeNames={"#t": "ttl"},
                ExpressionAttributeValues={":one": 1, ":limit": limit, ":ttl": int(now + 3 * 24 * 3600)})
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise


class MemoryStore:
    """In-memory stand-in with the same interface, for tests."""

    def __init__(self):
        self.projects, self.jobs, self.locks, self.counters = {}, {}, {}, {}
        self._lock = threading.Lock()

    def put_project(self, project: dict) -> None:
        self.projects[project["id"]] = json.loads(json.dumps(project))

    def get_project(self, project_id: str) -> dict | None:
        project = self.projects.get(project_id)
        return json.loads(json.dumps(project)) if project else None

    def list_featured(self) -> list[dict]:
        return sorted((json.loads(json.dumps(p)) for p in self.projects.values() if p.get("featured")),
                      key=lambda p: p.get("order", 99))

    def put_job(self, job: dict) -> None:
        self.jobs[job["id"]] = json.loads(json.dumps(job))

    def get_job(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        return json.loads(json.dumps(job)) if job else None

    def update_job(self, job_id: str, fields: dict) -> None:
        self.jobs[job_id].update(json.loads(json.dumps(fields)))

    def active_jobs(self) -> list[dict]:
        return [json.loads(json.dumps(j)) for j in self.jobs.values() if j["state"] in ACTIVE_STATES]

    def acquire_lock(self, name: str, owner: str, ttl: float, now: float) -> bool:
        with self._lock:
            held = self.locks.get(name)
            if held and held["expires"] >= now:
                return False
            self.locks[name] = {"owner": owner, "expires": now + ttl}
            return True

    def release_lock(self, name: str, owner: str) -> None:
        with self._lock:
            if self.locks.get(name, {}).get("owner") == owner:
                del self.locks[name]

    def bump(self, name: str, day: str, limit: int, now: float) -> bool:
        with self._lock:
            key = (name, day)
            if self.counters.get(key, 0) >= limit:
                return False
            self.counters[key] = self.counters.get(key, 0) + 1
            return True
