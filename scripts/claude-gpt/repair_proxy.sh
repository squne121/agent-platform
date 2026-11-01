#!/bin/sh
# scripts/claude-gpt/repair_proxy.sh
#
# Issue #2801 AC4/AC5: repository-supported one-command bounded repair/bootstrap
# helper. Installs an isolated, compatible `claude-code-proxy` binary into
# `$CLAUDE_GPT_HOME/bin` using the upstream official installer
# (`raine/claude-code-proxy`'s `scripts/install.sh`), then re-verifies the
# installed binary's live `/v1/models` catalog against the same
# consumer-derived required model set the launcher uses. An installer exit 0 is
# NOT treated as success on its own -- required models must actually be
# present in the live catalog afterwards (AC5).
#
# Issue #2925 AC3 (scope of the repair): this helper repairs a BINARY, not a
# running SERVER. It installs a compatible binary under `$CLAUDE_GPT_HOME/bin`
# (or `CLAUDE_CODE_PROXY_INSTALL_DIR`) and verifies that binary on a throwaway
# loopback port that it starts and stops itself. The default launcher
# (`launch.sh`) never starts, stops or restarts a proxy, so the server that
# `ANTHROPIC_BASE_URL` points at keeps running the OLD binary until its owner
# restarts it with the repaired binary. The result JSON therefore always carries
# `repaired_scope: binary_only` and `server_restart_required: true`.
#
# This helper does not build a custom downloader / checksum framework: it
# fetches the upstream installer script text and executes it as-is (the
# installer performs its own artifact download and checksum verification).
# It also never kills or hot-swaps a running proxy/session (Out of Scope), and
# never runs on every normal launch (invoked explicitly by the operator, or
# via the `repair_command` surfaced in a launch.sh failure receipt).
#
# Usage:
#   scripts/claude-gpt/repair_proxy.sh [--dry-run]
#
# Env:
#   CLAUDE_CODE_PROXY_VERSION        upstream installer version pin
#                                    (default: $CLAUDE_GPT_MIN_KNOWN_COMPATIBLE_PROXY_VERSION)
#   CLAUDE_CODE_PROXY_INSTALL_DIR    override install dir
#                                    (default: $CLAUDE_GPT_HOME/bin -- normally left unset
#                                    so the launcher-owned isolated location is used)
#   CLAUDE_GPT_REPAIR_INSTALLER_URL  override upstream installer source URL
#                                    (test/injection point only; also accepts a `file://`
#                                    URL for hermetic fixture-based verification)
#
# Exit code:
#   0   install + re-verify PASS (required models actually present in live catalog)
#   1   installer download/execution failed, or binary missing after install
#   2   installer succeeded but re-verified catalog is still missing required models
#   3   curl unavailable (download tool missing)
#
# Output: structured JSON (`CLAUDE_GPT_REPAIR_PROXY_RESULT_V1`) to stdout on
# success/dry-run, stderr on failure (scripts/CLAUDE.md 構造化出力不変条件準拠).

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

DRY_RUN=false
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    *)
      printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"blocked","reason":"unknown_option","option":"%s"}\n' "$1" >&2
      exit 1
      ;;
  esac
done

INSTALL_DIR="${CLAUDE_CODE_PROXY_INSTALL_DIR:-$(claude_gpt_home_bin_dir)}"
# Issue #2801 fix_delta F1: operator が明示的に CLAUDE_CODE_PROXY_VERSION を
# 設定した場合はその値をそのまま installer に渡す（override intent を維持し、
# `v` を強制付与しない）。未設定時のフォールバックのみ upstream の GitHub
# Releases tag 形式（`v<version>`、例: `v0.1.42`）に変換する。upstream
# installer はこの値をそのまま release download URL に埋め込むため、
# fallback 側で `v` prefix を付けないと既定の repair 経路が配布物取得段階で
# 失敗する（tag 名は常に `v` prefix 付き）。
if [ -n "${CLAUDE_CODE_PROXY_VERSION:-}" ]; then
  VERSION_PIN="$CLAUDE_CODE_PROXY_VERSION"
else
  case "$CLAUDE_GPT_MIN_KNOWN_COMPATIBLE_PROXY_VERSION" in
    v*) VERSION_PIN="$CLAUDE_GPT_MIN_KNOWN_COMPATIBLE_PROXY_VERSION" ;;
    *) VERSION_PIN="v${CLAUDE_GPT_MIN_KNOWN_COMPATIBLE_PROXY_VERSION}" ;;
  esac
fi
INSTALLER_URL="${CLAUDE_GPT_REPAIR_INSTALLER_URL:-https://raw.githubusercontent.com/raine/claude-code-proxy/main/scripts/install.sh}"

if [ "$DRY_RUN" = "true" ]; then
  printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"dry_run","install_dir":%s,"version_pin":%s,"installer_url":%s}\n' \
    "$(claude_gpt_json_escape "$INSTALL_DIR")" \
    "$(claude_gpt_json_escape "$VERSION_PIN")" \
    "$(claude_gpt_json_escape "$INSTALLER_URL")"
  exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
  printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"failed","reason":"curl_unavailable"}\n' >&2
  exit 3
fi

