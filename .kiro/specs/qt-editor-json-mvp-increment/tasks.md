# 实施计划

> Feature 5 `TextMatcher` 是基础搜索的硬依赖；本计划不提供本地 casefold/Whole Word 替代实现。历史 Close-without-Saving 缺陷不属于本规格；这里只实现需求 5.5 明确要求的批量撤销点跨项目清理。

> **Q1 tasks-only amendment（2026-08-19）**：按 `feature5-ui-integration-review-clustering` 的 Checkpoint Q，将原 1.1、3.1、3.2 中混合的 Requirement 3 搜索切片与 speaker inventory、preprocessing/batch baseline 切片拆开，并新增 4.3a fresh acceptance 子任务。Q1 只可勾选 1.1a、2.6、3.1a、3.2a、4.3、4.3a；不得借搜索实施完成相邻产品范围。

> **Q2 tasks-only amendment（2026-08-19）**：按同一 Checkpoint Q，将原 4.7 中混合的 term mutation 与 preprocessing 四视图刷新拆为 4.7a/4.7b，并新增 4.5a fresh acceptance 子任务。Q2 只可勾选 2.7、3.4、3.5、4.5、4.5a、4.7a；不得借术语实施完成 preprocessing 或其他 Requirement。

> **Q1 search-surface amendment 已批准（2026-08-19）**：根据 Requirement 3 实机冒烟反馈，新增 1.1c、2.6a、3.2c、4.3b、4.3c，将项目搜索收纳为顶栏可折叠入口，增加明确清除和“未填写 / 草稿 / 已翻译”筛选。该 amendment 不授权 status-only 伪 offset、Approved/Revise 状态或 Replace/Replace All，并必须在 Q2 累计评审前完成 Q1 fresh acceptance。

- [x] 1. 建立冻结契约与能力边界

- [x] 1.1a 建立单 JSON 搜索与 matcher 能力契约
  - 覆盖项目工具可用性、搜索字段、命中、报告和三态 matcher readiness
  - 约束基础能力、高级选项与 Integration `TextMatcherDisplayState` 的合法组合，不定义第二份 readiness/digest authority
  - 完成时，合法契约可稳定构造，非法 capability、tuple 或 offset 组合会在边界测试中失败
  - _Requirements: 3.1, 3.3, 3.7, 3.9, 9.1, 9.7_

- [x] 1.1c 扩展项目搜索状态契约
  - 新增 exact `SegmentTranslationStatus`，并在 `ProjectSearchRequest` 中以 `None | UNFILLED | DRAFT | TRANSLATED` 表达一个可选筛选
  - 继续要求非空 query、非空字段与唯一 Core `SearchOptions`；不改 hit 的 text field/half-open offset 契约
  - 完成时，合法状态可稳定往返，foreign enum、错型和 status-only 伪命中在契约边界失败
  - _Requirements: 3.1, 3.5, 3.14, 3.15, 3.16, 9.1, 9.7_

- [x] 1.1b 建立 speaker inventory 能力契约
  - 覆盖项目工具可用性、speaker inventory item、空 speaker 计数和稳定顺序
  - 完成时，合法 inventory 可稳定构造，非法计数、重复身份或顺序组合会在边界测试中失败
  - _Requirements: 1.1, 1.3, 9.1, 9.7_

- [x] 1.2 建立预处理、批次报告与撤销会话契约
  - 覆盖有序 literal rule、段落前后差异、项目 session、revision、dirty 和 saved baseline
  - 约束 changed segment ID 唯一、before/after 状态完整及 stale preview 所需字段
  - 完成时，preview、apply report 和单批次 undo state 能完整表达正常、无变化和 stale 场景
  - _Requirements: 4.2, 4.3, 4.4, 4.5, 5.2, 5.3, 5.5_

- [x] 1.2a 建立设备本地预处理规则偏好契约
  - 冻结有序 literal rules、启用状态与 include_draft/include_confirmed；两个状态不得同时为 false
  - 不携带 preview、project/session/revision、batch undo 或项目正文
  - _Requirements: 4.12, 4.14, 4.17, 4.18_

