# Feature 5 规格恢复记录

## 恢复结论

`tm-storage-retrieval-index` 的正式规格不是根据 brief 重新创作，而是从 2026-07-27 原始会话中成功落盘的补丁链逐条重放得到。恢复结果保留了当时已经完成的 Requirements、Design、Tasks 三阶段批准状态。

## 证据来源

- 原 Feature5 audit 会话：`019fa2e9-33d2-77c1-b562-76e6dbf788f1`
- 原主协调会话：`019fa0e9-94f9-7293-ad85-301ffeb02b72`
- 仅重放返回 `Success` 的文件补丁；历史失败补丁被排除。
- 原始会话日志只作为本地恢复证据，不进入仓库。日志可能包含会话元数据或敏感信息，不得复制、提交或作为普通项目文档传播。

## 恢复文件校验

下表固定法证恢复提交 `87c6513` 的批准基线；该提交在 2026-07-28 历史重整后不再位于活动分支的一阶历史中，但由 `refs/backup/localcat/20260728/ui-mvp` 持久保留。后续抢救性修订会有意改变当前文件哈希，不得用当前工作文件与本表不一致来否定历史恢复。

| 文件 | 行数 | SHA-256 |
|---|---:|---|
| `requirements.md` | 132 | `e65745b3b03614c2ebd691424dd7977bbb8d293823d5a7d1718f5593b461a294` |
| `research.md` | 169 | `2692a515b1a0de480ddda7d0c28bbf3860884cc76fec3039ff910bda587d8682` |
| `design.md` | 786 | `b6154d75f9f6a41ea9e67bfcae161f0dbe42746610ad6dd88bbd59d36e33f6bc` |
| `tasks.md` | 237 | `f6248a3cb83e5280cbe9badb758fff4365c2f950056ecaa9a1a380e052d9f221` |
| `spec.json` | 22 | `7e52e04b20e3d26084b281bbc224be53932d50cf2501cb31915a93a57bac7013` |

## 权威性与边界

- `requirements.md`、`design.md`、`tasks.md` 和 `spec.json` 是 Feature 5 当前正式规格。
- `brief.md` 是发现阶段输入；若与正式规格冲突，以已批准的正式规格为准。
- Qt increment 只消费 Feature 5 的 matcher/capability 契约，不拥有 SQLite、迁移、候选索引、fuzzy 评分或资源激活语义。
- Feature 5 不拥有 Qt 控件、项目 revision、批次撤销、术语事务、Parser/TMX 格式语义。
- 暂存数据库必须在候选索引闭合、计数核对、完整性与 exact parity 校验全部成功后，才可由资源协调器执行唯一激活。

## 后续纪律

- 任何恢复后的修订都必须作为新补丁进入正常 Requirements → Design → Tasks 审批链，不能改写成本次历史恢复的一部分。
- 任务完成标记应与对应代码和验证证据同一提交；不要在实现前批量勾选。
- 工作树完整看到其他规格是 Git 的正常行为；提交所有权由规格边界和显式暂存决定，不以“当前目录可见”推断。
