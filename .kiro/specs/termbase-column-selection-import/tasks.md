# Implementation Plan

- [ ] 1. 建立术语 preview 合同与 Foundation capability
- [ ] 1.1 冻结有界 preview DTO 与 capability
  - 在 `parser_contracts.py` 增加 request/column/report、256列与256字符边界，以及 `termbase_column_preview` capability。
  - 保证 report 绑定 source/codec/format，truncation 与 total count 一致，Parser合同不导入Editor/Qt/Store。
  - 完成时，合同正负边界与metadata/body-safe约束测试通过。
  - _Requirements: 1.3, 1.6, 1.7, 5.1, 5.4_

- [ ] 1.2 扩展 registry pinned preview behavior
  - 验证声明 capability 的 reader factory product 实现 structural preview protocol；固定 descriptor与bound method。
  - property/exception/错误report类型或identity不匹配全部body-safe fail closed。
  - 完成时，内建与ProviderBinding hostile factory测试通过，未声明能力在source open前拒绝。
  - _Requirements: 5.1, 5.2, 5.3_
  - _Depends: 1.1_

- [ ] 1.3 在唯一 Application surface 编排 sealed preview
  - 先选择/验证factory，再创建rooted sealed snapshot与descriptor lease；调用pinned preview后验证source/codec/format identity并释放snapshot。
  - 失败或取消不得遗留lease/snapshot，不暴露Foundation私有对象。
  - 完成时，source安全、lifecycle、cancel与factory-before-open测试通过。
  - _Requirements: 1.1, 1.5, 1.6, 3.1, 5.3_
  - _Depends: 1.2_

- [ ] 2. 实现 CSV/XLSX codec-owned 列 preview
- [ ] 2.1 实现 CSV 首行 preview
  - 复用现有strict UTF-8-SIG line decoder、descriptor-bound csv reader、`_header_text`与legacy header识别。
  - 保留空/重复header；发布物理列count、前256列与truncation，不读取Store或推断语言。
  - 完成时，quoted/multiline/BOM/empty/invalid/long/257列和全局csv limit恢复测试通过。
  - _Requirements: 1.2, 1.3, 1.5, 1.7, 2.5, 2.6_
  - _Depends: 1.1_

- [ ] 2.2 实现 XLSX active-sheet preview
  - 在同一lease上先运行既有OPC/XML preflight，再以非执行flags打开workbook并读取active sheet首行。
  - 返回有界sheet name和列候选；其他Sheet不聚合，所有workbook/lease在失败路径关闭。
  - 完成时，active/no-active、多Sheet、危险OPC、dependency与truncation测试通过。
  - _Requirements: 1.2, 1.4, 1.5, 1.7, 5.5_
  - _Depends: 1.1_

- [ ] 3. 闭合 Application/Controller 显式选择与 stale gate
- [ ] 3.1 增加 Qt-safe preview/selection contracts
  - 在 `editor_contracts.py` 增加preview column/report、Qt-safe source identity、header mode、selection，并扩展ImportRequest可选字段。
  - 显式selection验证不同非负索引与完整preview identity；TMX不得携带term selection，None保持compat。
  - 完成时，typed contract测试覆盖invalid/same-column/identity/compat。
  - _Requirements: 2.1, 2.2, 2.7, 3.1, 4.4_

- [ ] 3.2 扩展 resource importer preview与显式 staging
  - 将Parser report映射成Qt-safe内容；显式selection映射为同一Parser `TermbaseColumnSelection`。
  - 正式stream前精确比较完整source identity，并在同一sealed snapshot上重新取得codec preview、核对可见列数；stale或列事实不符时不产生records、不进入Store。
  - `read_legacy_termbase_import()`默认签名/行为继续使用前两列preset。
  - 完成时，FIRST_ROW/NO_HEADER、stale、parser fatal与legacy import测试通过。
  - _Requirements: 2.3, 2.4, 2.7, 3.1, 3.2, 3.3, 5.6, 6.1_
  - _Depends: 1.3, 2.1, 2.2, 3.1_

- [ ] 3.3 增加 Controller preview command并保持事务
  - preview只读取且不持有Controller TM lock跨UI；import继续在既有资源锁/TermbaseStore transaction内提交。
  - terminal后才prepare/commit，失败保持LKG和ImportReport语义。
  - 完成时，preview零副作用、stale零prepare、commit failure/LKG与reload测试通过。
  - _Requirements: 3.3, 3.4, 3.5, 4.4, 6.3_
  - _Depends: 3.2_

- [ ] 4. 实现 Qt 异步 preview 与列选择对话
- [ ] 4.1 增加 preview worker与busy lifecycle
  - 术语文件选择后启动独立QThread；preview/import共享单busy门，完成/取消/异常后释放worker并恢复控件。
  - TMX locale路径和ImportWorker保持不变。
  - 完成时，QtTest覆盖异步成功/失败/并发拒绝/关闭保护/worker释放。
  - _Requirements: 4.1, 4.5, 4.6_
  - _Depends: 3.3_

- [ ] 4.2 增加列选择对话并提交typed request
  - 显示格式、active sheet、source/target combo与首行表头复选框；默认0/1及legacy detection。
  - 取消零import；确认后携selection与preview identity调用既有start_import，反馈仍用ImportReport。
  - 完成时，默认/修改/同列阻止/truncated/cancel/feedback tests通过。
  - _Requirements: 2.1, 2.2, 2.5, 4.2, 4.3, 4.4, 4.7_
  - _Depends: 4.1_

- [ ] 5. 完成架构、兼容与治理闭环
- [ ] 5.1 对抗验证单一grammar、无副作用与边界
  - 增加AST/import guards，证明Qt/Controller不导入Parser具体codec或csv/openpyxl，composition仍是唯一内建注册点。
  - 注入factory、preview、stale、parser fatal、consumer、commit故障，验证无Store/target修改。
  - 完成时，合同/codec/Application/Qt mutation-sensitive矩阵通过。
  - _Requirements: 3.3, 3.5, 5.2, 5.3, 5.4, 5.6, 6.2, 6.3_

- [ ] 5.2 执行 fresh completion并同步真实Steering
  - 运行Parser contracts/registry/composition/termbase、resource importer、Controller term import、Qt settings与architecture suites。
  - 只把已落地的explicit column preview/selection写入tech/roadmap；不声称多Sheet、自动语言匹配、ResourcePackage或项目XLSX。
  - 完成时，本任务无未勾项、工作树只有预期路径、fresh回归无未解释失败。
  - _Requirements: 6.1, 6.2, 6.4, 6.5_
  - _Depends: 5.1_
