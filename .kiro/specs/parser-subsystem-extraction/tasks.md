# Parser 子系统重新基线实施计划

## Tasks

- [x] 1. Wave 0：建立现行行为与失败护栏
- [x] 1.1 固定单文档项目 facade 的兼容行为
  - 覆盖 LocalCAT JSON 数组根、对象根、字段缺省、顺序、局部 ID、空项目和 TXT source-only 读取。
  - 固定保存 schema、目标替换原子性、Controller 错误映射、session 安装与 dirty 清理时机。
  - 完成时，现有项目入口在尚未切换 codec 前已有可重复的成功与失败 characterization 证据。
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 13.3, 14.1, 14.3_

- [x] 1.2 固定 TMX 导入 facade 的兼容行为
  - 覆盖 locale 精确匹配与无歧义 fallback、同 source variants 与顺序、record warning、无有效 pair 和输入级 fatal。
  - 固定 canonical/legacy 分流、source digest、stage/commit/receipt，以及 `ImportReport` 中 imported、skipped、overwritten、errors 的现行关系。
  - 完成时，TMX 语法迁移前后的 Application 与 Store 可观察结果可逐项比较。
  - _Requirements: 5.1, 5.2, 5.3, 5.8, 5.9, 5.10, 7.4, 14.2, 14.3_

- [x] 1.3 固定术语资源 facade 的兼容行为
  - 覆盖只读导入 seam、事务导入、前两列兼容 preset、header allowlist、跳过计数、source-LWW、metadata 保留与 reload。
  - 记录 active worksheet 与 Excel 三态 adapter 的非回归边界，不把多 sheet 聚合成项目。
  - 完成时，CSV/XLSX row-selection 被替换前已有无副作用读取与原子提交的兼容基线。
  - _Requirements: 5.5, 5.6, 5.7, 5.8, 5.9, 5.11, 5.12, 5.13, 14.2, 14.3, 14.6_

- [x] 1.4 固定 normalized TM JSON CLI 与 gettext runner 的兼容行为
  - 覆盖单文件记录接受、跨文件 source-LWW、输出失败、singular PO/POT、runner 输出与现行异常行为。
  - 将坏行静默跳过、非字符串 speaker 置空、gettext partial/empty success 标记为设计已声明的版本化变化，而不是兼容期望。
  - 完成时，CLI 与 runner 的保留行为和有意退役行为都有独立断言。
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 5.4, 12.2, 12.3, 14.2, 14.3, 14.5_

- [x] 1.5 建立项目文档格式的可分发 golden fixtures
  - 为 LocalCAT JSON 数组/对象、source-only TXT、PO/POT singular profile 建立 valid、格式边界、encoding、limit 与 cancel 合成输入。
  - 仅为可构造尾部错误的格式增加 fatal-tail；不为没有 recoverable warning 合同的项目格式发明 warning 行为。
  - 完成时，四个 project-document 组合均可从受控 fixture 重现其接受、拒绝和边界结果。
  - _Requirements: 1.6, 3.1, 3.2, 3.5, 3.6, 3.7, 4.1, 4.2, 4.3, 4.4, 4.7, 4.8, 4.9, 15.1_

- [x] 1.6 建立 TMX 与 normalized TM JSON 的可分发 golden fixtures
  - 为两个 translation-memory 组合建立 valid、格式边界、record warning、fatal-tail、encoding、limit 与 cancel 合成输入。
  - 从已知 TMX 样本只提炼最小合成 fixture 或安全摘要，不提交未确认授权的原文件。
  - 完成时，locale、variant、坏记录、speaker 与输入失败均能在本地独立复现。
  - _Requirements: 1.7, 5.1, 5.2, 5.3, 5.4, 5.8, 5.10, 12.1, 12.2, 12.3, 12.4, 15.1_

- [x] 1.7 建立 CSV/XLSX 术语资源的可分发 golden fixtures
  - 覆盖 header-name、zero-based index、headerless、legacy preset、缺列、重复 header、同列、空行和不完整行。
  - 覆盖 XLSX active sheet、archive expansion、DTD/ENTITY member 与条件依赖缺失；多 sheet 样本只验证不聚合。
  - 完成时，两个 termbase 组合的 valid、record warning、格式边界、encoding、limit 与 cancel 均有确定性 fixture。
  - _Requirements: 1.8, 5.5, 5.6, 5.7, 5.11, 5.12, 5.13, 9.4, 9.5, 15.1_

