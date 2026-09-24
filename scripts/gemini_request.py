#!/usr/bin/env python3

import argparse
import json
import os
import random
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import gemini_request_log
import youtube_work_common as common


TRANSIENT_HTTP_STATUSES = {408, 500, 502, 503, 504}
KEY_INVALID_MARKERS = (
    "api_key_invalid",
    "api key not valid",
    "api key is invalid",
    "invalid api key",
)
POOL_STATE_FIELDS = {"fileFormatVersion", "buckets"}
BUCKET_STATE_FIELDS = {"cooldownUntil", "disabled", "reason"}
BUCKET_STATE_REASONS = {None, "rate_limited", "transient_failures", "credential_failure"}
CREDENTIAL_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEY_FALLBACK")


def utc_now():
    return datetime.now(timezone.utc)


def isoformat(value):
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso8601(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def write_json(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def load_json(path):
    with Path(path).resolve().open("r", encoding="utf-8") as handle:
        return json.load(handle)


def default_bucket_state():
    return {"cooldownUntil": None, "disabled": False, "reason": None}


def validate_state(state):
    try:
        common.require_exact_fields(
            state, POOL_STATE_FIELDS, set(), "Gemini pool state"
        )
    except common.YouTubeWorkError as error:
        raise SystemExit(str(error)) from error
    if state["fileFormatVersion"] != common.FILE_FORMAT_VERSION:
        raise SystemExit(
            "Unsupported Gemini pool-state format: "
            f"{state['fileFormatVersion']}"
        )
    if not isinstance(state["buckets"], dict) or set(state["buckets"]) != {
        "primary",
        "fallback",
    }:
        raise SystemExit("Gemini pool state must contain only primary and fallback")
    for alias, bucket in state["buckets"].items():
        try:
            common.require_exact_fields(
                bucket, BUCKET_STATE_FIELDS, set(), f"{alias} bucket state"
            )
        except common.YouTubeWorkError as error:
            raise SystemExit(str(error)) from error
        if not isinstance(bucket["disabled"], bool):
            raise SystemExit(f"{alias} bucket disabled must be Boolean")
        if bucket["reason"] not in BUCKET_STATE_REASONS:
            raise SystemExit(f"{alias} bucket has an unsupported reason")
        cooldown = bucket["cooldownUntil"]
        if cooldown is not None:
            try:
                common.parse_timestamp(cooldown, f"{alias} cooldownUntil")
            except common.YouTubeWorkError as error:
                raise SystemExit(str(error)) from error
        if bucket["disabled"]:
            if cooldown is not None or bucket["reason"] != "credential_failure":
                raise SystemExit(f"{alias} disabled state is inconsistent")
        elif bucket["reason"] == "credential_failure":
            raise SystemExit(f"{alias} credential failure must disable the bucket")
        elif cooldown is None and bucket["reason"] is not None:
            raise SystemExit(f"{alias} bucket reason requires a cooldown")
        elif cooldown is not None and bucket["reason"] not in {
            "rate_limited",
            "transient_failures",
        }:
            raise SystemExit(f"{alias} cooldown reason is inconsistent")
    return state


def load_state(path):
    path = Path(path).resolve()
    if not path.exists():
        return validate_state({
            "fileFormatVersion": common.FILE_FORMAT_VERSION,
            "buckets": {
                "primary": default_bucket_state(),
                "fallback": default_bucket_state(),
            },
        })
    state = load_json(path)
    return validate_state(state)


def credentials_path():
    codex_home = os.environ.get("CODEX_HOME")
    home = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return home.resolve() / "secrets" / "work-with-youtube.env"


def load_credential_file(path):
    path = Path(path)
    try:
        directory_stat = path.parent.stat()
        file_stat = path.lstat()
    except FileNotFoundError as error:
        raise SystemExit(
            f"Missing Gemini credentials file: {path}"
        ) from error
    if stat.S_IMODE(directory_stat.st_mode) != 0o700:
        raise SystemExit(f"Gemini credentials directory must have mode 0700: {path.parent}")
    if not stat.S_ISREG(file_stat.st_mode) or path.is_symlink():
        raise SystemExit(f"Gemini credentials path must be a regular file: {path}")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise SystemExit(f"Gemini credentials file must have mode 0600: {path}")
    if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
        raise SystemExit(f"Gemini credentials file must be owned by the current user: {path}")

    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise SystemExit(f"Could not read Gemini credentials file: {path}") from error
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(
                f"Malformed Gemini credentials file line {line_number}: expected NAME=VALUE"
            )
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name not in CREDENTIAL_NAMES:
            raise SystemExit(
                f"Unsupported Gemini credentials field on line {line_number}: {name}"
            )
        if name in values:
            raise SystemExit(
                f"Duplicate Gemini credentials field on line {line_number}: {name}"
            )
        if not value:
            raise SystemExit(f"Empty Gemini credential on line {line_number}: {name}")
        values[name] = value
    return values


def load_buckets():
    environment_supplied = any(name in os.environ for name in CREDENTIAL_NAMES)
    if environment_supplied:
        primary = os.environ.get("GEMINI_API_KEY")
        fallback = os.environ.get("GEMINI_API_KEY_FALLBACK")
    else:
        credentials = load_credential_file(credentials_path())
        primary = credentials.get("GEMINI_API_KEY")
        fallback = credentials.get("GEMINI_API_KEY_FALLBACK")
    if not primary:
        raise SystemExit("Missing required Gemini credential: GEMINI_API_KEY")
    if fallback and primary == fallback:
        raise SystemExit("Primary and fallback Gemini credentials must differ")
    buckets = [{"alias": "primary", "key": primary}]
    if fallback:
        buckets.append({"alias": "fallback", "key": fallback})
    return buckets


def error_parts(payload):
    error = payload.get("error", payload) if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    status = str(error.get("status", "")).upper()
    message = str(error.get("message", ""))
    return status, message, error.get("details", [])


def retry_delay_from_payload(payload):
    _, message, details = error_parts(payload)
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
                value = detail.get("retryDelay")
                if isinstance(value, str):
                    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)s\s*", value)
                    if match:
                        return float(match.group(1))
    match = re.search(
        r"(?:retry\s+(?:in|after)|reset\s+after)\s+([0-9]+(?:\.[0-9]+)?)\s*s",
        message,
        flags=re.IGNORECASE,
    )
    return float(match.group(1)) if match else None


def retry_delay_from_headers(headers, now):
    if not headers:
        return None
    retry_after = headers.get("Retry-After") or headers.get("retry-after")
    if not retry_after:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - now).total_seconds())


