#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="/home/neotag/.local/share/opencode/worktree/bd10770111131a050c457174553a67d555e13df2/jolly-orchid"
MODEL="${GOV_MONITOR_MODEL:-zhipuai-coding-plan/glm-5.1}"
SESSION="${GOV_MONITOR_SESSION:-}"
AGENT="${GOV_MONITOR_AGENT:-governance-monitor}"
INTERACTIVE="${GOV_MONITOR_INTERACTIVE:-0}"
DRY_RUN="${GOV_MONITOR_DRY_RUN:-0}"

AGENT_FILE="$WORKSPACE/.opencode/agents/governance-monitor.md"
SYNC_MECHANISM="/home/neotag/文档/CAT/CAT/.kiro/steering/steering-sync-mechanism.md"
SYNC_SNAPSHOT="$WORKSPACE/.opencode/runtime/steering-sync-mechanism.md"

usage() {
  cat <<'EOF'
Usage:
  scripts/governance-monitor.sh [extra prompt]

Environment:
  GOV_MONITOR_SESSION=ses_xxx              Continue a prior opencode session.
  GOV_MONITOR_MODEL=provider/model         Override model. Defaults to configured GLM model.
  GOV_MONITOR_AGENT=name                   Override opencode agent. Defaults to governance-monitor.
  GOV_MONITOR_INTERACTIVE=1                Open interactive split-footer mode.
  GOV_MONITOR_DRY_RUN=1                    Print the command instead of running it.

Examples:
  scripts/governance-monitor.sh
  GOV_MONITOR_SESSION=ses_xxx scripts/governance-monitor.sh
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
  "$SYNC_MECHANISM"; do
  if [[ ! -f "$required" ]]; then
    echo "Missing required governance input: $required" >&2
    exit 1
  fi
done

mkdir -p "$(dirname "$SYNC_SNAPSHOT")"
cp "$SYNC_MECHANISM" "$SYNC_SNAPSHOT"

EXTRA_PROMPT="${*:-}"
MESSAGE="执行一次治理体系分支监控。
- 只监控治理体系分支。
- 先读取固定输入，包括 .opencode/runtime/steering-sync-mechanism.md，再检查 git status 与治理文件变化。
- 输出 governance todolist，不要直接修改文件。"

if [[ -n "$EXTRA_PROMPT" ]]; then
  MESSAGE="$MESSAGE

用户追加要求：
$EXTRA_PROMPT"
fi

cmd=(
  opencode run
  --dir "$WORKSPACE"
  --model "$MODEL"
  --agent "$AGENT"
  --title "Governance Monitor"
)

if [[ -n "$SESSION" ]]; then
  cmd+=(--session "$SESSION")
fi

if [[ "$INTERACTIVE" == "1" ]]; then
  cmd+=(--interactive)
fi

cmd+=("$MESSAGE")

if [[ "$DRY_RUN" == "1" ]]; then
  printf '%q ' "${cmd[@]}"
  printf '\n'
  exit 0
fi

"${cmd[@]}"