- [x] 1.8 建立终态与迭代视图对抗测试骨架
  - 提供可注入 raw event、fatal tail、early close、consumer exception 和缺失 EOF 的测试 doubles。
  - 预置 iterator/materialized 等价断言所需的 records、issues、counts 与顺序比较器。
  - 完成时，后续 guarded session 实现可直接证明 provisional record 不会被伪成功授权。
  - _Requirements: 7.1, 7.2, 7.3, 7.7, 15.2, 15.3_

- [x] 1.9 建立 source、writer 与 commit 故障注入骨架
  - 提供原文件并发变化、root escape、non-regular file、snapshot stale、临时写入、fsync、replace 和 resource commit 故障点。
  - 所有夹具使用隔离目标并能断言失败前后字节及 receipt 状态。
  - 完成时，Source Boundary 和 Application staging 可在不触碰用户文件的条件下验证 fail-closed。
  - _Requirements: 3.9, 6.3, 6.4, 6.6, 9.6, 10.2, 15.3_

- [x] 1.10 建立依赖方向与延期边界的架构测试骨架
  - 准备 AST/import 检查，约束 Parser、Engine/Store、Application、composition 和 plugin implementation 的允许依赖方向。
  - 为 RPY 类型/token、workspace、多文档、chunk、同步和 TM storage authority 建立负向边界断言。
  - 完成时，后续每波迁移都能检测第二 parser、反向依赖或延期 Feature 越界。
  - _Requirements: 2.7, 2.8, 11.1, 11.4, 11.5, 11.6, 11.7, 11.8, 13.1, 13.2, 13.4, 13.5, 13.6, 15.4, 15.6, 15.7_

- [x] 2. Wave 1：实现中立 Parser Foundation
- [x] 2.1 冻结用途、格式选择与读取请求合同
  - 实现三个闭合用途、八个稳定格式标识、有界 hints、显式术语列选择与结构化 selection failure。
  - 校验 header-name/index selector、header policy、purpose/format/options 组合，不允许隐式用途或术语列默认。
  - 完成时，所有读取请求在消费输入记录前得到唯一且可验证的选择结果。
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.8, 1.9, 5.11, 5.12, 5.13_

- [x] 2.2 实现单输入中立记录与 canonical write DTO
  - 实现 ParsedDocument、ParsedSegment、ResourceRecord、target presence、translation state、RawSpeaker、metadata 与局部 ID 不变量。
  - 实现不依赖 EditorProject 的 canonical document write 表示，并保持一个输入内顺序与身份边界。
  - 完成时，中立对象不导入编辑器、Engine 或 Store 类型，且非法组合在构造边界确定性失败。
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 3.8, 12.1, 12.2, 12.3, 12.4, 12.5, 13.1_

- [x] 2.3 实现 capability 与 opaque round-trip token 合同
  - 区分 reader、validator、canonical write、source round-trip write、streaming、iterator、materialized 和格式 profile 能力。
  - 让 opaque token 绑定 provider/codec identity、版本、source fingerprint、format-state fingerprint 与不可解释 payload。
  - 完成时，foreign、stale、缺失或版本不兼容 token 均在打开写目标前结构化失败，Core 不解释 payload。
  - _Requirements: 2.8, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 11.7, 11.8_

- [x] 2.4 实现限制、诊断、验证报告与成功终态合同
  - 实现不可变 LimitProfile、有限 issue-code namespace、结构化 ParseIssue、确定性计数与截断状态。
  - 冻结 ValidationReport 和仅由 Foundation 签发的 TerminalSuccess 形状、invariants 与 snapshot/codec/profile 绑定。
  - 完成时，合同层可拒绝无界 metadata、未知 issue code、fatal success 或矛盾计数，且诊断不携正文。
  - _Requirements: 6.1, 6.2, 6.5, 7.3, 8.1, 8.3, 8.4, 9.3_

- [x] 2.5 实现 rooted regular-file opener
  - 从调用方 safe root 逐 component no-follow 打开目标，并证明最终对象为 root 内的 regular file。
  - 平台无法建立等价 rooted handle 时 fail closed，不回退到先 resolve 再普通 pathname open。
  - 完成时，outside-root、symlink/reparse、non-regular 与 read failure 均在消费内容前结构化失败。
  - _Requirements: 6.6, 9.6, 9.7_

