# Repository map

## Authority by concern

| Concern | Owning source |
|---|---|
| Purpose, architecture, installation, and development | [`README.md`](README.md) |
| Runtime source order and Codex procedure | [`SKILL.md`](SKILL.md) |
| Exact paths, pins, formats, and operating values | [`references/contracts.md`](references/contracts.md) |
| Executable behavior | `scripts/` |
| Behavioral verification | `tests/` |
| Contribution and commit conventions | [`CONTRIBUTING.md`](CONTRIBUTING.md) |

## Runtime skill files

| Path | Role |
|---|---|
| [`SKILL.md`](SKILL.md) | Codex skill entry point and runtime workflow. |
| [`agents/openai.yaml`](agents/openai.yaml) | Codex-facing metadata and invocation policy. |
| [`references/contracts.md`](references/contracts.md) | Exact local-state, MCP, Gemini, saved-response, index, and request-log contracts. |
| [`scripts/ensure_youtube_mcp.sh`](scripts/ensure_youtube_mcp.sh) | Restores and verifies the pinned portable YouTube MCP runtime. |
| [`scripts/call_youtube_mcp.mjs`](scripts/call_youtube_mcp.mjs) | Lists and calls tools on the local MCP server. |
| [`scripts/build_gemini_chunk_request.py`](scripts/build_gemini_chunk_request.py) | Builds timestamp-bounded Gemini video requests. |
| [`scripts/gemini_request.py`](scripts/gemini_request.py) | Executes verified Gemini requests using environment credentials. |
| [`scripts/gemini_request_log.py`](scripts/gemini_request_log.py) | Maintains local per-video request history. |
| [`scripts/saved_gemini_responses.py`](scripts/saved_gemini_responses.py) | Saves, validates, indexes, and searches local Gemini material. |
| [`scripts/youtube_work_common.py`](scripts/youtube_work_common.py) | Shared validation, hashing, interval, and filename helpers. |

## Development-only files

| Path | Role |
|---|---|
| [`tests/test_portable_layout.py`](tests/test_portable_layout.py) | Portable MCP layout and bootstrap tests. |
| [`tests/test_transcript_request.py`](tests/test_transcript_request.py) | Gemini request-builder tests. |
| [`tests/test_gemini_routing.py`](tests/test_gemini_routing.py) | Environment credentials, routing, cooldown, and secret-safety tests. |
| [`tests/test_gemini_request_log.py`](tests/test_gemini_request_log.py) | Local request-log state-machine tests. |
| [`tests/test_saved_gemini_responses.py`](tests/test_saved_gemini_responses.py) | Local response, material-index, verification, and chunk-planning tests. |

The installed skill should exclude repository metadata, tests, and contribution
documentation.
