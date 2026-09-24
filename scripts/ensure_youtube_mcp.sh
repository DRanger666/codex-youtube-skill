#!/bin/sh
set -eu

INSTALL_NAME=youtube
MCP_REPOSITORY=https://github.com/coyaSONG/youtube-mcp-server.git
MCP_COMMIT=06d5e7a83783f7a44498da88ade2ccaa42238747
MCP_VERSION=1.2.0
NODE_VERSION=v24.14.0

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
default_parent=${YOUTUBE_SKILL_HOME:-${CODEX_HOME:-$HOME/.codex}/mcp-servers}
search_root=$default_parent
install_parent=$default_parent

usage() {
  echo "usage: $0 [--search-root DIR] [--install-parent DIR]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --search-root)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      search_root=$2
      shift 2
      ;;
    --install-parent)
      [ "$#" -ge 2 ] || { usage; exit 2; }
      install_parent=$2
      shift 2
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

verify_install() {
  candidate=$1
  [ -f "$candidate/VERSION" ] || return 1
  [ -f "$candidate/README.md" ] || return 1
  [ -d "$candidate/state" ] || return 1
  [ -d "$candidate/work" ] || return 1
  [ -x "$candidate/runtime/bin/node" ] || return 1
  [ -f "$candidate/app/dist/stdio-server.js" ] || return 1
  for entry in "$candidate"/* "$candidate"/.[!.]* "$candidate"/..?*; do
    [ -e "$entry" ] || continue
    case "${entry##*/}" in
      app | runtime | state | work | README.md | VERSION) ;;
      *) return 1 ;;
    esac
  done
  rg -q "^installation_name=$INSTALL_NAME$" "$candidate/VERSION" || return 1
  rg -q "^youtube_mcp_version=$MCP_VERSION$" "$candidate/VERSION" || return 1
  rg -q "^youtube_mcp_commit=$MCP_COMMIT$" "$candidate/VERSION" || return 1
  rg -q "^node_version=$NODE_VERSION$" "$candidate/VERSION" || return 1
  [ "$("$candidate/runtime/bin/node" --version)" = "$NODE_VERSION" ] || return 1
  "$candidate/runtime/bin/node" "$script_dir/call_youtube_mcp.mjs" \
    --install "$candidate" --list-tools 2>/dev/null |
    rg -q '"research-video"' || return 1
}

mkdir -p "$install_parent"
install_parent=$(CDPATH= cd -- "$install_parent" && pwd)
destination=$install_parent/$INSTALL_NAME

case "$destination" in
  *[[:space:]]*)
    echo "Refusing a path containing spaces: $destination" >&2
    exit 1
    ;;
esac

if [ -e "$destination" ]; then
  if [ -d "$destination" ] && verify_install "$destination"; then
    printf '%s\n' "$destination"
    exit 0
  fi
  echo "Replacement required: existing path does not match the maintained layout: $destination" >&2
  exit 1
fi

if [ -d "$search_root/$INSTALL_NAME" ] &&
  verify_install "$search_root/$INSTALL_NAME"; then
  printf '%s\n' "$search_root/$INSTALL_NAME"
  exit 0
fi

if [ -d "$search_root" ]; then
  while IFS= read -r version_file; do
    candidate=${version_file%/VERSION}
    if verify_install "$candidate"; then
      printf '%s\n' "$candidate"
      exit 0
    fi
  done <<EOF
$(find "$search_root" -maxdepth 6 -type f -path "*/$INSTALL_NAME/VERSION" -print 2>/dev/null)
EOF
fi

for required in git npm rg; do
  command -v "$required" >/dev/null 2>&1 || {
    echo "Missing required command: $required" >&2
    exit 1
  }
done

build_root=$(mktemp -d "$install_parent/.youtube-mcp-build.XXXXXX")
portable=$build_root/$INSTALL_NAME
npm_cache=$build_root/npm-cache
cleanup() {
  rm -rf -- "$build_root"
}
trap cleanup EXIT HUP INT TERM

mkdir -p "$portable/app" "$portable/runtime/bin" "$portable/state" \
  "$portable/work" "$npm_cache"

echo "Fetching pinned YouTube MCP source..." >&2
git -C "$portable/app" init -q
git -C "$portable/app" remote add origin "$MCP_REPOSITORY"
git -C "$portable/app" fetch -q --depth 1 origin "$MCP_COMMIT"
git -C "$portable/app" checkout -q --detach FETCH_HEAD

node_source=${CODEX_PRIMARY_RUNTIME_NODE:-}
if [ ! -x "$node_source" ] || [ "$("$node_source" --version 2>/dev/null || true)" != "$NODE_VERSION" ]; then
  node_source=$(command -v node || true)
fi

if [ -x "$node_source" ] && [ "$("$node_source" --version)" = "$NODE_VERSION" ]; then
  cp "$node_source" "$portable/runtime/bin/node"
else
  command -v curl >/dev/null 2>&1 || {
    echo "Node $NODE_VERSION is unavailable and curl is not installed." >&2
    exit 1
  }
  command -v tar >/dev/null 2>&1 || {
    echo "Node $NODE_VERSION is unavailable and tar is not installed." >&2
    exit 1
  }
  node_archive=$build_root/node.tar.xz
  node_extract=$build_root/node
  mkdir -p "$node_extract"
  curl --fail --location --silent --show-error \
    "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-x64.tar.xz" \
    --output "$node_archive"
  tar -xJf "$node_archive" --strip-components=1 -C "$node_extract"
  cp "$node_extract/bin/node" "$portable/runtime/bin/node"
fi
chmod 755 "$portable/runtime/bin/node"

echo "Installing locked dependencies and compiling..." >&2
(
  cd "$portable/app"
  export NPM_CONFIG_CACHE=$npm_cache
  PATH="$portable/runtime/bin:$PATH" npm ci --no-audit --no-fund >&2
  PATH="$portable/runtime/bin:$PATH" npm run build >&2
)

printf '%s\n' \
  "installation_name=$INSTALL_NAME" \
  "youtube_mcp_version=$MCP_VERSION" \
  "youtube_mcp_commit=$MCP_COMMIT" \
  "node_version=$NODE_VERSION" \
  "platform=linux-x86_64" \
  >"$portable/VERSION"

printf '%s\n' \
  '# YouTube MCP Portable' \
  '' \
  'Pinned YouTube MCP installation for Codex.' \
  '' \
  '- `app/`: source, dependencies, and build' \
  '- `runtime/`: bundled Node.js runtime' \
  '- `state/`: Gemini router state created when first needed' \
  '- `work/`: disposable requests, responses, arguments, and intermediate files' \
  >"$portable/README.md"

if [ -e "$destination" ]; then
  echo "Replacement required: destination appeared during installation: $destination" >&2
  exit 1
fi

mv "$portable" "$destination"
verify_install "$destination" || {
  echo "Installed files failed MCP verification." >&2
  exit 1
}

printf '%s\n' "$destination"