- [x] 1.3 建立 mixed termbase 与提交结果契约
  - 覆盖 legacy/v1 row kind、稳定 locator、term draft、prepared mutation、commit outcome 和 cleanup report
  - 明确 committed、not committed、rolled back、indeterminate、recovery 和 quarantine 的组合不变量
  - 完成时，legacy flags 只能为空，v1 locator 必须带稳定 ID，失败 outcome 能携带可操作 recovery 信息
  - _Requirements: 7.1, 7.5, 7.6, 7.7, 7.9, 7.10, 7.13_

- [x] 2. 实现纯领域能力与本地术语存储

- [x] 2.1 (P) 实现确定性的 raw speaker inventory
  - 仅扫描规范化后的独立 speaker 字段，按首次出现顺序去重和计数
  - 空 speaker 单独计数，不读取、解析或回填 source
  - 完成时，重复扫描结果一致，扫描前后项目全部字段、顺序和身份保持不变
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_
  - _Boundary: SpeakerInventoryService_
  - _Depends: 1.1b_

- [x] 2.2 (P) 实现 target-only 有序 literal 预处理预览
  - 按用户可见顺序执行区分大小写、从左到右、非重叠的普通字符串替换
  - 只为 target 实际变化的段落生成 before/after 差异，不改变项目或 confirmed
  - 拒绝空 find、无启用规则和无实际变化，不加入 regex、脚本、Unicode normalization 或递归重跑
  - 完成时，规则顺序、no-op、空 target 和 confirmed 保持均由纯函数测试证明
  - _Requirements: 4.1, 4.2, 4.3, 4.6, 4.7, 4.9, 4.10, 4.11_

- [x] 2.2b 在 preview 前按草稿/已确认状态筛选
  - 以 confirmed boolean 为唯一状态事实，两个独立 boolean 可同时选择；都未选时整体拒绝
  - preview changes 继续携带 before_confirmed，以便 Qt 展示受影响状态分布
  - _Requirements: 4.12, 4.13, 4.14, 4.15_
  - _Boundary: TargetPreprocessor_
  - _Depends: 1.2_

- [x] 2.3 (P) 实现 legacy/v1 mixed CSV 的严格读取
  - 将严格两列行识别为 legacy，将带 marker、稳定 ID 和两个选项的六列行识别为 v1
  - 为 legacy 生成 digest/ordinal locator，为 v1 使用持久 ID，并保留文件显示顺序
  - 处理 UTF-8-SIG、quoted comma、空行、未知 marker、重复 source/ID 和无效布尔值
  - 完成时，mixed fixture 可无损 round-trip，损坏文件得到结构化错误且原字节不变
  - _Requirements: 7.1, 7.5, 7.9, 7.10, 7.13, 9.4_
  - _Boundary: TermbaseStore_
  - _Depends: 1.3_

- [x] 2.4 实现术语变更的 prepare 阶段
  - 为新增、更新、删除生成完整 candidate records，不在 prepare 阶段替换权威文件
  - 新记录持久化 `Match Case=false`、`Whole Word=true`；legacy 更新仍保持两列
  - 导入按 source 执行 last-write-wins：保留既有 row kind、v1 ID/flags 和原位置，新 source 追加为 legacy
  - stale locator、重复、冲突或无效输入必须整体拒绝
  - 完成时，每种 mutation 都产生可验证 candidate，源文件在 prepare 成功或失败后保持原样
  - _Requirements: 7.2, 7.3, 7.4, 7.5, 7.7, 7.9, 7.10, 7.13, 9.4_

- [x] 2.5 实现可恢复的术语 commit 与 cleanup
  - 在同目录创建并 fsync staged/recovery，提交前复核 source digest
  - replace 后失败时原子恢复旧字节；恢复失败返回 indeterminate outcome 并保留 recovery/quarantine 信息
  - 只允许 committed outcome 进入 finalize；cleanup 失败只产生可操作 warning，不改已提交资源
  - 对 prepare、replace、directory fsync、rollback、digest verification 和 cleanup 注入故障
  - 完成时，所有可恢复失败保持旧字节，indeterminate 不伪装成功，committed 文件重启后可完整读取
  - _Requirements: 7.5, 7.6, 7.13, 9.4_

