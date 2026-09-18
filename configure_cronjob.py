"""Create or update the cron-job.org health check for the cloud archive."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import urllib.error
import urllib.request

from local_env import load_local_env


API_ROOT = "https://api.cron-job.org"
JOB_TITLE = "Telegram group insights health"


def api_request(api_key: str, method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        API_ROOT + path,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8"))
            message = detail.get("message") or detail.get("error") or "request failed"
        except (json.JSONDecodeError, UnicodeError, AttributeError):
            message = "request failed"
        raise RuntimeError(f"cron-job.org HTTP {error.code}: {message}") from error


def job_payload(base_url: str, insights_api_key: str) -> dict:
    return {
        "title": JOB_TITLE,
        "url": base_url.rstrip("/") + "/api/status",
        "enabled": True,
        "saveResponses": False,
        "requestTimeout": 30,
        "redirectSuccess": False,
        "requestMethod": 0,
        "schedule": {
            "timezone": "Asia/Manila",
            "expiresAt": 0,
            "hours": [-1],
            "mdays": [-1],
            "minutes": [0, 15, 30, 45],
            "months": [-1],
            "wdays": [-1],
        },
        "notification": {
            "onFailure": True,
            "onFailureCount": 2,
            "onSuccess": True,
            "onDisable": True,
            "onSslCertExpiry": True,
            "onSslCertExpirySeconds": 604800,
        },
        "extendedData": {
            "headers": {"Authorization": "Bearer " + insights_api_key},
            "body": "",
        },
    }


def configure(api_key: str, base_url: str, insights_api_key: str) -> tuple[str, int]:
    desired = job_payload(base_url, insights_api_key)
    jobs = api_request(api_key, "GET", "/jobs").get("jobs", [])
    existing = next((job for job in jobs if job.get("title") == JOB_TITLE), None)
    if existing:
        job_id = int(existing["jobId"])
        api_request(api_key, "PATCH", f"/jobs/{job_id}", {"job": desired})
        return "updated", job_id
    created = api_request(api_key, "PUT", "/jobs", {"job": desired})
    return "created", int(created["jobId"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Production Vercel URL, such as https://example.vercel.app")
    args = parser.parse_args()

    if not args.base_url.startswith("https://"):
        raise SystemExit("The production URL must start with https://")

    load_local_env()
    insights_api_key = os.environ.get("INSIGHTS_API_KEY", "")
    if not insights_api_key:
        raise SystemExit("INSIGHTS_API_KEY is missing from .env.local")

    api_key = os.environ.get("CRONJOB_API_KEY") or getpass.getpass("cron-job.org API key: ")
    if not api_key:
        raise SystemExit("A cron-job.org API key is required")

    action, job_id = configure(api_key, args.base_url, insights_api_key)
    print(f"Cron health check {action}; job ID {job_id}; runs every 15 minutes.")


if __name__ == "__main__":
    main()
