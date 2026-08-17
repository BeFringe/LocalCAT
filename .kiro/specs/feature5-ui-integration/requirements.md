# 需求文档

## 简介

LocalCAT Feature 5 UI 集成面向在本地 Qt 编辑器中使用翻译记忆的个人译者。Feature 5 已将 TM 能力扩展为可保留同源多译文、支持 exact/context/fuzzy 检索并提供统一文本匹配语义；本规格负责让这些能力成为编辑器中真实、可解释且可安全使用的产品行为。

本集成重点闭合“当前段查询 → 查看匹配类型、相似度与实际命中原文 → 显式应用建议”的翻译流程，同时让旧 legacy TM 与已激活 canonical TM 可以诚实共存。它还提供本地 fuzzy 阈值、资源状态和 macOS `LocalCAT.app` 入口，但不接管原 Qt 增量中的项目搜索、术语 CRUD、speaker 盘点或 target-only 预处理。

## 范围边界

- **范围内**：当前段 canonical exact/context/fuzzy TM 建议、query source 与 matched source 的可见区分、mixed legacy/canonical 资源、跨资源全局 top-10、应用级本地 fuzzy 阈值、显式应用与过期建议拒绝、canonical 激活与资源状态、能力不可用或资源局部失败的诚实反馈、统一文本匹配能力的 UI 对接、TM 阈值/状态入口，以及 macOS `LocalCAT.app`。
- **范围外**：重写 Feature 5 的 TM 算法、证明、迁移和存储；完成原 Qt Requirement 3 的项目搜索产品或 Requirement 7 的术语 CRUD；speaker alias/profile、target-only 预处理与批量撤销；Parser、多文档、项目 chunk、TMX DTD/ENTITY 方言扩展；机器翻译、云端、协作、签名、公证、DMG 和大型部署体系。
- **相邻期望**：`tm-storage-retrieval-index` 提供已验证的 canonical TM 与统一文本匹配语义；`qt-editor-json-mvp-increment` 继续拥有搜索控件、结果导航和术语管理行为，并负责交付 Termbase 与语言资源设置中的两个“管理术语”入口。现有 JSON/TXT、精确 TM、raw speaker identity、Trie 术语、Excel 三态、confirmed/dirty 与本地隐私不得回归。

### Scope Lineage

`qt-editor-json-mvp-increment` 的获批基线明确排除了模糊 TM、context ranking、SQLite TM schema 与 JSONL TM 迁移。本规格以独立 Spec 新增 Feature 5 与 Qt 之间的跨线实施授权，不修订、回溯改变或将这些历史排除项记为原 Qt Spec 已完成。

## 需求

### Requirement 1：当前段 TM 建议与可解释信息

**目标：** 作为个人译者，我希望在当前段看到 canonical TM 的精确、上下文和模糊建议及其真实来源，以便判断哪条译文适合复用。

#### 验收标准

1. When 用户进入或切换当前段, the LocalCAT Qt 编辑器 shall 查询所有 Active 且启用 Lookup 的翻译记忆资源
2. When 已激活 canonical 资源返回建议, the LocalCAT Qt 编辑器 shall 显示 `EXACT`、`CONTEXT` 或 `FUZZY` 匹配类型、最终相似度、目标译文和资源名称
3. When 建议的实际命中原文与当前查询原文不同, the LocalCAT Qt 编辑器 shall 分别显示或明确标注 query source 与 matched source
4. When 建议为 `EXACT` 或 `CONTEXT`, the LocalCAT Qt 编辑器 shall 将其最终相似度显示为 100%
5. When 建议为 `FUZZY`, the LocalCAT Qt 编辑器 shall 显示 Feature 5 已计算的最终相似度，而不得在 UI 中重新评分
6. If 当前段没有合格建议且没有资源或能力错误, the LocalCAT Qt 编辑器 shall 显示明确的无 TM 建议状态
7. The LocalCAT Qt 编辑器 shall 不向普通用户显示候选证明、文本折叠、中间评分或其他内部诊断材料

### Requirement 2：Mixed 资源、稳定顺序与全局 Top-10

**目标：** 作为同时使用新旧 TM 的个人译者，我希望所有建议按一个稳定规则汇总，以便可靠地优先查看最有价值的结果。

