#!/bin/sh
# scripts/claude-gpt/lib.sh
#
# claude-gpt launcher の共有関数ライブラリ。POSIX sh 準拠、外部ライブラリ依存なし。
# `launch.sh` / `preflight.sh` / `runtime_smoke_test.sh` / `repair_proxy.sh` から
# `. lib.sh` で source される。単体実行は想定しない（source only）。
#
# Issue #2925: default launcher を upstream claude-code-proxy の Minimal client
# contract（https://claude-code-proxy.raine.dev/using/configure-claude-code/）へ
# 縮退した。このライブラリは、その薄い wrapper が必要とする以下だけを持つ。
#
#   1. Minimal Default Contract と現行 role alias の定数
#   2. 接続先 server（`ANTHROPIC_BASE_URL`）の到達性と `/v1/models` catalog の診断
#   3. 補助 evidence（PATH 上の claude-code-proxy の path / version）の取得
#   4. 証跡用の小さな helper（sha256 / git / JSON escape）
#
# 次の層は意図的に持たない（旧 launcher の履歴は Git history が rollback authority）:
# isolated HOME / XDG / CLAUDE_CONFIG_DIR、launcher 生成 `--settings`、strict MCP、
# custom autoMode prose、`CCP_AUTO_REVIEW_MODEL` 注入、isolation 補償用 credential /
# path carrier、hook / settings injection、proxy lifecycle（起動・停止）。

# --- Minimal client contract（upstream の source of truth に合わせる） ---

# upstream 既定の loopback endpoint（https://claude-code-proxy.raine.dev/using/configure-claude-code/）。
# 呼び出し元が `ANTHROPIC_BASE_URL` を設定していればそちらが接続先 authority になる。
CLAUDE_GPT_DEFAULT_BASE_URL="http://127.0.0.1:18765"

CLAUDE_GPT_AUTH_TOKEN_PLACEHOLDER="unused"

# `[1m]` suffix は upstream が案内する起動形式で、Claude Code 本体の local context
# window policy を拡張する hint である。proxy は upstream（ChatGPT backend）へ転送する
# 前にこの suffix を除去するため、実際に backend へ送られる model ID は base 名のまま。
CLAUDE_GPT_MODEL_MAIN="gpt-6-sol[1m]"
CLAUDE_GPT_MODEL_SMALL_FAST="gpt-6-luna[1m]"

# role-based model routing。現行 launcher の値を維持する（配分変更は別 Issue の
# explicit specification change であり、launcher 縮退と同時に比較しない。Issue #2925）。
CLAUDE_GPT_MODEL_OPUS="gpt-6-sol[1m]"
CLAUDE_GPT_MODEL_SONNET="gpt-6-sol[1m]"
CLAUDE_GPT_MODEL_HAIKU="gpt-6-luna[1m]"

# ChatGPT backend の実 context 上限（272k）に合わせた compaction window（upstream 案内値）。
CLAUDE_GPT_AUTO_COMPACT_WINDOW="272000"

# repair helper（repair_proxy.sh）が既定で pin する upstream proxy の version 補助情報。
# 単独では起動可否を決めない（可否の authority は接続先 server の `/v1/models`）。
CLAUDE_GPT_MIN_KNOWN_COMPATIBLE_PROXY_VERSION="0.1.42"

CLAUDE_GPT_REPAIR_COMMAND="scripts/claude-gpt/repair_proxy.sh"

# CLAUDE_GPT_HOME は repair_proxy.sh が補助 binary を導入する先、および smoke の
# 一時証跡の置き場としてのみ使う。Claude 子プロセスの HOME / config root には
# 一切影響しない（export しない）。
: "${CLAUDE_GPT_HOME:=${HOME}/.claude-gpt}"

# claude_gpt_evidence_dir: スクリプト自身の場所からリポジトリ内の
# scripts/claude-gpt/.evidence を解決する。
claude_gpt_evidence_dir() {
  script_dir=$(CDPATH= cd -- "$(dirname -- "$1")" && pwd -P)
  printf '%s/.evidence\n' "$script_dir"
}

# claude_gpt_strip_context_hint: model alias 末尾の `[1m]` 等 context-window hint suffix
# を取り除き、proxy `/v1/models` が返す base model 名と比較できる形にする。
# 引数1: model alias 文字列（例: "gpt-6-sol[1m]"）
claude_gpt_strip_context_hint() {
  printf '%s' "$1" | sed 's/\[[^]]*\]$//'
}

