# Governance Monitor Runner

This project uses `governance-monitor` as the first domain agent for the governance branch.

## Modes

```bash
scripts/governance-monitor.sh "检查 ADR 文件是否遗漏更新"
GOV_MONITOR_MODE=session scripts/governance-monitor.sh "继续上一轮治理检查"
GOV_MONITOR_MODE=diff scripts/governance-monitor.sh "只检查本次 diff 是否触发 ADR 更新"
GOV_MONITOR_SESSION=ses_xxx scripts/governance-monitor.sh "显式续接指定治理会话"
```

## Session Selection

`session` mode degrades in this order:

1. Use `GOV_MONITOR_SESSION` when explicitly provided.
2. Use `.opencode/runtime/governance-monitor-session` when the saved session still validates.
3. Search recent `opencode session list --format json` results by title prefix and workspace directory, then validate the exported session.
4. Start a new session when no validated session is found.

Validation uses `opencode export` and requires:

- `info.agent == "governance-monitor"`
- the session contains `Governance-Monitor-Session-Key: localcat-governance-monitor-v1`

The runtime session file is written after a successful run when a validated latest governance-monitor session can be found.

## Titles

New monitor sessions use a compact title:

```text
GM-YYMMDD-<summary>
```

The summary is derived from the extra prompt and kept short to avoid duplicate `Governance Monitor` titles.

## Workspace and Model Resolution

The runner derives the workspace from its own tracked script with `git rev-parse --show-toplevel`. It reads `.kiro/steering/steering-sync-mechanism.md` from that same checkout and never copies a host-specific source snapshot.

No model provider is pinned in the repository. The host's OpenCode configuration is used by default; `GOV_MONITOR_MODEL` is an explicit per-run override.
