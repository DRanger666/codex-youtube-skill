import argparse
import copy
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gemini_request_log as request_log
import saved_gemini_responses as saved
import youtube_work_common as common
from build_gemini_chunk_request import TRANSCRIPT_PROMPT, TRANSCRIPT_RESPONSE_SCHEMA


VIDEO_ID = "lhSq1RzDcZg"
ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.6-flash:generateContent"
)
MODEL = "gemini-3.6-flash"
T0 = "2026-08-01T10:00:00Z"
T1 = "2026-08-01T10:00:01Z"
T2 = "2026-08-01T10:00:02Z"
REMOVED_TIME_COORDINATE_FIELDS = (
    "timestamps" + "RelativeTo",
    "timestamp" + "Basis",
)
REMOVED_LANGUAGE_FIELDS = (
    "language" + "Policy",
    "source" + "Language",
)


def timestamp(milliseconds):
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def transcript_content(start_ms=0, end_ms=600_000, completed_ms=None, **overrides):
    completed_ms = end_ms if completed_ms is None else completed_ms
    value = {
        "clip_start_timestamp": timestamp(start_ms),
        "clip_end_timestamp": timestamp(end_ms),
        "segments": [
            {
                "start_timestamp": timestamp(start_ms),
                "end_timestamp": timestamp(min(start_ms + 2_000, end_ms)),
                "vocal_type": "spoken",
                "language": "en",
                "text": "Test segment",
                "audibility": "clear",
            }
        ],
        "completed_through_timestamp": timestamp(completed_ms),
        "transcription_complete": completed_ms == end_ms,
        "truncation_detected": completed_ms != end_ms,
    }
    value.update(overrides)
    return value


def gemini_response(generated, finish_reason="STOP"):
    text = json.dumps(generated, ensure_ascii=False) if isinstance(generated, dict) else generated
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": text}]},
                "finishReason": finish_reason,
            }
        ],
        "modelVersion": MODEL,
    }


def request_value(prompt, start_ms=0, end_ms=600_000, transcript=False):
    request = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "fileData": {
                            "fileUri": f"https://www.youtube.com/watch?v={VIDEO_ID}",
                            "mimeType": "video/*",
                        },
                        "videoMetadata": {
                            "startOffset": f"{start_ms / 1000:g}s",
                            "endOffset": f"{end_ms / 1000:g}s",
                        },
                    },
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    if transcript:
        request["generationConfig"]["responseJsonSchema"] = TRANSCRIPT_RESPONSE_SCHEMA
    return request


def router_result(request, run, response_hash, attempts=None):
    attempts = attempts or [
        {
            "attemptNumber": 1,
            "bucket": "primary",
            "startedAt": T0,
            "finishedAt": T1,
            "httpStatus": 200,
            "classification": "success",
        }
    ]
    return {
        "fileFormatVersion": 1,
        "status": "succeeded",
        "requestId": request["requestId"],
        "runNumber": run["runNumber"],
        "exactRequestSha256": run["exactRequestSha256"],
        "selectedBucket": attempts[-1]["bucket"],
        "attempts": attempts,
        "completedAt": attempts[-1]["finishedAt"],
        "responseSha256": response_hash,
    }


class SavedResponseFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.responses = self.directory / "responses"
        self.responses.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def build_response(
        self,
        output_type="transcript",
        start_ms=0,
        end_ms=600_000,
        completed_ms=None,
        generated=None,
        finish_reason="STOP",
        prompt=None,
    ):
        transcript = output_type == "transcript"
        output_format = common.OUTPUT_FORMATS[output_type]
        prompt = prompt or (TRANSCRIPT_PROMPT if transcript else f"Create {output_type} material.")
        request_path = self.directory / f"request-{output_type}-{len(list(self.directory.glob('request-*')))}.json"
        common.write_json(
            request_path,
            request_value(prompt, start_ms, end_ms, transcript=transcript),
        )
        log = request_log.new_request_log(VIDEO_ID, updated_at=T0)
        request, run = request_log.start_run(
            log,
            request_path,
            VIDEO_ID,
            ENDPOINT,
            MODEL,
            "POST",
            "video_material",
            output_type=output_type,
            output_format=output_format,
            started_at=T0,
        )
        if generated is None:
            generated = (
                transcript_content(start_ms, end_ms, completed_ms)
                if transcript
                else f"Reusable {output_type} text"
            )
        response_path = self.directory / f"raw-{output_type}-{run['runNumber']}-{len(list(self.directory.glob('raw-*')))}.json"
        common.write_json(response_path, gemini_response(generated, finish_reason))
        result = router_result(
            request, run, common.sha256_hex(response_path.read_bytes())
        )
        saved_response = saved.build_saved_response(
            log,
            request_path,
            request["requestId"],
            run["runNumber"],
            result,
            response_path,
        )
        path = saved.write_immutable_saved_response(self.responses, saved_response)
        return {
            "log": log,
            "request": request,
            "run": run,
            "requestPath": request_path,
            "responsePath": response_path,
            "routerResult": result,
            "saved": saved_response,
            "path": path,
        }