- [x] 2.6 (P) 通过唯一 TextMatcher 实现项目搜索编排
  - 开始条件是 Feature 5 已提供 `SearchOptions`、稳定 offsets 和 `BASIC_VALIDATED` 的 `TextMatcher` port
  - 按项目段落顺序遍历 source、target、speaker，并把每个字段交给同一个 matcher
  - 基础请求固定使用 `match_case=false`、`whole_word=false`，不得实现本地 casefold 或词界 fallback
  - 保持 Core 返回的 offsets，生成字段、预览、总数和稳定导航身份；空 query/无结果不修改项目
  - 完成时，false/false golden cases 证明默认不区分大小写连续子串搜索，移除 matcher 后测试会失败而不是切换本地实现
  - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 3.8, 3.9, 3.10_
  - _Boundary: ProjectSearchService_
  - _Depends: 1.1a_

- [x] 2.6a 在 matcher 前闭合段状态筛选
  - 只由 Qt-free `ProjectSearchService` 从 `target + confirmed` 派生 UNFILLED/DRAFT/TRANSLATED，Qt 不得事后筛选 hits
  - 状态匹配的段落继续按 segment 与 SOURCE/TARGET/SPEAKER 固定顺序交给同一 matcher，offset 与 preview 原样保留
  - 完成时，未填写、草稿、已翻译与全部四组在 source/target/raw-speaker 上均稳定，空 query 仍拒绝
  - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.14, 3.15_
  - _Boundary: ProjectSearchService Status Filter_
  - _Depends: 1.1c, 2.6_

- [x] 2.7 (P) 集成 legacy Trie 与 configured term matcher
  - legacy 行始终保持区分大小写、连续子串和既有长词优先语义
  - capability 未验收时，v1 行以 legacy preset 参与建议，保存的 flags 不改变匹配结果
  - `TEXT_V1_VALIDATED` 后，仅 v1 cohort 使用 Feature 5 matcher 和逐记录选项；纯 CJK Whole Word 复用共享 golden 语义
  - 稳定合并 legacy/configured hits，保持资源顺序、记录顺序和既有 suggestion 选择规则
  - 完成时，同一 fixture 在 pre-gate、post-gate、legacy 与 CJK 场景得到设计规定的结果
  - _Requirements: 7.2, 7.8, 7.9, 7.10, 7.11, 7.12, 9.6_
  - _Boundary: ConfiguredTermAdapter, GlossaryEngine_
  - _Depends: 1.1a, 1.3, 2.3_

- [x] 3. 在 EditorController 中闭合会话与事务

- [x] 3.1a 建立项目 session 与单 JSON capability
  - 在成功安装、打开、切换或关闭项目时维护 session identity
  - 只有扩展名不区分大小写为 JSON 的项目启用本规格项目工具；TXT 与无路径 sample 保持可打开但工具明确不可用
  - 项目 codec 错误统一转换为 Controller 错误
  - 完成时，JSON、大小写变体 JSON、TXT、sample、失败打开和 session 切换测试均返回正确 capability 且不破坏现有会话
  - _Requirements: 9.1, 9.7_

- [x] 3.1b 建立 revision、saved baseline 与 batch 状态生命周期
  - 项目内容变化时递增 revision，成功打开/保存时更新 canonical saved baseline
  - 关闭或切换项目时只清除本规格的 batch undo/preview 状态，不扩展到历史 Close-without-Saving 修复
  - 完成时，编辑、保存、关闭和 session 切换测试得到一致 revision/baseline，且不破坏现有会话
  - _Requirements: 5.5, 9.4_