# claude_gpt_required_model_set: effective runtime consumer（main / opus / sonnet /
# haiku / small-fast）から実際に使用される model alias を base model ID へ変換し、
# 重複を除いた一意な ID を改行区切りで返す（一方向 derivation）。固定の手書き列挙は
# せず、role alias が将来変わっても自動的に required set へ反映される。
# `gpt-6-astra` 等の on-demand model は通常 consumer に含まれないため対象外。
claude_gpt_required_model_set() {
  _cgt_req_seen=""
  for _cgt_req_alias in \
    "$CLAUDE_GPT_MODEL_MAIN" \
    "$CLAUDE_GPT_MODEL_SMALL_FAST" \
    "$CLAUDE_GPT_MODEL_OPUS" \
    "$CLAUDE_GPT_MODEL_SONNET" \
    "$CLAUDE_GPT_MODEL_HAIKU"; do
    _cgt_req_base=$(claude_gpt_strip_context_hint "$_cgt_req_alias")
    case " $_cgt_req_seen " in
      *" $_cgt_req_base "*) : ;;
      *)
        _cgt_req_seen="$_cgt_req_seen $_cgt_req_base"
        printf '%s\n' "$_cgt_req_base"
        ;;
    esac
  done
}

# claude_gpt_missing_models: 引数1 に `/v1/models` の生 JSON 文字列、引数2 以降に
# required base model ID 群を受け取り、catalog に存在しない model ID だけを改行区切りで
# 返す（欠落が無ければ何も出力しない）。1 つでも欠ければ catalog は不完全であり、
# 一方だけ揃っている状態を PASS にしない。
# 判定は JSON を構造的に parse し、top-level object の `data` が list であり、その要素
# （object）の `id` が required model ID と完全一致するものだけを authority とする。
# 生 JSON 中の部分文字列一致・別 field の値・prefix / suffix の似た ID は存在と扱わない。
# parse 不能 / 想定外 schema / python3 不在は全 model 欠落（fail-closed）とする。
claude_gpt_missing_models() {
  _cgt_miss_json="$1"
  shift
  if ! command -v python3 >/dev/null 2>&1; then
    for _cgt_miss_required in "$@"; do printf '%s\n' "$_cgt_miss_required"; done
    return 0
  fi
  printf '%s' "$_cgt_miss_json" | python3 -c '
import json, sys
required = sys.argv[1:]
try:
    doc = json.loads(sys.stdin.read())
except ValueError:
    doc = None
ids = set()
if isinstance(doc, dict) and isinstance(doc.get("data"), list):
    for entry in doc["data"]:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            ids.add(entry["id"])
for name in required:
    if name not in ids:
        print(name)
' "$@"
}

# --- claude 実行バイナリの解決 ---
#
# CLAUDE_GPT_CLAUDE_BIN が明示されていればそれを使う。未指定なら command -v claude を
# 一度だけ解決する。
claude_gpt_resolve_claude_bin() {
  if [ -n "${CLAUDE_GPT_CLAUDE_BIN:-}" ]; then
    printf '%s\n' "$CLAUDE_GPT_CLAUDE_BIN"
    return 0
  fi
  command -v claude 2>/dev/null
}

# --- PATH 上の claude-code-proxy（補助 evidence） ---
#
# この binary は「接続先 server」ではない。launcher は proxy を起動しないため、ここで
# 解決する path / version は診断の補助 evidence としてのみ記録する（接続先 server が
# どの binary で動いているかは、この値からは分からない）。CLAUDE_GPT_PROXY_BIN が
# 明示されていればそれを優先する。
claude_gpt_resolve_proxy_bin() {
  if [ -n "${CLAUDE_GPT_PROXY_BIN:-}" ]; then
    printf '%s\n' "$CLAUDE_GPT_PROXY_BIN"
    return 0
  fi
  command -v claude-code-proxy 2>/dev/null
}

# claude_gpt_home_bin_dir: repair_proxy.sh が補助 binary を導入する isolated directory。
claude_gpt_home_bin_dir() {
  printf '%s/bin\n' "$CLAUDE_GPT_HOME"
}

