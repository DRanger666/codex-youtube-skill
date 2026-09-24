#!/usr/bin/env python3

import argparse
import copy
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import youtube_work_common as common


LOG_FIELDS = {"fileFormatVersion", "videoId", "requests", "updatedAt"}
REQUEST_REQUIRED_FIELDS = {
    "requestId",
    "videoId",
    "requestedTimeRange",
    "contentClass",
    "promptText",
    "endpoint",
    "model",
    "requestMethod",
    "normalizedRequestSha256",
    "runs",
}
REQUEST_OPTIONAL_FIELDS = {
    "outputType",
    "outputFormat",
}
RUN_REQUIRED_FIELDS = {
    "runNumber",
    "exactRequestSha256",
    "startedAt",
    "endedAt",
    "runStatus",
    "routingAttempts",
}
RUN_OPTIONAL_FIELDS = {
    "retryAuthorization",
    "earliestCooldownUntil",
    "interruptionReason",
    "savedResponseId",
    "savedResponseFileName",
    "savedResponseFileSha256",
    "responseNotSavedByPolicy",
}
ATTEMPT_REQUIRED_FIELDS = {
    "attemptNumber",
    "bucket",
    "startedAt",
    "finishedAt",
    "httpStatus",
    "classification",
}
ATTEMPT_OPTIONAL_FIELDS = {
    "errorStatus",
    "cooldownUntil",
    "retryDelaySeconds",
    "backoffSeconds",
}
ROUTER_RESULT_REQUIRED_FIELDS = {
    "fileFormatVersion",
    "status",
    "requestId",
    "runNumber",
    "exactRequestSha256",
    "selectedBucket",
    "attempts",
    "completedAt",
}
ROUTER_RESULT_OPTIONAL_FIELDS = {"earliestCooldownUntil", "responseSha256"}
TRANSIENT_HTTP_STATUSES = {0, 408, 500, 502, 503, 504}
GEMINI_ENDPOINT_HOST = "generativelanguage.googleapis.com"
MODEL_ENDPOINT_PATH_PATTERN = re.compile(
    r"^/v1beta/models/([^/:]+):generateContent$"
)


class RequestLogError(common.YouTubeWorkError):
    pass


class PendingRunError(RequestLogError):
    pass


class RetryAuthorizationRequired(RequestLogError):
    pass


class TerminalRequestError(RequestLogError):
    pass


def request_log_filename(video_source: str) -> str:
    video_id = common.normalize_youtube_video_id(video_source)
    return f"{video_id}--gemini-requests.json"


def new_request_log(video_source: str, updated_at=None):
    return {
        "fileFormatVersion": common.FILE_FORMAT_VERSION,
        "videoId": common.normalize_youtube_video_id(video_source),
        "requests": [],
        "updatedAt": updated_at or common.utc_now(),
    }


def _request_parts(request):
    if not isinstance(request, dict):
        raise RequestLogError("Gemini request must be a JSON object")
    contents = request.get("contents")
    if not isinstance(contents, list) or not contents:
        raise RequestLogError("Gemini request contents must be a non-empty array")
    video_parts = []
    prompt_texts = []
    for content_index, content in enumerate(contents):
        if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
            raise RequestLogError(
                f"Gemini request contents[{content_index}].parts must be an array"
            )
        for part in content["parts"]:
            if not isinstance(part, dict):
                raise RequestLogError("Gemini request parts must be objects")
            if "text" in part:
                if not isinstance(part["text"], str) or not part["text"]:
                    raise RequestLogError("Gemini prompt text must be non-empty")
                prompt_texts.append(part["text"])
            if "fileData" in part:
                file_data = part["fileData"]
                metadata = part.get("videoMetadata")
                if not isinstance(file_data, dict) or not isinstance(metadata, dict):
                    raise RequestLogError(
                        "Every Gemini video part needs fileData and videoMetadata"
                    )
                video_parts.append((file_data, metadata))
    if len(video_parts) != 1:
        raise RequestLogError("One logged Gemini request must contain exactly one video")
    if len(prompt_texts) != 1:
        raise RequestLogError("One logged Gemini request must contain exactly one prompt")
    return video_parts[0], prompt_texts[0]


