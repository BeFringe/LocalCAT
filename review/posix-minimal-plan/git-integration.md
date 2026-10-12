# ADR-033 与开发基线的 Git 集成方案

本文件是待执行方案，不是已完成重放的记录。评审源码／实验旧 SHA 只解释历史证据；本轮接线从当前开发树继续。

## 已核实的输入

| 引用 | 完整对象 ID | 用途 |
| --- | --- | --- |
| `governance/kiro-steering` 与同名远端 | `a057d260091ea3f97bbb808689512ba5b5af68a4` | 现治理 tip，含 ADR-032 |
| 当前 `maintenance/project-codec-adjacency` 与 `origin/reassessment` | `2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5` | 开发现状，继续开发的内容基线 |
| 外援 review 远端 | `b3d5e26e76860ef6f673f1d6f63abb401d55210d` | 评审输入，不并入生产实现 |
| `v0.5.2-windows-frozen` | `c426e34e8bdeb2c2fe60bd772c916933a9ef6495` | 既有发行标签，不移动 |

治理和当前开发的 `repository-safety.md` blob 一致，为 `b2df408032b26418830d4cf1da6d9fc7a1fdc8c9`；本地和远端目前未发现另一版活动 Git 规则。按其中「治理集成与受控重放」执行，不把旧备份分支中出现过的版本当新规则。

## 拟议拓扑与影响范围

```text
现状：共同治理 → a057d26 → 34c7f75 → … → 2e1cbce
拟议：共同治理 → a057d26 → G（ADR-033及最小治理同步）
                                    → 34c7f75' → … → 2e1cbce'
                                    → 按owner形成的R/D/T修订 → 实施提交
```

- 拟议重放区间是 `(a057d260091ea3f97bbb808689512ba5b5af68a4, 2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5]`，共 **225 个线性提交、0 个 merge**。
- 第一个是 `34c7f755fdd91bb28c765585ca18c6145d4bacb1`「docs(platform): 建立 Windows 兼容改造基线」。将它及其后提交重放到 G 之后，满足治理在开发基线前的顺序；不是退回旧源码，也不是把治理追加到开发 tip 后。
- 该区间包含既有开发／验收提交，新父节点会改变其 hash。原链、旧报告和旧引用必须保留；新 SHA 不改签历史验收。现 frozen 标签不在这条活动栈的祖先链上，仍保持原对象。
- ADR 内容审批与这 **225 个提交的精确重放区间**分开确认。未取得后者的直接授权前，不移动活动治理／开发引用，不执行 rebase 或 force-push。

## 实施顺序

1. 执行前重新读取上述实际远端 tip、工作树占用和未提交状态。若远端已前进或本地新增工作，先重算区间及影响，不沿用旧 lease。
2. 先建立并推送原开发链保留引用（建议 `archive/posix-before-adr033-20261012` 指向上述完整开发 tip），并从远端读回验证；现 review、archive、backup、tags 全部保留。保存本轮未提交测试/fixture 的内容，不能 reset、覆盖或自动 stash。
3. 在既有治理线从 `a057d26` 续接唯一治理提交 G，仅含 ADR-033、索引与精确 steering 差异。未批准版本不把 ADR 标作已采纳，不将本审批包/外援材料混入治理或 production owner 代码。
4. 在隔离的临时集成引用／工作树验证 `rebase --onto G a057d26 <开发临时引用>`。这不是新产品开发基线；它保持当前代码内容并改变获准父链。原活动工作树和其他脏工作树保持不动直到核验闭合。
5. 用 `range-diff` 和最终 tree diff 验证 225 个提交无遗漏、重复或范围外修改；治理路径以外的生产代码、测试、fixture、历史证据与原 `2e1cbce` 相同。确有引用修订须单列，不全库替换历史 SHA。
6. 依审批结果按四个 owner 分别形成规格修订；其历史审批与完成记录保留原范围，新 delta 的审批只在会话及相应 `spec.json` 的明确范围中登记，不改写旧测试结论。
7. 原链远端保全和核验均完成后，以精确 lease 更新 `governance/kiro-steering` 与 `reassessment`。开发远端预期旧 tip 为完整 `2e1cbce…`，治理为完整 `a057d26…`；不匹配就停止，不覆盖他人工作。推送后核对新治理在新开发祖先链中，以及所有保护引用未变。
8. 从新开发 tip 继续最小修复。Git 父节点变化不触发全量产品重验；代码/合同实际变化按 owner 清单验证，Windows 新候选与历史发行证据分别报告。

## 工作树保护

- `/CAT` 的 `ui-mvp`、`/tm-lifecycle-fix` 的独立修复线不移动。
- 治理工作树现有 `.DS_Store` 未跟踪文件不清理、不暂存。
- 当前开发工作树的 `tests/fixtures/rpy/manual_package/`、`tests/test_rpy_manual_package_journey.py`、`tests/test_tm_resource_snapshot_posix.py` 保持；接入测试前逐项核对来源，不整目录暂存。
- 本审批包先保存在评审分支的后继提交，production HEAD 不变；无需为本方案重写评审历史。

## 预期提交描述

- 治理：`docs(governance): 明确 TM 稳定目录与外部替换边界`。
- owner 规格分别用 `docs(core)`、`docs(platform)`、`docs(resource)`、`docs(ui)`，描述正式合同差异。
- 后续实现按完整能力使用相应 scope；小修吸收入其所属功能，不追加与功能无法独立交付的零碎修复。
