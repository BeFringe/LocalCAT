#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKSPACE="$(git -C "$SCRIPT_DIR/.." rev-parse --show-toplevel)"
MODEL="${GOV_MONITOR_MODEL:-}"
SESSION="${GOV_MONITOR_SESSION:-}"
AGENT="${GOV_MONITOR_AGENT:-governance-monitor}"
MODE="${GOV_MONITOR_MODE:-full}"
SESSION_KEY="${GOV_MONITOR_SESSION_KEY:-localcat-governance-monitor-v1}"
TITLE_PREFIX="${GOV_MONITOR_TITLE_PREFIX:-GM}"
INTERACTIVE="${GOV_MONITOR_INTERACTIVE:-0}"
DRY_RUN="${GOV_MONITOR_DRY_RUN:-0}"

AGENT_FILE="$WORKSPACE/.opencode/agents/governance-monitor.md"
SYNC_MECHANISM="$WORKSPACE/.kiro/steering/steering-sync-mechanism.md"
SESSION_SNAPSHOT="$WORKSPACE/.opencode/runtime/governance-monitor-session"
NODE_BIN="${GOV_MONITOR_NODE:-node}"

usage() {
  cat <<'EOF'
Usage:
  scripts/governance-monitor.sh [extra prompt]

Environment:
  GOV_MONITOR_SESSION=ses_xxx              Continue a prior opencode session.
  GOV_MONITOR_MODE=full|session|diff       Run mode. Defaults to full.
  GOV_MONITOR_TITLE_PREFIX=GM              Title prefix for new sessions.
  GOV_MONITOR_MODEL=provider/model         Optional model override. Defaults to the host configuration.
  GOV_MONITOR_AGENT=name                   Override opencode agent. Defaults to governance-monitor.
  GOV_MONITOR_INTERACTIVE=1                Open interactive split-footer mode.
  GOV_MONITOR_DRY_RUN=1                    Print the command instead of running it.

Examples:
  scripts/governance-monitor.sh
  GOV_MONITOR_MODE=session scripts/governance-monitor.sh
  GOV_MONITOR_SESSION=ses_xxx scripts/governance-monitor.sh
  GOV_MONITOR_MODE=diff scripts/governance-monitor.sh "只检查本次 diff 是否触发 ADR 更新"
  GOV_MONITOR_INTERACTIVE=1 scripts/governance-monitor.sh "重点检查 ADR 提升通道"
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

for required in \
  "$AGENT_FILE" \
  "$WORKSPACE/.kiro/steering/governance-understanding.md" \
  "$WORKSPACE/.kiro/steering/governance-baseline.md" \
  "$WORKSPACE/.kiro/steering/evolution-risk-analysis.md" \
  "$WORKSPACE/.kiro/steering/adr/README.md" \
  "$WORKSPACE/.kiro/settings/rules/governance.md" \
  "$SYNC_MECHANISM"; do
  if [[ ! -f "$required" ]]; then
    echo "Missing required governance input: $required" >&2
    exit 1
  fi
done

mkdir -p "$(dirname "$SESSION_SNAPSHOT")"

case "$MODE" in
  full|session|diff) ;;
  *)
    echo "Invalid GOV_MONITOR_MODE: $MODE (expected full, session, or diff)" >&2
    exit 1
    ;;
esac

find_latest_session() {
  if ! command -v "$NODE_BIN" >/dev/null 2>&1; then
    return 0
  fi

  opencode session list --format json -n 50 | "$NODE_BIN" -e '
const fs = require("fs");
const sessions = JSON.parse(fs.readFileSync(0, "utf8"));
const workspace = process.argv[1];
const prefix = process.argv[2];
const found = sessions.find((session) =>
  session.directory === workspace &&
  (session.title === "Governance Monitor" || session.title.startsWith(`${prefix}-`))
);
if (found) process.stdout.write(found.id);
' "$WORKSPACE" "$TITLE_PREFIX"
}