# claude_gpt_proxy_version: proxy バイナリの version 識別子を補助 evidence として取得する。
# PATH 上の binary は launcher の前提ではないため、`--version` は bounded（2 秒、超過時は
# 強制終了）で実行する。出力は pipe ではなく一時 file へ受け、孫 process が pipe を保持して
# 呼び出し元を block することを避ける。timeout command が無い / 時間切れ / 空出力は
# "unknown"（未確認）として扱い、診断を block しない。
# 引数1: proxy バイナリの絶対パス
claude_gpt_proxy_version() {
  proxy_bin="$1"
  if [ -z "$proxy_bin" ] || ! command -v timeout >/dev/null 2>&1; then
    printf 'unknown\n'
    return 0
  fi
  _cgt_pv_tmp=$(mktemp 2>/dev/null) || { printf 'unknown\n'; return 0; }
  timeout -k 1 2 "$proxy_bin" --version >"$_cgt_pv_tmp" 2>/dev/null </dev/null || true
  version_output=$(head -n1 "$_cgt_pv_tmp" 2>/dev/null)
  rm -f "$_cgt_pv_tmp" 2>/dev/null
  if [ -n "$version_output" ]; then
    printf '%s\n' "$version_output"
  else
    printf 'unknown\n'
  fi
}

# claude_gpt_sha256_file: 任意ファイルの sha256。sha256sum / shasum いずれも無ければ "unknown"。
claude_gpt_sha256_file() {
  file="$1"
  if [ -z "$file" ] || [ ! -f "$file" ]; then
    printf 'unknown\n'
    return 0
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" 2>/dev/null | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" 2>/dev/null | cut -d' ' -f1
  else
    printf 'unknown\n'
  fi
}

# claude_gpt_git_head: repo_root の現行 HEAD SHA（取得不可なら "unknown"）。
claude_gpt_git_head() {
  repo_root="$1"
  head_sha=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null)
  if [ -n "$head_sha" ]; then
    printf '%s\n' "$head_sha"
  else
    printf 'unknown\n'
  fi
}

# claude_gpt_git_dirty: repo_root が dirty かどうかを "true"/"false"/"unknown" で返す。
claude_gpt_git_dirty() {
  repo_root="$1"
  if ! git -C "$repo_root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    printf 'unknown\n'
    return 0
  fi
  if [ -n "$(git -C "$repo_root" status --porcelain 2>/dev/null)" ]; then
    printf 'true\n'
  else
    printf 'false\n'
  fi
}