def classify_response(http_status, payload):
    error_status, message, _ = error_parts(payload)
    lowered_message = message.lower()
    if 200 <= http_status < 300:
        return "success", error_status
    if http_status == 429 or error_status == "RESOURCE_EXHAUSTED":
        return "rate_limited", error_status
    if http_status in (401, 403):
        return "credential_failure", error_status
    if http_status == 400 and any(marker in lowered_message for marker in KEY_INVALID_MARKERS):
        return "credential_failure", error_status
    if http_status in TRANSIENT_HTTP_STATUSES or http_status == 0:
        return "transient", error_status
    return "request_failure", error_status


def send_request(endpoint, api_key, request_bytes, timeout_seconds):
    request = urllib.request.Request(
        endpoint,
        data=request_bytes,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
            return response.status, dict(response.headers.items()), body
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        payload = {
            "error": {
                "code": 0,
                "status": "NETWORK_ERROR",
                "message": type(error).__name__,
            }
        }
        return 0, {}, json.dumps(payload).encode("utf-8")


def decode_payload(response_bytes):
    try:
        value = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "error": {
                "code": 0,
                "status": "INVALID_RESPONSE",
                "message": "Gemini returned a non-JSON response",
            }
        }
    return value


def bucket_available(alias, state, now):
    bucket_state = state["buckets"][alias]
    if bucket_state.get("disabled"):
        return False
    cooldown_until = parse_iso8601(bucket_state.get("cooldownUntil"))
    return cooldown_until is None or cooldown_until <= now


