#!/bin/sh
# scripts/claude-gpt/launch.sh
#
# repository-owned claude-gpt launcher（Issue #2925 で upstream Minimal client contract へ縮退）。
#
# 既に起動している loopback `claude-code-proxy`（ChatGPT subscription 経由の GPT-6 Sol/Luna）
# へ Claude Code を向けるために必要な process env だけを追加し、`claude` を exec する薄い
# wrapper である。Native Claude Code と同じ ambient な user/project config・HOME・
# plugins・Skills・SubAgents・hooks・MCP/settings・GitHub auth をそのまま使う。
#
#   https://claude-code-proxy.raine.dev/using/configure-claude-code/
#
# この launcher は次をしない（旧 launcher の履歴は Git history が rollback authority）:
#   - HOME / XDG / CLAUDE_CONFIG_DIR の隔離
#   - launcher 生成の --settings / settings.local.json / strict MCP / permission mode 注入
#   - custom autoMode prose、CCP_AUTO_REVIEW_MODEL の注入
#   - isolation を補償する credential / path carrier、hook / settings injection
#   - proxy の起動・停止・再起動（proxy server の policy は server の所有者が管理する）
#
# Usage:
#   scripts/claude-gpt/launch.sh [--check-only] [--dry-run] [--claude-bin <path>] [-- <claude 追加引数...>]
#
#   --check-only   接続先 server の診断（到達性と /v1/models の required model set）だけを行い、
#                  JSON を stdout に出力して終了する（`claude` 本体は起動しない）。
#   --dry-run      診断も起動も行わず、起動予定の内容を JSON で表示するのみ。
#   --claude-bin   claude 実行ファイルの絶対パスを明示する（CLAUDE_GPT_CLAUDE_BIN と同義）。
#   --             以降は claude 本体へそのまま渡す追加引数（"$@" のまま保持する）。
#
# 接続先: `ANTHROPIC_BASE_URL`（未設定なら upstream 既定の http://127.0.0.1:18765）。
# loopback host のみ受け付ける。
#
# Exit code:
#   0   --check-only / --dry-run の成功（通常起動では claude の exit code をそのまま返す）
#   2   launcher 自身の引数エラー（未知オプションなど）
#   3   claude バイナリが見つからない
#   7   接続先 server の診断失敗（到達不能 / base URL 不正 / required model 不足）

SELF_PATH=$0
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SELF_PATH")" && pwd -P)
# shellcheck source=./lib.sh
. "$SCRIPT_DIR/lib.sh"

REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd -P)
LAUNCHER_ABS_PATH="$SCRIPT_DIR/launch.sh"
BASE_URL="${ANTHROPIC_BASE_URL:-$CLAUDE_GPT_DEFAULT_BASE_URL}"

# --- どの経路（通常起動・--check-only・--dry-run・引数エラー含む）で終了しても、どの
#     worktree / commit から起動したかを stderr で確認できるようにする（stale worktree
#     起動事故の早期検出）。proxy= は PATH 上の binary の有無のみ（補助 evidence であり、実行も
#     version 取得もしない。接続先 server の version ではない）。 ---
CLAUDE_GPT_GIT_HEAD=$(claude_gpt_git_head "$REPO_ROOT")
CLAUDE_GPT_GIT_HEAD_SHORT="unknown"
if [ "$CLAUDE_GPT_GIT_HEAD" != "unknown" ]; then
  CLAUDE_GPT_GIT_HEAD_SHORT=$(printf '%s' "$CLAUDE_GPT_GIT_HEAD" | cut -c1-7)
fi
CLAUDE_GPT_GIT_DIRTY=$(claude_gpt_git_dirty "$REPO_ROOT")
LOCAL_PROXY_BIN=$(claude_gpt_resolve_proxy_bin)
# PATH 上の binary は補助 evidence に過ぎないため、通常起動 / --dry-run では実行しない
# （version の取得は --check-only の auxiliary evidence に限る）。
LOCAL_PROXY_NOTE="not_probed"
if [ -z "$LOCAL_PROXY_BIN" ]; then
  LOCAL_PROXY_NOTE="absent"
fi
echo "launcher=${LAUNCHER_ABS_PATH} git=${CLAUDE_GPT_GIT_HEAD_SHORT} dirty=${CLAUDE_GPT_GIT_DIRTY} proxy=${LOCAL_PROXY_NOTE}" >&2

CHECK_ONLY=false
DRY_RUN=false