- [x] 2.6 实现 sealed snapshot copy、fingerprint 与封存
  - 从已绑定 descriptor 单次复制实际解析字节，在输入上限内同步计算 digest，并在复制前后核对 fstat 稳定性。
  - 私有 snapshot 完整 flush 后封存，codec 不能按 pathname 重开 snapshot 或原文件。
  - 完成时，并发变化不产生记录，snapshot identity 与实际解析 bytes 的 digest/byte count 一致。
  - _Requirements: 6.3, 6.4, 9.6_

- [x] 2.7 实现 snapshot lease、stale 与取消原语
  - 为每次 validation/parse pass 签发绑定同一 sealed bytes 的 offset-0 cursor lease；XLSX 使用单活跃 seekable lease。
  - 在 bounded byte/record 边界传播取消，并在 snapshot 已释放时用 digest/profile 比较阻止 stale parse。
  - 完成时，重复 pass 不重读原路径，取消或 stale 均无 TerminalSuccess 且能安全清理所有 lease。
  - _Requirements: 6.3, 6.4, 7.2, 8.2, 9.6_

- [x] 2.8 实现 Foundation-owned guarded parse session
  - 只接受 codec raw events，并校验 header cardinality、用途对应的 event kind、局部 ID 唯一性、limits、counts 与真实 EOF。
  - raw codec 不得发布可提交终态；wrapper 只在自然 EOF、完整消费和 fatal_count 为零后单次签发成功终态。
  - 完成时，伪造 terminal、terminal 后事件、fatal tail、early close 与 consumer exception 都不能授权 commit。
  - _Requirements: 2.4, 7.1, 7.2, 7.3, 8.3_

- [x] 2.9 让 validation、iterator 与 materialized view 共用唯一 grammar
  - validation 和 materialization 都消费 guarded raw stream，不允许 codec 实现第二套 validator。
  - materialized helper 在 profile 上限内保留 records、issues、顺序与终态等价；超限返回稳定 fatal。
  - 完成时，同一 sealed snapshot 的三个视图具有可比较的结果，只有 verified terminal 能产生 SUCCESS report。
  - _Requirements: 6.1, 6.3, 6.4, 6.5, 7.7, 8.1, 8.3, 8.4, 14.4_

- [x] 2.10 实现用途感知 registry
  - 以 `(EffectivePurpose, FormatId)` 为唯一键注册不可变 descriptor，并支持有界 hint 缩小与 supported-combination 报告。
  - 重复键、用途不兼容、capability 不匹配和无兼容 codec 均确定性拒绝，注册顺序不改变结果。
  - 完成时，非继承实现可按行为合同注册，且 registry 不导入任何具体 codec。
  - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.9, 11.4, 11.6_

- [x] 2.11 实现外部 provider port 与空 composition seam
  - 定义显式 provider 注册协议、版本兼容和 missing/disabled 失败，不做动态扫描或 Core fallback。
  - composition seam 此时只接收 descriptor/provider；内建 codec 等其实现完成后再统一注册。
  - 完成时，模拟 plugin 可仅凭中立合同注册和返回 opaque capability，私有 token/sidecar 类型不进入 Core。
  - _Requirements: 2.8, 10.4, 10.5, 11.4, 11.7, 11.8_

- [x] 2.12 实现格式中立的原子 byte target writer
  - 在已绑定 target parent 内创建独占临时对象，完成 flush/fsync、验证和原子 replace 后才签发 WriteReceipt。
  - writer 只接受已序列化 canonical bytes，不拥有 LocalCAT schema 或 EditorProject 映射。
  - 完成时，任一注入失败都保留原目标字节且无 receipt，成功 receipt 绑定目标 identity 与 digest。
  - _Requirements: 3.9, 9.6, 10.2, 10.6_

- [x] 2.13 实现共享的 bounded JSON lexical preflight
  - 在标准库 materialization 前验证完整输入、字符串边界、结构深度、编码与 profile 限制。
  - 只提供 JSON 词法/结构安全，不解释 LocalCAT 或 normalized TM 字段语义。
  - 完成时，两类 JSON codec 可复用同一 preflight，并对截断、深度、超限和无效编码返回稳定 fatal。
  - _Requirements: 8.1, 8.3, 9.1, 9.2, 9.3_

