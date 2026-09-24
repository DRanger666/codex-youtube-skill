# Codex YouTube Skill

This repository provides the `work-with-youtube` skill for local Codex use.
It turns YouTube videos into research material for summarization, comparison,
translation, transcription, visual inspection, and timestamped answers.

## Source order

The skill uses the least expensive sufficient source first:

1. Retrieve captions, transcript research, and metadata from the pinned YouTube
   MCP server.
2. Reuse compatible Gemini material already stored on the local machine.
3. Send only the unresolved video ranges or sensory questions to Gemini.

MCP results are temporary. Reusable Gemini responses, material indexes,
request logs, and router state are kept locally under the portable runtime's
`state/` directory. By default the runtime lives under
`${XDG_DATA_HOME:-$HOME/.local/share}/codex-youtube`; set
`YOUTUBE_SKILL_HOME` to override it. The skill has no Google Drive dependency.

## Local credentials

Provide Gemini credentials through the local process environment before
starting Codex:

```sh
export GEMINI_API_KEY='...'
# Optional second project:
export GEMINI_API_KEY_FALLBACK='...'
```

Use a shell-integrated secret manager if preferred. Never paste keys into a
Codex conversation or store them in this repository. If used, the fallback key
belongs to a different Google Cloud project and must differ from the primary.

## Reproducible YouTube MCP

The primary source is
[`coyaSONG/youtube-mcp-server`](https://github.com/coyaSONG/youtube-mcp-server),
pinned to:

- Version: `1.2.0`
- Commit: `06d5e7a83783f7a44498da88ade2ccaa42238747`
- Node.js: `v24.14.0`

The one-time setup script installs this exact upstream revision, builds it with
an isolated npm cache, verifies the MCP handshake, and registers it as the
user-level `youtube` MCP in Codex. Codex then starts the stdio server on demand;
normal skill runs neither reinstall it nor use a separate client wrapper.

## Gemini fallback

Gemini is used only when captions and saved local material do not satisfy the
request. It supplies missing visual or audible evidence; Codex performs the
final reasoning.

Long videos are processed in deterministic timestamp-bounded chunks. Reusable
responses are immutable and indexed by video, output type, format, and covered
time. Request logs prevent blind repetition after failures or interruptions.

## Install in Codex

Install the `work-with-youtube` skill from:

```text
https://github.com/DRanger666/codex-youtube-skill
```

The repository is the staged development source. The installed Codex skill
should contain only runtime files: `SKILL.md`, `agents/`, `references/`, and
`scripts/`.

After installing the skill, perform the machine setup once:

```sh
sh /path/to/work-with-youtube/scripts/setup_youtube_mcp.sh
codex mcp get youtube
```

This creates the pinned installation under
`${YOUTUBE_SKILL_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/codex-youtube}` and
adds its stdio command to
`${CODEX_HOME:-$HOME/.codex}/config.toml`. Start a new Codex session afterward
so the newly registered tools are available.

## Development

Run the structural validator and unit tests before installation:

```sh
/usr/bin/python3 /path/to/skill-creator/scripts/quick_validate.py .
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
```

[`REPOSITORY_MAP.md`](REPOSITORY_MAP.md) documents the maintained files and
their responsibilities.