- [x] 3.2a 接入项目搜索与稳定导航
  - 所有入口先通过单 JSON gate，再调用纯搜索能力
  - 基础搜索只有在 `BASIC_VALIDATED` 时可执行；高级 options 只有在 `TEXT_V1_VALIDATED` 时接受
  - 使用稳定 segment identity 导航命中，保留当前未保存 target；空 query、无结果和 stale hit 不改变当前段
  - 完成时，Controller 测试可从搜索结果前后导航，同时搜索失败保持原项目和当前位置
  - _Requirements: 3.1, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 9.1, 9.4_

- [x] 3.2c 绑定搜索状态与显式清除
  - Controller issued context 增加 request status，project digest 纳入 confirmed，target/confirmed/filter 任一变化都拒绝旧 hit
  - 公开 `clear_project_search()` 同时清 report、issued hits 与 context，不导航、不修改 project/revision/dirty
  - 完成时，clear、status stale、confirmed-only stale、foreign/tampered request 都在项目状态变化前闭合
  - _Requirements: 3.4, 3.13, 3.14, 3.16, 9.1, 9.4_
  - _Boundary: EditorController Project Search Issuance_
  - _Depends: 2.6a, 3.1a, 3.2a_

- [x] 3.2b 接入 speaker inventory
  - 入口先通过单 JSON gate，再调用纯 inventory 能力
  - 完成时，Controller 重复读取 inventory 一致，失败保持原项目和当前位置
  - _Requirements: 1.1, 1.5, 9.1, 9.4_

- [x] 3.3 闭合预处理 preview、apply 与最近批次 undo
  - preview 绑定 session/revision；apply 复核 revision、segment identity 和 before target，stale 时整体拒绝
  - apply 只替换实际变化的 target，将变化段落设为未确认，并创建唯一 batch undo point
  - 新批次覆盖旧撤销点；相关段落后来被编辑时拒绝整批 undo，无关段落编辑继续保留
  - undo 按当前 saved baseline 重算 dirty，恢复涉及段落的 target/confirmed 且不改变 source、speaker、ID 或顺序
  - 完成时，clean、already-dirty、save-after-apply、跨项目、stale preview 和 stale undo 测试均得到一致 report
  - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 5.2, 5.3, 5.4, 5.5, 5.6, 9.4_

- [x] 3.3a 持久化并 defensive 投影预处理规则偏好
  - 在 workspace schema v1 可选 member 中原子保存/恢复规则与状态筛选，旧文件默认兼容
  - 保存失败保留旧文件、旧内存偏好与当前项目；保存不改变 dirty/revision/search/undo
  - _Requirements: 4.17, 4.18, 9.3, 9.4_

- [x] 3.4 闭合术语 prepare、候选 Engine、commit 与发布事务
  - 对可写 termbase 执行 prepare，并在 commit 前从 candidate records 构建完整候选 Engine 集合
  - candidate build 失败时 discard；commit 失败保留 last-known-good；indeterminate 时 quarantine 并携带 recovery 指引
  - 只有 committed outcome 才交换预构建引用并发布 mutation report；随后执行无数据修改的 cleanup
  - 完成时，新增、修改、删除以及每个故障点都不会产生“磁盘新版本、运行时旧版本”的普通成功状态
  - _Requirements: 7.2, 7.3, 7.4, 7.5, 7.6, 7.13, 9.4_

- [x] 3.5 接入术语导入、configured adapter 与资源热重载
  - 让 termbase import 经统一 mixed store merge，保留 v1 ID、flags、row kind 和既有 overwrite 计数
  - 按 matcher capability 构建 legacy/configured cohorts，并在成功 mutation 或 capability 切换后一次交换
  - 保持精确 TM 优先级、raw speaker TM identity、既有术语建议和资源状态行为
  - 完成时，导入前后的 v1 metadata 无损，重启恢复一致，当前段建议即时反映 committed 术语变化
  - _Requirements: 7.2, 7.3, 7.4, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13, 9.6_

- [x] 4. 实现 Qt 项目工具、编辑体验与桌面呈现