- [x] 2.14 实现 XLSX archive 资源 preflight
  - 在 openpyxl 前枚举 archive members，限制 member 数、总展开字节与压缩比，并拒绝异常 ZIP 结构。
  - 不执行 macro、formula、external link 或 embedded object，只允许后续以 data-only 读取 cell value。
  - 完成时，archive 配额越界在 workbook 打开前以稳定 limit code 失败。
  - _Requirements: 8.1, 8.3, 9.5_

- [x] 2.15 实现 XLSX OPC XML 安全 preflight
  - 枚举并检查每个 OPC XML member 的 bounded well-formedness，禁用参数实体并在 DTD、ENTITY 或 external entity callback 出现时 fail closed。
  - 该检查不依赖 openpyxl 环境是否启用 defusedxml，也不把 keep_links 当成 XML 安全替代。
  - 完成时，包含内部或外部实体的 workbook 在 openpyxl 之前失败，安全 workbook 保持可读。
  - _Requirements: 9.4, 9.5_

- [x] 3. Wave 2：实现八个首波格式 codec
- [x] 3.1 (P) 实现 LocalCAT JSON reader
  - 按兼容规则读取数组/对象根、严格字段类型、trim、缺省 locale/name、ID 生成和整文档 fatal。
  - descriptor 单独发布 `localcat-json-v1` limits、issue allowlist 与 readable、non-streaming capability；canonical write 在任务 3.3 完成后启用。
  - 完成时，JSON golden 与现有读取 characterization 对记录、顺序、presence、RawSpeaker 和失败结果一致。
  - _Requirements: 1.6, 2.1, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 8.1, 9.1, 9.2, 9.3, 12.1, 12.2, 12.3, 12.4, 12.5, 13.1, 13.3_
  - _Boundary: LocalCAT Project Codec_
  - _Depends: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.13_

- [x] 3.2 实现 source-only TXT reader
  - 按顺序过滤 trimmed empty line，为每个非空行产生 source-only segment 和稠密局部 ID。
  - descriptor 单独发布 `line-text-v1` limits、issue allowlist 与 reader-only、streaming capability，不推断 target、确认状态或 speaker profile。
  - 完成时，TXT golden 与现有 facade 对 name、顺序、missing target、空 RawSpeaker、空文档 fatal 和无 writer 能力一致。
  - _Requirements: 1.6, 2.1, 2.3, 2.4, 2.5, 2.6, 3.6, 3.7, 3.10, 8.1, 9.1, 9.2, 9.3, 12.2, 12.5, 13.1, 13.3_

- [x] 3.3 实现 LocalCAT JSON v1 canonical serializer
  - 把中立 write DTO 确定性序列化为 schema version 1 UTF-8 JSON，按序输出 name、locales 与全部 segment 字段。
  - 只声明 canonical write，不声称保留源字节/排版；TXT 始终拒绝 writer 请求。
  - 完成时，序列化 bytes 可交给原子 byte writer，load → canonical save 兼容结果与失败注入满足现有保存合同。
  - _Requirements: 3.8, 3.9, 3.10, 10.2, 10.3, 10.6, 10.7, 14.1_

- [x] 3.4 (P) 实现 CSV 术语 codec 与显式列选择
  - 严格读取 UTF-8/UTF-8-BOM CSV，以 header 名或零基索引选择两列，并支持显式 legacy 0/1 preset。
  - header、空行、缺列与空值产生结构化 warning/skipped；保留物理行 ordinal、接受顺序和重复 source。
  - descriptor 发布 CSV 自身 limits、issue allowlist 与 reader-only capability。
  - 完成时，缺失/重复/同列选择在首条记录前 fatal，数据行不回退到其他列。
  - _Requirements: 1.8, 2.2, 2.4, 5.5, 5.8, 5.9, 5.11, 5.12, 5.13, 8.1, 9.1, 9.2, 9.3, 10.7_
  - _Boundary: Termbase CSV Codec_
  - _Depends: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9_

- [x] 3.5 实现 XLSX 术语 codec 与 active-sheet 边界
  - 在两层 preflight 通过后，以只读、data-only、禁 links/vba 的方式消费 active worksheet，并复用同一列选择与 row 语义。
  - 保持物理 row ordinal、warning/skipped、顺序和重复 source；报告 active-sheet-only capability，不聚合多 sheet。
  - descriptor 发布 XLSX limits、issue allowlist、条件依赖与 reader-only capability。
  - 完成时，安全 XLSX 与 CSV 列语义一致，危险或超限 workbook 在记录输出前失败。
  - _Requirements: 1.8, 2.2, 2.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.11, 5.12, 5.13, 8.1, 8.3, 9.4, 9.5, 10.7, 13.5_