# Issue #2801 fix_delta F2: upstream installer の shebang は
# `#!/usr/bin/env bash` であり、内部で `[[ ... ]]` / `&>/dev/null` など
# Bash 専用構文を使う。`sh`（環境によっては dash）で実行すると構文エラーや
# 判定結果の変化（例: `[[ -w "$install_dir" ]]` の書き込み可否判定）を
# 起こしうるため、必ず bash で実行する。
if ! command -v bash >/dev/null 2>&1; then
  printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"failed","reason":"bash_unavailable"}\n' >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR" || {
  printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"failed","reason":"install_dir_not_writable","install_dir":%s}\n' \
    "$(claude_gpt_json_escape "$INSTALL_DIR")" >&2
  exit 1
}

REPAIR_TMP_DIR=$(mktemp -d 2>/dev/null) || REPAIR_TMP_DIR="${INSTALL_DIR}/.repair-tmp-$$"
mkdir -p "$REPAIR_TMP_DIR" 2>/dev/null
INSTALLER_SCRIPT_TMP="$REPAIR_TMP_DIR/install.sh"
INSTALL_LOG="$REPAIR_TMP_DIR/install.log"

# --- 1. upstream installer script text を取得する（独自 downloader/checksum
#     framework は作らない。installer 自身の download/checksum ロジックを
#     そのまま実行する）。 ---
if ! curl --fail --show-error -sSL "$INSTALLER_URL" -o "$INSTALLER_SCRIPT_TMP" 2>"$INSTALL_LOG"; then
  printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"failed","reason":"installer_download_failed","installer_url":%s}\n' \
    "$(claude_gpt_json_escape "$INSTALLER_URL")" >&2
  rm -rf "$REPAIR_TMP_DIR" 2>/dev/null
  exit 1
fi

# --- 2. `$CLAUDE_GPT_HOME/bin`（または明示 override）へ version pin 付きで
#     installer を実行する（AC4）。 ---
if ! CLAUDE_CODE_PROXY_VERSION="$VERSION_PIN" CLAUDE_CODE_PROXY_INSTALL_DIR="$INSTALL_DIR" \
    bash "$INSTALLER_SCRIPT_TMP" >>"$INSTALL_LOG" 2>&1; then
  printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"failed","reason":"installer_execution_failed","version_pin":%s,"install_dir":%s}\n' \
    "$(claude_gpt_json_escape "$VERSION_PIN")" \
    "$(claude_gpt_json_escape "$INSTALL_DIR")" >&2
  rm -rf "$REPAIR_TMP_DIR" 2>/dev/null
  exit 1
fi

INSTALLED_BIN="$INSTALL_DIR/claude-code-proxy"
if [ ! -x "$INSTALLED_BIN" ]; then
  printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"failed","reason":"binary_missing_after_install","expected_path":%s}\n' \
    "$(claude_gpt_json_escape "$INSTALLED_BIN")" >&2
  rm -rf "$REPAIR_TMP_DIR" 2>/dev/null
  exit 1
fi

INSTALLED_VERSION=$(claude_gpt_proxy_version "$INSTALLED_BIN")

# --- 3. install exit 0 だけを成功としない -- 実際に起動し、live catalog を
#     re-verify する（AC5）。running な既存 proxy/session には触れない。 ---
REPAIR_PORT=$(claude_gpt_find_free_port)
REVERIFY_MODELS_JSON=$(claude_gpt_probe_live_catalog "$INSTALLED_BIN" "$REPAIR_PORT")
REQUIRED_MODELS_NL=$(claude_gpt_required_model_set)
# shellcheck disable=SC2086 # 意図的な word-splitting: 改行区切りの model ID 一覧を可変引数として渡す
MISSING_MODELS_NL=$(claude_gpt_missing_models "$REVERIFY_MODELS_JSON" $REQUIRED_MODELS_NL)

rm -rf "$REPAIR_TMP_DIR" 2>/dev/null

if [ -n "$MISSING_MODELS_NL" ]; then
  printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"failed","reason":"catalog_still_incompatible_after_install","installed_path":%s,"installed_version":%s,"missing_models":%s}\n' \
    "$(claude_gpt_json_escape "$INSTALLED_BIN")" \
    "$(claude_gpt_json_escape "$INSTALLED_VERSION")" \
    "$(claude_gpt_json_array_from_lines "$MISSING_MODELS_NL")" >&2
  exit 2
fi

printf '{"schema":"CLAUDE_GPT_REPAIR_PROXY_RESULT_V1","status":"ok","repaired_scope":"binary_only","server_restart_required":true,"installed_path":%s,"installed_version":%s,"install_dir":%s,"required_models":%s}\n' \
  "$(claude_gpt_json_escape "$INSTALLED_BIN")" \
  "$(claude_gpt_json_escape "$INSTALLED_VERSION")" \
  "$(claude_gpt_json_escape "$INSTALL_DIR")" \
  "$(claude_gpt_json_array_from_lines "$REQUIRED_MODELS_NL")"
echo "NOTE: repaired binary only (${INSTALLED_BIN}); the running server that ANTHROPIC_BASE_URL points at was NOT restarted. Its owner must restart it with this binary for the fix to take effect." >&2
exit 0
