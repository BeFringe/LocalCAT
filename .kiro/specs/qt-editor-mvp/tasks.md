# 实施计划

- [x] 1. 建立编辑器基础契约与项目能力

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

- [x] 2. 构建本地语言资源持久化与导入

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

- [x] 3. 实现编辑会话与资源协调逻辑

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

- [x] 4. 构建语言资源设置前端

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

- [x] 5. 构建双栏主编辑器

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

- [x] 6. 完成运行时闭环与项目同步

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

- [x] 6.3 更新 steering 与 README 并执行端到端资源闭环
  - 按用户明确要求与 steering 同步机制的例外，更新 structure/tech/product 和 README
  - 保留用户已有 Feature 3 标签和已推送状态修改，再补充 Qt MVP 架构、依赖、启动、格式、限制、验证和 `ui-mvp` 分支
  - 使用临时项目与真实临时 TMX/CSV/XLSX 完成“设置导入 → 当前段建议 → 确认写回 → 保存 → 重载查询”闭环
  - 对照 border.md 更新每步状态与归档检查，不把未实现能力写成已完成
  - 完成后，README 命令可复制运行，规格、steering、代码和实际验证证据一致
  - _Requirements: 2.3, 3.6, 4.1, 5.1, 7.2, 7.3, 7.6, 8.1, 8.4, 8.5, 8.7_
  - _Boundary: Integration Validation, Steering, README_
  - _Depends: 6.2_

- [x] 7. 修复真实语言资源设置回归

- [x] 7.1 归一化界面资源类型并覆盖两种真实创建路径
  - 在语言资源创建的受控输入边界接受 ResourceKind 或其受支持字符串值，未知值仍明确拒绝
  - 用 PySide6 QComboBox 的真实 currentData 返回值覆盖 TM 与术语表创建，避免只直传 Enum 的假阳性
  - 完成后，从新建对话框选择任一种资源都能创建正确扩展名的活动资源且立即加载
  - _Requirements: 6.3, 6.7_
  - _Boundary: ResourceRepository, EditorController Resource Creation_

- [x] 7.2 让设置表格在中文内容和窗口拉伸下保持可读
  - 类型与导入列提供覆盖中文按钮和单元格内容的最小宽度
  - 名称与本地路径作为弹性列分配窗口新增空间，Active/Lookup/Update 保持紧凑
  - 完成后，“翻译记忆库”和“导入术语表”完整显示，放大对话框时名称/路径列宽之和增加
  - _Requirements: 6.2, 6.8_
  - _Boundary: QtSettingsDialog Resource Table_
  - _Depends: 7.1_

- [x] 8. 建立桌面启动和可恢复项目生命周期

- [x] 8.1 持久化最近项目、最后段落和显示偏好
  - 使用版本化本地 JSON 原子保存最多十个最近项目、稳定 segment id、索引回退和显示偏好
  - EditorController 在打开、导航、确认、保存和退出项目时协调断点，不把工作区状态写进翻译项目
  - 无效断点回到首段，损坏或失效最近记录不阻止应用启动
  - 完成后，新控制器实例重新打开同一路径时恢复上次段落，最近顺序与偏好均可重建
  - _Requirements: 9.2, 9.3, 9.4, 9.6, 10.7_
  - _Boundary: WorkspaceStateRepository, EditorController Project State_

- [x] 8.2 提供项目菜单、最近项目、退出项目和桌面启动入口
  - 顶栏项目入口包含打开、最近项目、退出当前项目和退出应用，切换与退出统一经过未保存保护
  - 退出当前项目后返回可操作空状态；最近项目动作可恢复项目和段落
  - stdlib bootstrap 支持安装 Linux 用户应用菜单入口，不要求启动器先导入 PySide6
  - 完成后，用户可从应用菜单启动 LocalCAT，并在 GUI 内完成项目切换、退出项目与最近项目重开
  - _Requirements: 2.5, 8.3, 8.6, 9.1, 9.2, 9.4, 9.5, 9.6, 9.7_
  - _Boundary: QtBootstrap, QtEditorWindow Project Lifecycle_
  - _Depends: 8.1_

- [x] 9. 实现段落密度和浏览校对工作区

- [x] 9.1 实现左栏紧凑等高与自动换行切换
  - 紧凑模式保持稳定单行高度、摘要省略和完整源文悬停提示
  - 自动换行模式展示完整源文并在栏宽变化后重算可读行高
  - 切换密度不改变当前段、当前译文或确认状态，并保存偏好
  - 完成后，同一长段在两种模式呈现不同高度，当前会话内容保持一致
  - _Requirements: 10.1, 10.2, 10.3, 10.7_
  - _Boundary: QtEditorWindow Segment Navigation_
  - _Depends: 8.2_