- [x] 3.6 (P) 实现 TMX 安全 XML 流与 locale 选择
  - 在 sealed snapshot 上禁用 DTD/ENTITY/外部解析，按物理 TU 顺序迭代，并限制输入和单 segment 字符数。
  - 先做规范化 locale 精确匹配，再做无歧义 base fallback；malformed、无 TU 和输入级限制 fatal。
  - descriptor 发布 TMX v1 limits、issue allowlist 与 reader-only/streaming capability。
  - 完成时，XML/locale golden 在不访问网络的情况下产生确定性 fatal 或候选 pair。
  - _Requirements: 1.7, 5.1, 5.3, 8.1, 8.3, 8.5, 9.1, 9.4, 9.7, 10.7_
  - _Boundary: TMX Codec_
  - _Depends: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9_

- [x] 3.7 实现 TMX record 映射与 warning 语义
  - 对 inline XML、缺 pair、歧义 fallback 和超长 segment 发 record warning 并跳过，使用保留空洞的物理 TU ordinal 生成局部 ID。
  - 保持 accepted records 的顺序与同 source variants，不去重、不提交、不推断 CONTEXT/provenance。
  - 完成时，TMX golden 的 records、warnings、counts 和 terminal 与 resource policy 解耦且可重复。
  - _Requirements: 2.2, 2.4, 2.6, 5.2, 5.8, 5.9, 5.10, 9.2, 9.3_

- [x] 3.8 (P) 实现 normalized TM JSON 单输入 codec
  - 严格接受 UTF-8 数组根，复用 JSON preflight，并按物理 array ordinal 保留接受顺序与 ID 空洞。
  - source/target trim 后非空；坏行和非字符串 speaker warning/skip，missing/null speaker 映射空 RawSpeaker；空有效结果 fatal。
  - descriptor 发布自身 limits、issue allowlist、reader-only/非 streaming capability，不写 JSONL。
  - 完成时，fixture 能区分 record warning、fatal 与成功终态，重复 source 不在 codec 内折叠。
  - _Requirements: 1.7, 2.2, 2.4, 2.5, 2.6, 5.4, 5.8, 5.9, 8.1, 9.1, 9.2, 9.3, 10.7, 12.1, 12.2, 12.3, 12.4_
  - _Boundary: Normalized TM JSON Codec_
  - _Depends: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.13_

- [x] 3.9 (P) 实现 gettext quoted-string 状态机
  - 统一解析 comments、msgctxt、msgid、msgstr、continuation 与合法 escapes，保留行位置并拒绝无效语法/转义。
  - 严格接受 UTF-8/UTF-8-BOM；header 只允许缺失 charset 或 UTF-8，其他 charset fatal。
  - descriptor 发布 PO/POT 各自 limits、issue allowlist 与 project-document reader-only capability。
  - 完成时，multiline/escape/encoding/fatal-tail fixture 不再出现 partial 或 empty success。
  - _Requirements: 1.6, 4.3, 4.8, 4.9, 8.1, 9.1, 9.2, 9.3, 9.4, 10.7_
  - _Boundary: Gettext Codec_
  - _Depends: 2.1, 2.2, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9_

- [x] 3.10 实现 gettext document 与 singular-profile 语义
  - 把 singular entry 映射为有序 segment；空 msgid header 进入文档 metadata，comments/references/flags/previous value 保持不透明。
  - fuzzy 保留 target 并映射未确认，POT/未翻译 entry 保留 explicit empty target，plural 以 unsupported fatal 结束。
  - 使用输入位置确定性生成局部 ID，不解释 msgctxt 为 speaker 或 TM context。
  - 完成时，PO/POT golden 的 metadata、presence、状态、ID 和 plural failure 满足单输入文档合同。
  - _Requirements: 2.1, 2.3, 2.4, 2.5, 2.6, 4.1, 4.2, 4.4, 4.5, 4.6, 4.7, 4.9, 12.5, 13.1_