- [x] 4.1 在编辑与浏览视图显示 raw speaker
  - 当前段显示规范化 raw speaker；空值使用稳定“无 speaker”状态
  - 浏览表增加同一 raw speaker 列，保持双击按稳定段落身份返回编辑
  - 不创建 alias、显式空白 profile、头像或推测名称
  - 完成时，同一段在编辑与浏览中显示一致 speaker，切段和浏览不会修改项目或 TM identity
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 4.2 增加只读 speaker inventory 对话框
  - 展示首次出现顺序、每个非空 speaker 次数和独立空 speaker 计数
  - 无非空 speaker 时显示空 inventory，不从 source 创建角色
  - TXT/sample 中入口禁用并展示 capability 原因
  - 完成时，用户可重复打开对话框得到相同结果，关闭对话框前后项目和 dirty 状态不变
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 9.1_

- [x] 4.2a 在 speaker inventory 中增加安全的内置头像投影
  - 将批准的 `[speaker]Half.png` 作为随应用分发的只读展示资产，只在 inventory 行显示等比缩略图
  - 以实际文件名建立 Unicode casefold 索引；raw speaker 只查询索引，不直接拼接路径，重复键、缺失或解码失败均显示无头像
  - 不给 `SpeakerInventory` 合同增加头像/路径/profile 字段，不写入 JSON、workspace、搜索、术语或 TM identity
  - 保持 Task 4.1 编辑与浏览视图无头像；不创建 alias、显式空白 profile、配置入口或推测名称
  - 完成时，命中、大小写匹配、缺失、重复键、无效图片、可访问文本和项目完全只读均由 QtTest/边界测试证明
  - _Requirements: 1.5, 1.8, 1.9, 1.10, 2.5, 9.3, 9.4_
  - _Boundary: QtSpeakerInventoryDialog Presentation Assets_
  - _Depends: 4.2_

- [x] 4.2b 修复 speaker inventory“出现次数”列裁切
  - 使用满足完整中文表头、计数与最小对话框尺寸的固定可读宽度
  - 不压缩 raw speaker 文本权威、头像或首次出现列
  - _Requirements: 1.11_
  - _Depends: 4.2a_

- [x] 4.3 增加项目搜索条、结果反馈与导航
  - 提供 query、source/target/speaker 范围、总数、字段、预览和前后导航
  - 空 query 显示有效输入提示；无结果显示明确状态，二者均不切换当前段
  - advanced checkboxes 在 gate 前禁用并说明第二阶段；gate 后才传递用户 options
  - 基础搜索完成验收的前提是 Feature 5 `BASIC_VALIDATED` matcher 已连接，不得以 disabled 搜索框代替完成
  - 完成时，QtTest 可在真实项目中搜索、导航并保留未保存 target，disabled 控件状态不会改变 false/false 结果
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_

- [x] 4.3a 完成 Requirement 3 fresh acceptance evidence
  - 使用真实单 JSON project、production Controller 与 Feature 5 matcher handoff 验证 source/target/raw-speaker、offset、结果顺序和前后导航
  - 分别验证 BASIC 与 TEXT_V1 gate、Match Case / Whole Word、纯 CJK Whole Word、空 query、无结果、stale hit 与未保存 target 保留
  - 移除 matcher 或换入 foreign handoff 时必须 fail closed，不得切换 Qt/Controller 本地 matcher
  - 完成时，Q1 累计评审与 current-source acceptance evidence 对 Requirement 3 给出 fresh PASS
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 9.1, 9.2, 9.4, 9.5_
  - _Boundary: Requirement 3 Acceptance_
  - _Depends: 1.1a, 2.6, 3.1a, 3.2a, 4.3_

- [x] 4.3b 收纳项目搜索表面并增加清除与状态入口
  - 在顶栏增加 checkable 放大镜，搜索面板默认折叠；点击或平台原生 Find 快捷键在布局内展开，编辑三栏/浏览两栏整体下移，底部动作保持完整可见并由 target/列表区自适应收矮；展开时聚焦，工作区模式切换保留展开状态
  - 增加显式“清除”和全部/未填写/草稿/已翻译筛选；clear 保留字段、options、status 与面板展开状态
  - 只有项目和 matcher capability 可用时才启用执行；无项目/TXT/sample/foreign handoff 继续 fail closed，Qt 不保存第二份 project/report authority
  - 完成时，mouse/Tab/Enter/Space/`Ctrl+F`、折叠、clear、状态筛选和无副作用由 QtTest 证明
  - _Requirements: 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 9.1, 9.4_
  - _Boundary: Qt Project Search Surface Remediation_
  - _Depends: 3.2c, 4.3_

