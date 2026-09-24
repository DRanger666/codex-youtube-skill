# YouTube workflow contracts

[`SKILL.md`](../SKILL.md) owns runtime source order and procedure. This file
supplies the exact current paths, pins, filenames, commands, fields, and
operating values that procedure depends on; it does not define an alternative
workflow.

## Contents

- [Local installation](#local-installation)
- [Local state and credentials](#local-state-and-credentials)
- [Gemini requests](#gemini-requests)
- [Long-video clipping](#long-video-clipping)
- [Reusable output formats](#reusable-output-formats)
- [Saved Gemini responses](#saved-gemini-responses)
- [Video material index](#video-material-index)
- [Gemini request log](#gemini-request-log)
- [Interruption decisions](#interruption-decisions)
- [Validated behavior](#validated-behavior)

## Local installation

Use this exact no-space layout under
`${YOUTUBE_SKILL_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-youtube}`:

```text
youtube-mcp-portable/
  app/
  runtime/
  state/
  work/
  README.md
  VERSION
```

Use `work/` for disposable argument files, requests, responses, downloaded
working copies, and intermediate JSON. Reserve
`state/gemini-keypool-state.json` for router state created when Gemini is first
used. The displayed tree is the complete maintained root layout. Invoke the MCP
through `scripts/call_youtube_mcp.mjs` and the pinned `runtime/bin/node`
executable.

Pinned implementation:

- Repository: `https://github.com/coyaSONG/youtube-mcp-server.git`
- Version: `1.2.0`
- Commit: `06d5e7a83783f7a44498da88ade2ccaa42238747`
- Node: `v24.14.0`
- Platform: `linux-x86_64`

## Local state and credentials

Keep persistent video material in
`youtube-mcp-portable/state/youtube-video-work/` below that local root. Its
three filename forms are:

```text
<videoId>--video-material-index.json
<videoId>--gemini-response--<outputType>--<savedResponseId>.json
<videoId>--gemini-requests.json
```

The local process environment supplies required `GEMINI_API_KEY` and optional
`GEMINI_API_KEY_FALLBACK`. When both are set, they must be non-empty and
different. Provision them through the user's shell or local secret manager
before starting Codex. Never place either value in chat, command arguments,
repository files, runtime state, saved responses, or logs.

## Gemini requests

Use the Generate Content endpoint:

`POST https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent`

Build timestamp-clipped requests with
`scripts/build_gemini_chunk_request.py`. The video part contains the canonical
YouTube URL and `videoMetadata.startOffset`/`endOffset` strings in seconds. The
prompt follows the video part.

When exact wording is needed, pass `--transcript-only`. The builder fixes the
tested prompt, JSON response schema, and 8192-token allowance. Transcript
timestamps use `MM:SS.mmm` video-start offsets, where minutes have at least two
digits and can exceed 99.

For reusable `gemini-free-form-text` video material, pass
`--max-output-tokens 8192` and require Gemini `finishReason` `STOP` before
indexing. In the representative live test, the ordinary 2048-token allowance
spent 1964 tokens on model reasoning and returned only 80 answer tokens with
`MAX_TOKENS`; the otherwise identical 8192-token request finished normally.
A truncated response remains saved and linked to its successful run but stays
out of the material index. Continue with a changed smaller-range request, not
an identical repetition.

Send only a request that has already been recorded as the highest pending run:

```sh
python3 scripts/gemini_request.py \
  --request REQUEST.json \
  --response RESPONSE.json \
  --router-result ROUTER_RESULT.json \
  --request-log VIDEO_REQUEST_LOG.json \
  --request-id REQUEST_ID \
  --run-number RUN_NUMBER \
  --state GEMINI_POOL_STATE.json
```

The router verifies the request file, video URI, clip, prompt, endpoint, model,
method, request ID, run number, normalized request hash, exact request-file
hash, and highest-pending state before loading a credential.

The pool contains at most two aliases: `primary` and `fallback`. Keep one
request in flight. Retry transient `408` and selected `5xx` responses with
bounded delay. Cool down a project after `429`; disable an invalid credential
for the run; do not rotate on a terminal request error. If every bucket is
unavailable, return the failure and earliest cooldown instead of looping.

Every retained router attempt has a sequential `attemptNumber`, bucket alias,
start and finish times, HTTP status, classification, and applicable safe
cooldown or backoff data. A successful router result also contains
`responseSha256`, calculated from the exact response-file bytes.

## Long-video clipping

Gemini accepts YouTube `videoMetadata.startOffset` and `endOffset` values, so
process a long or rejected whole-video request as bounded clips. The operating
defaults come from observed behavior in this project:

- a public captionless 2-hour-15-minute Hindi movie failed as one whole-video
  request;
- its `0s`–`1800s` clip succeeded; and
- the resulting five-part 30-minute plan covers that movie through `8100s`.

Google's current
[video-understanding documentation](https://ai.google.dev/gemini-api/docs/generate-content/video-understanding)
says a model with a 1-million-token context window can process up to one hour
of video at default media resolution or three hours at low media resolution.
That is context capacity, not a guarantee that every YouTube ingestion and
requested output will succeed. Therefore, 1,800 seconds remains this skill's
tested conservative default for general video analysis; it is not represented
as a Gemini hard limit.

After saved-material verification, turn the missing-range plan into clips:

```sh
python3 scripts/saved_gemini_responses.py plan-chunks \
  --missing-ranges-plan MISSING_RANGES.json \
  --chunk-seconds 1800 \
  --overlap-seconds 0 \
  --output CHUNKS.json
```

`plan-chunks` accepts only a normalized missing-range plan produced after
selected saved responses have been verified. Its output retains
`fileFormatVersion`, `videoId`, `outputType`, and `outputFormat`, records the
chosen `chunkSeconds` and `overlapSeconds`, and lists half-open millisecond
`chunks` without filling gaps between missing ranges.

For transcript-only work, use `--chunk-seconds 600 --overlap-seconds 4` because
structured transcript output reaches practical output limits earlier than
general analysis. Reconcile the overlap using video-start offsets. If a
representative clip fails ingestion or produces incomplete output, reduce its
size rather than repeating the identical request.

Run chunks sequentially. Start with one representative clip and expand only
after it succeeds. Use overlap only when continuity at a boundary matters; the
general default has no overlap to avoid duplicate processing.

## Reusable output formats

Only `video_material` can be saved and searched later. Its initial registry is
deliberately asymmetric:

| `outputType` | `outputFormat` | Validation |
| --- | --- | --- |
| `transcript` | `gemini-transcript` version `1` | Deterministic structured check |
| `summary` | `gemini-free-form-text` version `1` | Codex review before indexing |
| `systematic_visual_description` | `gemini-free-form-text` version `1` | Codex review before indexing |
| `systematic_onscreen_text` | `gemini-free-form-text` version `1` | Codex review before indexing |

Reject any other type, format, or type-format pairing. Transcript uses the
original spoken language and native script. Systematic onscreen text preserves
the text and script visible in the video. All `startMs` and `endMs` values are
millisecond offsets from the beginning of the YouTube video. The `MM:SS.mmm`
fields defined by `gemini-transcript` version `1` represent the same video-start
offsets. Translate on demand in Codex from saved source-language transcripts
or source onscreen text; do not save or search translation as a reusable Gemini
output. Do not store a top-level language field or divide material into
language variants.

`task_specific_observation` and `direct_answer` are one-time content classes.
Their response text is returned to the current conversation and is not retained
in local persistent state. Their successful run is still verified and recorded.

## Saved Gemini responses

Use `scripts/saved_gemini_responses.py save-response` after a successful
`video_material` router result. One immutable JSON file records one response
from one numbered run. Its format version is `1` and it contains:

- normalized `videoId`;
- `savedResponseId`, `requestId`, `runNumber`, and `exactRequestSha256`;
- fixed `contentClass`, controlled `outputType`, and compatible `outputFormat`;
- exact requested `sourceTimeRange`;
- the complete safe `routerResult`;
- `responseSha256` and the exact UTF-8 Gemini response file text in
  `responseJsonText`;
- for transcript format only, a mechanically derived `formatCheck` and, when
  valid, `coveredTimeRanges`.

The saver independently hashes the exact response bytes and requires equality
with the router result. `savedResponseId` is derived from every immutable saved
field except the repeated response text; `responseSha256` already binds those
bytes. A different authorized run therefore receives a different saved
response ID even when Gemini returns identical text.

The transcript checker requires the exact response fields and segment fields,
valid video-start timestamp offsets, the requested clip bounds, ordered
in-range segments, consistent completion flags, and a compatible API finish
reason. It derives covered time from the clip start through
`completed_through_timestamp`. A malformed response is still saved but has a
failed check and no covered time, so it cannot enter the material index.

An incomplete or truncated transcript is saved normally and contributes only
its checked partial coverage. Request the remaining interval with smaller clip
bounds, which creates a distinct request; never rerun the identical request as
truncation handling.

## Video material index

The material index is a small search file with only:

- `fileFormatVersion`, normalized `videoId`, `updatedAt`;
- a sorted `materials` list.

Each material entry contains its `savedResponseId`, predictable filename,
exact stored-file SHA-256, output type and format, and covered time
ranges. A concise reviewed material description can also appear. Request IDs,
run numbers, prompts, router attempts, retry reasons, cooldowns, and request
status do not belong here.

Search the readable index fields before reading response files. Match
output type, format, and half-open millisecond coverage. The planner supports
exact, containing, combined, overlapping, partial, incompatible, and missing
coverage. It returns only selected saved response IDs and remaining ranges.
Verify only selected local files; replan around missing, stale, or
invalid selections before constructing a request.

Every distinct saved response ID is retained even when its type and interval
match another entry. Transcript coverage comes only from the checker.
Free-form material requires explicit Codex review and conservative coverage
within its source range.

If the index is missing, enumerate the video's saved-response files. Rebuild
it from validated local files, including explicit free-form
review decisions. The rebuild-admissions JSON uses `admitted: true` plus
covered time for accepted free-form material and only `admitted: false` for a
reviewed response that remains unindexed. Failed structured responses remain
saved but are skipped. Initialize an empty index directly only after
enumeration confirms that no saved-response files exist.

## Gemini request log

Use `scripts/gemini_request_log.py`. The per-video file has format version `1`,
the normalized video ID, `updatedAt`, and a `requests` list.

One request entry keeps immutable logical-request information: request ID,
video and requested range, content class, exact prompt, endpoint, model,
method, normalized request hash, and applicable reusable-output declarations.
It owns an ordered `runs` list. Request status is not separately writable.

Every run contains:

- monotonically increasing `runNumber` and exact request-file hash;
- start/end times and one of `pending`, `succeeded`, `failed`, or
  `interrupted`;
- its own complete ordered routing attempts and applicable cooldown;
- for runs above 1, the authorization reason and its authorization/use times;
- on reusable success, saved-response ID, filename, and file hash;
- on one-time success, the generated `responseNotSavedByPolicy: true` marker.

Previous terminal runs are never removed or overwritten. At most one run for a
video is pending. An identical pending request blocks another call. Any later
run requires a non-empty authorization reason, must wait for recorded
cooldowns, and cannot repeat an unchanged terminal request failure.

`finish-run` accepts only a result bound to the exact request and highest
pending run. Existing retained attempts must be an exact prefix of returned
attempts; only the missing suffix is copied. Conflicts, gaps, duplicates,
reordering, or a non-final terminal attempt stop the update. Completion time
comes from the terminal router result, not from a later file update.

For reusable success, save the response locally before finishing the run;
then add eligible content to the material index. For one-time success,
`finish-run` verifies the actual response-file hash before generating the
non-storage marker. Caller-supplied status, attempts, time, or marker are not
success evidence.

## Interruption decisions

Do not intentionally use two write-capable sessions for the same normalized
video ID. This is an operating rule, not a filesystem lock or an exactly-once
guarantee.

If a run remains pending after the active workflow loses the terminal router
result, its network outcome is unknown. Do not invent attempts, infer an
outcome, or automatically retry. Ask the user to confirm that the earlier
session has stopped.

If reusable response saving completed but the final request-log update did
not, enumerate all saved responses for that video. Exactly one response with
the pending request ID, run number, and exact request hash can finish the
existing run after byte, identity, and router-result validation. Restore its
retained attempts and original terminal time. No new Gemini call occurs.

No match leaves the result uncertain. Multiple claimants or any conflict stops
for investigation. A confirmed stopped run can instead be marked
`interrupted`; a later deliberate execution is a new numbered run with explicit
authorization.

## Validated behavior

- Caption retrieval returns timestamp-linked citations for supported videos.
- A captionless 2-hour-15-minute movie failed as a whole-video request and
  succeeded for `0s`–`1800s`; the deterministic default planner reproduces its
  five bounded clips.
- Transcript timestamps work beyond 99 minutes.
- Transcript validation rejects malformed structure, clip mismatches,
  inconsistent completion flags, bad segment order, and false coverage.
- Material search covers exact, containing, combined, overlapping, partial,
  incompatible, stale, and missing cases, including 5,500 randomized interval
  cases.
- Request history preserves separate authorized runs, all returned safe
  attempts, cooldowns, failures, and distinct saved-response references.
- Request/router/response byte mismatches and invalid one-time completion are
  rejected before persistent state changes.
- No production module imports or calls a cache-v2 implementation.

Primary external documentation:

- Gemini video understanding: `https://ai.google.dev/gemini-api/docs/generate-content/video-understanding`
- Gemini Generate Content API: `https://ai.google.dev/api/generate-content`
- YouTube captions download: `https://developers.google.com/youtube/v3/docs/captions/download`