#### 验收标准

1. While legacy exact-only 资源与 canonical 资源同时启用, the LocalCAT Qt 编辑器 shall 在同一次当前段查询中使用两类资源
2. When mixed 资源返回多种匹配, the LocalCAT Qt 编辑器 shall 在全局结果中保持 `EXACT` 先于 `CONTEXT` 和 `FUZZY`
3. When 多条 `FUZZY` 建议具有不同最终相似度, the LocalCAT Qt 编辑器 shall 先显示相似度更高的建议
4. When 多条建议需要进一步仲裁, the LocalCAT Qt 编辑器 shall 保持已验证 TM 能力返回的完整稳定顺序，不自行省略或改写其仲裁条件
5. When 多个资源共同返回超过十条合格建议, the LocalCAT Qt 编辑器 shall 在全部资源汇总后只显示全局前十条，而不得为每个资源分别显示十条
6. When 相同资源状态、阈值和当前段重复查询, the LocalCAT Qt 编辑器 shall 返回相同的结果集合和顺序
7. While legacy 资源尚未完成 canonical 激活, the LocalCAT Qt 编辑器 shall 只从该资源返回 exact 建议，不得因其他 canonical 资源存在而把它提升为 context 或 fuzzy

### Requirement 3：应用级本地 Fuzzy 阈值

**目标：** 作为个人译者，我希望在经过验证的范围内调整模糊匹配下限，以便控制建议噪声而不改变 TM 内容。

#### 验收标准

1. When 本机尚未保存有效 fuzzy 阈值, the LocalCAT Qt 编辑器 shall 使用 60% 作为默认值
2. When 用户设置 fuzzy 阈值, the LocalCAT Qt 编辑器 shall 只接受 60% 至 100% 闭区间内的有限数值
3. If 用户提交低于 60%、高于 100%、非数值或非有限阈值, the LocalCAT Qt 编辑器 shall 保留上一个有效值并显示可理解的原因
4. When 用户成功改变 fuzzy 阈值, the LocalCAT Qt 编辑器 shall 刷新当前段建议，并使所有可见 `FUZZY` 建议满足新阈值
5. The LocalCAT Qt 编辑器 shall 只用 fuzzy 阈值排除低于阈值的 `FUZZY` 建议，不得隐藏有效的 `EXACT` 或 `CONTEXT` 建议
6. When 一个 fuzzy 建议的未舍入最终相似度恰好等于当前阈值, the LocalCAT Qt 编辑器 shall 将该建议视为合格
7. While 阈值为 100%, the LocalCAT Qt 编辑器 shall 仍允许正式匹配类型为 `FUZZY` 且最终相似度恰好为 100% 的建议
8. When 用户重启应用或切换项目, the LocalCAT Qt 编辑器 shall 恢复同一设备上最近成功保存的有效阈值
9. The LocalCAT Qt 编辑器 shall 不把 fuzzy 阈值写入项目、TM、术语表或网络位置
10. The LocalCAT Qt 编辑器 shall 在首版保持全局结果上限为十条，且不提供修改该上限的控件

### Requirement 4：显式应用与过期建议拒绝

**目标：** 作为个人译者，我希望自己决定是否应用每条 TM 建议，并避免旧建议覆盖后来发生变化的段落。

#### 验收标准

1. The LocalCAT Qt 编辑器 shall 不自动把 `EXACT`、`CONTEXT` 或 `FUZZY` 建议写入当前译文
2. When 用户显式应用一条仍然有效的建议, the LocalCAT Qt 编辑器 shall 只把所选目标译文写入当前 target，且不自动确认、写回 TM 或跳转段落
3. If 当前项目、当前段、当前 source、参与查询的资源、可用匹配能力或 fuzzy 阈值在建议产生后发生变化, the LocalCAT Qt 编辑器 shall 拒绝应用该过期建议
4. If 建议应用被拒绝或失败, the LocalCAT Qt 编辑器 shall 保持当前 target、confirmed、dirty、当前位置和 TM 内容不变
5. When 建议应用成功或被拒绝, the LocalCAT Qt 编辑器 shall 使用非阻塞反馈说明结果
6. When 用户随后确认当前段, the LocalCAT Qt 编辑器 shall 只向同时启用 Active 与 Update 的翻译记忆资源写回译文
7. While 一个翻译记忆资源未启用 Update, the LocalCAT Qt 编辑器 shall 允许该资源按 Active 与 Lookup 配置参与查询，但不得因应用或确认建议改变该资源字节

