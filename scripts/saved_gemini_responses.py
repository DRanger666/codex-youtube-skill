#!/usr/bin/env python3

import argparse
import copy
import json
import re
from pathlib import Path

import gemini_request_log as request_log
import youtube_work_common as common


SAVED_RESPONSE_REQUIRED_FIELDS = {
    "fileFormatVersion",
    "videoId",
    "savedResponseId",
    "requestId",
    "runNumber",
    "exactRequestSha256",
    "contentClass",
    "outputType",
    "outputFormat",
    "sourceTimeRange",
    "routerResult",
    "responseSha256",
    "responseJsonText",
}
SAVED_RESPONSE_OPTIONAL_FIELDS = {
    "coveredTimeRanges",
    "formatCheck",
}
INDEX_FIELDS = {"fileFormatVersion", "videoId", "materials", "updatedAt"}
MATERIAL_REQUIRED_FIELDS = {
    "savedResponseId",
    "fileName",
    "fileSha256",
    "outputType",
    "outputFormat",
    "coveredTimeRanges",
}
MATERIAL_OPTIONAL_FIELDS = {
    "materialDescription",
}
QUERY_REQUIRED_FIELDS = {
    "video",
    "outputType",
    "outputFormat",
    "requestedTimeRanges",
}
QUERY_OPTIONAL_FIELDS = set()
FORMAT_CHECK_NAME = "gemini-transcript-v1"
TIMESTAMP_PATTERN = re.compile(r"^([0-9]{2,}):([0-5][0-9])\.([0-9]{3})$")
TRANSCRIPT_FIELDS = {
    "clip_start_timestamp",
    "clip_end_timestamp",
    "segments",
    "completed_through_timestamp",
    "transcription_complete",
    "truncation_detected",
}
SEGMENT_FIELDS = {
    "start_timestamp",
    "end_timestamp",
    "vocal_type",
    "language",
    "text",
    "audibility",
}
COVERAGE_CASE_ORDER = (
    "exact",
    "containing",
    "composite",
    "overlapping",
    "incompatible",
    "missing",
)


class SavedResponseError(common.YouTubeWorkError):
    pass


class _TranscriptFormatError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def material_index_filename(video_source: str) -> str:
    video_id = common.normalize_youtube_video_id(video_source)
    return f"{video_id}--video-material-index.json"


def saved_response_filename(video_source: str, output_type: str, saved_response_id: str):
    video_id = common.normalize_youtube_video_id(video_source)
    common.validate_output_format(output_type, common.OUTPUT_FORMATS.get(output_type))
    common.validate_sha256(saved_response_id, "saved response ID")
    return f"{video_id}--gemini-response--{output_type}--{saved_response_id}.json"


def _parse_video_start_timestamp(value, failure="timestamp_syntax"):
    if not isinstance(value, str):
        raise _TranscriptFormatError(failure)
    match = TIMESTAMP_PATTERN.fullmatch(value)
    if not match:
        raise _TranscriptFormatError(failure)
    minutes, seconds, milliseconds = (int(item) for item in match.groups())
    return ((minutes * 60) + seconds) * 1000 + milliseconds