class SavedGeminiResponseTests(SavedResponseFixture):
    def test_uses_local_state_directory_and_predictable_names(self):
        item = self.build_response()
        self.assertEqual(common.LOCAL_STATE_DIRECTORY, "youtube-video-work")
        self.assertEqual(
            saved.material_index_filename(VIDEO_ID),
            f"{VIDEO_ID}--video-material-index.json",
        )
        self.assertEqual(
            item["path"].name,
            f"{VIDEO_ID}--gemini-response--transcript--{item['saved']['savedResponseId']}.json",
        )

    def test_saved_response_is_self_identifying_and_bound_to_exact_router_bytes(self):
        item = self.build_response()
        record = saved.validate_saved_response_file(item["path"])
        self.assertEqual(record["requestId"], item["request"]["requestId"])
        self.assertEqual(record["runNumber"], 1)
        self.assertEqual(record["routerResult"], item["routerResult"])
        self.assertEqual(
            record["responseSha256"],
            common.sha256_hex(item["responsePath"].read_bytes()),
        )
        self.assertEqual(
            record["savedResponseId"], saved.calculate_saved_response_id(record)
        )

    def test_normal_completion_links_the_saved_response_from_the_run(self):
        item = self.build_response()
        request_log.finish_run(
            item["log"],
            item["request"]["requestId"],
            1,
            router_result=item["routerResult"],
            saved_response_path=item["path"],
            updated_at=T2,
        )
        run = item["log"]["requests"][0]["runs"][0]
        self.assertEqual(run["runStatus"], "succeeded")
        self.assertEqual(run["savedResponseId"], item["saved"]["savedResponseId"])
        self.assertEqual(run["savedResponseFileName"], item["path"].name)
        self.assertEqual(run["endedAt"], T1)

    def test_two_authorized_successes_keep_two_distinct_response_references(self):
        item = self.build_response()
        request_log.finish_run(
            item["log"],
            item["request"]["requestId"],
            1,
            router_result=item["routerResult"],
            saved_response_path=item["path"],
            updated_at=T2,
        )
        request, second_run = request_log.start_run(
            item["log"],
            item["requestPath"],
            VIDEO_ID,
            ENDPOINT,
            MODEL,
            "POST",
            "video_material",
            output_type="transcript",
            output_format={"name": "gemini-transcript", "version": 1},
            retry_reason="User requested another transcription run",
            started_at="2026-08-01T10:10:00Z",
        )
        second_attempts = [
            {
                "attemptNumber": 1,
                "bucket": "primary",
                "startedAt": "2026-08-01T10:10:00Z",
                "finishedAt": "2026-08-01T10:10:01Z",
                "httpStatus": 200,
                "classification": "success",
            }
        ]
        second_result = router_result(
            request,
            second_run,
            common.sha256_hex(item["responsePath"].read_bytes()),
            attempts=second_attempts,
        )
        second_saved = saved.build_saved_response(
            item["log"],
            item["requestPath"],
            request["requestId"],
            2,
            second_result,
            item["responsePath"],
        )
        second_path = saved.write_immutable_saved_response(
            self.responses, second_saved
        )
        request_log.finish_run(
            item["log"],
            request["requestId"],
            2,
            router_result=second_result,
            saved_response_path=second_path,
            updated_at="2026-08-01T10:10:02Z",
        )
        runs = item["log"]["requests"][0]["runs"]
        self.assertEqual(len(runs), 2)
        self.assertEqual([run["runStatus"] for run in runs], ["succeeded", "succeeded"])
        self.assertNotEqual(runs[0]["savedResponseId"], runs[1]["savedResponseId"])
        self.assertEqual(
            [run["savedResponseFileName"] for run in runs],
            [item["path"].name, second_path.name],
        )

    def test_interrupted_final_log_write_can_finish_without_another_gemini_call(self):
        item = self.build_response()
        with self.assertRaisesRegex(request_log.RequestLogError, "enumeration"):
            request_log.finish_run(
                item["log"],
                item["request"]["requestId"],
                1,
                candidate_response_paths=[item["path"]],
                confirmed_session_stopped=True,
            )
        request_log.finish_run(
            item["log"],
            item["request"]["requestId"],
            1,
            candidate_response_paths=[item["path"]],
            confirmed_session_stopped=True,
            confirmed_response_enumeration=True,
            updated_at=T2,
        )
        run = item["log"]["requests"][0]["runs"][0]
        self.assertEqual(run["runStatus"], "succeeded")
        self.assertEqual(run["routingAttempts"], item["routerResult"]["attempts"])
        self.assertEqual(run["endedAt"], T1)

    def test_interrupted_completion_rejects_multiple_responses_claiming_one_run(self):
        item = self.build_response()
        conflicting = copy.deepcopy(item["saved"])
        conflicting_text = json.dumps(gemini_response({**transcript_content(), "segments": []}), sort_keys=True, indent=2) + "\n"
        conflicting_hash = common.sha256_hex(conflicting_text.encode("utf-8"))
        conflicting["responseJsonText"] = conflicting_text
        conflicting["responseSha256"] = conflicting_hash
        conflicting["routerResult"]["responseSha256"] = conflicting_hash
        format_check, coverage = saved.check_transcript_response(
            json.loads(conflicting_text), conflicting["sourceTimeRange"]
        )
        conflicting["formatCheck"] = format_check
        conflicting["coveredTimeRanges"] = coverage
        conflicting["savedResponseId"] = saved.calculate_saved_response_id(conflicting)
        conflicting_path = saved.write_immutable_saved_response(
            self.responses, conflicting
        )
        with self.assertRaisesRegex(request_log.RequestLogError, "exactly one"):
            request_log.finish_run(
                item["log"],
                item["request"]["requestId"],
                1,
                candidate_response_paths=[item["path"], conflicting_path],
                confirmed_session_stopped=True,
                confirmed_response_enumeration=True,
            )
        self.assertEqual(
            item["log"]["requests"][0]["runs"][0]["runStatus"], "pending"
        )

    def test_response_saver_rejects_correct_run_with_different_response_bytes(self):
        item = self.build_response()
        wrong = self.directory / "wrong.json"
        common.write_json(wrong, {"different": True})
        with self.assertRaisesRegex(saved.SavedResponseError, "bytes"):
            saved.build_saved_response(
                item["log"],
                item["requestPath"],
                item["request"]["requestId"],
                1,
                item["routerResult"],
                wrong,
            )

    def test_one_time_content_cannot_create_a_saved_response(self):
        request_path = self.directory / "one-time.json"
        common.write_json(request_path, request_value("Answer this question."))
        log = request_log.new_request_log(VIDEO_ID, updated_at=T0)
        request, run = request_log.start_run(
            log,
            request_path,
            VIDEO_ID,
            ENDPOINT,
            MODEL,
            "POST",
            "direct_answer",
            started_at=T0,
        )
        response_path = self.directory / "answer.json"
        common.write_json(response_path, gemini_response("One-time answer"))
        result = router_result(request, run, common.sha256_hex(response_path.read_bytes()))
        with self.assertRaisesRegex(saved.SavedResponseError, "Only video_material"):
            saved.build_saved_response(
                log, request_path, request["requestId"], 1, result, response_path
            )
        self.assertEqual(list(self.responses.iterdir()), [])

    def test_complete_and_incomplete_transcripts_derive_coverage(self):
        complete = self.build_response()
        self.assertEqual(complete["saved"]["formatCheck"]["status"], "passed")
        self.assertEqual(
            complete["saved"]["coveredTimeRanges"],
            [{"startMs": 0, "endMs": 600_000}],
        )
        incomplete = self.build_response(
            start_ms=600_000, end_ms=1_200_000, completed_ms=850_000
        )
        self.assertEqual(incomplete["saved"]["formatCheck"]["status"], "passed")
        self.assertEqual(
            incomplete["saved"]["coveredTimeRanges"],
            [{"startMs": 600_000, "endMs": 850_000}],
        )

    def test_transcript_preserves_segment_language_and_source_text(self):
        generated = transcript_content()
        generated["segments"][0]["language"] = "hi"
        generated["segments"][0]["text"] = "नमस्ते"
        item = self.build_response(generated=generated)
        envelope = json.loads(item["saved"]["responseJsonText"])
        retained = json.loads(envelope["candidates"][0]["content"]["parts"][0]["text"])
        self.assertEqual(retained["segments"][0]["language"], "hi")
        self.assertEqual(retained["segments"][0]["text"], "नमस्ते")

    def test_malformed_transcript_is_saved_but_has_no_covered_time(self):
        item = self.build_response(generated={"not": "a transcript"})
        self.assertEqual(item["saved"]["formatCheck"]["status"], "failed")
        self.assertNotIn("coveredTimeRanges", item["saved"])
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        with self.assertRaisesRegex(saved.SavedResponseError, "cannot enter"):
            saved.add_to_material_index(index, item["path"])
        self.assertEqual(index["materials"], [])

    def test_transcript_checker_rejects_clip_mismatch_segment_order_and_bad_flags(self):
        cases = []
        clip_mismatch = transcript_content()
        clip_mismatch["clip_end_timestamp"] = "09:59.000"
        cases.append((clip_mismatch, "clip_mismatch"))
        segment_order = transcript_content()
        segment_order["segments"] = [
            {
                **segment_order["segments"][0],
                "start_timestamp": "00:05.000",
                "end_timestamp": "00:06.000",
            },
            {
                **segment_order["segments"][0],
                "start_timestamp": "00:03.000",
                "end_timestamp": "00:04.000",
            },
        ]
        cases.append((segment_order, "segment_order"))
        bad_flags = transcript_content(transcription_complete=True, truncation_detected=True)
        cases.append((bad_flags, "completion_flags"))
        false_full_coverage = transcript_content(
            transcription_complete=False, truncation_detected=True
        )
        cases.append((false_full_coverage, "completion_flags"))
        for generated, failure in cases:
            with self.subTest(failure=failure):
                item = self.build_response(generated=generated)
                self.assertEqual(item["saved"]["formatCheck"]["failure"], failure)

    def test_transcript_checker_binds_segments_and_finish_reason_to_completion(self):
        segment_after_completion = transcript_content(completed_ms=1_000)
        item = self.build_response(generated=segment_after_completion)
        self.assertEqual(
            item["saved"]["formatCheck"]["failure"], "completion_range"
        )

        stopped_for_safety = self.build_response(finish_reason="SAFETY")
        self.assertEqual(
            stopped_for_safety["saved"]["formatCheck"]["failure"],
            "completion_flags",
        )

    def test_transcript_checker_accepts_video_start_minutes_above_99(self):
        item = self.build_response(
            start_ms=7_200_000,
            end_ms=7_260_000,
        )
        self.assertEqual(item["saved"]["formatCheck"]["status"], "passed")
        self.assertEqual(
            item["saved"]["coveredTimeRanges"],
            [{"startMs": 7_200_000, "endMs": 7_260_000}],
        )

    def test_transcript_checker_rejects_clip_relative_labels_for_nonzero_source(self):
        item = self.build_response(
            start_ms=600_000,
            end_ms=1_200_000,
            generated=transcript_content(start_ms=0, end_ms=600_000),
        )
        self.assertEqual(item["saved"]["formatCheck"]["failure"], "clip_mismatch")

    def test_free_form_material_has_no_fabricated_format_check(self):
        item = self.build_response(output_type="summary")
        self.assertNotIn("formatCheck", item["saved"])
        self.assertNotIn("coveredTimeRanges", item["saved"])
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        with self.assertRaisesRegex(saved.SavedResponseError, "explicit Codex review"):
            saved.add_to_material_index(index, item["path"])
        saved.add_to_material_index(
            index,
            item["path"],
            reviewed_free_form=True,
            covered_time_ranges=[{"startMs": 0, "endMs": 500_000}],
        )
        self.assertNotIn("formatCheck", index["materials"][0])

    def test_free_form_coverage_cannot_exceed_source_range(self):
        item = self.build_response(output_type="systematic_visual_description")
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        with self.assertRaisesRegex(saved.SavedResponseError, "exceeds"):
            saved.add_to_material_index(
                index,
                item["path"],
                reviewed_free_form=True,
                covered_time_ranges=[{"startMs": 0, "endMs": 700_000}],
            )

    def test_translation_cannot_be_saved_or_searched_as_reusable_material(self):
        output_format = {"name": "gemini-free-form-text", "version": 1}
        with self.assertRaisesRegex(
            common.YouTubeWorkError, "Unsupported reusable output type"
        ):
            saved.saved_response_filename(VIDEO_ID, "translation", "a" * 64)
        with self.assertRaisesRegex(
            common.YouTubeWorkError, "Unsupported reusable output type"
        ):
            saved.validate_query(
                {
                    "video": VIDEO_ID,
                    "outputType": "translation",
                    "outputFormat": output_format,
                    "requestedTimeRanges": [{"startMs": 0, "endMs": 600_000}],
                }
            )

    def test_saved_responses_reject_removed_metadata_fields(self):
        item = self.build_response()
        for field in (*REMOVED_TIME_COORDINATE_FIELDS, *REMOVED_LANGUAGE_FIELDS):
            with self.subTest(field=field):
                changed = copy.deepcopy(item["saved"])
                changed[field] = (
                    "full_video"
                    if field in REMOVED_TIME_COORDINATE_FIELDS
                    else "removed"
                )
                with self.assertRaisesRegex(common.YouTubeWorkError, "unsupported fields"):
                    saved.validate_saved_response(changed)

    def test_saved_response_files_are_immutable(self):
        item = self.build_response()
        original = item["path"].read_bytes()
        self.assertEqual(
            saved.write_immutable_saved_response(self.responses, item["saved"]),
            item["path"],
        )
        self.assertEqual(item["path"].read_bytes(), original)
        changed = copy.deepcopy(item["saved"])
        changed["responseJsonText"] += " "
        with self.assertRaises(saved.SavedResponseError):
            saved.write_immutable_saved_response(self.responses, changed)