- [x] 4.3c 完成 Requirement 3 search-surface amendment fresh acceptance
  - 使用真实 `卷二_引.json`、production composition 与 Qt 验证 speaker-only `littleoldme` 从段 1 起返回稳定命中
  - 验证非常驻顶栏入口、显式 clear、三状态与 BASIC/TEXT_V1 组合，以及 target/confirmed/matcher generation 改变后旧结果拒绝
  - 验证未引入 Approved/Revise、status-only 伪命中、Replace/Replace All 或 Qt 本地 matcher fallback
  - 完成时，Q1 current-source acceptance 对 Requirement 3.1–3.16 给出 fresh PASS，再恢复 Q2 累计评审
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15, 3.16, 9.1, 9.2, 9.4, 9.5_
  - _Boundary: Requirement 3 Search Surface Amendment Acceptance_
  - _Depends: 1.1c, 2.6a, 3.2c, 4.3b_

- [x] 4.4 增加预处理规则、preview、apply 与 batch undo 对话框
  - 支持规则增删、启停和可见顺序；只呈现普通 literal 能力
  - preview 展示受影响段落数和逐段 before/after；取消不修改项目
  - 只有用户明确确认才 apply；成功后提供可发现的“撤销最近一次应用”
  - invalid/no-op/stale/no-undo 都显示原因且不创建虚假修改
  - 完成时，QtTest 覆盖 preview→cancel、preview→apply、apply→undo、新批次覆盖和跨项目隔离
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 4.10, 4.11, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

- [x] 4.4a 增加规则保存与草稿/已确认复选筛选
  - 恢复已保存规则/顺序/启用状态与两个复选框，提供显式“保存规则”操作
  - preview 展示总数与草稿/已确认分布；无状态选择、无变化、保存失败均明确反馈
  - draft-only 仍显式确认；只要包含已确认变化，应用对话框保留既有“变化段落设为待确认”警告
  - _Requirements: 4.12, 4.13, 4.14, 4.15, 4.16, 4.17, 4.18_
  - _Depends: 2.2b, 3.3a, 4.4_

- [x] 4.5 (P) 增加集中式术语管理对话框
  - 从主窗口 Termbase 页和设置资源菜单提供两个“管理术语”入口；两者列出当前 Active+Update resource 并打开同一个对话框
  - 在集中式对话框列出 source、target、policy 和匹配选项状态
  - 新记录显示 false/true 默认值；gate 前禁用 options 并说明 flags 尚不参与匹配
  - 支持 source/target 编辑、legacy locator、删除确认、冲突反馈和 committed/recovery/quarantine outcome
  - 只有 committed outcome 更新列表并触发建议刷新；legacy 不显示虚假 flags，也不静默迁移
  - 完成时，新增、修改、删除、失败、重开对话框和重启恢复均由 Qt 集成测试证明
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13, 7.14_
  - _Boundary: QtTermbaseDialog, QtEditorWindow, QtSettingsDialog Integration_
  - _Depends: 3.4, 3.5_

- [x] 4.5a 完成 Requirement 7 fresh acceptance evidence
  - 使用真实 mixed legacy/v1 termbase、production Controller 与 Qt 对话框验证列表、新增、修改、删除与重启恢复
  - 分别验证 pre-gate legacy preset 与 `TEXT_V1_VALIDATED` configured matcher，包含纯 CJK Whole Word 与 legacy 两列行不被静默改写
  - 覆盖 committed、not committed、rolled back、indeterminate、recovery/quarantine 及 import metadata 保留；只有 committed outcome 可刷新当前建议
  - 完成时，Q2 累计评审与 current-source acceptance evidence 对 Requirement 7 给出 fresh PASS
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10, 7.11, 7.12, 7.13, 9.2, 9.4, 9.5, 9.6_
  - _Boundary: Requirement 7 Acceptance_
  - _Depends: 1.3, 2.3, 2.4, 2.5, 2.7, 3.4, 3.5, 4.5, 4.7a_

