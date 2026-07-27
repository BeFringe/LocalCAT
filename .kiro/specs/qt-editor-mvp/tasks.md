# 实施计划

- [ ] 1. 建立编辑器基础契约与项目能力

- [x] 1.1 建立 UI 依赖清单、不可变编辑契约和测试入口
  - 定义段落、项目、语言资源、TM/术语建议、导入统计和写回结果的数据契约，并保证跨层值对象不可变
  - 增加独立 UI 依赖文件，固定经验证的 PySide6 与 XLSX 读取依赖，不让核心模块强制依赖 Qt
  - 建立标准库测试发现入口，使后续模块能在临时目录中隔离验证
  - 完成后，纯 Python 环境可导入全部编辑契约，UI 依赖安装命令明确且不会改变现有核心启动方式
  - _Requirements: 2.1, 2.3, 3.2, 4.2, 5.2, 6.2, 7.2, 8.2, 8.3_
  - _Boundary: EditorContracts, UI Runtime Prerequisites_

- [x] 1.2 实现 JSON/TXT 项目读取、示例项目和原子保存
  - 支持对象数组与带 segments 根对象的 JSON 项目，保留 source、target、speaker 和 confirmed
  - 将 TXT 每个非空行转换为待翻译段落，并生成稳定且唯一的段落 ID
  - 提供内置示例项目，保证首次启动可立即体验真实编辑流程
  - 保存为带 schema 版本的 UTF-8 JSON，并以同目录临时文件和原子替换避免半写入
  - 完成后，JSON/TXT 可在临时目录往返读写，无效输入不会覆盖已有有效项目
  - _Requirements: 1.2, 2.1, 2.2, 2.3, 2.4, 2.6, 8.2_
  - _Boundary: EditorProjectCodec_
  - _Depends: 1.1_

- [x] 1.3 修复已知逻辑自检漂移并建立 Excel 适配器绿色基线
  - 只把 `logic_controller.py` 自检输入修正为当前默认 TM 中真实存在的句子，不改变业务方法、三态字段或 TM 优先规则
  - 用临时 XLSX 执行 `excel_adapter_openpyxl.run_file_mode_benchmark`，验证 TM_HIT、TERMS_FOUND 和 NO_MATCH 格式仍可写入结果工作簿
  - 对交互式 `excel_adapter.py` 做编译/import 边界检查，确认没有 Qt 依赖且仍只通过 LogicController 访问 Engine
  - 完成后，LogicController 自检和 headless Excel adapter smoke 都返回 0，已知失败不再拖到最终验证阶段
  - _Requirements: 8.4_
  - _Boundary: LogicController Regression Fixture, Excel Adapter Contract_
  - _Depends: 1.1_

- [ ] 2. 构建本地语言资源持久化与导入

- [x] 2.1 实现资源清单、默认资源引导和状态持久化
  - 使用带 schema 版本的本地清单保存资源名称、类型、绝对路径、Active、Lookup 和 Update
  - 首次运行时把仓库现有 `tm.jsonl` 与 `terms.csv` 注册为默认活动资源
  - 新建资源时在受控应用数据目录创建空 JSONL 或 UTF-8-SIG CSV，不接受任意输出路径
  - 配置更新采用原子写入，非活动资源保留文件和状态，重建仓库后能恢复全部设置
  - 完成后，资源的新建、启停和 Lookup/Update 修改在进程重启模拟后保持一致
  - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.6, 8.1, 8.2_
  - _Boundary: ResourceRepository_
  - _Depends: 1.1_

- [x] 2.2 实现安全的 TMX 与 CSV/XLSX 术语表原子导入
  - TMX 导入支持用户指定语言对、locale 规范化、重复 source 后写胜出和结构化统计
  - 拒绝超过 100 MB、DTD/ENTITY、缺少语言对或整体无效的 TMX；带行内标签单元明确跳过并记录
  - 术语表导入读取 CSV/XLSX 前两列、识别常见表头、跳过空行并按源术语后写胜出
  - 只有完整解析成功才合并并原子替换目标 JSONL/CSV，失败保留原资源字节不变
  - 完成后，真实临时 TMX、CSV 和 XLSX 可导入并返回 imported/skipped/overwritten/errors，破坏性输入不改变目标
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 8.1, 8.2_
  - _Boundary: ResourceImporter_
  - _Depends: 1.1_

- [ ] 3. 实现编辑会话与资源协调逻辑