while [ $# -gt 0 ]; do
  case "$1" in
    --check-only)
      CHECK_ONLY=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --claude-bin)
      if [ $# -lt 2 ]; then
        printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"missing_value","option":"--claude-bin"}\n' >&2
        exit 2
      fi
      CLAUDE_GPT_CLAUDE_BIN="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    -*)
      printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"unknown_launcher_option","option":"%s"}\n' "$1" >&2
      exit 2
      ;;
    *)
      printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"unexpected_positional_argument_before_double_dash","value":"%s"}\n' "$1" >&2
      exit 2
      ;;
  esac
done

# --- 以降 "$@" は `--` の後ろに来た claude 追加引数そのもの（文字列へ結合して再分割しない）。
#     明示した permission mode も含め、launcher は判定・注入せずそのまま渡す。 ---

if [ "$DRY_RUN" = "true" ]; then
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"dry_run","base_url":%s,"launch_env":%s,"isolation":"none","generated_settings":false}\n' \
    "$(claude_gpt_json_escape "$BASE_URL")" "$(claude_gpt_launch_env_json "$BASE_URL" "$(claude_gpt_resolve_claude_bin)")"
  exit 0
fi

# --- 接続先 server（ANTHROPIC_BASE_URL が実際に向く running server）の診断。
#     launcher は proxy を起動せず、停止もしない。 ---
claude_gpt_run_connected_server_diagnostics "$BASE_URL"
if [ "$CGD_CLASS" != "ok" ]; then
  claude_gpt_server_failure_json "$CGD_JSON"
  exit 7
fi

if [ "$CHECK_ONLY" = "true" ]; then
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"ok","mode":"check_only","connected_server":%s,"local_proxy_binary_auxiliary":%s,"launch_env":%s}\n' \
    "$CGD_JSON" "$(claude_gpt_local_proxy_auxiliary_json)" "$(claude_gpt_launch_env_json "$BASE_URL" "$(claude_gpt_resolve_claude_bin)")"
  exit 0
fi

CLAUDE_BIN=$(claude_gpt_resolve_claude_bin)
if [ -z "$CLAUDE_BIN" ]; then
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"blocked","reason":"claude_binary_not_found"}\n'
  exit 3
fi

# --- upstream Minimal client contract（+ 現行 contract として残す role alias / Auto mode
#     互換設定）。これが launcher が追加する env の全てである。
#
#   ANTHROPIC_DEFAULT_*_MODEL ............ role-based model routing（現行値を維持）
#   CLAUDE_CODE_AUTO_MODE_SERVER=0 ....... 現行の Claude Code と proxy の組合せで classifier
#                                          request を client 側から proxy 経由で routing する
#                                          互換設定（PR #2800 で実機確認済み）
#   LOOP_TASK_CONTEXT_RUNTIME_VARIANT .... Task Context が runtime flavor を識別するための
#                                          非 behavior-changing marker（permissions / tools /
#                                          HOME / config / MCP / auth surface は変えない）
#   CLAUDE_GPT_CLAUDE_BIN ................. 解決済みの claude 実行ファイル path。session manifest hook
#                                          （`.claude/hooks/generate_session_manifest_from_hook.mjs` の
#                                          `resolveRuntimeLane`）がこの変数の有無だけから
#                                          `runtime_lane: claude_gpt` を識別する、旧 launcher からの
#                                          既存 runtime identification（非 behavior-changing）
#
# `CCP_AUTO_REVIEW_MODEL` は proxy **server** 側の設定であり、client launcher は設定しない。 ---
export ANTHROPIC_BASE_URL="$BASE_URL"
export ANTHROPIC_AUTH_TOKEN="$CLAUDE_GPT_AUTH_TOKEN_PLACEHOLDER"
export ANTHROPIC_MODEL="$CLAUDE_GPT_MODEL_MAIN"
export ANTHROPIC_SMALL_FAST_MODEL="$CLAUDE_GPT_MODEL_SMALL_FAST"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$CLAUDE_GPT_MODEL_OPUS"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$CLAUDE_GPT_MODEL_SONNET"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$CLAUDE_GPT_MODEL_HAIKU"
export CLAUDE_CODE_AUTO_MODE_SERVER=0
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="$CLAUDE_GPT_AUTO_COMPACT_WINDOW"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK=1
export LOOP_TASK_CONTEXT_RUNTIME_VARIANT=claude_gpt
export CLAUDE_GPT_CLAUDE_BIN="$CLAUDE_BIN"

# shell を claude に置き換える。signal・stdin・exit code・process 名は Native と同一になり、
# launcher が後始末すべき子 process も存在しない。
exec "$CLAUDE_BIN" "$@"