class MaterialIndexTests(SavedResponseFixture):
    def add(self, index, item, coverage=None):
        free_form = item["saved"]["outputFormat"]["name"] == "gemini-free-form-text"
        return saved.add_to_material_index(
            index,
            item["path"],
            reviewed_free_form=free_form,
            covered_time_ranges=(
                coverage
                if coverage is not None
                else ([item["saved"]["sourceTimeRange"]] if free_form else None)
            ),
            updated_at=T2,
        )

    def query(self, output_type="transcript", start=0, end=600_000, **extra):
        query = {
            "video": VIDEO_ID,
            "outputType": output_type,
            "outputFormat": common.OUTPUT_FORMATS[output_type],
            "requestedTimeRanges": [{"startMs": start, "endMs": end}],
            **extra,
        }
        return query

    def test_index_contains_search_fields_only(self):
        item = self.build_response()
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, item)
        serialized = json.dumps(index)
        for forbidden in (
            "requestId",
            "runNumber",
            "routingAttempts",
            "retryAuthorization",
            "promptText",
            "responseJsonText",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_distinct_responses_are_preserved_even_when_ranges_match(self):
        first = self.build_response(output_type="summary", prompt="Summary version one")
        second = self.build_response(output_type="summary", prompt="Summary version two")
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, first)
        first_bytes = common.stored_json_bytes(index)
        self.add(index, first)
        self.assertEqual(common.stored_json_bytes(index), first_bytes)
        self.add(index, second)
        self.assertEqual(len(index["materials"]), 2)
        self.assertNotEqual(
            index["materials"][0]["savedResponseId"],
            index["materials"][1]["savedResponseId"],
        )

    def test_material_index_time_cannot_move_backwards(self):
        item = self.build_response()
        index = saved.new_material_index(VIDEO_ID, updated_at=T2)
        original = copy.deepcopy(index)
        with self.assertRaisesRegex(saved.SavedResponseError, "move backwards"):
            saved.add_to_material_index(
                index,
                item["path"],
                updated_at=T1,
            )
        self.assertEqual(index, original)

    def test_all_four_reusable_output_types_coexist_for_one_interval(self):
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        for output_type in common.OUTPUT_FORMATS:
            item = self.build_response(output_type=output_type)
            self.add(index, item)
        self.assertEqual(
            {item["outputType"] for item in index["materials"]},
            set(common.OUTPUT_FORMATS),
        )

    def test_rebuild_uses_self_contained_responses_and_requires_free_form_review(self):
        transcript = self.build_response()
        summary = self.build_response(output_type="summary")
        with self.assertRaisesRegex(saved.SavedResponseError, "review metadata"):
            saved.rebuild_material_index(
                VIDEO_ID, [transcript["path"], summary["path"]]
            )
        admissions = {
            summary["path"].name: {
                "admitted": True,
                "coveredTimeRanges": [{"startMs": 0, "endMs": 600_000}],
            }
        }
        rebuilt = saved.rebuild_material_index(
            VIDEO_ID,
            [transcript["path"], summary["path"]],
            admissions,
            updated_at=T2,
        )
        self.assertEqual(len(rebuilt["materials"]), 2)
        with self.assertRaisesRegex(saved.SavedResponseError, "initialize only"):
            saved.rebuild_material_index(VIDEO_ID, [])

    def test_rebuild_skips_failed_transcript_and_rejected_free_form_response(self):
        malformed = self.build_response(generated={"not": "a transcript"})
        summary = self.build_response(output_type="summary")
        rebuilt = saved.rebuild_material_index(
            VIDEO_ID,
            [malformed["path"], summary["path"]],
            {summary["path"].name: {"admitted": False}},
            updated_at=T2,
        )
        self.assertEqual(rebuilt["materials"], [])

    def test_exact_and_containing_material_are_selected_without_file_reads(self):
        exact = self.build_response()
        containing = self.build_response(start_ms=0, end_ms=900_000)
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, containing)
        self.add(index, exact)
        plan = saved.find_material(index, self.query())
        self.assertEqual(plan["coverageStatus"], "complete")
        self.assertEqual(plan["coverageCases"], ["exact"])
        self.assertEqual(
            plan["selectedMaterials"][0]["savedResponseId"],
            exact["saved"]["savedResponseId"],
        )
        exact["path"].unlink()
        containing["path"].unlink()
        repeated = saved.find_material(index, self.query())
        self.assertEqual(repeated, plan)

    def test_adjacent_and_overlapping_material_compose_coverage(self):
        adjacent = [
            self.build_response(start_ms=0, end_ms=300_000),
            self.build_response(start_ms=300_000, end_ms=600_000),
        ]
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        for item in adjacent:
            self.add(index, item)
        plan = saved.find_material(index, self.query())
        self.assertEqual(plan["coverageCases"], ["composite"])
        self.assertEqual(plan["missingTimeRanges"], [])

        overlapping = [
            self.build_response(start_ms=0, end_ms=350_000),
            self.build_response(start_ms=250_000, end_ms=600_000),
        ]
        overlap_index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        for item in overlapping:
            self.add(overlap_index, item)
        overlap_plan = saved.find_material(overlap_index, self.query())
        self.assertEqual(overlap_plan["coverageCases"], ["composite", "overlapping"])

    def test_partial_and_missing_ranges_become_only_new_work(self):
        partial = self.build_response(end_ms=300_000)
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, partial)
        plan = saved.find_material(index, self.query())
        self.assertEqual(plan["coverageStatus"], "partial")
        self.assertEqual(
            plan["missingTimeRanges"], [{"startMs": 300_000, "endMs": 600_000}]
        )

    def test_focused_nonzero_transcript_is_saved_indexed_and_reused_immediately(self):
        item = self.build_response(start_ms=600_000, end_ms=1_200_000)
        self.assertEqual(
            item["saved"]["sourceTimeRange"],
            {"startMs": 600_000, "endMs": 1_200_000},
        )
        self.assertEqual(
            item["saved"]["coveredTimeRanges"],
            [{"startMs": 600_000, "endMs": 1_200_000}],
        )

        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, item)
        query = self.query(start=600_000, end=1_200_000)
        plan = saved.find_material(index, query)
        self.assertEqual(plan["coverageStatus"], "complete")
        self.assertEqual(plan["coverageCases"], ["exact"])
        verified = saved.verify_selected(index, self.responses, query, plan)
        self.assertEqual(verified["verificationStatus"], "verified")
        self.assertEqual(saved.plan_missing_ranges(verified)["missingTimeRanges"], [])

    def test_source_onscreen_text_is_saved_and_searched_without_language_fragmentation(self):
        visible_text = "মূল দৃশ্যপট"
        onscreen = self.build_response(
            output_type="systematic_onscreen_text",
            generated=visible_text,
        )
        summary = self.build_response(output_type="summary")
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, onscreen)
        self.add(index, summary)
        query = self.query(output_type="systematic_onscreen_text")
        plan = saved.find_material(index, query)
        self.assertEqual(plan["coverageStatus"], "complete")
        self.assertEqual(plan["coverageCases"], ["exact"])
        self.assertEqual(
            plan["selectedMaterials"][0]["savedResponseId"],
            onscreen["saved"]["savedResponseId"],
        )
        self.assertIn(visible_text, onscreen["saved"]["responseJsonText"])
        for value in (onscreen["request"], onscreen["saved"], index, plan):
            serialized = json.dumps(value, ensure_ascii=False)
            for field in REMOVED_LANGUAGE_FIELDS:
                self.assertNotIn(field, serialized)

    def test_material_search_boundaries_reject_removed_metadata_fields(self):
        item = self.build_response()
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, item)

        for field in (*REMOVED_TIME_COORDINATE_FIELDS, *REMOVED_LANGUAGE_FIELDS):
            removed_value = (
                "full_video"
                if field in REMOVED_TIME_COORDINATE_FIELDS
                else "removed"
            )
            with self.subTest(boundary="index", field=field):
                changed_index = copy.deepcopy(index)
                changed_index["materials"][0][field] = removed_value
                with self.assertRaisesRegex(common.YouTubeWorkError, "unsupported fields"):
                    saved.validate_material_index(changed_index)

            with self.subTest(boundary="query", field=field):
                changed_query = self.query()
                changed_query[field] = removed_value
                with self.assertRaisesRegex(common.YouTubeWorkError, "unsupported fields"):
                    saved.validate_query(changed_query)

            with self.subTest(boundary="search-plan", field=field):
                changed_plan = saved.find_material(index, self.query())
                changed_plan[field] = removed_value
                with self.assertRaisesRegex(common.YouTubeWorkError, "unsupported fields"):
                    saved.plan_missing_ranges(changed_plan)

            with self.subTest(boundary="missing-range-plan", field=field):
                missing_plan = {
                    "fileFormatVersion": 1,
                    "videoId": VIDEO_ID,
                    "outputType": "summary",
                    "outputFormat": common.OUTPUT_FORMATS["summary"],
                    "missingTimeRanges": [{"startMs": 0, "endMs": 600_000}],
                    field: removed_value,
                }
                with self.assertRaisesRegex(common.YouTubeWorkError, "unsupported fields"):
                    saved.plan_chunks(missing_plan)

    def test_verification_reads_only_selected_files_and_replans_around_stale_entry(self):
        first = self.build_response(start_ms=0, end_ms=300_000)
        second = self.build_response(start_ms=300_000, end_ms=600_000)
        unrelated = self.build_response(output_type="summary")
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, first)
        self.add(index, second)
        self.add(index, unrelated)
        plan = saved.find_material(index, self.query())
        unrelated["path"].write_text("not json", encoding="utf-8")
        verified = saved.verify_selected(index, self.responses, self.query(), plan)
        self.assertEqual(verified["verificationStatus"], "verified")
        self.assertEqual(verified["missingTimeRanges"], [])
        first["path"].unlink()
        replanned = saved.verify_selected(index, self.responses, self.query(), plan)
        self.assertEqual(
            replanned["missingTimeRanges"], [{"startMs": 0, "endMs": 300_000}]
        )
        self.assertEqual(len(replanned["staleIndexEntries"]), 1)

    def test_missing_range_plan_requires_selected_response_verification(self):
        item = self.build_response(end_ms=300_000)
        index = saved.new_material_index(VIDEO_ID, updated_at=T0)
        self.add(index, item)
        plan = saved.find_material(index, self.query())
        with self.assertRaisesRegex(saved.SavedResponseError, "verified"):
            saved.plan_missing_ranges(plan)
        verified = saved.verify_selected(index, self.responses, self.query(), plan)
        result = saved.plan_missing_ranges(verified)
        self.assertEqual(
            result["missingTimeRanges"], [{"startMs": 300_000, "endMs": 600_000}]
        )

    def test_default_long_video_chunks_reproduce_validated_movie_plan(self):
        plan = {
            "fileFormatVersion": 1,
            "videoId": VIDEO_ID,
            "outputType": "summary",
            "outputFormat": common.OUTPUT_FORMATS["summary"],
            "missingTimeRanges": [{"startMs": 0, "endMs": 8_100_000}],
        }
        result = saved.plan_chunks(plan)
        self.assertEqual(result["chunkSeconds"], 1_800)
        self.assertEqual(result["overlapSeconds"], 0)
        self.assertEqual(
            result["chunks"],
            [
                {"startMs": 0, "endMs": 1_800_000},
                {"startMs": 1_800_000, "endMs": 3_600_000},
                {"startMs": 3_600_000, "endMs": 5_400_000},
                {"startMs": 5_400_000, "endMs": 7_200_000},
                {"startMs": 7_200_000, "endMs": 8_100_000},
            ],
        )

    def test_chunk_plan_splits_each_missing_range_without_filling_gaps(self):
        plan = {
            "fileFormatVersion": 1,
            "videoId": VIDEO_ID,
            "outputType": "transcript",
            "outputFormat": common.OUTPUT_FORMATS["transcript"],
            "missingTimeRanges": [
                {"startMs": 0, "endMs": 900_000},
                {"startMs": 1_200_000, "endMs": 1_500_000},
            ],
        }
        result = saved.plan_chunks(plan, chunk_seconds=600)
        self.assertEqual(
            result["chunks"],
            [
                {"startMs": 0, "endMs": 600_000},
                {"startMs": 600_000, "endMs": 900_000},
                {"startMs": 1_200_000, "endMs": 1_500_000},
            ],
        )

    def test_chunk_plan_applies_requested_boundary_overlap(self):
        plan = {
            "fileFormatVersion": 1,
            "videoId": VIDEO_ID,
            "outputType": "transcript",
            "outputFormat": common.OUTPUT_FORMATS["transcript"],
            "missingTimeRanges": [{"startMs": 0, "endMs": 1_200_000}],
        }
        result = saved.plan_chunks(plan, chunk_seconds=600, overlap_seconds=4)
        self.assertEqual(
            result["chunks"],
            [
                {"startMs": 0, "endMs": 600_000},
                {"startMs": 596_000, "endMs": 1_196_000},
                {"startMs": 1_192_000, "endMs": 1_200_000},
            ],
        )

    def test_chunk_plan_rejects_invalid_parameters_and_noncanonical_input(self):
        plan = {
            "fileFormatVersion": 1,
            "videoId": VIDEO_ID,
            "outputType": "summary",
            "outputFormat": common.OUTPUT_FORMATS["summary"],
            "missingTimeRanges": [{"startMs": 0, "endMs": 600_000}],
        }
        for size, overlap in ((0, 0), (600, -1), (600, 600)):
            with self.subTest(size=size, overlap=overlap):
                with self.assertRaises(saved.SavedResponseError):
                    saved.plan_chunks(plan, size, overlap)
        noncanonical = copy.deepcopy(plan)
        noncanonical["missingTimeRanges"] = [
            {"startMs": 300_000, "endMs": 600_000},
            {"startMs": 0, "endMs": 300_000},
        ]
        with self.assertRaisesRegex(saved.SavedResponseError, "normalized"):
            saved.plan_chunks(noncanonical)

    def test_randomized_interval_planning_matches_interval_union(self):
        rng = random.Random(20260801)
        for case_number in range(5_500):
            requested_start = rng.randrange(0, 1_000)
            requested_end = rng.randrange(requested_start + 1, 1_200)
            requested = [{"startMs": requested_start, "endMs": requested_end}]
            coverage = []
            materials = []
            for material_number in range(rng.randrange(0, 7)):
                start = rng.randrange(0, 1_200)
                end = rng.randrange(start + 1, 1_300)
                interval = {"startMs": start, "endMs": end}
                coverage.append(interval)
                saved_response_id = common.sha256_hex(
                    f"{case_number}:{material_number}".encode("utf-8")
                )
                materials.append(
                    {
                        "savedResponseId": saved_response_id,
                        "fileName": saved.saved_response_filename(
                            VIDEO_ID, "transcript", saved_response_id
                        ),
                        "fileSha256": "f" * 64,
                        "outputType": "transcript",
                        "outputFormat": common.OUTPUT_FORMATS["transcript"],
                        "coveredTimeRanges": [interval],
                    }
                )
            index = {
                "fileFormatVersion": 1,
                "videoId": VIDEO_ID,
                "materials": sorted(
                    materials, key=lambda item: item["savedResponseId"]
                ),
                "updatedAt": T0,
            }
            plan = saved.find_material(index, self.query(requestedTimeRanges=requested))
            normalized = common.normalize_intervals(coverage)
            expected_missing = common.subtract_intervals(requested, normalized)
            expected_covered = common.intersect_intervals(normalized, requested)
            self.assertEqual(plan["coveredTimeRanges"], expected_covered)
            self.assertEqual(plan["missingTimeRanges"], expected_missing)
            self.assertEqual(
                common.normalize_intervals(
                    plan["coveredTimeRanges"] + plan["missingTimeRanges"]
                ),
                requested,
            )

    def test_no_cache_v2_or_old_namespace_is_used(self):
        source = (ROOT / "scripts" / "saved_gemini_responses.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("gemini_cache", source)
        self.assertNotIn("YouTubeResearchCache", source)
        self.assertNotIn("YouTubeArtifactCacheV3", source)
        self.assertEqual(common.LOCAL_STATE_DIRECTORY, "youtube-video-work")


class CommandSurfaceTests(unittest.TestCase):
    def subcommands(self, parser):
        action = next(
            item
            for item in parser._actions
            if isinstance(item, argparse._SubParsersAction)
        )
        return set(action.choices)

    def test_request_log_exposes_only_run_oriented_commands(self):
        self.assertEqual(
            self.subcommands(request_log.build_parser()),
            {
                "locate",
                "init-log",
                "start-run",
                "verify-run",
                "finish-run",
                "mark-run-interrupted",
            },
        )

    def test_saved_response_tool_exposes_concrete_file_operations(self):
        self.assertEqual(
            self.subcommands(saved.build_parser()),
            {
                "locate",
                "save-response",
                "init-material-index",
                "add-to-material-index",
                "rebuild-material-index",
                "find-material",
                "verify-selected",
                "plan-missing-ranges",
                "plan-chunks",
            },
        )


if __name__ == "__main__":
    unittest.main()
