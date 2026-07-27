# 实现验收报告

## 结论

Qt 专业编辑器 MVP 及本轮真实试用增量已按 `requirements.md`、`design.md` 与 `qt-editor-mvp-border.md` 完成。九个设计步骤均有真实运行证据，没有用静态窗口或模拟返回值替代 EditorController、资源仓储、导入器和现有引擎的完整调用链。

## 需求覆盖

| 范围 | 验收结果 | 主要证据 |
|------|----------|----------|
| 专业工作区与项目 I/O | 通过 | 空状态、JSON/TXT 打开、版本化 JSON 原子保存、未保存保护、响应分栏 |
| 分段编辑与确认 | 通过 | 编辑保留、确认进度、未确认导航、快捷键、Update TM 写回 |
| TM 与术语建议 | 通过 | 多资源并列查询、来源标记、安全高亮、应用/插入、新增术语即时刷新 |
| 语言资源设置 | 通过 | 齿轮入口、新建、Active/Lookup/Update、持久化恢复 |
| TMX/CSV/XLSX 导入 | 通过 | 后台导入、统计反馈、原子替换、失败重试、成功后热重载 |
| 项目生命周期 | 通过 | 项目菜单、最近项目、退出当前项目、未保存保护、稳定段落 ID 与索引回退 |
| 显示与校对 | 通过 | 紧凑等高、完整自动换行、双语浏览校对、双击同段返回、显示偏好恢复 |
| 桌面启动 | 通过 | stdlib bootstrap 生成 Linux 用户 `.desktop` 应用菜单入口 |
| 本地性与兼容性 | 通过 | 无网络路径、无 Qt bootstrap、offscreen 启动、旧 LogicController 三态与 Excel 边界回归 |
| 文档同步 | 通过 | README 与 steering 已反映 Qt MVP、无状态 Kiro 开发事实、安装启动、格式与限制 |

## 验证证据

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
# Ran 87 tests ... OK

QT_QPA_PLATFORM=offscreen python qt_editor.py \
  --smoke-test --data-dir /tmp/localcat-followup-smoke-20260727
# Qt editor smoke test passed.

python glossary_engine.py
python tm_engine.py
python logic_controller.py
python stress_runner.py
python translation_runner.py
# 五个入口均返回 0

python -m compileall -q .
# 返回 0
```

`tests/test_qt_e2e_resource_loop.py` 使用真实临时 JSON 项目、TMX、CSV 和 XLSX，通过设置对话框完成导入，再执行当前段建议、TM 应用、确认写回、项目保存，以及新仓储/新控制器重载查询。`tests/test_qt_user_journey.py` 使用 QtTest 向齿轮、建议、术语和 `Ctrl+Enter` 发送真实事件。`tests/test_workspace_state.py` 与 `tests/test_qt_browse_mode.py` 覆盖最近项目/断点、显示偏好、两种列表密度、最新会话译文浏览和双击同段返回。

### 真实个人项目增量证据

通过 `QtEditorWindow.open_project_path()` 打开 `po/卷一_引.json`，使用真实 `QComboBox.currentData()` 的普通字符串 `translation_memory` 经 `QtSettingsDialog.create_resource()` 创建 TM，再从设置后台导入 `RpySeriesExtract/OWNattempt.tmx`：

| 指标 | 结果 |
|------|------|
| 项目段数 | 2942 |
| TMX 导入 | 165 |
| 跳过缺失语言对 | 67 |
| 重复源文覆盖 | 30 |
| 导入错误 | 0 |
| 项目精确命中 | 112 |
| 首个命中 | 第 61 段：`Yes.` → `是的。` |
| 浏览表构建 | 2942 行，约 0.14 秒（本机 offscreen） |
| 恢复断点 | 第 409 段，稳定 ID `segment-409` |

真实设置截图确认“翻译记忆库”和“导入 TMX”完整可见；同一长篇会话中切换自动换行与浏览校对后，未保存译文仍显示在浏览行中，双击该行返回同一段编辑。重建 Repository、Controller 和窗口后，段落、自动换行与浏览模式均恢复。

含外部 DOCTYPE 的 `chinese__english.tmx` 仍按安全边界拒绝且不改写目标资源。

## 设计对照

- Qt 前端只依赖 EditorController/编辑契约，AST 回归守护 Layer 4 → Layer 3 边界。
- 旧 LogicController 的 `TM_HIT / TERMS_FOUND / NO_MATCH` 无状态契约未改变。
- 资源导入失败保留旧文件与上一组可用引擎；成功后一次热替换。
- Active + Lookup 控制查询，Active + Update 控制确认写回。
- 项目、资源清单和语言资源均保存在本地；当前实现没有网络调用。
- 最近项目、断点与显示偏好保存在独立 `workspace.json`，不污染可交换翻译项目。
- 浏览校对与编辑器共享当前 `EditorProject`，没有复制或覆盖未保存译文。

## 明确未实现

模糊 TM、机器翻译、QA、账号、云端、共享资源、多人协作、MateCat 服务端兼容和复杂格式回写不属于本 MVP，README 未将这些能力标为完成。