def mark_cooldown(state, alias, delay_seconds, reason, now):
    cooldown_until = now + timedelta(seconds=delay_seconds)
    state["buckets"][alias] = {
        "cooldownUntil": isoformat(cooldown_until),
        "disabled": False,
        "reason": reason,
    }
    return cooldown_until


def mark_disabled(state, alias, reason):
    state["buckets"][alias] = {
        "cooldownUntil": None,
        "disabled": True,
        "reason": reason,
    }


def attempt_record(
    attempt_number,
    alias,
    started_at,
    finished_at,
    http_status,
    classification,
    error_status,
):
    attempt = {
        "attemptNumber": attempt_number,
        "bucket": alias,
        "startedAt": isoformat(started_at),
        "finishedAt": isoformat(finished_at),
        "httpStatus": http_status,
        "classification": classification,
    }
    if error_status:
        attempt["errorStatus"] = error_status
    return attempt


def safe_failure_payload(http_status, payload, earliest_cooldown=None):
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload
    result = {
        "error": {
            "code": http_status,
            "status": "REQUEST_FAILED",
            "message": "Gemini request failed",
        }
    }
    if earliest_cooldown:
        result["error"]["earliestCooldownUntil"] = isoformat(earliest_cooldown)
    return result


def earliest_cooldown(state):
    values = []
    for bucket_state in state["buckets"].values():
        value = parse_iso8601(bucket_state.get("cooldownUntil"))
        if value:
            values.append(value)
    return min(values) if values else None