- [x] 4. Wave 3：注册内建 codec 并迁移 Application facade
- [x] 4.1 建立唯一内建 composition
  - 在唯一 composition root 注册八个用途/格式 descriptor，并核对每个 codec 自行发布的 capability、limit profile 与 issue allowlist。
  - 保持 registry 不导入具体 codec，不做动态扫描、用途 fallback 或 reader 推导 writer。
  - 完成时，支持矩阵与 Requirements 一致，非法组合和重复注册在读取前确定性失败。
  - _Requirements: 1.2, 1.4, 1.6, 1.7, 1.8, 1.9, 8.1, 10.1, 10.3, 10.7, 11.4, 11.7_

- [x] 4.2 (P) 迁移单文档项目打开 facade
  - 让现有打开入口显式选择 project purpose，经 sealed snapshot 与 guarded terminal 后映射为 EditorProject。
  - 只在完整成功后安装新 session；稳定 ProjectError/Controller code、字段缺省和 one-project/one-document 行为不变。
  - 完成时，生产入口不再独立解析 JSON/TXT，characterization 与 Qt 单项目路径保持通过。
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 11.2, 13.3, 14.1, 14.2, 14.3_
  - _Boundary: Editor Project Application Facade_
  - _Depends: 3.1, 3.2, 4.1_

- [x] 4.3 迁移单文档项目保存 facade
  - 由 Application 映射 EditorProject 到中立 write DTO，经 LocalCAT serializer 生成 bytes，再调用原子 byte writer。
  - 保持绝对 Path 返回、稳定保存错误、目标原子性和成功后才清 dirty；不把 schema 复制回 facade。
  - 完成时，读取与保存均只经过一个 LocalCAT 语法权威，TXT writer 仍在打开目标前拒绝。
  - _Requirements: 3.8, 3.9, 3.10, 10.2, 10.6, 11.2, 14.1, 14.2, 14.3, 14.4_

- [x] 4.4 (P) 迁移术语资源读取与事务导入 facade
  - 让只读和事务入口显式传入 legacy 0/1 preset，并在 verified terminal 后映射 accepted rows。
  - 保持 tuple/skipped/ImportFailure、source-LWW、原子写、metadata、reload 与 Store transaction owner；无有效行或 fatal 不写目标。
  - 完成时，resource importer 不再拥有第二份 header/列/active-sheet 规则。
  - _Requirements: 5.5, 5.6, 5.8, 5.9, 5.13, 7.4, 11.3, 14.2, 14.3, 14.4, 14.6_
  - _Boundary: Termbase Resource Application Facade_
  - _Depends: 3.4, 3.5, 4.1_

- [x] 4.5 (P) 迁移 Glossary consumer 并退出重复 row parser
  - 让既有 consumer 使用同一 termbase codec 结果，仅在成功终态后向现有 Engine 添加 accepted rows。
  - 移除吞异常/print 与 CSV/XLSX 私有语法；若无真实调用者则删除 loader，不在 Engine re-export Parser。
  - 完成时，LogicController/self-check 只剩 consumer mapping，生产中没有第二套术语 row-selection。
  - _Requirements: 5.5, 5.8, 11.3, 11.4, 14.2, 14.4, 14.5_
  - _Boundary: Glossary Application Adapter_
  - _Depends: 3.4, 3.5, 4.1_

- [x] 4.6 (P) 迁移 TMX 资源导入 facade
  - 让现有入口通过 TMX codec stage provisional records，仅在 verified terminal 后执行 canonical/legacy policy 与 Store transaction。
  - 保持 digest、variants/order、legacy LWW、ImportReport 映射和 warning 进入 errors 的现行可观察行为。
  - 完成时，私有 XML tokenizer 退出，fatal/cancel/commit failure 均不留下 partial store effect。
  - _Requirements: 5.1, 5.2, 5.3, 5.8, 5.9, 5.10, 7.4, 11.3, 14.2, 14.3, 14.4_
  - _Boundary: TMX Resource Application Facade_
  - _Depends: 3.6, 3.7, 4.1, 4.4_

- [x] 4.7 (P) 迁移 normalized TM JSON CLI/batch facade
  - 每个输入文件独立取得终态后才进入调用方 batch；目录发现、continue/stop、跨文件 source-LWW 与输出 policy 留在 CLI。
  - 将输出改为失败不截断目标；坏文件不再被解释为 partial success，成功 stdout 保持兼容。
  - 完成时，CLI 不再拥有单输入 JSON row parser，per-file 结果不会互相重标。
  - _Requirements: 5.4, 5.8, 5.9, 7.4, 7.5, 7.6, 11.3, 14.2, 14.3, 14.4, 14.5_
  - _Boundary: Normalized TM JSON CLI Facade_
  - _Depends: 3.8, 4.1_