- [x] 9.2 实现只读双语浏览校对页和同段返回编辑
  - 浏览校对页按段显示完整源文、最新译文和确认状态并自动换行
  - 编辑/浏览切换共享同一个 EditorController 会话，不复制或覆盖项目状态
  - 双击浏览行回到编辑模式并定位同一段；编辑或确认后再次浏览显示最新值
  - 完成后，长篇项目可连续浏览双语全文，并从任意浏览行精确回到编辑器
  - _Requirements: 10.4, 10.5, 10.6, 10.7_
  - _Boundary: QtEditorWindow Browse Review_
  - _Depends: 9.1_

- [x] 9.3 用真实长篇项目和 OWNattempt 记忆库完成增量验收
  - 使用 `po/卷一_引.json` 与 `RpySeriesExtract/OWNattempt.tmx` 验证创建 TM、导入 en-US → zh-CN、当前项目精确命中和应用建议
  - 覆盖项目断点恢复、最近项目、退出项目、两种段落密度、浏览校对双击返回和设置列宽
  - 运行完整 unittest、offscreen smoke、五个既有入口并更新 README、steering、border 与验收报告
  - 完成后，真实 TMX 导入 165 条且对真实项目产生 112 个精确命中，新增 UI 旅程和既有契约全部通过
  - _Requirements: 6.7, 6.8, 7.2, 7.6, 8.4, 8.5, 8.7, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_
  - _Boundary: Integration Validation, README, Steering_
  - _Depends: 9.2_

## Implementation Notes

- 基线记录：实施前 `logic_controller.py` 自检因夹具句子不在当前 `tm.jsonl` 中失败；其他核心脚本通过。
- 权威顺序：当前用户要求 → 最新 steering → 当前分支可运行契约 → 本规格；遗留 MCA playbook 仅作历史参考。
- 真实 UI 根因：PySide6 QComboBox 把 str Enum 的 itemData 还原为普通 str，资源创建边界必须显式归一化。

- [x] 10. 修复资源治理、真实 TMX 可用性和桌面入口

- [x] 10.1 实现安全资源删除与设置更多菜单
  - ResourceRepository 对托管文件采用 tombstone + 清单原子提交 + 失败回滚，外部资源只取消登记
  - EditorController 删除后热重载资源集合；设置行更多菜单显示删除并在确认后刷新
  - 导入期间禁用删除；取消确认不修改清单或文件
  - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_
  - _Boundary: ResourceRepository, EditorController, QtSettingsDialog_

- [x] 10.2 恢复 MateCat/Ren'Py TM 的严格精确兼容
  - 新增 Qt 无关纯函数，仅对安全 speaker token 生成 `speaker "text"` 查询别名
  - 普通 exact 优先；兼容命中只解包同 speaker 的目标并保持 suggestion.source 为当前正文
  - 使用真实 `po/卷一_引.json` 和现有 LocalCAT 已导入资源证明命中显著恢复，无法无歧义解包时保持原文
  - _Requirements: 4.1, 4.2, 4.3, 11.7, 11.8_
  - _Boundary: RenPyTMCompat, EditorController_

- [x] 10.3 解释内部资源路径并修复桌面图标安装
  - 设置页明确显示 TMX/术语表导入后合并到 JSONL/CSV 内部存储，保留完整统计反馈
  - `.desktop` 通过 freedesktop 主题名引用 `LocalCAT-logo-silver.png`，包含工作目录/启动类并刷新桌面数据库
  - QApplication 使用同一窗口图标；安装后的真实桌面入口通过 validate、缓存刷新和 GUI 启动检查
  - _Requirements: 7.2, 7.6, 9.1, 11.6, 11.9_
  - _Boundary: QtSettingsDialog, QtBootstrap_

- [x] 10.4 完成规格对照、真实数据和全量回归
  - 运行新增 repository/controller/Qt/bootstrap 测试、完整 unittest、offscreen smoke 和既有核心入口
  - 更新 validation、README/steering（如事实变化）以及 Parser/Feature 5 的权威关系说明
  - 对照 Requirement 11 逐项验收，不把 MyMemory context、SQLite 或通用 RPY Parser 宣称为已完成
  - _Requirements: 8.4, 8.5, 8.7, 11.1–11.9_
  - _Boundary: Verification, Documentation_
  - _Depends: 10.1, 10.2, 10.3_

- [x] 11. 完成 Checkpoint M：修复 Qt 跨平台编辑交互回归

- [x] 11.1 修正现有确认与段落导航快捷键
  - 让确认动作响应平台主修饰键与主 Return，并保留需要支持的数字键盘 Enter 等价入口；macOS 物理 Control + Return 继续由译文编辑器处理换行
  - 上一段、下一段使用不劫持译文编辑的可发现绑定，按钮提示从实际 `QKeySequence` 的 NativeText 生成，不再硬编码 Ctrl、Alt 或 macOS 键名
  - 使用真实 Qt 按键事件覆盖主 Return、数字键盘 Enter、macOS Control + Return 和上一段/下一段，禁止以 `activated.emit()` 代替行为验收
  - 完成后，Command + Return 在 macOS 与确认按钮执行同一动作，Control + Return 不确认段落，段落导航不修改目标文本且提示与实际绑定一致
  - _Requirements: 3.3, 3.4, 8.6_
  - _Boundary: QtEditorWindow Editing Workflow_