- [x] 3.1 实现项目会话、段落编辑导航和并列建议查询
  - EditorController 打开项目或示例后维护当前段、未保存状态、确认状态和完成进度
  - 修改已确认译文时恢复待确认，上一段、下一段和未确认导航不丢失当前编辑
  - 按活动且启用 Lookup 的资源分别构建 TM 与术语查询集合，并同时返回精确 TM 和术语建议
  - 非活动或关闭 Lookup 的资源不得产生建议，无匹配时返回结构化空集合
  - 完成后，纯逻辑测试能切换段落、编辑译文、过滤未确认段，并观察资源开关立即改变建议结果
  - _Requirements: 3.1, 3.4, 3.5, 4.1, 4.2, 4.4, 4.5, 5.1, 5.6_
  - _Boundary: EditorController Session and Lookup_
  - _Depends: 1.2, 2.1_

- [x] 3.2 实现确认回写、建议应用和术语添加
  - 非空译文确认时写入所有活动且启用 Update 的 TM，全部成功后才确认并前往下一未确认段
  - 提供应用 TM 建议和插入术语目标词的结构化方法，不自动确认
  - 将新术语持久化到一个活动且启用 Update 的术语表；无可写资源或空输入时明确失败
  - 完成后，确认译文重新加载 TM 后仍可查询，应用建议不确认，新术语写入后在当前句立即可查询
  - _Requirements: 3.2, 3.3, 3.6, 4.3, 5.3, 5.4, 5.5_
  - _Boundary: EditorController Editing Writes_
  - _Depends: 2.1, 3.1_

- [x] 3.3 实现资源变更、导入协调和引擎安全热重载
  - EditorController 提供资源新建、状态修改和 TMX/术语导入的单一公开入口
  - 资源新建或 Active/Lookup/Update 变化后重建查询与写回集合
  - 导入成功后热重载对应引擎；重载失败时保留上一组可用实例并返回结构化错误
  - 完成后，关闭 Lookup 会使建议消失，重新启用或成功导入后建议无需重启即可出现
  - _Requirements: 4.1, 4.5, 5.1, 6.3, 6.4, 7.6_
  - _Boundary: EditorController Resource Operations and Reload_
  - _Depends: 2.2, 3.2_

- [ ] 4. 构建语言资源设置前端

- [x] 4.1 实现活动/非活动资源表、新建资源和状态编辑
  - 齿轮设置按活动与非活动分组显示名称、类型、本地路径和 Active/Lookup/Update 控件
  - 新建流程收集资源名与 TM/术语表类型，并通过 EditorController 创建资源
  - checkbox 只调用控制器公开方法，对话框不直接写资源清单或重建引擎
  - 完成后，创建和状态修改会刷新资源表；关闭再打开设置仍显示持久化结果且当前项目编辑保持不变
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_
  - _Boundary: QtSettingsDialog Resource Management_
  - _Depends: 3.3_

- [x] 4.2 实现设置中的 TMX/术语表选择与后台导入反馈
  - TM 资源提供精确 TMX 过滤器与源/目标语言输入，术语资源只展示 CSV/XLSX 过滤器
  - worker 线程调用 EditorController 导入接口，主线程只更新忙碌、禁用和统计状态
  - 成功显示 imported/skipped/overwritten/errors；失败显示可操作原因且允许重试
  - 完成后，TMX、CSV/XLSX 成功与失败路径均可从设置触发且界面在导入期间仍处理事件
  - _Requirements: 7.1, 7.2, 7.3, 7.5_
  - _Boundary: QtSettingsDialog Import Worker_
  - _Depends: 4.1_

- [ ] 5. 构建双栏主编辑器

- [x] 5.1 实现专业窗口骨架、空状态、项目打开保存和响应分栏
  - 构建深蓝顶栏、项目/语言信息、段落导航、双栏当前段、右侧页签、进度和状态栏
  - 空状态提供打开 JSON/TXT 和载入示例；打开/保存成功后更新标题与状态
  - splitter 设置合理 stretch 与最小尺寸，窗口缩放后段落、编辑和建议主区域仍可见可读
  - 无效文件保留当前会话；打开其他项目或关闭前调用未保存保护选择
  - 完成后，示例和真实项目均可打开、编辑并保存，改变窗口尺寸不会压扁任一主区域到不可操作
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - _Boundary: QtEditorWindow Shell and Project Flow_
  - _Depends: 4.2_

- [x] 5.2 实现 TM/术语建议页、源文安全高亮和术语添加
  - TM 页显示源文、译文、资源和 100% 标记；术语页显示源/目标词与所属资源
  - 源文先 HTML 转义再按非重叠范围高亮术语，项目文本不能注入标记
  - 应用 TM 替换目标译文，应用术语插入光标位置，二者均不自动确认
  - 添加术语调用控制器并立即重新查询；无可写术语表时显示明确原因
  - 完成后，测试资源的 TM/术语可见可应用，新增术语在当前段不重启即可出现
  - _Requirements: 4.2, 4.3, 4.4, 5.2, 5.3, 5.4, 5.5, 5.6_
  - _Boundary: QtEditorWindow Suggestions and Termbase_
  - _Depends: 5.1_