- [x] 4.8 (P) 迁移 gettext runners 并退出 Engine parser
  - 将 translation/stress runners 显式选择 project purpose，并在终态后映射为既有 SourceUnit 使用形状。
  - 语法、plural 或编码 fatal 使 runner 明确失败；删除 POHandler，Engine 不 re-export Parser。
  - 完成时，valid singular 输出与三态处理保持兼容，生产代码只剩一个 gettext grammar。
  - _Requirements: 4.1, 4.2, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 11.2, 14.2, 14.3, 14.4, 14.5_
  - _Boundary: Gettext Runner Application Adapters_
  - _Depends: 3.9, 3.10, 4.1_

- [x] 5. Wave 4：收口、复验与治理同步
- [x] 5.1 验证 guarded terminal 与 Application commit 原子性
  - 对每个 iterator/resource 路径注入 fatal tail、early close、consumer exception、cancel、无 EOF 与 resource commit failure。
  - 断言 provisional records 全部销毁、verified terminal 单次签发、batch 文件结果独立，且失败没有 store/target 修改。
  - 完成时，终态和事务测试矩阵对所有受影响入口通过。
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 15.3_

- [x] 5.2 验证 source、snapshot、stale 与取消安全
  - 覆盖 safe-root、regular-file、sealed digest、原文件并发变化、validation/parse stale、lease 生命周期与有界取消点。
  - 断言失败或取消无 verified terminal、无残留 snapshot，且不回退到越过 root 的 pathname 访问。
  - 完成时，Source Boundary 故障矩阵在所有支持平台上通过或以 root-binding unavailable 明确 fail closed。
  - _Requirements: 6.3, 6.4, 6.6, 7.2, 8.2, 9.6, 9.7, 15.3_

- [x] 5.3 验证 limits、diagnostics、metadata 与编码边界
  - 参数化覆盖各 codec profile 的输入、字段、记录、materialization、issue、metadata 与结构深度边界。
  - 验证稳定 limit/encoding code、issue truncation、按 code 计数和安全摘要；确认 Gate D 100k 未被解释为 Parser limit。
  - 完成时，所有超限或无效编码均 fatal、无正文泄露、无 best-effort decoding，validation/terminal 携实际 profile/version。
  - _Requirements: 6.1, 6.2, 6.5, 8.1, 8.3, 8.4, 8.5, 8.6, 9.1, 9.2, 9.3_

- [x] 5.4 验证 JSON、XML 与 XLSX 专项安全
  - 覆盖 JSON lexical depth/完整输入、TMX DTD/ENTITY/外部解析，以及 XLSX archive expansion、每个 OPC XML member 与非执行型 cell 读取。
  - 断言 Parser 不访问网络、不执行 macro/formula/external link/object，危险输入在记录输出前失败。
  - 完成时，三类格式安全 fixture 均返回预期稳定结果，安全输入仍可正常解析。
  - _Requirements: 5.3, 8.1, 8.3, 9.4, 9.5, 9.7_

- [x] 5.5 验证 iterator、materialized 与 validation 等价
  - 在 materialization 限制内比较同 snapshot 的接受顺序、records、issues、counts 与 terminal，validation 复用同一 grammar。
  - 覆盖空输入、warning、fatal tail 和 materialization limit，不允许任何视图重标 provisional record。
  - 完成时，跨格式 metamorphic/property tests 对全部八个组合通过。
  - _Requirements: 2.1, 2.2, 2.4, 7.7, 15.2_

- [x] 5.6 验证内容、RawSpeaker、target presence 与单输入身份
  - 验证内部字符、大小写、局部 ID 空洞/唯一性、missing/explicit-empty target 和无持久确认状态时的派生结果。
  - 断言不做 Unicode normalize、escape、speaker 推断、项目级身份、folder/workbook 聚合、进度或同步语义。
  - 完成时，全部支持格式的中立记录只表达单输入事实，延期 owner 边界保持可观察。
  - _Requirements: 2.3, 2.5, 2.6, 2.7, 9.2, 9.3, 12.1, 12.2, 12.3, 12.4, 12.5, 13.1, 13.2, 13.4, 13.5, 13.6_