- [x] 11.2 修复新建资源类型选择框的正常与悬停对比度
  - 为新建资源 `QComboBox` 的关闭状态、popup 普通项、悬停项和选中项定义明确且相互可辨的前景与背景
  - 保持翻译记忆库、术语表的 `currentData()` 及创建语义不变，不把视觉修复扩展为资源类型或持久化改造
  - 使用真实 popup view 的 palette/render 结果验证两种资源文字在普通、悬停和选中状态均可读，不只断言 stylesheet 字符串
  - 完成后，两种资源类型在 macOS 原生窗口与 offscreen 测试环境下均无白字白底或浅字浅底
  - _Requirements: 1.4, 6.3, 6.7, 6.8_
  - _Boundary: QtSettingsDialog New Resource Presentation_

- [x] 11.3 为无可写术语表提供明确且零写入的操作指引
  - 继续只把术语写入 active 且 `Update=true` 的术语表，不自动启用资源、不改 Lookup，也不创建或实现术语管理页
  - 当不存在可写术语表时，明确说明应在语言资源设置中激活至少一个术语表并开启 Update；空输入继续给出独立原因
  - 覆盖所有术语表 `Update=false`、无术语表、恢复一个 `Update=true` 后重试成功，并验证失败路径不改变任何术语资源字节
  - 完成后，“添加术语”失败不再只显示英文内部错误，用户可据提示完成配置且当前项目/资源保持不变
  - _Requirements: 5.4, 5.5, 6.4_
  - _Boundary: EditorController Error Semantics, QtEditorWindow Term Add Feedback_

- [x] 11.4 完成 Checkpoint M 的累计交互回归
  - 用同一 offscreen 旅程覆盖真实快捷键、资源类型 popup 各状态、`Update=false` 添加术语指引及重新开启后的成功写入
  - 运行 Qt 定向套件、完整 unittest、offscreen smoke、changed-file `basedpyright --level error`，并仅对 M-owned 显式 changed/staged paths 运行 `git diff --check`；受保护用户 WIP 以独立 SHA-256 复核
  - 复核 Layer 4 仍只调用 `EditorController`，新快捷键候选、Req7 CRUD、Feature 5 capability 与 Integration checkbox 均未进入本簇
  - 完成后，三类缺陷的正向、失败和恢复路径全绿，四个用户 WIP SHA-256 不变，可进入独立 M cluster review
  - _Depends: 11.1, 11.2, 11.3_
  - _Requirements: 1.4, 3.3, 3.4, 5.4, 5.5, 6.3, 6.4, 6.7, 6.8, 8.4, 8.5, 8.6_
  - _Boundary: Qt Maintenance Verification_

## Checkpoint M Implementation Notes

- Cluster review base：`13adb2a99507238b916f6e62bb3f9a6270cf9229`。
- Maintenance ledger commit：`2df687055c43839a726d2a98f0ed73b72c4a7129`。
- Task 11.1：macOS 主 Return、keypad Enter、物理 Control + Return 与 Option 导航均以真实 Qt 键事件通过；Qt editor 定向套件 35/35 通过。`qt_editor_window.py` 的 changed-file basedpyright 在 base/当前均有 42 个既存诊断，本任务未增加；最终零诊断门由 Task 11.4 闭合。
- Task 11.2：新建资源类型的 closed/popup normal/hover/selected 六态在 offscreen 与 macOS Cocoa 通过真实渲染对比度验收；单深色像素对抗图被 oracle 拒绝。`qt_settings_dialog.py` 的 basedpyright 在 base/当前均有 31 个既存诊断，最终零诊断门由 Task 11.4 闭合。
- Task 11.3：所有术语表 `Update=false`、无术语表、inactive+Update 对抗与恢复可写资源均已闭合；失败保持 registry、资源字节和项目零变化，成功路径仍确定性选择第一个 active+Update 术语表。Controller/Qt 定向套件及 changed-file basedpyright 均通过。
- Task 11.4：单一 offscreen 旅程累计闭合确认/导航、新建资源 popup、`Update=false` 零写入与恢复；按用户补充纳入顶栏编辑/校对模式 closed/normal/hover/selected 四态真实渲染，不改 mode payload/偏好，不增加新快捷键。M changed-file basedpyright 从 73 个既存诊断闭合为 0。
- Parent completion evidence：官方 acceptance 33/33（fingerprint `ee6a2f9ded665567684ef2baaa51beddee1f7d74571a87e45c14cd4a3b7ee43e`），release 86/86 GO（fingerprint `76b387e3bd0ffb719304c0633708647f2ed9e3b08e73d3003d6a03994a92a7ea`）；`QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v` 于当前源码/证据状态 1637/1637 通过，耗时 401.509s，1 个既存明确 opt-in skip；Qt smoke 通过，literal 5000/200 的 FTS5/fallback 均 `missing_above=0`、`missing_top10=0`。