# claude_gpt_json_escape: 任意文字列を JSON 文字列リテラル（引用符込み）へ変換する。
# python3 が使えない環境では簡易 fallback（改行・制御文字は非対応）を使う。
claude_gpt_json_escape() {
  value="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import json, sys; sys.stdout.write(json.dumps(sys.argv[1], ensure_ascii=False))' "$value"
  else
    esc=$(printf '%s' "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '"%s"' "$esc"
  fi
}

# claude_gpt_json_array_from_lines: 改行区切り文字列を JSON string array へ変換する。
# 空文字列なら `[]` を返す。
claude_gpt_json_array_from_lines() {
  _cgt_arr_lines="$1"
  if [ -z "$_cgt_arr_lines" ]; then
    printf '[]'
    return 0
  fi
  _cgt_arr_out="["
  _cgt_arr_first=true
  _cgt_arr_old_ifs=$IFS
  IFS='
'
  for _cgt_arr_line in $_cgt_arr_lines; do
    [ -z "$_cgt_arr_line" ] && continue
    if [ "$_cgt_arr_first" = "true" ]; then
      _cgt_arr_first=false
    else
      _cgt_arr_out="${_cgt_arr_out},"
    fi
    _cgt_arr_out="${_cgt_arr_out}$(claude_gpt_json_escape "$_cgt_arr_line")"
  done
  IFS=$_cgt_arr_old_ifs
  _cgt_arr_out="${_cgt_arr_out}]"
  printf '%s' "$_cgt_arr_out"
}

# --- 接続先 server の診断（Issue #2925 AC3） ---
#
# 診断の authority は、実際に `ANTHROPIC_BASE_URL` が接続する running server である。
# 同じ URL に対して到達性と `/v1/models` を判定し、required model set（`gpt-6-sol` および
# `gpt-6-luna`）が両方揃っていることを PASS 条件とする。launcher はこの診断のために
# proxy を起動せず、停止もしない。

# claude_gpt_parse_base_url: `scheme://host[:port]` 形式の URL を分解する。
# 結果は CGD_SCHEME / CGD_HOST / CGD_PORT に設定する（path / query / userinfo を含む URL は
# 不正として戻り値 1）。port 省略時は scheme の既定値（http=80 / https=443）。
# 引数1: base URL
claude_gpt_parse_base_url() {
  _cgt_url="$1"
  CGD_SCHEME=""
  CGD_HOST=""
  CGD_PORT=""
  case "$_cgt_url" in
    http://*) CGD_SCHEME="http"; _cgt_rest="${_cgt_url#http://}" ;;
    https://*) CGD_SCHEME="https"; _cgt_rest="${_cgt_url#https://}" ;;
    *) return 1 ;;
  esac
  _cgt_rest="${_cgt_rest%/}"
  case "$_cgt_rest" in
    ""|*/*|*\?*|*\#*|*@*) return 1 ;;
  esac
  case "$_cgt_rest" in
    \[*\]:*)
      CGD_HOST="${_cgt_rest%%\]*}]"
      CGD_PORT="${_cgt_rest##*\]:}"
      ;;
    \[*\])
      CGD_HOST="$_cgt_rest"
      ;;
    *:*)
      CGD_HOST="${_cgt_rest%%:*}"
      CGD_PORT="${_cgt_rest#*:}"
      ;;
    *)
      CGD_HOST="$_cgt_rest"
      ;;
  esac
  if [ -z "$CGD_PORT" ]; then
    if [ "$CGD_SCHEME" = "https" ]; then CGD_PORT="443"; else CGD_PORT="80"; fi
  fi
  case "$CGD_PORT" in
    *[!0-9]*) return 1 ;;
  esac
  [ -n "$CGD_HOST" ]
}

# claude_gpt_is_loopback_host: loopback host のみ 0 を返す。受理するのは次だけ:
#   - 127.0.0.0/8 の 4 octet 10 進 IPv4 リテラル（各 octet 0-255。先頭 0 付きの曖昧表記は拒否）
#   - localhost
#   - ::1 / [::1]
# `127.evil.example` や `127.0.0.1.evil.example` のような 127. 始まりの host 名は拒否する。
# 引数1: host（IPv6 は角括弧付きでよい）
claude_gpt_is_loopback_host() {
  case "$1" in
    localhost|"[::1]"|"::1") return 0 ;;
    127.*) ;;
    *) return 1 ;;
  esac
  case "$1" in
    *[!0-9.]*|*..*|*.) return 1 ;;
  esac
  _cgt_lh_old_ifs="$IFS"
  IFS=.
  # shellcheck disable=SC2086 # 意図的な word-splitting: `.` 区切りで octet を分解する
  set -- $1
  IFS="$_cgt_lh_old_ifs"
  [ "$#" -eq 4 ] || return 1
  for _cgt_lh_octet in "$@"; do
    case "$_cgt_lh_octet" in
      ""|0?*) return 1 ;;
    esac
    [ "${#_cgt_lh_octet}" -le 3 ] || return 1
    [ "$_cgt_lh_octet" -le 255 ] || return 1
  done
  return 0
}

# claude_gpt_probe_models: 引数1 の base URL の `/v1/models` を bounded（接続 2 秒・全体 3 秒）で
# 1 回だけ取得する。結果は CGD_MODELS_HTTP_STATUS（curl 失敗時は 000）と CGD_MODELS_JSON に
# 設定する。
claude_gpt_probe_models() {
  _cgt_pm_base="${1%/}"
  CGD_MODELS_JSON=""
  CGD_MODELS_HTTP_STATUS="000"
  if ! command -v curl >/dev/null 2>&1; then
    CGD_MODELS_HTTP_STATUS="curl_unavailable"
    return 1
  fi
  _cgt_pm_tmp=$(mktemp 2>/dev/null) || return 1
  CGD_MODELS_HTTP_STATUS=$(curl -q --noproxy '*' -s --connect-timeout 2 -m 3 -o "$_cgt_pm_tmp" -w '%{http_code}' "${_cgt_pm_base}/v1/models" 2>/dev/null) || CGD_MODELS_HTTP_STATUS="000"
  [ -n "$CGD_MODELS_HTTP_STATUS" ] || CGD_MODELS_HTTP_STATUS="000"
  if [ "$CGD_MODELS_HTTP_STATUS" = "200" ]; then
    CGD_MODELS_JSON=$(cat "$_cgt_pm_tmp" 2>/dev/null)
  fi
  rm -f "$_cgt_pm_tmp" 2>/dev/null
  [ "$CGD_MODELS_HTTP_STATUS" = "200" ]
}

# claude_gpt_run_connected_server_diagnostics: 接続先 server の診断を実行し、結果の JSON
# object（`connected_server` の値）を CGD_JSON に、分類を CGD_CLASS に設定する。
# command substitution の subshell で変数が失われないよう、stdout ではなく変数で返す。
#   CGD_CLASS = ok | invalid_base_url | non_loopback | unreachable | models_http_error |
#               required_models_missing
# server version は公開 endpoint から取得できない（`/healthz` は `{"ok":true}` のみで
# version を返さない）ため、常に「未確認」と記録する。PATH 上の binary の version を
# server version として代用しない。
# 引数1: base URL
claude_gpt_run_connected_server_diagnostics() {
  _cgt_ds_base="$1"
  CGD_CLASS="ok"
  _cgt_ds_reachable=false
  _cgt_ds_catalog_ok=false
  _cgt_ds_http="null"
  _cgt_ds_required_nl=$(claude_gpt_required_model_set)
  _cgt_ds_missing_nl="$_cgt_ds_required_nl"
  _cgt_ds_host=""
  _cgt_ds_port=""
  if ! claude_gpt_parse_base_url "$_cgt_ds_base"; then
    CGD_CLASS="invalid_base_url"
  else
    _cgt_ds_host="$CGD_HOST"
    _cgt_ds_port="$CGD_PORT"
    if ! claude_gpt_is_loopback_host "$CGD_HOST"; then
      CGD_CLASS="non_loopback"
    elif claude_gpt_probe_models "$_cgt_ds_base"; then
      _cgt_ds_reachable=true
      _cgt_ds_http=200
      # shellcheck disable=SC2086 # 意図的な word-splitting: 改行区切りの model ID 一覧を反復する
      _cgt_ds_missing_nl=$(claude_gpt_missing_models "$CGD_MODELS_JSON" $_cgt_ds_required_nl)
      if [ -z "$_cgt_ds_missing_nl" ]; then
        _cgt_ds_catalog_ok=true
      else
        CGD_CLASS="required_models_missing"
      fi
    elif [ "$CGD_MODELS_HTTP_STATUS" = "000" ] || [ "$CGD_MODELS_HTTP_STATUS" = "curl_unavailable" ]; then
      CGD_CLASS="unreachable"
    else
      # server は応答したが `/v1/models` が 200 ではない。到達はしている。
      _cgt_ds_reachable=true
      _cgt_ds_http="$CGD_MODELS_HTTP_STATUS"
      CGD_CLASS="models_http_error"
    fi
  fi
  CGD_JSON=$(printf '{"base_url":%s,"host":%s,"port":%s,"reachable":%s,"models_http_status":%s,"required_models":%s,"missing_models":%s,"model_catalog_ok":%s,"classification":%s,"version":"未確認","version_note":%s}' \
    "$(claude_gpt_json_escape "$_cgt_ds_base")" \
    "$(claude_gpt_json_escape "$_cgt_ds_host")" \
    "${_cgt_ds_port:-null}" \
    "$_cgt_ds_reachable" \
    "$_cgt_ds_http" \
    "$(claude_gpt_json_array_from_lines "$_cgt_ds_required_nl")" \
    "$(claude_gpt_json_array_from_lines "$_cgt_ds_missing_nl")" \
    "$_cgt_ds_catalog_ok" \
    "$(claude_gpt_json_escape "$CGD_CLASS")" \
    "$(claude_gpt_json_escape "server version is not exposed by public endpoints (/healthz returns {\"ok\":true} only); local binary version is auxiliary evidence and is not the connected server version")")
}

# claude_gpt_local_proxy_auxiliary_json: PATH 上の claude-code-proxy の path / version を
# 補助 evidence として JSON object で返す。binary が無い場合は path / version を null にする。
# 接続先 server とは別項目であり、server の version や設定を証明しない。
claude_gpt_local_proxy_auxiliary_json() {
  _cgt_aux_bin=$(claude_gpt_resolve_proxy_bin)
  if [ -z "$_cgt_aux_bin" ]; then
    printf '{"path":null,"version":null,"note":%s}' \
      "$(claude_gpt_json_escape "auxiliary evidence only: no claude-code-proxy found on PATH")"
    return 0
  fi
  _cgt_aux_version=$(claude_gpt_proxy_version "$_cgt_aux_bin")
  printf '{"path":%s,"version":%s,"note":%s}' \
    "$(claude_gpt_json_escape "$_cgt_aux_bin")" \
    "$(claude_gpt_json_escape "$_cgt_aux_version")" \
    "$(claude_gpt_json_escape "auxiliary evidence only: the PATH binary is not necessarily the binary the connected server runs")"
}

# claude_gpt_launch_env_json: launcher が claude 子プロセスへ追加する env（key と値）を
# JSON object で返す。値は全て非 secret（`ANTHROPIC_AUTH_TOKEN` は placeholder）。
# 引数1: base URL
# 引数2: 解決済みの claude 実行ファイル path（未解決なら空文字列 -> null）
claude_gpt_launch_env_json() {
  printf '{"ANTHROPIC_BASE_URL":%s,"ANTHROPIC_AUTH_TOKEN":%s,"ANTHROPIC_MODEL":%s,"ANTHROPIC_SMALL_FAST_MODEL":%s,"ANTHROPIC_DEFAULT_OPUS_MODEL":%s,"ANTHROPIC_DEFAULT_SONNET_MODEL":%s,"ANTHROPIC_DEFAULT_HAIKU_MODEL":%s,"CLAUDE_CODE_AUTO_MODE_SERVER":"0","CLAUDE_CODE_AUTO_COMPACT_WINDOW":%s,"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":"1","CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK":"1","LOOP_TASK_CONTEXT_RUNTIME_VARIANT":"claude_gpt","CLAUDE_GPT_CLAUDE_BIN":%s}' \
    "$(claude_gpt_json_escape "$1")" \
    "$(claude_gpt_json_escape "$CLAUDE_GPT_AUTH_TOKEN_PLACEHOLDER")" \
    "$(claude_gpt_json_escape "$CLAUDE_GPT_MODEL_MAIN")" \
    "$(claude_gpt_json_escape "$CLAUDE_GPT_MODEL_SMALL_FAST")" \
    "$(claude_gpt_json_escape "$CLAUDE_GPT_MODEL_OPUS")" \
    "$(claude_gpt_json_escape "$CLAUDE_GPT_MODEL_SONNET")" \
    "$(claude_gpt_json_escape "$CLAUDE_GPT_MODEL_HAIKU")" \
    "$(claude_gpt_json_escape "$CLAUDE_GPT_AUTO_COMPACT_WINDOW")" \
    "$(if [ -n "$2" ]; then claude_gpt_json_escape "$2"; else printf null; fi)"
}

# claude_gpt_server_failure_json: 診断が失敗した場合の構造化 failure（stdout 用 JSON）を返す。
# model catalog の不足は account entitlement や推論能力の failure とは分類しない
# （`cause` は catalog 不整合のみを示す）。
# 引数1: 診断 JSON（claude_gpt_run_connected_server_diagnostics が設定した CGD_JSON）
claude_gpt_server_failure_json() {
  case "$CGD_CLASS" in
    required_models_missing)
      _cgt_sf_reason="model_alias_not_resolved"
      _cgt_sf_cause="connected_server_model_catalog_incomplete"
      _cgt_sf_hint="the running server at ANTHROPIC_BASE_URL does not list every required model; update or restart the SERVER OWNER's process with a compatible claude-code-proxy (the launcher never starts, stops or restarts a proxy)"
      ;;
    models_http_error)
      _cgt_sf_reason="connected_server_models_unavailable"
      _cgt_sf_cause="connected_server_models_endpoint_not_ok"
      _cgt_sf_hint="the server at ANTHROPIC_BASE_URL answered but /v1/models was not HTTP 200; confirm that the endpoint is a claude-code-proxy"
      ;;
    non_loopback)
      _cgt_sf_reason="connected_server_not_loopback"
      _cgt_sf_cause="base_url_host_is_not_loopback"
      _cgt_sf_hint="ANTHROPIC_BASE_URL must point at a loopback claude-code-proxy (127.0.0.1 / localhost / ::1)"
      ;;
    invalid_base_url)
      _cgt_sf_reason="invalid_anthropic_base_url"
      _cgt_sf_cause="base_url_not_scheme_host_port"
      _cgt_sf_hint="ANTHROPIC_BASE_URL must look like http://127.0.0.1:18765 (no path, query or userinfo)"
      ;;
    *)
      _cgt_sf_reason="connected_server_unreachable"
      _cgt_sf_cause="no_server_listening_or_not_responding"
      _cgt_sf_hint="no claude-code-proxy answered at ANTHROPIC_BASE_URL; start one yourself, for example: claude-code-proxy serve --port 18765 (see https://claude-code-proxy.raine.dev/using/configure-claude-code/)"
      ;;
  esac
  printf '{"schema":"CLAUDE_GPT_LAUNCH_RESULT_V1","status":"failed","reason":%s,"cause":%s,"connected_server":%s,"local_proxy_binary_auxiliary":%s,"start_hint":%s,"repair_command":%s,"repair_scope":%s}\n' \
    "$(claude_gpt_json_escape "$_cgt_sf_reason")" \
    "$(claude_gpt_json_escape "$_cgt_sf_cause")" \
    "$1" \
    "$(claude_gpt_local_proxy_auxiliary_json)" \
    "$(claude_gpt_json_escape "$_cgt_sf_hint")" \
    "$(claude_gpt_json_escape "$CLAUDE_GPT_REPAIR_COMMAND")" \
    "$(claude_gpt_json_escape "repair_proxy.sh installs a compatible BINARY under CLAUDE_GPT_HOME/bin only; it does not touch the running server, whose owner must restart it with that binary")"
}

# claude_gpt_find_free_port: OS に ephemeral port を割り当てさせ、bind 可能な loopback port
# 番号を 1 つ返す（repair_proxy.sh の再検証 probe 専用）。
claude_gpt_find_free_port() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
  else
    printf '48732\n'
  fi
}

# claude_gpt_probe_live_catalog: repair_proxy.sh の再検証専用の軽量 probe。指定した proxy
# バイナリを使い捨て ephemeral port・使い捨て HOME で起動し、`/v1/models` の生 JSON を
# 取得してから、**この関数自身が起動した** process だけを kill する。running な既存
# proxy / session には一切触れない。取得できなければ空文字列を返す。
# 引数1: proxy バイナリの絶対パス
# 引数2: probe に使う loopback port
claude_gpt_probe_live_catalog() {
  _cgt_probe_bin="$1"
  _cgt_probe_port="$2"
  if [ -z "$_cgt_probe_bin" ] || [ -z "$_cgt_probe_port" ]; then
    printf ''
    return 0
  fi
  _cgt_probe_home=$(mktemp -d 2>/dev/null) || { printf ''; return 0; }
  env -i \
    "PATH=$PATH" \
    "HOME=$_cgt_probe_home" \
    "CCP_CONFIG_DIR=$_cgt_probe_home/proxy-config" \
    "XDG_STATE_HOME=$_cgt_probe_home/xdg-state" \
    "CCP_BIND_ADDRESS=127.0.0.1" \
    "CCP_LOG_STDERR=1" \
    "$_cgt_probe_bin" serve --port "$_cgt_probe_port" --no-monitor >"$_cgt_probe_home/proxy.log" 2>&1 &
  _cgt_probe_pid=$!

  _cgt_probe_i=0
  _cgt_probe_ready=false
  while [ "$_cgt_probe_i" -lt 20 ]; do
    if ! kill -0 "$_cgt_probe_pid" 2>/dev/null; then
      break
    fi
    if curl -q --noproxy '*' --fail --show-error -s -o /dev/null -m 1 "http://127.0.0.1:${_cgt_probe_port}/v1/models" 2>/dev/null; then
      _cgt_probe_ready=true
      break
    fi
    _cgt_probe_i=$((_cgt_probe_i + 1))
    sleep 0.5
  done

  _cgt_probe_models=""
  if [ "$_cgt_probe_ready" = "true" ]; then
    _cgt_probe_models=$(curl -q --noproxy '*' --fail --show-error -s -m 3 "http://127.0.0.1:${_cgt_probe_port}/v1/models" 2>/dev/null)
  fi

  kill "$_cgt_probe_pid" 2>/dev/null
  wait "$_cgt_probe_pid" 2>/dev/null
  rm -rf "$_cgt_probe_home" 2>/dev/null
  printf '%s' "$_cgt_probe_models"
}
