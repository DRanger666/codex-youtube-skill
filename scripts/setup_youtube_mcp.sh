#!/bin/sh
set -eu

MCP_NAME=youtube

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
codex_cli=${CODEX_MCP_CLI:-codex}

command -v "$codex_cli" >/dev/null 2>&1 || {
  echo "Missing required command: $codex_cli" >&2
  exit 1
}

install=$(sh "$script_dir/ensure_youtube_mcp.sh" "$@")
node=$install/runtime/bin/node
server=$install/app/dist/stdio-server.js

if current=$("$codex_cli" mcp get "$MCP_NAME" 2>/dev/null); then
  if ! printf '%s\n' "$current" | rg -F "command: $node" >/dev/null ||
    ! printf '%s\n' "$current" | rg -F "args: $server" >/dev/null; then
    echo "Codex already has a different MCP registration named '$MCP_NAME'." >&2
    echo "Inspect it with: codex mcp get $MCP_NAME" >&2
    echo "Remove it deliberately, then rerun this setup if it should be replaced." >&2
    exit 1
  fi
  echo "Codex MCP '$MCP_NAME' is already registered to this installation."
else
  "$codex_cli" mcp add "$MCP_NAME" -- "$node" "$server"
fi

"$codex_cli" mcp get "$MCP_NAME"
echo "Start a new Codex session to make the YouTube MCP tools available."