session_is_valid() {
  local candidate="$1"
  if [[ -z "$candidate" ]]; then
    return 1
  fi

  if ! command -v "$NODE_BIN" >/dev/null 2>&1; then
    return 1
  fi

  opencode export "$candidate" 2>/dev/null | "$NODE_BIN" -e '
const fs = require("fs");
const raw = fs.readFileSync(0, "utf8");
const jsonStart = raw.indexOf("{");
if (jsonStart < 0) process.exit(1);
const data = JSON.parse(raw.slice(jsonStart));
const agent = process.argv[1];
const key = process.argv[2];
const info = data.info || {};
const messages = JSON.stringify(data.messages || []);
if (info.agent === agent && messages.includes(`Governance-Monitor-Session-Key: ${key}`)) {
  process.exit(0);
}
process.exit(1);
' "$AGENT" "$SESSION_KEY"
}

make_title() {
  local raw="$1"
  local date_part
  local summary
  date_part="$(date +%y%m%d)"

  if command -v "$NODE_BIN" >/dev/null 2>&1; then
    summary="$(printf '%s' "$raw" | "$NODE_BIN" -e '
const fs = require("fs");
const input = fs.readFileSync(0, "utf8");
const clean = input
  .replace(/\s+/gu, " ")
  .trim()
  .replace(/[^\p{Letter}\p{Number}_-]+/gu, "-")
  .replace(/^-+|-+$/gu, "")
  .replace(/-+/gu, "-");
process.stdout.write(Array.from(clean).slice(0, 24).join(""));
')"
  else
    summary="governance-check"
  fi

  if [[ -z "$summary" ]]; then
    summary="governance-check"
  fi

  printf '%s-%s-%s' "$TITLE_PREFIX" "$date_part" "$summary"
}

if [[ "$MODE" == "session" && -z "$SESSION" ]]; then
  if [[ -f "$SESSION_SNAPSHOT" ]]; then
    SESSION="$(sed -n '1p' "$SESSION_SNAPSHOT")"
    if ! session_is_valid "$SESSION"; then
      SESSION=""
    fi
  fi

  if [[ -z "$SESSION" ]]; then
    SESSION="$(find_latest_session)"
    if ! session_is_valid "$SESSION"; then
      SESSION=""
    fi
  fi
fi

EXTRA_PROMPT="${*:-}"
MESSAGE="执行一次治理体系分支监控。
- Governance-Monitor-Session-Key: ${SESSION_KEY}。
- 运行模式：${MODE}。
- 只监控治理体系分支。
- 先读取固定输入，包括 ADR 索引、项目治理规则和 .kiro/steering/steering-sync-mechanism.md，再检查 git status 与治理文件变化。
- 输出 governance todolist，不要直接修改文件。"

if [[ "$MODE" == "session" ]]; then
  MESSAGE="$MESSAGE
- 这是 session check：如果已续接上一轮 Governance Monitor 会话，优先使用已有上下文；只刷新当前治理规则、ADR 索引、同步机制、git 状态和用户指定对象。必要时再重读完整固定输入。"
elif [[ "$MODE" == "diff" ]]; then
  MESSAGE="$MESSAGE
- 这是 diff check：优先检查当前 git diff / 最近变更是否触发 Steering、ADR、Spec、Skill 边界风险。必要时再读取完整固定输入。"
else
  MESSAGE="$MESSAGE
- 这是 full check：完整读取固定治理输入并输出 R1-R7 风险检查。"
fi

if [[ -n "$EXTRA_PROMPT" ]]; then
  MESSAGE="$MESSAGE

用户追加要求：
$EXTRA_PROMPT"
fi

cmd=(
  opencode run
  --dir "$WORKSPACE"
  --agent "$AGENT"
  --title "$(make_title "$EXTRA_PROMPT")"
)

if [[ -n "$MODEL" ]]; then
  cmd+=(--model "$MODEL")
fi

if [[ -n "$SESSION" ]]; then
  cmd+=(--session "$SESSION")
fi

if [[ "$INTERACTIVE" == "1" ]]; then
  cmd+=(--interactive)
fi

cmd+=("$MESSAGE")

if [[ "$DRY_RUN" == "1" ]]; then
  printf 'Command arguments:\n'
  printf '  %s\n' "${cmd[@]}"
  exit 0
fi

"${cmd[@]}"

latest_session="$(find_latest_session)"
if [[ -n "$latest_session" ]] && session_is_valid "$latest_session"; then
  printf '%s\n' "$latest_session" > "$SESSION_SNAPSHOT"
fi
