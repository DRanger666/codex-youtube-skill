---
name: work-with-youtube
description: Reproducible YouTube research in Codex using MCP-first caption and metadata retrieval, locally saved Gemini video material, structured transcript generation, visual inspection, and long-video chunking. Use for requests to inspect, summarize, query, compare, cite, translate, transcribe, or verify one or more YouTube URLs or videos.
---

# Work with YouTube

## Status

Provisionally installed for Codex. The structure and local-only behavior are
unit tested; a live YouTube/Gemini workflow still needs behavioral validation.

For each video, use every relevant YouTube MCP result first, previously saved
Gemini material second, and a new Gemini request only for the unresolved need.
Read [references/contracts.md](references/contracts.md) completely before using
Gemini.

## Keep the workflow safe

- Keep the portable installation under
  `${YOUTUBE_SKILL_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-youtube}`,
  with disposable files under `work/` and persistent local data under `state/`.
- Never display, quote, log, or commit API keys. Keep credential files out of
  prompts, saved responses, request logs, material indexes, `work/`, and
  `state/`.
- Store saved Gemini work only in `$install/state/youtube-video-work/`. Keep
  disposable request and response files in `$install/work/`.
- Never edit a saved Gemini response. Use the material index only to find
  reusable responses; use the request log only to record Gemini requests and
  their known outcomes.
- Persist a pending numbered run before every Gemini call. Never duplicate a
  pending run, silently repeat a finished run, ignore a recorded cooldown, or
  retry an unchanged terminal request failure.
- Preserve every routing attempt returned by the router. If the router does not
  return a terminal result, keep the run pending and invent no outcome.
- Allow only one write-capable Codex session per video. Read-only work can
  proceed elsewhere, and different videos can be processed concurrently.
- Run Gemini chunks sequentially.

## Use the registered YouTube MCP

The `youtube` MCP is a persistent user-level Codex registration. Use its native
tools directly; do not run the local MCP client wrapper during normal work.
Codex starts the stdio server when needed, so no daemon needs to remain running.

Machine setup is a one-time operation. If the `youtube` MCP tools are absent,
obtain permission for the local install and user-level Codex configuration
change, then run:

```sh
sh "$skill_dir/scripts/setup_youtube_mcp.sh"
```

Start a new Codex session after setup. Do not reinstall or reregister the MCP
for each video request. The setup script installs and verifies the pinned
server before registering it in Codex. Its lower-level client wrapper exists
only for that handshake check.

For local Gemini state, set:

```sh
install=${YOUTUBE_SKILL_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-youtube}/youtube-mcp-portable
```

Paginate broad MCP transcript reads with `offset` and `maxSegments`; prefer
focused queries.

## Resolve the task in source order

1. Normalize each URL to its YouTube video ID.
2. Ask the MCP for every relevant caption, transcript-research, metadata, or
   other applicable result. If this satisfies the task, answer from it and
   stop. Keep MCP results temporary; do not copy them into the Gemini material
   index.
3. For anything still missing, search the video's saved Gemini material.
4. Reuse sufficient verified material. Plan a Gemini request only for returned
   missing time ranges or sensory information absent from both earlier sources.
5. Process videos independently before comparing or synthesizing them.

## Search saved Gemini material

Use `scripts/saved_gemini_responses.py` and the local filenames and JSON fields
defined in the contract.

1. Set `material_dir="$install/state/youtube-video-work"`, create it if needed,
   and run `locate --video VIDEO` to obtain the exact local filenames.
2. If the index is absent, enumerate that video's saved-response filenames.
   Rebuild the index when responses exist. Initialize an empty index only after
   confirming that none exist.
3. Build a query for the video, controlled output type and format, and requested
   half-open millisecond ranges. Run `find-material` against the local index.
4. Run `verify-selected` against `material_dir`. Replan around stale or invalid
   local files before generating anything new.
5. Run `plan-missing-ranges` only after verification. Do not construct a
   Gemini request for covered time.

Reusable material has exactly four output types:

- `transcript`: `gemini-transcript` version `1`, checked mechanically.
- `summary`, `systematic_visual_description`, and
  `systematic_onscreen_text`: `gemini-free-form-text` version `1`, admitted
  to the index only after Codex review and conservative assignment of covered
  time within the requested source range.

When rebuilding an index, supply explicit admission decisions for every
free-form response as defined in the contract. Keep structurally invalid
transcript responses saved but unindexed.

Do not save or search translations. Translate on demand in Codex from MCP
material or saved source-language transcripts and onscreen text. Preserve
multilingual evidence in the source content itself.

## Use Gemini for the remaining video access

Use Gemini as a video sensor and keep reasoning in Codex. Prefer prompts that
ask Gemini to report what is visible or audible: a board, slide, onscreen text,
scene, action, or other specific evidence.

Classify the requested result before building it:

- `video_material`: systematic source material worth reusing. Save it under
  one controlled output type.
- `task_specific_observation`: narrow sensory evidence for the current task.
  Return it to Codex and do not save its response text.
- `direct_answer`: Gemini's task-specific answer, used only when actually
  wanted. Return it without saving its response text.