def _extract_generated_transcript(response):
    if not isinstance(response, dict):
        raise _TranscriptFormatError("response_envelope")
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise _TranscriptFormatError("response_envelope")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise _TranscriptFormatError("response_envelope")
    content = candidate.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("parts"), list):
        raise _TranscriptFormatError("response_envelope")
    texts = [
        part.get("text")
        for part in content["parts"]
        if isinstance(part, dict) and "text" in part
    ]
    if len(texts) != 1 or not isinstance(texts[0], str):
        raise _TranscriptFormatError("response_envelope")
    try:
        transcript = json.loads(texts[0])
    except json.JSONDecodeError as error:
        raise _TranscriptFormatError("generated_json") from error
    finish_reason = candidate.get("finishReason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise _TranscriptFormatError("finish_reason")
    return transcript, finish_reason


def _checked_transcript_coverage(response, source_time_range):
    transcript, finish_reason = _extract_generated_transcript(response)
    if not isinstance(transcript, dict) or set(transcript) != TRANSCRIPT_FIELDS:
        raise _TranscriptFormatError("transcript_fields")
    clip_start = _parse_video_start_timestamp(transcript["clip_start_timestamp"])
    clip_end = _parse_video_start_timestamp(transcript["clip_end_timestamp"])
    completed = _parse_video_start_timestamp(
        transcript["completed_through_timestamp"]
    )
    if (
        clip_start != source_time_range["startMs"]
        or clip_end != source_time_range["endMs"]
    ):
        raise _TranscriptFormatError("clip_mismatch")
    if not clip_start <= completed <= clip_end:
        raise _TranscriptFormatError("completion_range")
    segments = transcript["segments"]
    if not isinstance(segments, list):
        raise _TranscriptFormatError("segment_fields")
    previous_start = None
    for segment in segments:
        if not isinstance(segment, dict) or set(segment) != SEGMENT_FIELDS:
            raise _TranscriptFormatError("segment_fields")
        start = _parse_video_start_timestamp(segment["start_timestamp"])
        end = _parse_video_start_timestamp(segment["end_timestamp"])
        if not clip_start <= start < end <= clip_end:
            raise _TranscriptFormatError("segment_range")
        if end > completed:
            raise _TranscriptFormatError("completion_range")
        if previous_start is not None and start < previous_start:
            raise _TranscriptFormatError("segment_order")
        previous_start = start
        if segment["vocal_type"] not in {
            "sung",
            "spoken",
            "spoken_over_music",
            "other",
        }:
            raise _TranscriptFormatError("segment_fields")
        if segment["audibility"] not in {"clear", "uncertain", "inaudible"}:
            raise _TranscriptFormatError("segment_fields")
        for field in ("language", "text"):
            if not isinstance(segment[field], str):
                raise _TranscriptFormatError("segment_fields")
    complete = transcript["transcription_complete"]
    truncated = transcript["truncation_detected"]
    if not isinstance(complete, bool) or not isinstance(truncated, bool):
        raise _TranscriptFormatError("completion_flags")
    token_limited = finish_reason in {"MAX_TOKENS", "OUTPUT_TOKEN_LIMIT"}
    normal_finish = finish_reason in {None, "STOP"}
    if complete:
        if truncated or not normal_finish or completed != clip_end:
            raise _TranscriptFormatError("completion_flags")
    else:
        if completed == clip_end:
            raise _TranscriptFormatError("completion_flags")
        if token_limited and not truncated:
            raise _TranscriptFormatError("completion_flags")
    return (
        [{"startMs": clip_start, "endMs": completed}]
        if completed > clip_start
        else []
    )


def check_transcript_response(response, source_time_range):
    try:
        coverage = _checked_transcript_coverage(response, source_time_range)
    except _TranscriptFormatError as error:
        return (
            {"name": FORMAT_CHECK_NAME, "status": "failed", "failure": error.code},
            None,
        )
    return {"name": FORMAT_CHECK_NAME, "status": "passed"}, coverage


def _validate_source_time_range(value):
    normalized = common.normalize_intervals([value], "source time range")
    if len(normalized) != 1 or normalized[0] != value:
        raise SavedResponseError("Source time range must be one normalized interval")
    return value


def _saved_response_identity_fields(saved_response):
    identity = dict(saved_response)
    identity.pop("savedResponseId", None)
    identity.pop("responseJsonText", None)
    return identity


def calculate_saved_response_id(saved_response):
    return common.sha256_hex(
        common.canonical_json_bytes(_saved_response_identity_fields(saved_response))
    )


def validate_saved_response(saved_response):
    common.require_exact_fields(
        saved_response,
        SAVED_RESPONSE_REQUIRED_FIELDS,
        SAVED_RESPONSE_OPTIONAL_FIELDS,
        "saved Gemini response",
    )
    if saved_response["fileFormatVersion"] != common.FILE_FORMAT_VERSION:
        raise SavedResponseError("Unsupported saved-response format version")
    saved_response["videoId"] = common.normalize_youtube_video_id(
        saved_response["videoId"]
    )
    common.validate_sha256(saved_response["savedResponseId"], "saved response ID")
    common.validate_sha256(saved_response["requestId"], "saved response request ID")
    common.validate_sha256(
        saved_response["exactRequestSha256"], "saved response exact request hash"
    )
    if (
        not isinstance(saved_response["runNumber"], int)
        or isinstance(saved_response["runNumber"], bool)
        or saved_response["runNumber"] < 1
    ):
        raise SavedResponseError("Saved response run number must be positive")
    if saved_response["contentClass"] != "video_material":
        raise SavedResponseError("Only video_material can have a saved response")
    output_type = saved_response["outputType"]
    output_format = common.validate_output_format(
        output_type, saved_response["outputFormat"]
    )
    _validate_source_time_range(saved_response["sourceTimeRange"])
    router_result = request_log.validate_router_result(
        dict(saved_response["routerResult"])
    )
    if router_result["status"] != "succeeded":
        raise SavedResponseError("Saved response requires successful routing")
    if (
        router_result["requestId"] != saved_response["requestId"]
        or router_result["runNumber"] != saved_response["runNumber"]
        or router_result["exactRequestSha256"]
        != saved_response["exactRequestSha256"]
    ):
        raise SavedResponseError("Saved response router binding is inconsistent")
    if not isinstance(saved_response["responseJsonText"], str):
        raise SavedResponseError("responseJsonText must be a string")
    response_bytes = saved_response["responseJsonText"].encode("utf-8")
    response_hash = common.sha256_hex(response_bytes)
    common.validate_sha256(saved_response["responseSha256"], "saved response hash")
    if (
        response_hash != saved_response["responseSha256"]
        or response_hash != router_result["responseSha256"]
    ):
        raise SavedResponseError("Saved response text does not match its response hash")
    try:
        response = json.loads(saved_response["responseJsonText"])
    except json.JSONDecodeError as error:
        raise SavedResponseError("responseJsonText must contain valid JSON") from error

    if output_format["name"] == "gemini-transcript":
        if "formatCheck" not in saved_response:
            raise SavedResponseError("Structured transcript requires formatCheck")
        expected_check, expected_coverage = check_transcript_response(
            response, saved_response["sourceTimeRange"]
        )
        if saved_response["formatCheck"] != expected_check:
            raise SavedResponseError("Transcript formatCheck is not mechanically derived")
        if expected_coverage is None:
            if "coveredTimeRanges" in saved_response:
                raise SavedResponseError("Failed transcript check cannot claim covered time")
        else:
            actual = common.normalize_intervals(
                saved_response.get("coveredTimeRanges", []), "covered time ranges"
            )
            if actual != expected_coverage:
                raise SavedResponseError("Transcript covered time is not mechanically derived")
            saved_response["coveredTimeRanges"] = actual
    else:
        if "formatCheck" in saved_response or "coveredTimeRanges" in saved_response:
            raise SavedResponseError(
                "Free-form saved responses cannot contain formatCheck or covered time"
            )
    expected_id = calculate_saved_response_id(saved_response)
    if saved_response["savedResponseId"] != expected_id:
        raise SavedResponseError("Saved response ID does not match its immutable fields")
    return saved_response


def validate_saved_response_file(path: Path):
    path = Path(path).resolve()
    saved_response = common.load_json(path)
    validate_saved_response(saved_response)
    expected_name = saved_response_filename(
        saved_response["videoId"],
        saved_response["outputType"],
        saved_response["savedResponseId"],
    )
    if path.name != expected_name:
        raise SavedResponseError("Saved response filename does not match its identity")
    return saved_response


def build_saved_response(
    log,
    request_path,
    request_id,
    run_number,
    router_result,
    response_path,
):
    request_log.validate_request_log(log)
    request, run = request_log.verify_pending_run(
        log,
        request_path,
        request_log.find_request(log, request_id)["endpoint"],
        request_log.find_request(log, request_id)["model"],
        request_log.find_request(log, request_id)["requestMethod"],
        request_id,
        run_number,
    )
    if request["contentClass"] != "video_material":
        raise SavedResponseError("Only video_material responses are saved")
    result = request_log.validate_router_result(dict(router_result))
    if result["status"] != "succeeded":
        raise SavedResponseError("Cannot save a failed Gemini response")
    if (
        result["requestId"] != request_id
        or result["runNumber"] != run_number
        or result["exactRequestSha256"] != run["exactRequestSha256"]
    ):
        raise SavedResponseError("Router result does not belong to the pending run")
    if run["routingAttempts"] != result["attempts"][: len(run["routingAttempts"])]:
        raise SavedResponseError("Pending routing attempts conflict with router history")
    response_path = Path(response_path).resolve()
    response_bytes = response_path.read_bytes()
    response_hash = common.sha256_hex(response_bytes)
    if response_hash != result["responseSha256"]:
        raise SavedResponseError("Response-file bytes do not match the router result")
    try:
        response_json_text = response_bytes.decode("utf-8")
        response = json.loads(response_json_text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SavedResponseError("Gemini response file must contain UTF-8 JSON") from error
    saved_response = {
        "fileFormatVersion": common.FILE_FORMAT_VERSION,
        "videoId": request["videoId"],
        "requestId": request_id,
        "runNumber": run_number,
        "exactRequestSha256": run["exactRequestSha256"],
        "contentClass": request["contentClass"],
        "outputType": request["outputType"],
        "outputFormat": request["outputFormat"],
        "sourceTimeRange": request["requestedTimeRange"],
        "routerResult": result,
        "responseSha256": response_hash,
        "responseJsonText": response_json_text,
    }
    if request["outputFormat"]["name"] == "gemini-transcript":
        format_check, coverage = check_transcript_response(
            response, request["requestedTimeRange"]
        )
        saved_response["formatCheck"] = format_check
        if coverage is not None:
            saved_response["coveredTimeRanges"] = coverage
    saved_response["savedResponseId"] = calculate_saved_response_id(saved_response)
    validate_saved_response(saved_response)
    return saved_response


def write_immutable_saved_response(directory: Path, saved_response):
    validate_saved_response(saved_response)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / saved_response_filename(
        saved_response["videoId"],
        saved_response["outputType"],
        saved_response["savedResponseId"],
    )
    payload = common.stored_json_bytes(saved_response)
    if path.exists():
        if path.read_bytes() != payload:
            raise SavedResponseError(f"Refusing to rewrite saved response: {path}")
        return path
    path.write_bytes(payload)
    return path


def new_material_index(video_source: str, updated_at=None):
    return {
        "fileFormatVersion": common.FILE_FORMAT_VERSION,
        "videoId": common.normalize_youtube_video_id(video_source),
        "materials": [],
        "updatedAt": updated_at or common.utc_now(),
    }


def validate_material_entry(entry, video_id):
    common.require_exact_fields(
        entry,
        MATERIAL_REQUIRED_FIELDS,
        MATERIAL_OPTIONAL_FIELDS,
        "video material entry",
    )
    common.validate_sha256(entry["savedResponseId"], "material saved response ID")
    common.validate_sha256(entry["fileSha256"], "material file hash")
    for field in ("fileName",):
        if not isinstance(entry[field], str) or not entry[field]:
            raise SavedResponseError(f"Material {field} must be non-empty")
    common.validate_output_format(entry["outputType"], entry["outputFormat"])
    expected_name = saved_response_filename(
        video_id, entry["outputType"], entry["savedResponseId"]
    )
    if entry["fileName"] != expected_name:
        raise SavedResponseError("Material filename does not match its identity")
    entry["coveredTimeRanges"] = common.normalize_intervals(
        entry["coveredTimeRanges"], "material covered time ranges"
    )
    if not entry["coveredTimeRanges"]:
        raise SavedResponseError("Indexed material must cover a non-empty time range")
    if "materialDescription" in entry and (
        not isinstance(entry["materialDescription"], str)
        or not entry["materialDescription"].strip()
    ):
        raise SavedResponseError("Material description must be non-empty")
    return entry


def validate_material_index(index):
    common.require_exact_fields(index, INDEX_FIELDS, set(), "video material index")
    if index["fileFormatVersion"] != common.FILE_FORMAT_VERSION:
        raise SavedResponseError("Unsupported material-index format version")
    index["videoId"] = common.normalize_youtube_video_id(index["videoId"])
    common.parse_timestamp(index["updatedAt"], "material index updatedAt")
    if not isinstance(index["materials"], list):
        raise SavedResponseError("Material index materials must be an array")
    ids = set()
    for entry in index["materials"]:
        validate_material_entry(entry, index["videoId"])
        if entry["savedResponseId"] in ids:
            raise SavedResponseError(
                f"Duplicate saved response ID: {entry['savedResponseId']}"
            )
        ids.add(entry["savedResponseId"])
    if [item["savedResponseId"] for item in index["materials"]] != sorted(ids):
        raise SavedResponseError("Material index entries must be sorted by savedResponseId")
    return index


def _material_entry(
    saved_response,
    saved_response_path,
    reviewed_free_form=False,
    covered_time_ranges=None,
    material_description=None,
):
    path = Path(saved_response_path).resolve()
    validate_saved_response(saved_response)
    expected_name = saved_response_filename(
        saved_response["videoId"],
        saved_response["outputType"],
        saved_response["savedResponseId"],
    )
    if path.name != expected_name:
        raise SavedResponseError("Saved response filename does not match its identity")
    if saved_response["outputFormat"]["name"] == "gemini-transcript":
        if saved_response["formatCheck"]["status"] != "passed":
            raise SavedResponseError("Failed transcript format check cannot enter the index")
        coverage = saved_response.get("coveredTimeRanges", [])
        if covered_time_ranges is not None:
            raise SavedResponseError("Transcript covered time cannot be supplied by a caller")
    else:
        if reviewed_free_form is not True:
            raise SavedResponseError("Free-form material requires explicit Codex review")
        if covered_time_ranges is None:
            raise SavedResponseError("Free-form material requires conservative covered time")
        coverage = common.normalize_intervals(
            covered_time_ranges, "free-form covered time ranges"
        )
        if not common.is_subset(coverage, [saved_response["sourceTimeRange"]]):
            raise SavedResponseError("Free-form covered time exceeds its source range")
    if not coverage:
        raise SavedResponseError("Indexed material must cover a non-empty time range")
    entry = {
        "savedResponseId": saved_response["savedResponseId"],
        "fileName": expected_name,
        "fileSha256": common.sha256_hex(path.read_bytes()),
        "outputType": saved_response["outputType"],
        "outputFormat": saved_response["outputFormat"],
        "coveredTimeRanges": coverage,
    }
    if material_description is not None:
        if not isinstance(material_description, str) or not material_description.strip():
            raise SavedResponseError("Material description must be non-empty")
        entry["materialDescription"] = material_description.strip()
    validate_material_entry(entry, saved_response["videoId"])
    return entry


def add_to_material_index(
    index,
    saved_response_path,
    reviewed_free_form=False,
    covered_time_ranges=None,
    material_description=None,
    updated_at=None,
):
    original_index = index
    index = copy.deepcopy(index)
    validate_material_index(index)
    path = Path(saved_response_path).resolve()
    saved_response = validate_saved_response_file(path)
    if saved_response["videoId"] != index["videoId"]:
        raise SavedResponseError("Saved response and material-index video IDs differ")
    entry = _material_entry(
        saved_response,
        path,
        reviewed_free_form,
        covered_time_ranges,
        material_description,
    )
    existing = next(
        (
            item
            for item in index["materials"]
            if item["savedResponseId"] == entry["savedResponseId"]
        ),
        None,
    )
    if existing is not None:
        if existing != entry:
            raise SavedResponseError(
                "Saved response ID already has different material-index metadata"
            )
        return existing
    index["materials"].append(entry)
    index["materials"].sort(key=lambda item: item["savedResponseId"])
    next_updated_at = updated_at or common.utc_now()
    if common.parse_timestamp(
        next_updated_at, "new material-index updatedAt"
    ) < common.parse_timestamp(index["updatedAt"], "previous material-index updatedAt"):
        raise SavedResponseError("Material-index updatedAt cannot move backwards")
    index["updatedAt"] = next_updated_at
    validate_material_index(index)
    original_index.clear()
    original_index.update(index)
    return entry


def rebuild_material_index(
    video_source,
    saved_response_paths,
    free_form_admissions=None,
    updated_at=None,
):
    paths = sorted((Path(item).resolve() for item in saved_response_paths), key=str)
    if not paths:
        raise SavedResponseError(
            "Cannot rebuild without saved responses; initialize only after confirming none exist"
        )
    video_id = common.normalize_youtube_video_id(video_source)
    index = new_material_index(video_id, updated_at=updated_at)
    admissions = free_form_admissions or {}
    for path in paths:
        saved_response = validate_saved_response_file(path)
        if saved_response["videoId"] != video_id:
            continue
        kwargs = {}
        if saved_response["outputFormat"]["name"] == "gemini-transcript":
            if saved_response["formatCheck"]["status"] != "passed":
                continue
        else:
            admission = admissions.get(path.name)
            if not isinstance(admission, dict):
                raise SavedResponseError(
                    f"Free-form response requires review metadata: {path.name}"
                )
            common.require_exact_fields(
                admission,
                {"admitted"},
                {"coveredTimeRanges", "materialDescription"},
                "free-form admission",
            )
            if not isinstance(admission["admitted"], bool):
                raise SavedResponseError("Free-form admitted decision must be Boolean")
            if admission["admitted"] is False:
                if len(admission) != 1:
                    raise SavedResponseError(
                        "Rejected free-form response cannot claim indexed metadata"
                    )
                continue
            if "coveredTimeRanges" not in admission:
                raise SavedResponseError(
                    "Admitted free-form response requires covered time"
                )
            kwargs = {
                "reviewed_free_form": True,
                "covered_time_ranges": admission["coveredTimeRanges"],
                "material_description": admission.get("materialDescription"),
            }
        add_to_material_index(
            index,
            path,
            updated_at=updated_at,
            **kwargs,
        )
    index["updatedAt"] = updated_at or common.utc_now()
    validate_material_index(index)
    return index


def verify_saved_response_for_entry(entry, response_directory, video_id):
    path = Path(response_directory).resolve() / entry["fileName"]
    if not path.is_file():
        raise SavedResponseError("missing_saved_response_file")
    if common.sha256_hex(path.read_bytes()) != entry["fileSha256"]:
        raise SavedResponseError("saved_response_integrity_mismatch")
    saved_response = validate_saved_response_file(path)
    if saved_response["videoId"] != video_id:
        raise SavedResponseError("saved_response_video_mismatch")
    if saved_response["savedResponseId"] != entry["savedResponseId"]:
        raise SavedResponseError("saved_response_identity_mismatch")
    expected = _material_entry(
        saved_response,
        path,
        reviewed_free_form=True,
        covered_time_ranges=(
            entry["coveredTimeRanges"]
            if saved_response["outputFormat"]["name"] == "gemini-free-form-text"
            else None
        ),
        material_description=entry.get("materialDescription"),
    )
    if expected != entry:
        raise SavedResponseError("material_index_metadata_mismatch")
    return saved_response


def validate_query(query):
    common.require_exact_fields(query, QUERY_REQUIRED_FIELDS, QUERY_OPTIONAL_FIELDS, "material query")
    query = dict(query)
    query["videoId"] = common.normalize_youtube_video_id(query.pop("video"))
    common.validate_output_format(query["outputType"], query["outputFormat"])
    query["requestedTimeRanges"] = common.normalize_intervals(
        query["requestedTimeRanges"], "query requested time ranges"
    )
    if not query["requestedTimeRanges"]:
        raise SavedResponseError("Material query requires a non-empty time range")
    return query


def _compatibility_reasons(entry, query):
    reasons = []
    if entry["outputFormat"] != query["outputFormat"]:
        reasons.append("output_format")
    return reasons


def _entry_summary(entry):
    return {
        key: entry[key]
        for key in (
            "savedResponseId",
            "fileName",
            "fileSha256",
            "coveredTimeRanges",
        )
    }


def _intervals_cover(covering, requested):
    return not common.subtract_intervals(requested, covering)


def _interval_duration(intervals):
    return sum(item["endMs"] - item["startMs"] for item in intervals)


def _choose_covering(candidates, requested):
    covering = [
        item for item in candidates if _intervals_cover(item["coveredTimeRanges"], requested)
    ]
    if not covering:
        return None
    covering.sort(
        key=lambda item: (
            item["coveredTimeRanges"] != requested,
            _interval_duration(item["coveredTimeRanges"]),
            item["savedResponseId"],
        )
    )
    return covering[0]


def _choose_composite(candidates, requested):
    selected = {}
    for target in requested:
        cursor = target["startMs"]
        while cursor < target["endMs"]:
            choices = []
            next_start = None
            for entry in candidates:
                for coverage in common.intersect_intervals(
                    entry["coveredTimeRanges"], [target]
                ):
                    if coverage["startMs"] <= cursor < coverage["endMs"]:
                        choices.append(
                            (coverage["endMs"], entry["savedResponseId"], entry)
                        )
                    elif coverage["startMs"] > cursor:
                        if next_start is None or coverage["startMs"] < next_start:
                            next_start = coverage["startMs"]
            if choices:
                _, _, chosen = max(choices, key=lambda item: (item[0], item[1]))
                selected[chosen["savedResponseId"]] = chosen
                cursor = max(
                    coverage["endMs"]
                    for coverage in common.intersect_intervals(
                        chosen["coveredTimeRanges"], [target]
                    )
                    if coverage["startMs"] <= cursor < coverage["endMs"]
                )
            elif next_start is not None and next_start < target["endMs"]:
                cursor = next_start
            else:
                break
    return [selected[key] for key in sorted(selected)]


def _selected_overlap(selected, requested):
    intervals = []
    for entry in selected:
        for interval in common.intersect_intervals(
            entry["coveredTimeRanges"], requested
        ):
            intervals.append(
                (interval["startMs"], interval["endMs"], entry["savedResponseId"])
            )
    intervals.sort()
    for index, left in enumerate(intervals):
        for right in intervals[index + 1 :]:
            if right[0] >= left[1]:
                break
            if left[2] != right[2]:
                return True
    return False


def find_material(index, query, excluded_saved_response_ids=None):
    validate_material_index(index)
    query = validate_query(query)
    if index["videoId"] != query["videoId"]:
        raise SavedResponseError("Material query and index video IDs differ")
    excluded = set(excluded_saved_response_ids or [])
    for saved_response_id in excluded:
        common.validate_sha256(saved_response_id, "excluded saved response ID")
    compatible = []
    incompatible = []
    for entry in index["materials"]:
        if entry["outputType"] != query["outputType"]:
            continue
        if entry["savedResponseId"] in excluded:
            continue
        reasons = _compatibility_reasons(entry, query)
        if reasons:
            incompatible.append({**_entry_summary(entry), "reasons": reasons})
            continue
        if common.intersect_intervals(
            entry["coveredTimeRanges"], query["requestedTimeRanges"]
        ):
            compatible.append(entry)
    selected = []
    cases = set()
    covering = _choose_covering(compatible, query["requestedTimeRanges"])
    if covering is not None:
        selected = [covering]
        cases.add(
            "exact"
            if covering["coveredTimeRanges"] == query["requestedTimeRanges"]
            else "containing"
        )
    else:
        selected = _choose_composite(compatible, query["requestedTimeRanges"])
        if len(selected) > 1:
            cases.add("composite")
            if _selected_overlap(selected, query["requestedTimeRanges"]):
                cases.add("overlapping")
    covered = common.normalize_intervals(
        [
            interval
            for entry in selected
            for interval in common.intersect_intervals(
                entry["coveredTimeRanges"], query["requestedTimeRanges"]
            )
        ],
        "selected covered time",
    )
    missing = common.subtract_intervals(query["requestedTimeRanges"], covered)
    if incompatible:
        cases.add("incompatible")
    if missing:
        cases.add("missing")
    status = "complete" if not missing else ("partial" if covered else "missing")
    return {
        "fileFormatVersion": common.FILE_FORMAT_VERSION,
        "videoId": index["videoId"],
        "outputType": query["outputType"],
        "outputFormat": query["outputFormat"],
        "coverageStatus": status,
        "coverageCases": [item for item in COVERAGE_CASE_ORDER if item in cases],
        "requestedTimeRanges": query["requestedTimeRanges"],
        "coveredTimeRanges": covered,
        "missingTimeRanges": missing,
        "selectedMaterials": [_entry_summary(entry) for entry in selected],
        "incompatibleMaterials": incompatible,
        "staleIndexEntries": [],
        "savedResponseIdsToVerify": [entry["savedResponseId"] for entry in selected],
        "verificationStatus": "verification_required" if selected else "not_required",
    }


def _validate_search_plan(plan):
    required = {
        "fileFormatVersion",
        "videoId",
        "outputType",
        "outputFormat",
        "coverageStatus",
        "coverageCases",
        "requestedTimeRanges",
        "coveredTimeRanges",
        "missingTimeRanges",
        "selectedMaterials",
        "incompatibleMaterials",
        "staleIndexEntries",
        "savedResponseIdsToVerify",
        "verificationStatus",
    }
    common.require_exact_fields(plan, required, set(), "material search plan")
    if plan["fileFormatVersion"] != common.FILE_FORMAT_VERSION:
        raise SavedResponseError("Unsupported material search-plan format version")
    if plan["verificationStatus"] not in {
        "verification_required",
        "verified",
        "not_required",
    }:
        raise SavedResponseError("Unsupported material verification status")
    return plan


def verify_selected(index, response_directory, query, plan):
    validate_material_index(index)
    raw_query = copy.deepcopy(query)
    query = validate_query(query)
    plan = _validate_search_plan(copy.deepcopy(plan))
    if index["videoId"] != query["videoId"] or plan["videoId"] != query["videoId"]:
        raise SavedResponseError("Index, query, and search-plan video IDs differ")
    stale_by_id = {
        item["savedResponseId"]: item for item in plan["staleIndexEntries"]
    }
    current = find_material(index, raw_query, excluded_saved_response_ids=stale_by_id)
    semantic_fields = {
        "videoId",
        "outputType",
        "outputFormat",
        "coverageStatus",
        "coverageCases",
        "requestedTimeRanges",
        "coveredTimeRanges",
        "missingTimeRanges",
        "selectedMaterials",
        "incompatibleMaterials",
    }
    if any(plan[field] != current[field] for field in semantic_fields):
        current["staleIndexEntries"] = [
            stale_by_id[key] for key in sorted(stale_by_id)
        ]
        return current
    planned_ids = {item["savedResponseId"] for item in plan["selectedMaterials"]}
    if planned_ids != set(plan["savedResponseIdsToVerify"]):
        raise SavedResponseError("Verification IDs do not match selected material")
    by_id = {item["savedResponseId"]: item for item in index["materials"]}
    stale = []
    for saved_response_id in sorted(planned_ids):
        entry = by_id.get(saved_response_id)
        if entry is None:
            stale.append(
                {
                    "savedResponseId": saved_response_id,
                    "fileName": None,
                    "reason": "missing_index_entry",
                }
            )
            continue
        try:
            verify_saved_response_for_entry(entry, response_directory, index["videoId"])
        except (SavedResponseError, common.YouTubeWorkError) as error:
            stale.append(
                {
                    "savedResponseId": entry["savedResponseId"],
                    "fileName": entry["fileName"],
                    "reason": str(error),
                }
            )
    if not stale:
        verified = dict(plan)
        verified["savedResponseIdsToVerify"] = []
        verified["verificationStatus"] = "verified" if planned_ids else "not_required"
        return verified
    for item in stale:
        stale_by_id[item["savedResponseId"]] = item
    replanned = find_material(
        index, raw_query, excluded_saved_response_ids=set(stale_by_id)
    )
    replanned["staleIndexEntries"] = [
        stale_by_id[key] for key in sorted(stale_by_id)
    ]
    return replanned


def plan_missing_ranges(plan):
    plan = _validate_search_plan(copy.deepcopy(plan))
    if (
        plan["savedResponseIdsToVerify"]
        or plan["verificationStatus"] == "verification_required"
    ):
        raise SavedResponseError("Selected responses must be verified before planning new work")
    return {
        "fileFormatVersion": common.FILE_FORMAT_VERSION,
        "videoId": plan["videoId"],
        "outputType": plan["outputType"],
        "outputFormat": plan["outputFormat"],
        "missingTimeRanges": plan["missingTimeRanges"],
    }


def _validate_missing_range_plan(plan):
    required = {
        "fileFormatVersion",
        "videoId",
        "outputType",
        "outputFormat",
        "missingTimeRanges",
    }
    common.require_exact_fields(plan, required, set(), "missing-range plan")
    if plan["fileFormatVersion"] != common.FILE_FORMAT_VERSION:
        raise SavedResponseError("Unsupported missing-range-plan format version")
    video_id = common.normalize_youtube_video_id(plan["videoId"])
    if video_id != plan["videoId"]:
        raise SavedResponseError("Missing-range-plan video ID must be normalized")
    output_format = common.validate_output_format(
        plan["outputType"], plan["outputFormat"]
    )
    missing = common.normalize_intervals(
        plan["missingTimeRanges"], "missing time ranges"
    )
    if missing != plan["missingTimeRanges"]:
        raise SavedResponseError("Missing time ranges must be normalized")
    return {
        "fileFormatVersion": common.FILE_FORMAT_VERSION,
        "videoId": video_id,
        "outputType": plan["outputType"],
        "outputFormat": output_format,
        "missingTimeRanges": missing,
    }


def plan_chunks(missing_range_plan, chunk_seconds=1_800, overlap_seconds=0):
    plan = _validate_missing_range_plan(copy.deepcopy(missing_range_plan))
    if (
        not isinstance(chunk_seconds, int)
        or isinstance(chunk_seconds, bool)
        or chunk_seconds <= 0
    ):
        raise SavedResponseError("Chunk size must be a positive integer")
    if (
        not isinstance(overlap_seconds, int)
        or isinstance(overlap_seconds, bool)
        or overlap_seconds < 0
        or overlap_seconds >= chunk_seconds
    ):
        raise SavedResponseError("Overlap must satisfy 0 <= overlap < chunk size")

    chunk_ms = chunk_seconds * 1_000
    overlap_ms = overlap_seconds * 1_000
    chunks = []
    for interval in plan["missingTimeRanges"]:
        start = interval["startMs"]
        while start < interval["endMs"]:
            end = min(start + chunk_ms, interval["endMs"])
            chunks.append({"startMs": start, "endMs": end})
            if end == interval["endMs"]:
                break
            start = end - overlap_ms

    return {
        "fileFormatVersion": common.FILE_FORMAT_VERSION,
        "videoId": plan["videoId"],
        "outputType": plan["outputType"],
        "outputFormat": plan["outputFormat"],
        "chunkSeconds": chunk_seconds,
        "overlapSeconds": overlap_seconds,
        "chunks": chunks,
    }


def command_locate(args):
    video_id = common.normalize_youtube_video_id(args.video)
    print(
        json.dumps(
            {
                "stateDirectoryName": common.LOCAL_STATE_DIRECTORY,
                "videoId": video_id,
                "materialIndexFileName": material_index_filename(video_id),
                "savedResponseNamePattern": f"{video_id}--gemini-response--<outputType>--<savedResponseId>.json",
            },
            sort_keys=True,
        )
    )
    return 0


def command_save_response(args):
    log = common.load_json(Path(args.request_log))
    router_result = common.load_json(Path(args.router_result))
    saved_response = build_saved_response(
        log,
        args.request,
        args.request_id,
        args.run_number,
        router_result,
        args.response,
    )
    path = write_immutable_saved_response(Path(args.output_dir), saved_response)
    print(
        json.dumps(
            {
                "savedResponseId": saved_response["savedResponseId"],
                "fileName": path.name,
                "path": str(path),
                "fileSha256": common.sha256_hex(path.read_bytes()),
                "formatCheck": saved_response.get("formatCheck"),
            },
            sort_keys=True,
        )
    )
    return 0


def command_init_index(args):
    if not args.confirmed_no_saved_responses:
        raise SavedResponseError(
            "Refusing empty index initialization until saved responses were enumerated"
        )
    common.write_json(
        Path(args.output), new_material_index(args.video, updated_at=args.updated_at)
    )
    print(Path(args.output).resolve())
    return 0


def command_add_to_index(args):
    index = common.load_json(Path(args.material_index))
    coverage = (
        common.load_json(Path(args.covered_time_ranges))
        if args.covered_time_ranges
        else None
    )
    entry = add_to_material_index(
        index,
        args.saved_response,
        reviewed_free_form=args.reviewed_free_form,
        covered_time_ranges=coverage,
        material_description=args.material_description,
        updated_at=args.updated_at,
    )
    common.write_json(Path(args.output), index)
    print(json.dumps(entry, sort_keys=True))
    return 0


def command_rebuild_index(args):
    directory = Path(args.responses_dir).resolve()
    video_id = common.normalize_youtube_video_id(args.video)
    admissions = (
        common.load_json(Path(args.free_form_admissions))
        if args.free_form_admissions
        else None
    )
    paths = directory.glob(f"{video_id}--gemini-response--*--*.json")
    index = rebuild_material_index(
        video_id, paths, admissions, updated_at=args.updated_at
    )
    common.write_json(Path(args.output), index)
    print(Path(args.output).resolve())
    return 0


def command_find_material(args):
    index = common.load_json(Path(args.material_index))
    query = common.load_json(Path(args.query))
    plan = find_material(index, query)
    common.write_json(Path(args.output), plan)
    print(Path(args.output).resolve())
    return 0


def command_verify_selected(args):
    index = common.load_json(Path(args.material_index))
    query = common.load_json(Path(args.query))
    plan = common.load_json(Path(args.search_plan))
    verified = verify_selected(index, args.responses_dir, query, plan)
    common.write_json(Path(args.output), verified)
    print(Path(args.output).resolve())
    return 0


def command_plan_missing(args):
    plan = common.load_json(Path(args.search_plan))
    result = plan_missing_ranges(plan)
    common.write_json(Path(args.output), result)
    print(Path(args.output).resolve())
    return 0


def command_plan_chunks(args):
    plan = common.load_json(Path(args.missing_ranges_plan))
    result = plan_chunks(plan, args.chunk_seconds, args.overlap_seconds)
    common.write_json(Path(args.output), result)
    print(Path(args.output).resolve())
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Save Gemini responses and find reusable YouTube material"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    locate = subparsers.add_parser("locate")
    locate.add_argument("--video", required=True)
    locate.set_defaults(handler=command_locate)

    save = subparsers.add_parser("save-response")
    save.add_argument("--request-log", required=True)
    save.add_argument("--request", required=True)
    save.add_argument("--request-id", required=True)
    save.add_argument("--run-number", required=True, type=int)
    save.add_argument("--router-result", required=True)
    save.add_argument("--response", required=True)
    save.add_argument("--output-dir", required=True)
    save.set_defaults(handler=command_save_response)

    initialize = subparsers.add_parser("init-material-index")
    initialize.add_argument("--video", required=True)
    initialize.add_argument("--output", required=True)
    initialize.add_argument("--updated-at")
    initialize.add_argument("--confirmed-no-saved-responses", action="store_true")
    initialize.set_defaults(handler=command_init_index)

    add = subparsers.add_parser("add-to-material-index")
    add.add_argument("--material-index", required=True)
    add.add_argument("--saved-response", required=True)
    add.add_argument("--reviewed-free-form", action="store_true")
    add.add_argument("--covered-time-ranges")
    add.add_argument("--material-description")
    add.add_argument("--updated-at")
    add.add_argument("--output", required=True)
    add.set_defaults(handler=command_add_to_index)

    rebuild = subparsers.add_parser("rebuild-material-index")
    rebuild.add_argument("--video", required=True)
    rebuild.add_argument("--responses-dir", required=True)
    rebuild.add_argument("--free-form-admissions")
    rebuild.add_argument("--updated-at")
    rebuild.add_argument("--output", required=True)
    rebuild.set_defaults(handler=command_rebuild_index)

    find = subparsers.add_parser("find-material")
    find.add_argument("--material-index", required=True)
    find.add_argument("--query", required=True)
    find.add_argument("--output", required=True)
    find.set_defaults(handler=command_find_material)

    verify = subparsers.add_parser("verify-selected")
    verify.add_argument("--material-index", required=True)
    verify.add_argument("--responses-dir", required=True)
    verify.add_argument("--query", required=True)
    verify.add_argument("--search-plan", required=True)
    verify.add_argument("--output", required=True)
    verify.set_defaults(handler=command_verify_selected)

    missing = subparsers.add_parser("plan-missing-ranges")
    missing.add_argument("--search-plan", required=True)
    missing.add_argument("--output", required=True)
    missing.set_defaults(handler=command_plan_missing)

    chunks = subparsers.add_parser("plan-chunks")
    chunks.add_argument("--missing-ranges-plan", required=True)
    chunks.add_argument("--chunk-seconds", type=int, default=1_800)
    chunks.add_argument("--overlap-seconds", type=int, default=0)
    chunks.add_argument("--output", required=True)
    chunks.set_defaults(handler=command_plan_chunks)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.handler(args)
    except (
        SavedResponseError,
        request_log.RequestLogError,
        common.YouTubeWorkError,
        OSError,
    ) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