- [x] 4.6 实现译文框原生 undo/redo 与 edit blocks
  - 在 target editor 聚焦时绑定 `Ctrl+Z`、`Ctrl+Y` 和 `Ctrl+Shift+Z`
  - 继续通过文本变化同步 Controller；首次改变已确认 target 时恢复待确认
  - suggestion 插入使用单个 cursor edit block；切段、换项目或批量刷新时清空 per-segment document history
  - 焦点不在 target 或 undo/redo 栈为空时保持 target 不变
  - 完成时，三个快捷键、普通输入、suggestion 单步撤销、确认状态和空栈行为全部通过 QtTest
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

- [x] 4.7a 建立术语变更的主窗口刷新协调
  - committed term mutation 从同一 Controller 会话刷新资源状态与当前 suggestions；非 committed outcome 只显示错误/recovery/quarantine
  - 对话框不维护项目、术语或 Engine 副本，失败操作不得渲染部分新状态
  - 完成时，创建、修改、删除只在 committed 后立即改变当前 term suggestions，切段或重开不会恢复旧 Engine
  - _Requirements: 7.2, 7.3, 7.4, 7.5, 7.6, 7.13, 9.4, 9.5_
  - _Boundary: QtTermbaseDialog, QtEditorWindow Refresh_
  - _Depends: 3.4, 3.5, 4.5_

- [x] 4.7b 建立预处理的主窗口四视图刷新协调
  - preprocess apply/undo 成功后从同一 Controller snapshot 刷新 edit、browse、progress/dirty 和 suggestions
  - 对话框不直接维护项目副本，失败操作不得渲染部分状态
  - 完成时，apply/undo 后四个视图一致，切换编辑/浏览不会显示旧 target 或 confirmed
  - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 5.2, 5.3, 5.4, 5.5, 5.6, 9.4, 9.5_
  - _Boundary: QtPreprocessDialog, QtEditorWindow Refresh_
  - _Depends: 3.3, 4.4_

- [x] 4.8 (P) 统一 desktop、application、window 与 dialog 的 silver logo
  - launcher 默认图标和 Qt application icon 使用 `LocalCAT-logo-silver.png`
  - 主窗口与子对话框继承或显式使用同一有效图标
  - 不改变无 Qt 环境下 launcher 安装和启动错误反馈
  - 完成时，launcher 内容、application/window/dialog icon 和无 Qt bootstrap 测试均指向 silver asset
  - _Requirements: 8.1, 8.2_
  - _Boundary: QtBootstrap_

- [x] 4.9 收紧资源 ellipsis 并保持键盘可用
  - 使用 auto-raise、固定横向策略和 32 logical px 最小键盘命中宽度
  - 宽度取内容 size hint 加安全 padding，最大 40 logical px；操作列不参与 Stretch
  - 名称/路径列承担剩余空间，窄/宽窗口下菜单按钮均可见、不遮挡相邻信息
  - 提供 tooltip、accessible name、键盘焦点和可打开菜单
  - 完成时，32–40 px、ResizeToContents/Fixed、resize、指针和键盘交互测试全部通过
  - _Requirements: 8.3, 8.4, 8.5_

- [x] 4.9a 对齐 macOS 页签与工作区模式修饰键
  - Translation Matches/Termbase 只注册实体 `Control+Tab` / `Control+Shift+Tab`，不得占用系统 `Command+Tab`
  - 编辑/校对继续使用 macOS `Command+1` / `Command+2`，不随页签修复改成实体 Control
  - tooltip、accessible name 与 QtTest 均按原生显示文本验证两组快捷键
  - _Requirements: 8.6, 8.7_