Do not turn a narrow question into reusable material merely to retain it.
Successful one-time requests still receive a verified request-log outcome.

## Plan only missing video ranges

Split a verified missing-range plan with `plan-chunks`. Use the transcript and
general-analysis chunk sizes and overlaps defined in the contract:

```sh
python3 "$skill_dir/scripts/saved_gemini_responses.py" plan-chunks \
  --missing-ranges-plan MISSING_RANGES_JSON \
  --chunk-seconds CHUNK_SECONDS \
  --overlap-seconds OVERLAP_SECONDS \
  --output CHUNK_PLAN_JSON
```

Build each returned chunk with one of these routes:

```sh
python3 "$skill_dir/scripts/build_gemini_chunk_request.py" \
  --video-url VIDEO_URL --start-seconds START --end-seconds END \
  --transcript-only --output REQUEST_JSON
```

```sh
python3 "$skill_dir/scripts/build_gemini_chunk_request.py" \
  --video-url VIDEO_URL --start-seconds START --end-seconds END \
  --prompt PROMPT --max-output-tokens 8192 --output REQUEST_JSON
```

Use the `8192` allowance for reusable free-form video material. After the call,
inspect Gemini's `finishReason`; save a `MAX_TOKENS` response as run evidence
but do not admit it to the material index. Build a changed request with a
smaller time range rather than repeating it unchanged.

Transcript mode requests only audible linguistic content in the original
language and native script. Its timestamps, every millisecond range, and every
chunk boundary are offsets from the beginning of the video.

Start long-video work with one representative clip and continue sequentially
only after it succeeds. If video ingestion or output fails, reduce the chunk
size instead of assuming the documented default is an API limit.

If transcript output is incomplete or truncated, save it and index only the
mechanically checked covered range. Plan smaller clips for the remaining range.
Never repeat the identical request as truncation handling.

## Load Gemini credentials privately

The router reads required `GEMINI_API_KEY` and optional
`GEMINI_API_KEY_FALLBACK` from the local process environment after request
verification. The user should provision them through their shell or local
secret manager before starting Codex. Never ask
the user to paste a key into chat, print either value, put a value in a command
argument, or save credentials under the repository or portable runtime.

When both keys are present, try the primary project first. Use the fallback
sequentially only after the primary is rate-limited, rejected as an invalid
credential, or placed in cooldown after exhausted transient retries. Do not
fail over for a terminal request error such as an invalid request.

## Run one logged Gemini request

Use `scripts/gemini_request_log.py` for the per-video request log in
`$install/state/youtube-video-work/`.

1. Run `locate --video VIDEO` and look up the exact request-log filename.
   Initialize a log only after confirming that no exact-name file exists.
2. Recheck saved material immediately before the call. Stop if nothing remains
   missing.
3. Build the exact request, then run `start-run`. Use `--transcript` for
   transcript mode; otherwise declare the content class and, for reusable
   material, its controlled output type and format. Add `--retry-reason` only
   when the user deliberately authorizes another run.
4. Persist the pending request log locally before invoking the router.
5. Run the router with the same request log, request ID, run number, endpoint,
   model, method, and exact request file:

```sh
python3 "$skill_dir/scripts/gemini_request.py" \
  --request REQUEST_JSON \
  --response RESPONSE_JSON \
  --router-result ROUTER_RESULT_JSON \
  --request-log REQUEST_LOG_JSON \
  --request-id REQUEST_ID \
  --run-number RUN_NUMBER \
  --state "$install/state/gemini-keypool-state.json"
```

6. For a returned terminal failure, run `finish-run` with the router result and
   save no response.
7. For successful `video_material`, run `save-response` into `material_dir`,
   then run `finish-run` with that saved response. Add checked or reviewed
   material to the local index afterward. Never index malformed transcript
   output, unreviewed free-form output, or free-form output whose
   `finishReason` is not `STOP`.
8. For successful one-time content, run `finish-run` with the router result
   and exact local response file. It records that response storage was
   deliberately omitted. Do not retain the response text.

The log keeps one logical request with numbered authorized runs. Each run keeps
its own exact request hash, authorization, returned routing attempts, cooldown,
status, and saved-response reference when applicable. Earlier runs are never
overwritten.

## Resolve an interrupted pending run

Never infer an outcome from elapsed time.

1. Ask the user to confirm that the earlier write-capable session stopped.
2. For reusable material, enumerate every local saved response that
   could claim the pending run, then pass every candidate path to `finish-run`
   with both interruption confirmations. The command completes the run only
   when exactly one file verifies the request ID, run number, exact request
   hash, response bytes, and router result.
3. If no exact response exists, or the interrupted run requested one-time
   content, use `mark-run-interrupted` only after confirmation. Any later run
   still requires explicit retry authorization.
4. If multiple responses claim the run or any binding conflicts, stop and
   investigate.

## Deliver the answer

- Cite caption-derived claims with timestamped YouTube links.
- Label Gemini-derived observations and include timestamps when available.
- Distinguish direct observation, inference, and uncertainty.
- State when only part of a long video was processed.
- Mention saved-material reuse when it prevented a Gemini call.