- [x] 5.7 验证 capability、writer 与 plugin token fail-closed
  - 核对八个内建 descriptor 的 reader/writer/streaming/profile 声明，以及 unsupported writer 在目标打开前失败。
  - 用模拟 provider 验证 opaque token 的 provider/codec/version/source/format-state identity；foreign、stale、missing、version mismatch 不修改目标。
  - 注入 canonical byte write 的 temp/fsync/replace failure，断言原目标保持不变且无成功 receipt。
  - 完成时，只有 LocalCAT JSON 可 canonical write，首波不存在内建 source-round-trip writer，plugin 私有状态不被 Core 解释。
  - _Requirements: 2.8, 3.9, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 11.7, 11.8, 15.3_

- [x] 5.8 删除平行语法并验证架构与延期边界
  - 删除已迁移入口中的独立 tokenization、unescaping、validation、row-selection 和 writer 语法，不保留 BaseParser 准入或 Engine re-export。
  - 运行 AST/import guards，证明 Parser 与 Engine/Store 互不导入，composition 是唯一内建注册点，Parser 不取得 Qt/workspace/TMStore/provider 权威。
  - 用负向守卫保持 multi-document、ProjectPackage、RPY 实现、XLIFF、TMX context/export、speaker profile、自动术语列推断、Office/PDF/OCR、chunk 与 sync 延期。
  - 完成时，生产树每种格式只有一个 grammar owner，延期能力只能进入其 owning Spec。
  - _Requirements: 11.1, 11.4, 11.5, 11.6, 11.7, 11.8, 13.2, 13.4, 13.5, 13.6, 14.2, 14.4, 15.4, 15.6, 15.7_

- [x] 5.9 执行完整 facade 与相邻功能兼容回归
  - 运行 editor project、Controller/Qt 单项目、TMX/resource importer、legacy TM、termbase reload/LKG、Excel 三态、normalized CLI、runner 和 self-check suites。
  - 对已声明的版本化变化使用新合同断言；其余失败必须修复，不以 legacy facade 为由保留第二 parser。
  - 完成时，兼容矩阵全部通过，或每个差异都已在 Requirements/Design 的既有变化表中有对应依据。
  - _Requirements: 13.3, 14.1, 14.2, 14.3, 14.5, 14.6, 15.5_

- [x] 5.10 由 Integration TM owner 重验 Parser 触发的 current-source evidence
  - 基于实际 Parser diff 判定是否命中 resource importer、TM adapter、Engine 或 Feature 5 source fingerprint；未命中时形成可复核的 no-impact 结论。
  - 命中时由 Integration TM owner 在当前源码上重跑 Gate C、acceptance、fault 与 release suites，并按实际 source roots 判断是否需要 Gate D；Parser 不复制或自签 evidence。
  - 完成时，受影响的 TM evidence 与当前源码 fingerprint 一致，100k Gate D 仍保持原性能语义 owner。
  - _Requirements: 8.6, 11.3, 14.6, 15.5_
  - _Depends: 5.8, 5.9_

- [x] 5.11 由 Governance owner 同步 Parser runtime 的真实派生事实
  - 仅在 runtime 文件树和依赖方向真实落地后，同步 Parser 相关 structure/tech/roadmap 索引；不提前写入 RPY、多文档、chunk、sync 或 TM store 维护线。
  - 保持 ADR-015 与本规格 ownership，任何新边界变化返回其 owning Spec/ADR，不把实施记录写成新权威。
  - 完成时，Steering diff 只描述已存在的 Parser 组件、依赖和交接触发器，并通过治理一致性检查。
  - _Requirements: 11.1, 11.4, 14.6, 15.4, 15.7_
  - _Depends: 5.8, 5.10_

- [x] 5.12 执行 Parser Feature fresh completion 验证
  - 从干净环境运行全部 parser contracts、golden、fault、AST、facade compatibility 与相邻回归，不复用旧测试结果。
  - 机械核对八个组合、所有 stable codes/profile、单一 grammar exit 条件和本任务图的 Requirements 覆盖。
  - 完成时，Parser runtime、下游 TM evidence 和治理派生事实形成同一 current-source 闭环，且不存在未解释失败。
  - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5_
  - _Depends: 5.11_