- [x] 4.9b 修复模式 popup 位置并增加段落密度切换键
  - 编辑/校对 popup 固定在顶栏 combo 下方，不再覆盖当前“编辑”控件
  - portable `Ctrl+Shift+L`在紧凑/自动换行间切换，并在 tooltip 中显示平台原生文本
  - _Requirements: 8.8, 8.9_

- [x] 5. 完成集成验收与回归保护

- [x] 5.1 验证领域与 Controller 的失败原子性
  - 覆盖 JSON/TXT/sample capability、inventory/search 只读性和失败时当前位置保持
  - 覆盖 stale preview、single undo、saved baseline dirty 以及相关/无关段落后续编辑
  - 对术语 prepare、candidate build、replace、fsync、rollback、quarantine 和 cleanup 执行故障注入
  - 完成时，所有普通失败保持之前的项目/资源状态，indeterminate 明确 fail-stop，测试无部分成功
  - _Requirements: 1.5, 3.4, 4.3, 4.8, 5.2, 5.3, 5.4, 5.5, 5.6, 7.5, 7.6, 9.1, 9.4, 9.7_

- [x] 5.2 (P) 验证完整 Qt 项目工具旅程
  - 使用真实 JSON fixture 验证 inventory、inventory-only 头像/退化状态、编辑/browse raw speaker、搜索导航和 capability gate
  - 验证预处理 cancel/apply/undo、target editor undo/redo、term CRUD 与四视图刷新
  - 断言空状态、stale 状态和失败 outcome 均不修改可见项目或资源
  - 完成时，offscreen Qt journeys 覆盖所有新增入口和关键失败路径并稳定通过
  - _Requirements: 1.1, 1.7, 2.1, 2.2, 2.3, 3.1, 3.4, 3.7, 4.2, 4.4, 4.8, 5.1, 5.5, 6.1, 6.2, 6.3, 6.4, 7.1, 7.2, 7.3, 7.4, 7.8, 7.13, 9.5_
  - _Boundary: Qt Project Tool Tests_
  - _Depends: 4.7a, 4.7b_

- [x] 5.3 (P) 验证 UI polish、可访问性与导入边界
  - 验证 silver logo、speaker inventory 头像等比缩放/退化状态、ellipsis 尺寸、resize、tooltip、accessible name 和键盘菜单
  - AST guard 覆盖主窗口、设置和三个新对话框，禁止 codec/store/domain/Core implementation 越层导入
  - composition root 仅构造依赖，不承载领域规则
  - 完成时，图标/布局/accessibility 测试和 Layer 4 boundary guard 全部通过
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 9.2, 9.4_
  - _Boundary: Qt Bootstrap, Settings and Boundary Tests_
  - _Depends: 4.8, 4.9_

- [x] 5.4 执行全量回归与本地性验收
  - 运行 canonical 单元、集成、offscreen smoke 和 Excel 相关测试，只修复本规格引入的回归
  - 验证 JSON/TXT 打开、JSON 保存、精确 TM 优先、raw speaker TM identity、Trie 建议、资源导入/删除和 Excel 三态
  - 证明没有新增 PO、RPY、XLIFF、多文件夹、多项目、网络、SQLite、云端或 fuzzy 入口
  - 不把历史 Close-without-Saving 缺陷作为本规格新能力或完成条件
  - 完成时，全量测试绿色，新能力仅在单 JSON gate 内可用，所有数据处理保持本地
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

- [x] 5.5 完成 project-tool usability amendment 验收
  - QtTest 证明 inventory 表头完整、规则保存/重开/重启、两个复选筛选、状态计数和两类确认提示
  - workspace 故障注入证明写失败原子，项目/dirty/revision/search/undo 不变
  - 刷新受影响 acceptance/release evidence，并运行全量 suite
  - 项目 owner 已人工采纳 ADR-014；Steering 已同步，并已复核最终五类语义门
  - _Requirements: 1.11, 4.12, 4.13, 4.14, 4.15, 4.16, 4.17, 4.18, 9.3, 9.4_
  - _Depends: 4.2b, 4.4a_