### Requirement 5：Canonical 激活、更新与唯一运行时状态

**目标：** 作为持有 legacy TM 的个人译者，我希望明确选择何时启用 canonical 能力，并在失败时保留已有可用资产。

#### 验收标准

1. The 语言资源设置 shall 区分 legacy exact-only、canonical active、source-diverged、degraded 和 unavailable 状态
2. When 用户明确请求首次 canonical 激活, the 语言资源设置 shall 在开始前说明目标资源与预期状态变化
3. The LocalCAT Qt 编辑器 shall 不在启动、打开项目、打开设置、刷新或首次查询时自动迁移 legacy 资源
4. When 用户在正式激活开始前取消, the LocalCAT Qt 编辑器 shall 保持原资源字节、资源配置和 legacy exact-only 可用性不变
5. While 正式激活正在进行, the 语言资源设置 shall 显示“激活中”、禁用重复激活与取消操作，并保持其他不冲突的编辑功能可用
6. If 首次激活失败, the LocalCAT Qt 编辑器 shall 不发布部分 canonical 状态，并继续提供原 legacy exact-only 能力
7. When canonical 激活完整成功, the LocalCAT Qt 编辑器 shall 将该资源显示为 canonical active，并让后续查询使用其 canonical exact 能力
8. While canonical 资源的 context 或 fuzzy 能力未分别获得当前授权, the LocalCAT Qt 编辑器 shall 不因资源已经激活而开放对应匹配类型
9. If 已激活资源的外部来源后来发生分歧或显式更新失败, the LocalCAT Qt 编辑器 shall 保留 last-known-good canonical 状态并显示分歧或失败，不得静默回落 legacy 查询
10. If 已激活资源不存在可验证的可用 canonical 状态, the LocalCAT Qt 编辑器 shall 停止使用该资源并显示明确错误

### Requirement 6：能力状态与资源局部失败

**目标：** 作为个人译者，我希望能力不足或单个资源失败时看到真实情况，同时继续使用其他安全可用的结果。

#### 验收标准

1. When context 或 fuzzy 能力不可用、过期或未通过当前验证, the LocalCAT Qt 编辑器 shall 分别显示对应匹配类型的可用或不可用状态
2. While fuzzy 能力不可用, the LocalCAT Qt 编辑器 shall 保留 canonical exact 和仍获授权的 context 建议，并禁用 fuzzy 阈值入口且说明原因
3. While context 能力不可用, the LocalCAT Qt 编辑器 shall 保留 canonical exact 和仍获授权的 fuzzy 建议，且不把其他结果伪装为 context
4. The LocalCAT Qt 编辑器 shall 不因资源处于 canonical active 状态而推断 context 或 fuzzy 可用，也不得提供由用户强制开放这些能力的控件
5. If 一个资源的路径、数据或查询发生局部失败, the LocalCAT Qt 编辑器 shall 标识受影响资源并保留其他资源的成功结果
6. If 一个资源查询失败且其他资源没有建议, the LocalCAT Qt 编辑器 shall 显示资源失败而不是普通“无匹配”
7. When 能力或资源状态恢复并通过验证, the LocalCAT Qt 编辑器 shall 刷新状态与当前段建议

### Requirement 7：统一文本匹配与 TM 设置入口

**目标：** 作为使用项目搜索、术语建议和 TM 建议的个人译者，我希望相关控件使用同一套已验证文本语义，并能从明确入口进入 TM 设置。

#### 验收标准