def _normalize_request_video_uris(value):
    if isinstance(value, list):
        return [_normalize_request_video_uris(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {}
    for key, item in value.items():
        if key == "fileUri" and isinstance(item, str):
            try:
                video_id = common.normalize_youtube_video_id(item)
            except common.YouTubeWorkError:
                normalized[key] = item
            else:
                normalized[key] = common.canonical_youtube_url(video_id)
        else:
            normalized[key] = _normalize_request_video_uris(item)
    return normalized


def inspect_request_file(request_path: Path):
    path = Path(request_path).resolve()
    request_bytes = path.read_bytes()
    try:
        request = json.loads(request_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestLogError(f"Cannot read Gemini request JSON from {path}: {error}") from error
    (file_data, metadata), prompt_text = _request_parts(request)
    uri = file_data.get("fileUri")
    video_id = common.normalize_youtube_video_id(uri)
    start_ms = common.parse_offset_ms(metadata.get("startOffset"), "startOffset")
    end_ms = common.parse_offset_ms(metadata.get("endOffset"), "endOffset")
    if end_ms <= start_ms:
        raise RequestLogError("Gemini video range must satisfy startOffset < endOffset")
    normalized_request = _normalize_request_video_uris(request)
    return {
        "request": request,
        "requestBytes": request_bytes,
        "exactRequestSha256": common.sha256_hex(request_bytes),
        "normalizedRequestSha256": common.sha256_hex(
            common.canonical_json_bytes(normalized_request)
        ),
        "videoId": video_id,
        "requestedTimeRange": {"startMs": start_ms, "endMs": end_ms},
        "promptText": prompt_text,
    }


def derive_request_id(
    normalized_request_sha256: str,
    endpoint: str,
    model: str,
    method: str,
    video_id: str,
    requested_time_range,
) -> str:
    common.validate_sha256(normalized_request_sha256, "normalized request hash")
    identity = {
        "normalizedRequestSha256": normalized_request_sha256,
        "endpoint": endpoint,
        "model": model,
        "requestMethod": method,
        "videoId": common.normalize_youtube_video_id(video_id),
        "requestedTimeRange": requested_time_range,
    }
    return common.sha256_hex(common.canonical_json_bytes(identity))


def _validate_non_empty_string(value, label):
    if not isinstance(value, str) or not value:
        raise RequestLogError(f"{label} must be a non-empty string")


def _require_nondecreasing_update(previous, current):
    if common.parse_timestamp(current, "new request-log updatedAt") < common.parse_timestamp(
        previous, "previous request-log updatedAt"
    ):
        raise RequestLogError("Request-log updatedAt cannot move backwards")


def validate_gemini_endpoint(endpoint, model):
    _validate_non_empty_string(endpoint, "endpoint")
    _validate_non_empty_string(model, "model")
    parsed = urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError as error:
        raise RequestLogError("Unsupported Gemini Generate Content endpoint") from error
    match = MODEL_ENDPOINT_PATH_PATTERN.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != GEMINI_ENDPOINT_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        raise RequestLogError("Unsupported Gemini Generate Content endpoint")
    if match.group(1) != model:
        raise RequestLogError("Gemini endpoint and model differ")
    return endpoint


def validate_attempt(attempt, expected_number=None, previous=None):
    common.require_exact_fields(
        attempt,
        ATTEMPT_REQUIRED_FIELDS,
        ATTEMPT_OPTIONAL_FIELDS,
        "routing attempt",
    )
    number = attempt["attemptNumber"]
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise RequestLogError("Routing attempt number must be a positive integer")
    if expected_number is not None and number != expected_number:
        raise RequestLogError("Routing attempt numbers must start at 1 without gaps")
    if attempt["bucket"] not in {"primary", "fallback"}:
        raise RequestLogError("Routing attempt bucket must be primary or fallback")
    started = common.parse_timestamp(attempt["startedAt"], "attempt startedAt")
    finished = common.parse_timestamp(attempt["finishedAt"], "attempt finishedAt")
    if finished < started:
        raise RequestLogError("Routing attempt cannot finish before it starts")
    if previous is not None:
        previous_finished = common.parse_timestamp(
            previous["finishedAt"], "previous attempt finishedAt"
        )
        if started < previous_finished:
            raise RequestLogError("Routing attempts must be chronological")
    status = attempt["httpStatus"]
    if not isinstance(status, int) or isinstance(status, bool) or status < 0:
        raise RequestLogError("Routing attempt HTTP status must be non-negative")
    classification = attempt["classification"]
    allowed = {
        "success",
        "rate_limited",
        "transient",
        "credential_failure",
        "request_failure",
    }
    if classification not in allowed:
        raise RequestLogError(f"Unsupported routing classification: {classification}")
    error_status = attempt.get("errorStatus")
    if error_status is not None and (
        not isinstance(error_status, str) or not error_status
    ):
        raise RequestLogError("Routing errorStatus must be a non-empty string")
    if classification == "success" and not 200 <= status < 300:
        raise RequestLogError("Successful routing attempt must have a 2xx status")
    if classification == "rate_limited" and not (
        not 200 <= status < 300
        and (status == 429 or error_status == "RESOURCE_EXHAUSTED")
    ):
        raise RequestLogError("Rate-limited attempt must report quota exhaustion")
    if classification == "transient" and status not in TRANSIENT_HTTP_STATUSES:
        raise RequestLogError("Transient attempt has an incompatible HTTP status")
    if classification == "credential_failure" and status not in {400, 401, 403}:
        raise RequestLogError("Credential failure has an incompatible HTTP status")
    if classification == "request_failure" and (
        status < 400
        or status == 429
        or status in TRANSIENT_HTTP_STATUSES
        or status in {401, 403}
    ):
        raise RequestLogError("Request failure has an incompatible HTTP status")
    if "cooldownUntil" in attempt:
        cooldown = common.parse_timestamp(attempt["cooldownUntil"], "attempt cooldownUntil")
        if cooldown < finished:
            raise RequestLogError("Routing cooldown cannot precede attempt completion")
    for field in ("retryDelaySeconds", "backoffSeconds"):
        if field in attempt and (
            not isinstance(attempt[field], (int, float))
            or isinstance(attempt[field], bool)
            or attempt[field] < 0
        ):
            raise RequestLogError(f"Routing {field} must be non-negative")
    timing_fields = {
        field
        for field in ("retryDelaySeconds", "backoffSeconds", "cooldownUntil")
        if field in attempt
    }
    if classification == "rate_limited":
        if timing_fields != {"retryDelaySeconds", "cooldownUntil"}:
            raise RequestLogError(
                "Rate-limited attempt requires retry delay and cooldown"
            )
    elif classification == "transient":
        if timing_fields not in ({"backoffSeconds"}, {"cooldownUntil"}):
            raise RequestLogError(
                "Transient attempt requires either backoff or cooldown"
            )
    elif timing_fields:
        raise RequestLogError(
            f"{classification} attempt cannot contain retry timing fields"
        )
    return dict(attempt)


def validate_router_result(result):
    common.require_exact_fields(
        result,
        ROUTER_RESULT_REQUIRED_FIELDS,
        ROUTER_RESULT_OPTIONAL_FIELDS,
        "safe router result",
    )
    if result["fileFormatVersion"] != common.FILE_FORMAT_VERSION:
        raise RequestLogError("Unsupported safe router result format version")
    if result["status"] not in {"succeeded", "failed"}:
        raise RequestLogError("Safe router result must be terminal")
    common.validate_sha256(result["requestId"], "router request ID")
    common.validate_sha256(result["exactRequestSha256"], "router exact request hash")
    if (
        not isinstance(result["runNumber"], int)
        or isinstance(result["runNumber"], bool)
        or result["runNumber"] < 1
    ):
        raise RequestLogError("Router run number must be positive")
    if result["selectedBucket"] not in {None, "primary", "fallback"}:
        raise RequestLogError("Unsupported selected Gemini bucket")
    if not isinstance(result["attempts"], list):
        raise RequestLogError("Safe router attempts must be an array")
    attempts = []
    for index, attempt in enumerate(result["attempts"], start=1):
        attempts.append(
            validate_attempt(
                attempt,
                expected_number=index,
                previous=attempts[-1] if attempts else None,
            )
        )
    if any(
        attempt["classification"] in {"success", "request_failure"}
        for attempt in attempts[:-1]
    ):
        raise RequestLogError("A terminal routing attempt must be final")
    completed = common.parse_timestamp(result["completedAt"], "router completedAt")
    if attempts:
        final_finished = common.parse_timestamp(
            attempts[-1]["finishedAt"], "terminal attempt finishedAt"
        )
        if completed != final_finished:
            raise RequestLogError(
                "Router completedAt must equal the final attempt completion time"
            )
    if result["status"] == "succeeded":
        if not attempts or attempts[-1]["classification"] != "success":
            raise RequestLogError("Successful router result must end in success")
        if result["selectedBucket"] != attempts[-1]["bucket"]:
            raise RequestLogError("Selected bucket must match the successful attempt")
        if "responseSha256" not in result:
            raise RequestLogError("Successful router result requires responseSha256")
        common.validate_sha256(result["responseSha256"], "router response hash")
        if "earliestCooldownUntil" in result:
            raise RequestLogError("Successful router result cannot carry a cooldown summary")
    else:
        if attempts and attempts[-1]["classification"] == "success":
            raise RequestLogError("Failed router result cannot end in success")
        if result["selectedBucket"] is not None:
            raise RequestLogError("Failed router result cannot select a bucket")
        if "responseSha256" in result:
            raise RequestLogError("Failed router result cannot carry responseSha256")
        if "earliestCooldownUntil" in result:
            common.parse_timestamp(
                result["earliestCooldownUntil"], "router earliestCooldownUntil"
            )
    return {**result, "attempts": attempts}


def _validate_retry_authorization(value, started_at):
    common.require_exact_fields(
        value,
        {"authorizedAt", "reason", "usedAt"},
        set(),
        "retry authorization",
    )
    if not isinstance(value["reason"], str) or not value["reason"].strip():
        raise RequestLogError("Retry authorization reason must be non-empty")
    authorized = common.parse_timestamp(value["authorizedAt"], "authorization authorizedAt")
    used = common.parse_timestamp(value["usedAt"], "authorization usedAt")
    started = common.parse_timestamp(started_at, "run startedAt")
    if authorized > used or used != started:
        raise RequestLogError("Retry authorization must be consumed when the run starts")


def _success_fields(run):
    return {
        key
        for key in (
            "savedResponseId",
            "savedResponseFileName",
            "savedResponseFileSha256",
            "responseNotSavedByPolicy",
        )
        if key in run
    }


def validate_run(run, request, expected_number):
    common.require_exact_fields(run, RUN_REQUIRED_FIELDS, RUN_OPTIONAL_FIELDS, "run")
    if run["runNumber"] != expected_number:
        raise RequestLogError("Run numbers must start at 1 without gaps")
    common.validate_sha256(run["exactRequestSha256"], "run exact request hash")
    started = common.parse_timestamp(run["startedAt"], "run startedAt")
    status = run["runStatus"]
    if status not in {"pending", "succeeded", "failed", "interrupted"}:
        raise RequestLogError(f"Unsupported run status: {status}")
    if not isinstance(run["routingAttempts"], list):
        raise RequestLogError("Run routingAttempts must be an array")
    attempts = []
    for index, attempt in enumerate(run["routingAttempts"], start=1):
        attempts.append(
            validate_attempt(
                attempt,
                expected_number=index,
                previous=attempts[-1] if attempts else None,
            )
        )
    run["routingAttempts"] = attempts
    if attempts and common.parse_timestamp(
        attempts[0]["startedAt"], "first attempt startedAt"
    ) < started:
        raise RequestLogError("Routing attempts cannot precede their run")
    if expected_number == 1 and "retryAuthorization" in run:
        raise RequestLogError("Run 1 cannot contain retry authorization")
    if expected_number > 1:
        if "retryAuthorization" not in run:
            raise RequestLogError("Every later run requires retry authorization")
        _validate_retry_authorization(run["retryAuthorization"], run["startedAt"])
    successes = _success_fields(run)
    if status == "pending":
        if run["endedAt"] is not None:
            raise RequestLogError("Pending run endedAt must be null")
        forbidden = successes | ({"interruptionReason", "earliestCooldownUntil"} & run.keys())
        if forbidden:
            raise RequestLogError(f"Pending run has terminal fields: {sorted(forbidden)}")
    else:
        ended = common.parse_timestamp(run["endedAt"], "run endedAt")
        if ended < started:
            raise RequestLogError("Run cannot end before it starts")
        if attempts:
            last_finished = common.parse_timestamp(
                attempts[-1]["finishedAt"], "last attempt finishedAt"
            )
            if ended != last_finished and status != "interrupted":
                raise RequestLogError("Terminal run endedAt must match its router result")
    if status == "succeeded":
        if not attempts or attempts[-1]["classification"] != "success":
            raise RequestLogError("Succeeded run must end with a successful attempt")
        if request["contentClass"] == "video_material":
            required = {
                "savedResponseId",
                "savedResponseFileName",
                "savedResponseFileSha256",
            }
            if successes != required:
                raise RequestLogError("Succeeded video-material run needs one complete response reference")
            common.validate_sha256(run["savedResponseId"], "saved response ID")
            common.validate_sha256(run["savedResponseFileSha256"], "saved response file hash")
            _validate_non_empty_string(
                run["savedResponseFileName"], "saved response filename"
            )
        elif successes != {"responseNotSavedByPolicy"} or run.get(
            "responseNotSavedByPolicy"
        ) is not True:
            raise RequestLogError("Succeeded one-time run needs the policy non-storage marker")
    elif status == "failed":
        if successes or "interruptionReason" in run:
            raise RequestLogError("Failed run cannot contain a success or interruption result")
        if attempts and attempts[-1]["classification"] == "success":
            raise RequestLogError("Failed run cannot end with a successful attempt")
        if "earliestCooldownUntil" in run:
            common.parse_timestamp(run["earliestCooldownUntil"], "run earliestCooldownUntil")
    elif status == "interrupted":
        if successes or "earliestCooldownUntil" in run:
            raise RequestLogError("Interrupted run cannot contain success or cooldown summary fields")
        if not isinstance(run.get("interruptionReason"), str) or not run[
            "interruptionReason"
        ].strip():
            raise RequestLogError("Interrupted run requires a reason")
    return run


def validate_request_entry(request):
    common.require_exact_fields(
        request,
        REQUEST_REQUIRED_FIELDS,
        REQUEST_OPTIONAL_FIELDS,
        "logical request",
    )
    request["videoId"] = common.normalize_youtube_video_id(request["videoId"])
    common.validate_sha256(request["normalizedRequestSha256"], "normalized request hash")
    validate_gemini_endpoint(request["endpoint"], request["model"])
    expected_id = derive_request_id(
        request["normalizedRequestSha256"],
        request["endpoint"],
        request["model"],
        request["requestMethod"],
        request["videoId"],
        request["requestedTimeRange"],
    )
    if request["requestId"] != expected_id:
        raise RequestLogError("Request ID does not match the normalized request identity")
    for field in ("promptText", "endpoint", "model", "requestMethod"):
        _validate_non_empty_string(request[field], f"logical request {field}")
    if request["requestMethod"] != "POST":
        raise RequestLogError("Gemini request method must be POST")
    ranges = common.normalize_intervals(
        [request["requestedTimeRange"]], "requested time range"
    )
    if len(ranges) != 1 or ranges[0] != request["requestedTimeRange"]:
        raise RequestLogError("Logical request time range must be one normalized interval")
    content_class = request["contentClass"]
    if content_class not in common.CONTENT_CLASSES:
        raise RequestLogError(f"Unsupported content class: {content_class}")
    if content_class == "video_material":
        if "outputType" not in request or "outputFormat" not in request:
            raise RequestLogError("video_material requires output type and format")
        common.validate_output_format(request["outputType"], request["outputFormat"])
    else:
        forbidden = REQUEST_OPTIONAL_FIELDS & request.keys()
        if forbidden:
            raise RequestLogError(
                f"One-time content class cannot contain reusable output fields: {sorted(forbidden)}"
            )
    if not isinstance(request["runs"], list) or not request["runs"]:
        raise RequestLogError("Logical request runs must be a non-empty array")
    pending_count = 0
    previous_run = None
    for index, run in enumerate(request["runs"], start=1):
        validate_run(run, request, index)
        if previous_run is not None:
            if previous_run["runStatus"] == "pending":
                raise RequestLogError("A pending run cannot have a later run")
            previous_end = common.parse_timestamp(
                previous_run["endedAt"], "previous run endedAt"
            )
            current_start = common.parse_timestamp(run["startedAt"], "run startedAt")
            authorized = common.parse_timestamp(
                run["retryAuthorization"]["authorizedAt"],
                "authorization authorizedAt",
            )
            if current_start < previous_end or authorized < previous_end:
                raise RequestLogError(
                    "A later run and its authorization cannot precede the prior run"
                )
        if run["runStatus"] == "pending":
            pending_count += 1
            if index != len(request["runs"]):
                raise RequestLogError("Only the highest-numbered run may be pending")
        previous_run = run
    if pending_count > 1:
        raise RequestLogError("A logical request may have at most one pending run")
    return request


def validate_request_log(log):
    common.require_exact_fields(log, LOG_FIELDS, set(), "Gemini request log")
    if log["fileFormatVersion"] != common.FILE_FORMAT_VERSION:
        raise RequestLogError("Unsupported Gemini request-log format version")
    log["videoId"] = common.normalize_youtube_video_id(log["videoId"])
    updated_at = common.parse_timestamp(log["updatedAt"], "request log updatedAt")
    if not isinstance(log["requests"], list):
        raise RequestLogError("Gemini request log requests must be an array")
    request_ids = set()
    pending_runs = 0
    for request in log["requests"]:
        validate_request_entry(request)
        if request["videoId"] != log["videoId"]:
            raise RequestLogError("Request and request-log video IDs differ")
        if request["requestId"] in request_ids:
            raise RequestLogError(f"Duplicate request ID: {request['requestId']}")
        request_ids.add(request["requestId"])
        pending_runs += sum(
            run["runStatus"] == "pending" for run in request["runs"]
        )
        for run in request["runs"]:
            event_times = [run["startedAt"]]
            if run["endedAt"] is not None:
                event_times.append(run["endedAt"])
            if "retryAuthorization" in run:
                event_times.extend(
                    (
                        run["retryAuthorization"]["authorizedAt"],
                        run["retryAuthorization"]["usedAt"],
                    )
                )
            event_times.extend(
                attempt["finishedAt"] for attempt in run["routingAttempts"]
            )
            if any(
                common.parse_timestamp(value, "request-log event time") > updated_at
                for value in event_times
            ):
                raise RequestLogError(
                    "Request-log updatedAt cannot precede recorded events"
                )
    if pending_runs > 1:
        raise RequestLogError("One video request log may have at most one pending run")
    return log


def find_request(log, request_id):
    common.validate_sha256(request_id, "request ID")
    match = next(
        (item for item in log["requests"] if item["requestId"] == request_id),
        None,
    )
    if match is None:
        raise RequestLogError(f"Unknown request ID: {request_id}")
    return match


def find_run(request, run_number):
    if not isinstance(run_number, int) or isinstance(run_number, bool) or run_number < 1:
        raise RequestLogError("Run number must be positive")
    match = next(
        (item for item in request["runs"] if item["runNumber"] == run_number),
        None,
    )
    if match is None:
        raise RequestLogError(f"Unknown run number: {run_number}")
    return match


def _build_request_entry(
    inspection,
    endpoint,
    model,
    method,
    content_class,
    output_type=None,
    output_format=None,
):
    validate_gemini_endpoint(endpoint, model)
    _validate_non_empty_string(method, "method")
    method = method.upper()
    if content_class not in common.CONTENT_CLASSES:
        raise RequestLogError(f"Unsupported content class: {content_class}")
    if content_class == "video_material":
        if output_type is None or output_format is None:
            raise RequestLogError("video_material requires output type and format")
        common.validate_output_format(output_type, output_format)
        if output_type == "transcript":
            from build_gemini_chunk_request import (
                TRANSCRIPT_PROMPT,
                TRANSCRIPT_RESPONSE_SCHEMA,
            )

            generation = inspection["request"].get("generationConfig")
            if (
                inspection["promptText"] != TRANSCRIPT_PROMPT
                or not isinstance(generation, dict)
                or generation.get("responseMimeType") != "application/json"
                or generation.get("responseJsonSchema") != TRANSCRIPT_RESPONSE_SCHEMA
            ):
                raise RequestLogError(
                    "Transcript material requires the transcript-only request contract"
                )
    request_id = derive_request_id(
        inspection["normalizedRequestSha256"],
        endpoint,
        model,
        method,
        inspection["videoId"],
        inspection["requestedTimeRange"],
    )
    entry = {
        "requestId": request_id,
        "videoId": inspection["videoId"],
        "requestedTimeRange": inspection["requestedTimeRange"],
        "contentClass": content_class,
        "promptText": inspection["promptText"],
        "endpoint": endpoint,
        "model": model,
        "requestMethod": method,
        "normalizedRequestSha256": inspection["normalizedRequestSha256"],
        "runs": [],
    }
    if content_class == "video_material":
        entry["outputType"] = output_type
        entry["outputFormat"] = output_format
    elif any(value is not None for value in (output_type, output_format)):
        raise RequestLogError("One-time content classes cannot declare reusable output fields")
    return entry


def _terminal_request_failure(run):
    return any(
        attempt["classification"] == "request_failure"
        for attempt in run["routingAttempts"]
    )


def _enforce_cooldowns(request, at):
    now = common.parse_timestamp(at, "new run startedAt")
    cooldowns = []
    for run in request["runs"]:
        if "earliestCooldownUntil" in run:
            cooldowns.append(
                common.parse_timestamp(run["earliestCooldownUntil"], "saved cooldown")
            )
        for attempt in run["routingAttempts"]:
            if "cooldownUntil" in attempt:
                cooldowns.append(
                    common.parse_timestamp(attempt["cooldownUntil"], "saved cooldown")
                )
    active = [value for value in cooldowns if value > now]
    if active:
        latest = max(active).isoformat().replace("+00:00", "Z")
        raise RequestLogError(f"Identical request is cooling down until {latest}")


def start_run(
    log,
    request_path,
    video_source,
    endpoint,
    model,
    method,
    content_class,
    output_type=None,
    output_format=None,
    retry_reason=None,
    authorized_at=None,
    started_at=None,
):
    original_log = log
    log = copy.deepcopy(log)
    validate_request_log(log)
    inspection = inspect_request_file(Path(request_path))
    video_id = common.normalize_youtube_video_id(video_source)
    if inspection["videoId"] != video_id or log["videoId"] != video_id:
        raise RequestLogError("Request, supplied video, and request-log video IDs differ")
    entry = _build_request_entry(
        inspection,
        endpoint,
        model,
        method,
        content_class,
        output_type,
        output_format,
    )
    existing_pending = [
        run
        for request in log["requests"]
        for run in request["runs"]
        if run["runStatus"] == "pending"
    ]
    if existing_pending:
        raise PendingRunError("Another Gemini run for this video is already pending")
    request = next(
        (item for item in log["requests"] if item["requestId"] == entry["requestId"]),
        None,
    )
    if request is None:
        if retry_reason is not None or authorized_at is not None:
            raise RequestLogError("Run 1 cannot contain retry authorization")
        request = entry
        log["requests"].append(request)
        log["requests"].sort(key=lambda item: item["requestId"])
    else:
        immutable_fields = (REQUEST_REQUIRED_FIELDS - {"runs"}) | REQUEST_OPTIONAL_FIELDS
        for field in immutable_fields:
            if request.get(field) != entry.get(field):
                raise RequestLogError(
                    f"Existing request has conflicting immutable metadata: {field}"
                )
    started_at = started_at or common.utc_now()
    common.parse_timestamp(started_at, "run startedAt")
    _require_nondecreasing_update(log["updatedAt"], started_at)
    run_number = len(request["runs"]) + 1
    if run_number > 1:
        latest = request["runs"][-1]
        if latest["runStatus"] == "pending":
            raise PendingRunError("The identical request already has a pending run")
        if _terminal_request_failure(latest):
            raise TerminalRequestError(
                "Terminal Gemini request failure cannot be repeated unchanged"
            )
        if not isinstance(retry_reason, str) or not retry_reason.strip():
            raise RetryAuthorizationRequired(
                "Another identical run requires an explicit authorization reason"
            )
        _enforce_cooldowns(request, started_at)
    elif retry_reason is not None or authorized_at is not None:
        raise RequestLogError("Run 1 cannot contain retry authorization")
    run = {
        "runNumber": run_number,
        "exactRequestSha256": inspection["exactRequestSha256"],
        "startedAt": started_at,
        "endedAt": None,
        "runStatus": "pending",
        "routingAttempts": [],
    }
    if run_number > 1:
        authorized_at = authorized_at or started_at
        run["retryAuthorization"] = {
            "authorizedAt": authorized_at,
            "reason": retry_reason.strip(),
            "usedAt": started_at,
        }
    request["runs"].append(run)
    log["updatedAt"] = started_at
    validate_request_log(log)
    original_log.clear()
    original_log.update(log)
    return request, run


def verify_pending_run(
    log,
    request_path,
    endpoint,
    model,
    method,
    request_id,
    run_number,
):
    validate_request_log(log)
    validate_gemini_endpoint(endpoint, model)
    request = find_request(log, request_id)
    run = find_run(request, run_number)
    if run is not request["runs"][-1] or run["runStatus"] != "pending":
        raise RequestLogError("Gemini may use only the highest pending run")
    inspection = inspect_request_file(Path(request_path))
    method = method.upper()
    expected_id = derive_request_id(
        inspection["normalizedRequestSha256"],
        endpoint,
        model,
        method,
        inspection["videoId"],
        inspection["requestedTimeRange"],
    )
    checks = {
        "requestId": expected_id,
        "videoId": inspection["videoId"],
        "requestedTimeRange": inspection["requestedTimeRange"],
        "promptText": inspection["promptText"],
        "endpoint": endpoint,
        "model": model,
        "requestMethod": method,
        "normalizedRequestSha256": inspection["normalizedRequestSha256"],
    }
    for field, actual in checks.items():
        if request[field] != actual:
            raise RequestLogError(f"Request-file verification failed for {field}")
    if run["exactRequestSha256"] != inspection["exactRequestSha256"]:
        raise RequestLogError("Request-file verification failed for exact bytes")
    return request, run


def _verified_attempt_history(run, router_result):
    stored = run["routingAttempts"]
    returned = router_result["attempts"]
    if len(stored) > len(returned) or stored != returned[: len(stored)]:
        raise RequestLogError(
            "Stored routing attempts are not an exact prefix of the router result"
        )
    return list(returned)


def _verify_result_binding(request, run, result):
    result = validate_router_result(dict(result))
    if (
        result["requestId"] != request["requestId"]
        or result["runNumber"] != run["runNumber"]
        or result["exactRequestSha256"] != run["exactRequestSha256"]
    ):
        raise RequestLogError("Safe router result does not belong to this request run")
    _verified_attempt_history(run, result)
    return result


def finish_run(
    log,
    request_id,
    run_number,
    router_result=None,
    response_path=None,
    saved_response_path=None,
    candidate_response_paths=None,
    confirmed_session_stopped=False,
    confirmed_response_enumeration=False,
    updated_at=None,
):
    original_log = log
    log = copy.deepcopy(log)
    validate_request_log(log)
    request = find_request(log, request_id)
    run = find_run(request, run_number)
    if run is not request["runs"][-1] or run["runStatus"] != "pending":
        raise RequestLogError("Only the highest pending run can be finished")

    saved_response = None
    saved_file_hash = None
    if router_result is None:
        if request["contentClass"] != "video_material":
            raise RequestLogError("One-time interrupted runs have no saved response to recover")
        if confirmed_session_stopped is not True:
            raise RequestLogError(
                "Finishing from a retained response requires confirmation that the earlier session stopped"
            )
        if confirmed_response_enumeration is not True:
            raise RequestLogError(
                "Interrupted completion requires confirmed saved-response enumeration"
            )
        import saved_gemini_responses as saved_responses

        matches = []
        for candidate in candidate_response_paths or []:
            candidate_path = Path(candidate).resolve()
            raw = common.load_json(candidate_path)
            claims_run = (
                isinstance(raw, dict)
                and raw.get("requestId") == request_id
                and raw.get("runNumber") == run_number
            )
            if not claims_run:
                continue
            candidate_response = saved_responses.validate_saved_response_file(
                candidate_path
            )
            if candidate_response["exactRequestSha256"] != run["exactRequestSha256"]:
                raise RequestLogError("A saved response claims the run with a conflicting request hash")
            matches.append((candidate_path, candidate_response))
        if len(matches) != 1:
            raise RequestLogError(
                "Interrupted completion requires exactly one verified response for the run"
            )
        saved_path, saved_response = matches[0]
        saved_response_path = saved_path
        saved_file_hash = common.sha256_hex(saved_path.read_bytes())
        router_result = saved_response["routerResult"]
    if router_result is None:
        raise RequestLogError("finish-run requires a safe router result")
    result = _verify_result_binding(request, run, router_result)
    attempts = _verified_attempt_history(run, result)

    if result["status"] == "failed":
        if (
            response_path is not None
            or saved_response_path is not None
        ):
            raise RequestLogError("Failed router result cannot finish with a response")
        run_status = "failed"
    else:
        run_status = "succeeded"
        if request["contentClass"] == "video_material":
            import saved_gemini_responses as saved_responses

            if saved_response_path is None:
                raise RequestLogError("video_material success requires a saved response")
            saved_path = Path(saved_response_path).resolve()
            if saved_response is None:
                saved_response = saved_responses.validate_saved_response_file(saved_path)
                saved_file_hash = common.sha256_hex(saved_path.read_bytes())
            if saved_response["routerResult"] != result:
                raise RequestLogError("Saved response and supplied router result differ")
            if (
                saved_response["requestId"] != request_id
                or saved_response["runNumber"] != run_number
                or saved_response["exactRequestSha256"] != run["exactRequestSha256"]
            ):
                raise RequestLogError("Saved response does not belong to this request run")
            if saved_response["responseSha256"] != result["responseSha256"]:
                raise RequestLogError("Saved response and router response hashes differ")
        else:
            if saved_response_path is not None:
                raise RequestLogError("One-time content classes cannot link saved responses")
            if response_path is None:
                raise RequestLogError("One-time success requires the actual response file")
            response_hash = common.sha256_hex(Path(response_path).resolve().read_bytes())
            if response_hash != result["responseSha256"]:
                raise RequestLogError("One-time response bytes do not match the router result")

    run["routingAttempts"] = attempts
    run["endedAt"] = result["completedAt"]
    run["runStatus"] = run_status
    if result.get("earliestCooldownUntil") is not None:
        run["earliestCooldownUntil"] = result["earliestCooldownUntil"]
    if run_status == "succeeded" and request["contentClass"] == "video_material":
        run["savedResponseId"] = saved_response["savedResponseId"]
        run["savedResponseFileName"] = Path(saved_response_path).resolve().name
        run["savedResponseFileSha256"] = saved_file_hash
    elif run_status == "succeeded":
        run["responseNotSavedByPolicy"] = True
    next_updated_at = updated_at or common.utc_now()
    _require_nondecreasing_update(log["updatedAt"], next_updated_at)
    log["updatedAt"] = next_updated_at
    validate_request_log(log)
    original_log.clear()
    original_log.update(log)
    return request, run


def mark_run_interrupted(
    log,
    request_id,
    run_number,
    reason,
    confirmed_session_stopped,
    at=None,
):
    original_log = log
    log = copy.deepcopy(log)
    validate_request_log(log)
    if confirmed_session_stopped is not True:
        raise RequestLogError("Marking a run interrupted requires user confirmation")
    if not isinstance(reason, str) or not reason.strip():
        raise RequestLogError("Interrupted run reason must be non-empty")
    request = find_request(log, request_id)
    run = find_run(request, run_number)
    if run is not request["runs"][-1] or run["runStatus"] != "pending":
        raise RequestLogError("Only the highest pending run can be marked interrupted")
    at = at or common.utc_now()
    common.parse_timestamp(at, "interrupted run time")
    run["runStatus"] = "interrupted"
    run["endedAt"] = at
    run["interruptionReason"] = reason.strip()
    _require_nondecreasing_update(log["updatedAt"], at)
    log["updatedAt"] = at
    validate_request_log(log)
    original_log.clear()
    original_log.update(log)
    return request, run


def _metadata_from_args(args):
    if args.transcript:
        forbidden = (
            args.content_class,
            args.output_type,
            args.output_format_name,
            args.output_format_version,
        )
        if any(value is not None for value in forbidden):
            raise RequestLogError("--transcript fixes its metadata and cannot be overridden")
        return {
            "content_class": "video_material",
            "output_type": "transcript",
            "output_format": {"name": "gemini-transcript", "version": 1},
        }
    if args.content_class is None:
        raise RequestLogError("Generic Gemini requests require --content-class")
    output_format = None
    if args.output_format_name is not None or args.output_format_version is not None:
        if args.output_format_name is None or args.output_format_version is None:
            raise RequestLogError("Output format requires both name and version")
        output_format = {
            "name": args.output_format_name,
            "version": args.output_format_version,
        }
    return {
        "content_class": args.content_class,
        "output_type": args.output_type,
        "output_format": output_format,
    }


def command_locate(args):
    video_id = common.normalize_youtube_video_id(args.video)
    print(
        json.dumps(
            {
                "stateDirectoryName": common.LOCAL_STATE_DIRECTORY,
                "videoId": video_id,
                "requestLogFileName": request_log_filename(video_id),
            },
            sort_keys=True,
        )
    )
    return 0


def command_init_log(args):
    if not args.confirmed_no_log:
        raise RequestLogError(
            "Refusing request-log initialization until exact-name lookup confirms none exists"
        )
    common.write_json(
        Path(args.output), new_request_log(args.video, updated_at=args.updated_at)
    )
    print(Path(args.output).resolve())
    return 0


def command_start_run(args):
    log = common.load_json(Path(args.request_log))
    metadata = _metadata_from_args(args)
    request, run = start_run(
        log,
        args.request,
        args.video,
        args.endpoint,
        args.model,
        args.method,
        retry_reason=args.retry_reason,
        authorized_at=args.authorized_at,
        started_at=args.started_at,
        **metadata,
    )
    common.write_json(Path(args.output), log)
    print(
        json.dumps(
            {
                "requestId": request["requestId"],
                "runNumber": run["runNumber"],
                "exactRequestSha256": run["exactRequestSha256"],
                "runStatus": run["runStatus"],
            },
            sort_keys=True,
        )
    )
    return 0


def command_verify_run(args):
    log = common.load_json(Path(args.request_log))
    request, run = verify_pending_run(
        log,
        args.request,
        args.endpoint,
        args.model,
        args.method,
        args.request_id,
        args.run_number,
    )
    print(
        json.dumps(
            {
                "requestId": request["requestId"],
                "runNumber": run["runNumber"],
                "exactRequestSha256": run["exactRequestSha256"],
                "contentClass": request["contentClass"],
                "outputType": request.get("outputType"),
                "outputFormat": request.get("outputFormat"),
            },
            sort_keys=True,
        )
    )
    return 0


def command_finish_run(args):
    log = common.load_json(Path(args.request_log))
    router_result = (
        common.load_json(Path(args.router_result)) if args.router_result else None
    )
    request, run = finish_run(
        log,
        args.request_id,
        args.run_number,
        router_result=router_result,
        response_path=args.response,
        saved_response_path=args.saved_response,
        candidate_response_paths=args.candidate_saved_response,
        confirmed_session_stopped=args.confirmed_session_stopped,
        confirmed_response_enumeration=args.confirmed_response_enumeration,
        updated_at=args.updated_at,
    )
    common.write_json(Path(args.output), log)
    print(
        json.dumps(
            {
                "requestId": request["requestId"],
                "runNumber": run["runNumber"],
                "runStatus": run["runStatus"],
            },
            sort_keys=True,
        )
    )
    return 0


def command_mark_interrupted(args):
    log = common.load_json(Path(args.request_log))
    request, run = mark_run_interrupted(
        log,
        args.request_id,
        args.run_number,
        args.reason,
        args.confirmed_session_stopped,
        at=args.at,
    )
    common.write_json(Path(args.output), log)
    print(
        json.dumps(
            {
                "requestId": request["requestId"],
                "runNumber": run["runNumber"],
                "runStatus": run["runStatus"],
            },
            sort_keys=True,
        )
    )
    return 0


def _add_request_binding_args(parser):
    parser.add_argument("--request-log", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--method", default="POST")


def build_parser():
    parser = argparse.ArgumentParser(description="Manage per-video Gemini request history")
    subparsers = parser.add_subparsers(dest="command", required=True)

    locate = subparsers.add_parser("locate")
    locate.add_argument("--video", required=True)
    locate.set_defaults(handler=command_locate)

    initialize = subparsers.add_parser("init-log")
    initialize.add_argument("--video", required=True)
    initialize.add_argument("--output", required=True)
    initialize.add_argument("--updated-at")
    initialize.add_argument("--confirmed-no-log", action="store_true")
    initialize.set_defaults(handler=command_init_log)

    start = subparsers.add_parser("start-run")
    _add_request_binding_args(start)
    start.add_argument("--video", required=True)
    start.add_argument("--output", required=True)
    start.add_argument("--transcript", action="store_true")
    start.add_argument("--content-class", choices=sorted(common.CONTENT_CLASSES))
    start.add_argument("--output-type")
    start.add_argument("--output-format-name")
    start.add_argument("--output-format-version", type=int)
    start.add_argument("--retry-reason")
    start.add_argument("--authorized-at")
    start.add_argument("--started-at")
    start.set_defaults(handler=command_start_run)

    verify = subparsers.add_parser("verify-run")
    _add_request_binding_args(verify)
    verify.add_argument("--request-id", required=True)
    verify.add_argument("--run-number", required=True, type=int)
    verify.set_defaults(handler=command_verify_run)

    finish = subparsers.add_parser("finish-run")
    finish.add_argument("--request-log", required=True)
    finish.add_argument("--request-id", required=True)
    finish.add_argument("--run-number", required=True, type=int)
    finish.add_argument("--router-result")
    finish.add_argument("--response")
    finish.add_argument("--saved-response")
    finish.add_argument("--candidate-saved-response", action="append", default=[])
    finish.add_argument("--confirmed-session-stopped", action="store_true")
    finish.add_argument("--confirmed-response-enumeration", action="store_true")
    finish.add_argument("--updated-at")
    finish.add_argument("--output", required=True)
    finish.set_defaults(handler=command_finish_run)

    interrupted = subparsers.add_parser("mark-run-interrupted")
    interrupted.add_argument("--request-log", required=True)
    interrupted.add_argument("--request-id", required=True)
    interrupted.add_argument("--run-number", required=True, type=int)
    interrupted.add_argument("--reason", required=True)
    interrupted.add_argument("--confirmed-session-stopped", action="store_true")
    interrupted.add_argument("--at")
    interrupted.add_argument("--output", required=True)
    interrupted.set_defaults(handler=command_mark_interrupted)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (RequestLogError, common.YouTubeWorkError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
