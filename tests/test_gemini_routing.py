import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gemini_request
import gemini_request_log
import youtube_work_common as common


VIDEO_ID = "lhSq1RzDcZg"
ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-3.6-flash:generateContent"
)
MODEL = "gemini-3.6-flash"
T0 = "2026-08-01T10:00:00Z"


def response(status, error_status=None, message="", headers=None, body=None):
    if body is None:
        if 200 <= status < 300:
            body = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        else:
            body = {
                "error": {
                    "code": status,
                    "status": error_status or "ERROR",
                    "message": message,
                }
            }
    return status, headers or {}, json.dumps(body).encode("utf-8")


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, endpoint, api_key, request_bytes, timeout_seconds):
        self.calls.append((endpoint, api_key, request_bytes, timeout_seconds))
        if not self.responses:
            raise AssertionError("Unexpected Gemini network call")
        return self.responses.pop(0)


class GeminiRouterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.request_path = self.directory / "request.json"
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
                            "videoMetadata": {"startOffset": "0s", "endOffset": "600s"},
                        },
                        {"text": "Describe only the requested interval."},
                    ],
                }
            ],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        common.write_json(self.request_path, request)
        self.log = gemini_request_log.new_request_log(VIDEO_ID, updated_at=T0)
        self.request_entry, self.run_entry = gemini_request_log.start_run(
            self.log,
            self.request_path,
            VIDEO_ID,
            ENDPOINT,
            MODEL,
            "POST",
            "direct_answer",
            started_at=T0,
        )
        self.log_path = self.directory / "request-log.json"
        common.write_json(self.log_path, self.log)
        self.args = SimpleNamespace(
            request=str(self.request_path),
            response=str(self.directory / "response.json"),
            router_result=str(self.directory / "router-result.json"),
            request_log=str(self.log_path),
            request_id=self.request_entry["requestId"],
            run_number=1,
            state=str(self.directory / "state.json"),
            endpoint=ENDPOINT,
            model=MODEL,
            method="POST",
            timeout_seconds=10.0,
            max_transient_retries=2,
            base_backoff_seconds=1.0,
            max_backoff_seconds=30.0,
            default_cooldown_seconds=60.0,
            default_transient_cooldown_seconds=30.0,
            jitter_seconds=0.0,
            no_sleep=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_router(self, responses, primary="primary-secret", fallback="fallback-secret"):
        transport = FakeTransport(responses)
        with mock.patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": primary,
                "GEMINI_API_KEY_FALLBACK": fallback,
            },
            clear=False,
        ), mock.patch.object(
            gemini_request, "send_request", transport
        ), mock.patch.object(gemini_request.random, "uniform", return_value=0.0):
            exit_code = gemini_request.run(self.args)
        result = common.load_json(Path(self.args.router_result))
        return exit_code, result, transport

    def test_primary_success_is_bound_to_request_run_and_exact_response_bytes(self):
        exit_code, result, transport = self.run_router([response(200)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["requestId"], self.request_entry["requestId"])
        self.assertEqual(result["runNumber"], 1)
        self.assertEqual(result["exactRequestSha256"], self.run_entry["exactRequestSha256"])
        self.assertEqual(result["attempts"][0]["attemptNumber"], 1)
        response_hash = common.sha256_hex(Path(self.args.response).read_bytes())
        self.assertEqual(result["responseSha256"], response_hash)
        gemini_request_log.validate_router_result(result)

    def test_rate_limit_cools_primary_and_uses_fallback(self):
        exit_code, result, transport = self.run_router(
            [
                response(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": "120"}),
                response(200),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual([call[1] for call in transport.calls], ["primary-secret", "fallback-secret"])
        self.assertEqual([item["attemptNumber"] for item in result["attempts"]], [1, 2])
        self.assertEqual(result["attempts"][0]["classification"], "rate_limited")
        self.assertIn("cooldownUntil", result["attempts"][0])
        self.assertEqual(result["selectedBucket"], "fallback")
        gemini_request_log.validate_router_result(result)

    def test_both_buckets_rate_limited_return_complete_safe_failure(self):
        exit_code, result, _ = self.run_router(
            [
                response(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": "60"}),
                response(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": "90"}),
            ]
        )
        self.assertEqual(exit_code, 3)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["selectedBucket"])
        self.assertEqual([item["attemptNumber"] for item in result["attempts"]], [1, 2])
        self.assertIn("earliestCooldownUntil", result)
        self.assertNotIn("responseSha256", result)
        gemini_request_log.validate_router_result(result)

    def test_failed_result_uses_the_final_attempt_time(self):
        start = datetime(2026, 8, 1, 10, 0, 1, tzinfo=timezone.utc)
        times = [start + timedelta(seconds=index) for index in range(9)]
        with mock.patch.object(gemini_request, "utc_now", side_effect=times):
            exit_code, result, _ = self.run_router(
                [
                    response(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": "60"}),
                    response(429, "RESOURCE_EXHAUSTED", headers={"Retry-After": "90"}),
                ]
            )
        self.assertEqual(exit_code, 3)
        self.assertEqual(result["completedAt"], result["attempts"][-1]["finishedAt"])
        gemini_request_log.validate_router_result(result)

    def test_transient_retry_remains_inside_one_numbered_run(self):
        exit_code, result, transport = self.run_router(
            [response(503, "UNAVAILABLE"), response(200)]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual([item["attemptNumber"] for item in result["attempts"]], [1, 2])
        self.assertEqual(result["attempts"][0]["classification"], "transient")
        self.assertEqual(result["attempts"][0]["backoffSeconds"], 1.0)

    def test_terminal_request_failure_does_not_touch_fallback(self):
        exit_code, result, transport = self.run_router(
            [response(400, "INVALID_ARGUMENT")]
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(result["attempts"][-1]["classification"], "request_failure")
        gemini_request_log.validate_router_result(result)

    def test_invalid_primary_credential_uses_fallback(self):
        exit_code, result, transport = self.run_router(
            [response(401, "UNAUTHENTICATED"), response(200)]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(result["attempts"][0]["classification"], "credential_failure")
        self.assertEqual(result["selectedBucket"], "fallback")

    def test_existing_primary_cooldown_starts_with_fallback(self):
        state = {
            "fileFormatVersion": 1,
            "buckets": {
                "primary": {
                    "cooldownUntil": "2999-01-01T00:00:00Z",
                    "disabled": False,
                    "reason": "rate_limited",
                },
                "fallback": gemini_request.default_bucket_state(),
            },
        }
        common.write_json(Path(self.args.state), state)
        exit_code, result, transport = self.run_router([response(200)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(transport.calls[0][1], "fallback-secret")
        self.assertEqual(result["attempts"][0]["bucket"], "fallback")

    def test_identical_environment_credentials_stop_before_network(self):
        transport = FakeTransport([])
        with mock.patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "same-secret",
                "GEMINI_API_KEY_FALLBACK": "same-secret",
            },
            clear=False,
        ), mock.patch.object(gemini_request, "send_request", transport):
            with self.assertRaises(SystemExit):
                gemini_request.run(self.args)
        self.assertEqual(transport.calls, [])

    def test_request_binding_is_verified_before_credentials_or_network(self):
        changed = json.loads(self.request_path.read_text(encoding="utf-8"))
        changed["contents"][0]["parts"][1]["text"] = "Changed after logging"
        common.write_json(self.request_path, changed)
        transport = FakeTransport([])
        with mock.patch.object(
            gemini_request, "load_buckets"
        ) as load_credentials, mock.patch.object(gemini_request, "send_request", transport):
            with self.assertRaises(gemini_request_log.RequestLogError):
                gemini_request.run(self.args)
        load_credentials.assert_not_called()
        self.assertEqual(transport.calls, [])
        self.assertFalse(Path(self.args.router_result).exists())

    def test_endpoint_model_mismatch_stops_before_network(self):
        self.args.model = "gemini-other"
        transport = FakeTransport([])
        with mock.patch.object(
            gemini_request, "load_buckets"
        ) as load_credentials, mock.patch.object(gemini_request, "send_request", transport):
            with self.assertRaises(gemini_request_log.RequestLogError):
                gemini_request.run(self.args)
        load_credentials.assert_not_called()
        self.assertEqual(transport.calls, [])

    def test_router_uses_environment_credentials(self):
        with mock.patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "environment-primary",
                "GEMINI_API_KEY_FALLBACK": "environment-fallback",
            },
            clear=True,
        ):
            _, _, transport = self.run_router(
                [response(200)],
                primary="environment-primary",
                fallback="environment-fallback",
            )
        self.assertEqual(transport.calls[0][1], "environment-primary")

    def test_fallback_environment_credential_is_optional(self):
        transport = FakeTransport([response(200)])
        with mock.patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "primary-secret"},
            clear=True,
        ), mock.patch.object(
            gemini_request, "send_request", transport
        ), mock.patch.object(gemini_request.random, "uniform", return_value=0.0):
            self.assertEqual(gemini_request.run(self.args), 0)
        self.assertEqual([call[1] for call in transport.calls], ["primary-secret"])

    def test_saved_state_and_router_result_contain_no_secret_material(self):
        _, result, _ = self.run_router([response(200)])
        serialized = json.dumps(result) + Path(self.args.state).read_text(encoding="utf-8")
        self.assertNotIn("primary-secret", serialized)
        self.assertNotIn("fallback-secret", serialized)
        self.assertNotIn("x-goog-api-key", serialized)
        self.assertEqual(common.load_json(Path(self.args.state))["fileFormatVersion"], 1)

    def test_pool_state_rejects_undeclared_fields_before_network(self):
        state = {
            "fileFormatVersion": 1,
            "buckets": {
                "primary": {
                    **gemini_request.default_bucket_state(),
                    "unexpected": "value",
                },
                "fallback": gemini_request.default_bucket_state(),
            },
        }
        common.write_json(Path(self.args.state), state)
        transport = FakeTransport([])
        with mock.patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "primary-secret",
                "GEMINI_API_KEY_FALLBACK": "fallback-secret",
            },
            clear=False,
        ), mock.patch.object(gemini_request, "send_request", transport):
            with self.assertRaises(SystemExit):
                gemini_request.run(self.args)
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