def run(args):
    request_path = Path(args.request).resolve()
    request_log = common.load_json(Path(args.request_log))
    request_entry, run_entry = gemini_request_log.verify_pending_run(
        request_log,
        request_path,
        args.endpoint,
        args.model,
        args.method,
        args.request_id,
        args.run_number,
    )
    request_bytes = request_path.read_bytes()
    buckets = load_buckets()
    state_path = Path(args.state).resolve()
    state = load_state(state_path)
    routing = {
        "fileFormatVersion": common.FILE_FORMAT_VERSION,
        "status": "pending",
        "requestId": request_entry["requestId"],
        "runNumber": run_entry["runNumber"],
        "exactRequestSha256": run_entry["exactRequestSha256"],
        "selectedBucket": None,
        "attempts": [],
        "completedAt": None,
    }
    tried_buckets = set()
    last_payload = None
    last_http_status = 0

    while True:
        now = utc_now()
        available = [
            bucket
            for bucket in buckets
            if bucket["alias"] not in tried_buckets
            and bucket_available(bucket["alias"], state, now)
        ]
        if not available:
            routing["status"] = "failed"
            routing["completedAt"] = (
                routing["attempts"][-1]["finishedAt"]
                if routing["attempts"]
                else isoformat(now)
            )
            cooldown = earliest_cooldown(state)
            if cooldown is not None:
                routing["earliestCooldownUntil"] = isoformat(cooldown)
            if last_payload is None:
                last_http_status = 429
                last_payload = {
                    "error": {
                        "code": 429,
                        "status": "RESOURCE_EXHAUSTED",
                        "message": "No healthy Gemini project bucket is available",
                    }
                }
            write_json(args.response, safe_failure_payload(last_http_status, last_payload))
            validate_state(state)
            gemini_request_log.validate_router_result(routing)
            write_json(args.router_result, routing)
            write_json(state_path, state)
            print(json.dumps({"status": "failed", "attempts": len(routing["attempts"])}))
            return 3

        bucket = available[0]
        alias = bucket["alias"]
        transient_retries = 0

        while True:
            started_at = utc_now()
            http_status, headers, response_bytes = send_request(
                args.endpoint,
                bucket["key"],
                request_bytes,
                args.timeout_seconds,
            )
            finished_at = utc_now()
            payload = decode_payload(response_bytes)
            classification, error_status = classify_response(http_status, payload)
            attempt = attempt_record(
                len(routing["attempts"]) + 1,
                alias,
                started_at,
                finished_at,
                http_status,
                classification,
                error_status,
            )
            routing["attempts"].append(attempt)
            last_payload = payload
            last_http_status = http_status

            if classification == "success":
                state["buckets"][alias] = default_bucket_state()
                routing["status"] = "succeeded"
                routing["selectedBucket"] = alias
                routing["completedAt"] = attempt["finishedAt"]
                common.write_json(Path(args.response), payload)
                routing["responseSha256"] = common.sha256_hex(
                    Path(args.response).resolve().read_bytes()
                )
                validate_state(state)
                gemini_request_log.validate_router_result(routing)
                write_json(args.router_result, routing)
                write_json(state_path, state)
                print(
                    json.dumps(
                        {
                            "status": "succeeded",
                            "bucket": alias,
                            "attempts": len(routing["attempts"]),
                        }
                    )
                )
                return 0

            if classification == "rate_limited":
                now = utc_now()
                delay = retry_delay_from_headers(headers, now)
                if delay is None:
                    delay = retry_delay_from_payload(payload)
                if delay is None:
                    delay = args.default_cooldown_seconds
                delay = max(1.0, delay) + random.uniform(0.0, args.jitter_seconds)
                cooldown_until = mark_cooldown(state, alias, delay, "rate_limited", now)
                attempt["retryDelaySeconds"] = round(delay, 3)
                attempt["cooldownUntil"] = isoformat(cooldown_until)
                tried_buckets.add(alias)
                break

            if classification == "credential_failure":
                mark_disabled(state, alias, "credential_failure")
                tried_buckets.add(alias)
                break

            if classification == "transient":
                if transient_retries < args.max_transient_retries:
                    delay = min(
                        args.max_backoff_seconds,
                        args.base_backoff_seconds * (2**transient_retries),
                    )
                    delay += random.uniform(0.0, args.jitter_seconds)
                    attempt["backoffSeconds"] = round(delay, 3)
                    transient_retries += 1
                    if not args.no_sleep:
                        time.sleep(delay)
                    continue
                now = utc_now()
                delay = args.default_transient_cooldown_seconds
                cooldown_until = mark_cooldown(state, alias, delay, "transient_failures", now)
                attempt["cooldownUntil"] = isoformat(cooldown_until)
                tried_buckets.add(alias)
                break

            routing["status"] = "failed"
            routing["completedAt"] = attempt["finishedAt"]
            write_json(args.response, safe_failure_payload(http_status, payload))
            validate_state(state)
            gemini_request_log.validate_router_result(routing)
            write_json(args.router_result, routing)
            write_json(state_path, state)
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "httpStatus": http_status,
                        "classification": classification,
                        "attempts": len(routing["attempts"]),
                    }
                )
            )
            return 2


def build_parser():
    parser = argparse.ArgumentParser(
        description="Send one Gemini request through a conservative two-project credential pool"
    )
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    parser.add_argument("--router-result", required=True)
    parser.add_argument("--request-log", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--run-number", required=True, type=int)
    parser.add_argument("--state", required=True)
    parser.add_argument(
        "--endpoint",
        default=(
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.6-flash:generateContent"
        ),
    )
    parser.add_argument("--model", default="gemini-3.6-flash")
    parser.add_argument("--method", default="POST")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--max-transient-retries", type=int, default=2)
    parser.add_argument("--base-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--max-backoff-seconds", type=float, default=30.0)
    parser.add_argument("--default-cooldown-seconds", type=float, default=60.0)
    parser.add_argument("--default-transient-cooldown-seconds", type=float, default=30.0)
    parser.add_argument("--jitter-seconds", type=float, default=1.0)
    parser.add_argument("--no-sleep", action="store_true", help=argparse.SUPPRESS)
    return parser


def main():
    args = build_parser().parse_args()
    if args.max_transient_retries < 0:
        raise SystemExit("max-transient-retries must be non-negative")
    for name in (
        "base_backoff_seconds",
        "max_backoff_seconds",
        "default_cooldown_seconds",
        "default_transient_cooldown_seconds",
        "jitter_seconds",
    ):
        if getattr(args, name) < 0:
            raise SystemExit(f"{name.replace('_', '-')} must be non-negative")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