1. While 统一文本匹配能力只支持基础连续搜索, the LocalCAT Qt 编辑器 shall 保持项目搜索中的 Match Case 和 Whole Word 控件禁用并说明原因
2. When 统一文本匹配能力已验证 Match Case 与 Whole Word, the LocalCAT Qt 编辑器 shall 允许原 Qt Requirement 3 按其已批准范围启用两个控件
3. Where Whole Word 已启用且查询为纯 CJK 文本, the LocalCAT Qt 编辑器 shall 使用连续文本匹配并返回与未启用 Whole Word 时相同的结果
4. While legacy 两列术语尚未被明确迁移, the LocalCAT Qt 编辑器 shall 保持既有区分大小写、连续子串和 Trie 仲裁行为
5. When 用户查看 TM suggestions, the LocalCAT Qt 编辑器 shall 提供可发现、键盘可操作且不依赖 hover 的 fuzzy 阈值入口
6. The 语言资源设置 shall 提供第二个 fuzzy 阈值与 TM 状态入口，并与 TM suggestions 入口显示同一有效值和状态
7. When 阈值或 TM 资源状态成功更新或更新失败, the LocalCAT Qt 编辑器 shall 提供非阻塞且可理解的结果反馈

### Requirement 8：macOS `LocalCAT.app` 入口

**目标：** 作为 macOS 用户，我希望从 Finder 或 Dock 启动具有真实 LocalCAT 身份的应用，以便不再看到 Python 解释器身份。

#### 验收标准

1. When 用户从 Finder 或 Dock 启动应用, macOS shall 显示应用名称 `LocalCAT` 和 silver logo，而不是 `python3.14`
2. When 用户从 macOS 应用入口冷启动, the LocalCAT Qt 编辑器 shall 使用与现有 CLI 相同的本地数据、资源配置和编辑器行为
3. When 用户未预先打开 Terminal 或仓库目录而从 Finder/Dock 冷启动, the LocalCAT Qt 编辑器 shall 正常定位应用资源并显示主窗口
4. If macOS 应用入口无法保持 LocalCAT 身份、已批准运行环境或应用资源可用, the LocalCAT Qt 编辑器 shall 明确报告启动失败且不伪装为成功的 Python 应用身份
5. The macOS 应用入口 shall 不改变现有 Linux launcher 或 `python qt_editor.py --sample` CLI 行为
6. When 用户通过 macOS 应用入口打开编辑器, the LocalCAT Qt 编辑器 shall 能够打开项目并显示与 CLI 启动一致的 TM 建议

### Requirement 9：兼容性、本地性与 Canonical 验收

**目标：** 作为现有用户和规格审批者，我希望集成保持既有工作流，并用真正的 canonical 行为证明新增能力，以便避免 legacy 结果造成虚假通过。

#### 验收标准

1. The LocalCAT Qt 编辑器 shall 保持 exact 建议优先及 100% 相似度语义
2. The LocalCAT Qt 编辑器 shall 保持 raw speaker TM identity 与严格同 speaker 的 Ren'Py legacy compatibility，不使用显示别名或头像改写匹配身份
3. The LocalCAT Qt 编辑器 shall 保持 Excel `TM_HIT / TERMS_FOUND / NO_MATCH` 三态，不向 Excel 引入 context 或 fuzzy 第四态
4. The LocalCAT Qt 编辑器 shall 保持 Trie 长词优先与非重叠、当前段术语建议、JSON/TXT 打开、JSON 保存、confirmed/dirty 和资源开关行为
5. The LocalCAT Qt 编辑器 shall 在本机完成项目、TM、术语、阈值、来源信息和诊断处理，不向网络发送这些数据
6. When 验收 legacy TM 兼容性, the LocalCAT 项目 shall 只使用 legacy source-LWW 输出证明 exact-only 行为
7. When 验收同源多译文、非 100% 相似度、阈值、context/fuzzy 或排序, the LocalCAT 项目 shall 使用已激活且包含相应变体的 canonical TM，不得以 legacy exact-only 输出替代
8. When 验收 canonical TM 建议, the LocalCAT 项目 shall 覆盖同 source 多译文、不同 matched source、60% 包含边界、低于阈值、exact/context/fuzzy 顺序和 mixed 资源全局 top-10
9. When 验收失败与降级行为, the LocalCAT 项目 shall 覆盖能力关闭或过期、资源局部失败、显式应用和过期应用零修改
10. If TMX 输入包含当前安全策略禁止的 DTD 或 ENTITY 声明, the LocalCAT Qt 编辑器 shall 拒绝导入并保持目标资源不变