- [x] 5.3 实现编辑确认、导航、过滤、快捷键和状态同步
  - 译文变化同步会话并撤销旧确认；确认按钮与 Ctrl+Enter 共用同一控制器动作
  - 段落列表、上一段、下一段和未确认过滤在切换前保留当前编辑
  - 确认成功更新进度与状态并移动下一未确认段，失败保持当前段并显示写回错误
  - 增加 Ctrl+O、Ctrl+S、Ctrl+Enter、Alt+Up、Alt+Down 和 Ctrl+,，并提供可发现按钮/提示
  - 完成后，仅用键盘可完成打开、编辑、确认和段落导航，列表、编辑器和状态栏保持一致
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 8.6_
  - _Boundary: QtEditorWindow Editing Workflow_
  - _Depends: 5.2_

- [x] 5.4 显式接通设置变更、控制器热重载和当前段建议刷新
  - 资源状态或导入完成后由对话框发出 resources_changed，主窗口重新请求 SuggestionBundle
  - 验证 Qt 层不直接访问 ResourceRepository、TMEngine 或 GlossaryEngine
  - 关闭设置后恢复当前段与未保存译文，新导入内容立即出现在对应建议页
  - 完成后，设置中关闭 Lookup 会让当前建议消失，重新启用或导入后建议无需重启即可出现
  - _Requirements: 4.1, 4.5, 5.1, 6.4, 7.6_
  - _Boundary: Qt Frontend to EditorController Integration_
  - _Depends: 4.2, 5.3_

- [ ] 6. 完成运行时闭环与项目同步

- [x] 6.1 实现无 Qt bootstrap、依赖诊断和 offscreen 冒烟入口
  - `qt_editor.py` 只使用 stdlib 解析参数并延迟导入 Qt 窗口模块
  - 模拟缺少 PySide6 时返回非零并输出 `python -m pip install -r requirements-ui.txt`，不输出未处理 traceback
  - 模拟 XLSX 导入缺少 openpyxl 时返回可操作错误，同时 CSV/TMX 路径不受影响
  - `--smoke-test` 在 offscreen 环境创建窗口、载入示例、报告标题与段落数并正常退出
  - 完成后，依赖缺失和依赖完整两种启动路径都有自动化证据
  - _Requirements: 1.1, 1.2, 7.3, 8.3, 8.5_
  - _Boundary: QtBootstrap and Dependency Diagnostics_
  - _Depends: 5.4_

- [x] 6.2 建立关键 GUI 交互测试和五个既有入口回归
  - 使用 QtTest 覆盖齿轮设置、TM 建议、术语插入、新增术语立即查询和 Ctrl+Enter 确认导航
  - 调整窗口为小/大尺寸并断言三个主区域保持可见和非零可用尺寸
  - 明确运行 `glossary_engine.py`、`tm_engine.py`、`logic_controller.py`、`stress_runner.py`、`translation_runner.py`、新增 unittest 和 Qt offscreen smoke
  - 再次运行临时 XLSX headless adapter smoke，并对交互式 Excel adapter 做编译/import 边界检查
  - 完成后，所有命令返回 0，GUI 关键控件真实接收事件且 Excel 适配器契约有独立证据
  - _Requirements: 1.5, 3.3, 4.3, 5.3, 5.4, 6.1, 8.4, 8.5, 8.6_
  - _Boundary: GUI Verification, LogicController Regression Fixture_
  - _Depends: 6.1_

- [ ] 6.3 更新 steering 与 README 并执行端到端资源闭环
  - 按用户明确要求与 steering 同步机制的例外，更新 structure/tech/product 和 README
  - 保留用户已有 Feature 3 标签和已推送状态修改，再补充 Qt MVP 架构、依赖、启动、格式、限制、验证和 `ui-mvp` 分支
  - 使用临时项目与真实临时 TMX/CSV/XLSX 完成“设置导入 → 当前段建议 → 确认写回 → 保存 → 重载查询”闭环
  - 对照 border.md 更新每步状态与归档检查，不把未实现能力写成已完成
  - 完成后，README 命令可复制运行，规格、steering、代码和实际验证证据一致
  - _Requirements: 2.3, 3.6, 4.1, 5.1, 7.2, 7.3, 7.6, 8.1, 8.4, 8.5, 8.7_
  - _Boundary: Integration Validation, Steering, README_
  - _Depends: 6.2_

## Implementation Notes

- 基线记录：实施前 `logic_controller.py` 自检因夹具句子不在当前 `tm.jsonl` 中失败；其他核心脚本通过。
- 权威顺序：当前用户要求 → 最新 steering → 当前分支可运行契约 → 本规格；遗留 MCA playbook 仅作历史参考。
