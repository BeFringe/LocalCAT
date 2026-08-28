# Windows 场景与迁移清单

## 目的与判定词汇

本清单是 Task 1.3 的可追踪输入，绑定基线 `ui-mvp@b925b803d81001f55dea46ace8b26159ca82db19`。它记录 production consumer、既有测试、Windows 反例、owning Spec/amendment、后续任务与稳定 evidence key；不把静态扫描结果或其他平台的测试结果提升为 Windows runtime 结论。

| 状态 | 含义 |
|---|---|
| `BLOCKER` | 基线已在真实 Windows 运行中失败，或现有实现明确依赖 Windows 不提供的 POSIX 能力。 |
| `COVERED_POSIX_ONLY` | 仓库已有相应 POSIX/逻辑测试，但尚未由 Windows backend 在真实 Windows 上重放。 |
| `CHARACTERIZED_WINDOWS_ONLY` | 只观察到 Windows/工具的一项局部事实，不能代答共享合同或业务旅程。 |
| `NOT_OBSERVED` | 尚无满足获批合同的 Windows runtime evidence；静态零命中也使用此状态。 |

只有 owning task 生成 fresh、manifest-bound、无 required skip 的运行证据后，最终矩阵才可记录 `PASS`。本清单中的 evidence key 是稳定索引，不是预先批准的结果。

## 可复跑扫描口径与完整命中 manifest

以下命令均从 repository root 执行；`--glob '!**/*/**'` 将输入严格限制为根目录 production Python 文件，排除 `tests/`、`tools/`、artifact/build/dist 与嵌套 WIP。计数是本 Task 1.3 工作树的 lexical scan fact，不代表 runtime coverage。

```powershell
rg -n --glob '*.py' --glob '!**/*/**' 'fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino' .
rg -l --glob '*.py' --glob '!**/*/**' 'fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino' .
(rg -n --glob '*.py' --glob '!**/*/**' 'fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino' . | Measure-Object -Line).Lines
(rg -o --glob '*.py' --glob '!**/*/**' 'fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino' . | Measure-Object -Line).Lines
```

结果为 583 个 matching lines、780 个 occurrences、32 个 production 文件：

```text
capability_host.py
collaborative_chunk_store.py
parser_source.py
project_package.py
project_workspace_intake.py
resource_artifact_save.py
resource_importer.py
resource_package.py
resource_portability.py
resource_receipt_ledger.py
resource_repository.py
termbase_store.py
tm_activation_journal.py
tm_activation_recovery.py
tm_benchmark.py
tm_benchmark_gate.py
tm_benchmark_process.py
tm_benchmark_query_process.py
tm_content_attestation.py
tm_engine.py
tm_json_importer.py
tm_migration.py
tm_resource_port.py
tm_schema_upgrade.py
tm_snapshot_artifacts.py
tm_snapshot_recovery.py
tm_sqlite_store.py
tm_stage_sealer.py
tmx_artifact_save.py
tmx_context_interchange.py
tmx_resource_package_handler.py
workspace_state.py
```

```powershell
rg -n --glob '*.py' --glob '!**/*/**' 'Path\.cwd\(|os\.getcwd\(|cwd\s*=|checkout|__file__|BASE_DIR|_MEIPASS|sys\.frozen|terms\.csv|tm\.jsonl' .
rg -l --glob '*.py' --glob '!**/*/**' 'Path\.cwd\(|os\.getcwd\(|cwd\s*=|checkout|__file__|BASE_DIR|_MEIPASS|sys\.frozen|terms\.csv|tm\.jsonl' .
(rg -n --glob '*.py' --glob '!**/*/**' 'Path\.cwd\(|os\.getcwd\(|cwd\s*=|checkout|__file__|BASE_DIR|_MEIPASS|sys\.frozen|terms\.csv|tm\.jsonl' . | Measure-Object -Line).Lines
(rg -o --glob '*.py' --glob '!**/*/**' 'Path\.cwd\(|os\.getcwd\(|cwd\s*=|checkout|__file__|BASE_DIR|_MEIPASS|sys\.frozen|terms\.csv|tm\.jsonl' . | Measure-Object -Line).Lines
```

结果为 109 个 matching lines、133 个 occurrences、25 个 production/development入口候选：

```text
backend_scaling_gate.py
backend_throughput_harness.py
capability_host.py
deterministic_workload.py
excel_adapter.py
excel_adapter_openpyxl.py
logic_controller.py
macos_app_launcher.py
matcher_validation.py
qt_editor.py
qt_speaker_avatar.py
resource_importer.py
resource_package_contracts.py
resource_portability.py
stress_runner.py
test_malformed_artifact.py
tm_benchmark.py
tm_benchmark_gate.py
tm_benchmark_process.py
tm_benchmark_query_process.py
tm_engine.py
tm_gate_a.py
tm_json_importer.py
tm_retrieval_validation.py
translation_runner.py
```

其中 `matcher_validation.py` 由 `capability_host.py:49` 直接导入，`tm_retrieval_validation.py` 由 `capability_host.py:72,1465,2080` 固定/动态加载；二者与 `capability_host`/Gate A 都是 WA-07/Gate A/C mandatory frozen closure，不是条件性开发入口。TM/benchmark、Resource、Qt/translation 与 Controller/workload 候选分别进入 WA-06/04/08/07 复核；`macos_app_launcher.py` 是明确的非 Windows 入口；Excel adapters 受 ADR-003 约束且不属于 Qt runtime；backend/stress validation 与根目录 `test_malformed_artifact.py` 是开发/验证入口，只有 owner manifest 将其纳入 Gate closure 时才进入 dist。`logic_controller.py`、`deterministic_workload.py` 等 lexical hit 在 packaged graph 未解析前保持 `NOT_OBSERVED`，不据此断言依赖或安全。

tests 对抗候选使用下列可复跑扫描；当前结果为 313 个 matching lines、351 个 occurrences、42 个测试文件。下方场景矩阵给出每一类的代表位置和缺口。

```powershell
rg -n --glob 'tests/test_*.py' 'target.open|target_open|self.shar|TerminateProcess|kill.release|symlink|junction|reparse|hardlink|hard.link|ancestor.*swap|final.*swap|FileId|file.id.*reuse|AccessCheck|TokenUser|mandatory.label|power.cut|power.loss|reboot|_MEIPASS|sys\.frozen' tests
(rg -n --glob 'tests/test_*.py' 'target.open|target_open|self.shar|TerminateProcess|kill.release|symlink|junction|reparse|hardlink|hard.link|ancestor.*swap|final.*swap|FileId|file.id.*reuse|AccessCheck|TokenUser|mandatory.label|power.cut|power.loss|reboot|_MEIPASS|sys\.frozen' tests | Measure-Object -Line).Lines
(rg -o --glob 'tests/test_*.py' 'target.open|target_open|self.shar|TerminateProcess|kill.release|symlink|junction|reparse|hardlink|hard.link|ancestor.*swap|final.*swap|FileId|file.id.*reuse|AccessCheck|TokenUser|mandatory.label|power.cut|power.loss|reboot|_MEIPASS|sys\.frozen' tests | Measure-Object -Line).Lines
```

Task 1.3 要求的 tests primitive scan 不限于 `test_*.py`，以 `tests/**/*.py` 纳入 helper：

```powershell
rg -n --glob 'tests/**/*.py' 'fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino' tests
(rg -n --glob 'tests/**/*.py' 'fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino' tests | Measure-Object -Line).Lines
(rg -o --glob 'tests/**/*.py' 'fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino' tests | Measure-Object -Line).Lines
rg -l --glob 'tests/**/*.py' 'fcntl|flock|dir_fd|O_DIRECTORY|O_NOFOLLOW|os\.fsync\(|st_dev|st_ino' tests
```

结果为 151 个 matching lines、209 个 occurrences、26 个文件：

```text
tests/parser_io_test_support.py
tests/test_collaborative_chunks_cluster1_store.py
tests/test_multi_document_cluster2a_aggregation.py
tests/test_multi_document_cluster2c_adversarial.py
tests/test_multi_document_cluster2c_zip_security.py
tests/test_parser_source.py
tests/test_parser_wave4_safety.py
tests/test_qt_settings_tm_lifecycle.py
tests/test_termbase_store.py
tests/test_tm_acceptance_matrix.py
tests/test_tm_activation_cluster_d_corrections.py
tests/test_tm_activation_journal.py
tests/test_tm_activation_publication.py
tests/test_tm_activation_recovery.py
tests/test_tm_activation_rollback.py
tests/test_tm_benchmark_process.py
tests/test_tm_canonical_reattestation.py
tests/test_tm_explicit_import_rebuild.py
tests/test_tm_export.py
tests/test_tm_initial_activation_tamper_update.py
tests/test_tm_schema_upgrade.py
tests/test_tm_schema_upgrade_module_boundaries.py
tests/test_tm_snapshot_artifacts_module_boundaries.py
tests/test_tm_snapshot_recovery.py
tests/test_tm_snapshot_refresh.py
tests/test_tm_stage_sealer.py
```

tests CWD/checkout/resource scan 使用相同 helper-inclusive 范围：

```powershell
rg -n --glob 'tests/**/*.py' 'Path\.cwd\(|os\.getcwd\(|cwd\s*=|checkout|__file__|BASE_DIR|_MEIPASS|sys\.frozen|terms\.csv|tm\.jsonl' tests
(rg -n --glob 'tests/**/*.py' 'Path\.cwd\(|os\.getcwd\(|cwd\s*=|checkout|__file__|BASE_DIR|_MEIPASS|sys\.frozen|terms\.csv|tm\.jsonl' tests | Measure-Object -Line).Lines
(rg -o --glob 'tests/**/*.py' 'Path\.cwd\(|os\.getcwd\(|cwd\s*=|checkout|__file__|BASE_DIR|_MEIPASS|sys\.frozen|terms\.csv|tm\.jsonl' tests | Measure-Object -Line).Lines
rg -l --glob 'tests/**/*.py' 'Path\.cwd\(|os\.getcwd\(|cwd\s*=|checkout|__file__|BASE_DIR|_MEIPASS|sys\.frozen|terms\.csv|tm\.jsonl' tests
```

结果为 232 个 matching lines、240 个 occurrences、95 个文件：

```text
tests/feature5_ui_canonical_fixture.py
tests/test_capability_host_gate_c.py
tests/test_capability_host_gate_d.py
tests/test_capability_host_gate_d_attestation.py
tests/test_capability_host_matcher_gate.py
tests/test_collaborative_chunks_cluster1_store.py
tests/test_collaborative_chunks_cluster1_topology.py
tests/test_collaborative_chunks_cluster2_progress_rebase.py
tests/test_collaborative_chunks_cluster3_controller_search.py
tests/test_collaborative_chunks_cluster4_dialog.py
tests/test_collaborative_chunks_cluster4_qt.py
tests/test_collaborative_chunks_current_source_evidence.py
tests/test_editor_contracts.py
tests/test_editor_controller_resources.py
tests/test_editor_tm_adapter_canonical.py
tests/test_excel_adapter_contract.py
tests/test_feature5_validation.py
tests/test_language_resource_portability_current_source_evidence.py
tests/test_macos_app_launcher.py
tests/test_matcher_capability.py
tests/test_multi_document_cluster0_architecture.py
tests/test_multi_document_cluster0_characterization.py
tests/test_multi_document_cluster1_contracts.py
tests/test_multi_document_cluster2a_aggregation.py
tests/test_multi_document_cluster4_qt.py
tests/test_parser_cli_runner_characterization.py
tests/test_parser_composition.py
tests/test_parser_gettext_codec.py
tests/test_parser_localcat_codec.py
tests/test_parser_project_golden.py
tests/test_parser_registry.py
tests/test_parser_termbase_codec.py
tests/test_parser_termbase_golden.py
tests/test_parser_tm_golden.py
tests/test_parser_tm_json_codec.py
tests/test_parser_tmx_codec.py
tests/test_parser_wave4_architecture.py
tests/test_parser_wave4_equivalence.py
tests/test_parser_wave4_safety.py
tests/test_project_search.py
tests/test_qt_bootstrap.py
tests/test_qt_e2e_resource_loop.py
tests/test_qt_localized_standard_buttons.py
tests/test_qt_project_search.py
tests/test_qt_project_search_acceptance.py
tests/test_qt_settings_import.py
tests/test_qt_termbase_dialog.py
tests/test_qt_tm_layer_boundary.py
tests/test_qt_user_journey.py
tests/test_resource_importer.py
tests/test_resource_package_carrier.py
tests/test_resource_package_contracts.py
tests/test_resource_portability_architecture.py
tests/test_resource_portability_exports.py
tests/test_resource_repository.py
tests/test_termbase_portable_snapshot.py
tests/test_termbase_store.py
tests/test_text_matcher_application_handoff.py
tests/test_text_matcher_unicode.py
tests/test_text_matcher_v1.py
tests/test_tm_acceptance_matrix.py
tests/test_tm_activation_contracts.py
tests/test_tm_activation_module_boundaries.py
tests/test_tm_architecture_guards.py
tests/test_tm_benchmark.py
tests/test_tm_benchmark_gate.py
tests/test_tm_benchmark_latency.py
tests/test_tm_benchmark_oracle.py
tests/test_tm_benchmark_process.py
tests/test_tm_benchmark_query_process.py
tests/test_tm_candidate_proof_index.py
tests/test_tm_candidate_store_contracts.py
tests/test_tm_core_selfchecks.py
tests/test_tm_explicit_import_rebuild.py
tests/test_tm_fault_matrix.py
tests/test_tm_gate_d_attestation.py
tests/test_tm_initial_activation_tamper_update.py
tests/test_tm_migration.py
tests/test_tm_operation_outcomes.py
tests/test_tm_preferences.py
tests/test_tm_release_criteria.py
tests/test_tm_retrieval_capability_module_boundaries.py
tests/test_tm_retrieval_validation.py
tests/test_tm_retrieval_validation_module_boundaries.py
tests/test_tm_schema_upgrade_module_boundaries.py
tests/test_tm_similarity.py
tests/test_tm_snapshot_artifacts_module_boundaries.py
tests/test_tm_snapshot_recovery.py
tests/test_tm_source_binding.py
tests/test_tm_sqlite_candidate_projection.py
tests/test_tm_store_candidate_projection_write_delegation.py
tests/test_tm_store_module_extraction_characterization.py
tests/test_tmx_context_interchange_current_source_evidence.py
tests/test_windows_release_evidence.py
tests/test_workspace_state.py
```

tests 命中只是既有 coverage/fixture/guard 候选：POSIX primitive facts 归入 `COVERED_POSIX_ONLY`，CWD/resource hits 进入对应 WA/WR frozen closure 或 development-only exclusion；只有后续 owning Windows task 的 fresh runtime evidence 才能改变状态。

## Production consumer 迁移清单

| Consumer / 代表位置 | 当前平台事实 | Owner / amendment / owning suffix | 必须保留的合同与反例 | Windows task | Evidence key | 基线状态 |
|---|---|---|---|---|---|---|
| Qt startup / `collaborative_chunk_store.py:12,1335-2065` | 顶层 `fcntl` 使 Windows 在 composition 前退出；内部直接使用 `flock`、`dir_fd`、`O_DIRECTORY`、`O_NOFOLLOW` 与目录 `fsync` | `collaborative-job-chunks` / WA-02 / source `1.3a,1.4a,3.2a,4.4a,4.5a`；frozen `1.4b,4.5b` | 无顶层 POSIX import；W1 LockFileEx、首次 two-creator/creator-crash、journal/LKG、kill release、target-open | source 2.1-3.7, 4.1, 4.3-4.4；frozen 1.6, platform 7.4 candidate, 7.4a-7.5 | `windows.startup.chunk.import`、`windows.lock.chunk.terminate-release` | `BLOCKER` |
| Parser source/writer / `parser_source.py:100-121,259,269,685,1449-1650` | Windows 明确不能 mint POSIX rooted capability | `parser-subsystem-extraction` / WA-01 / source `2.5a,2.12a,5.2a,5.12a`；frozen `5.12b` | sealed source、body-safe failure、rooted writer；junction/reparse/hardlink/ancestor/final swap；能力缺失保留 `PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE` | source 2.1-3.7, 4.1-4.2；frozen 1.6, platform 7.4 candidate, 7.4a-7.5 | `windows.fs.rooted.parser.reparse`、`windows.fs.publish.parser.atomic` | `BLOCKER` |
| Project package/intake/state / `project_package.py:924-1017,1089-1121,2290-2323,2627-2820`; `project_workspace_intake.py:150-237,501-612`; `workspace_state.py:607` | 本地 helper 直接绑定 POSIX identity/dirfd/publish/durability | `multi-document-project-workspace` / WA-03 / `2.1a,2.4a,2.8a,4.3a,4.4a` | deterministic carrier、owner lease、target-open、uncooperative swap、old/new/recovery-only、restart reproof | 4.2, 5.1-5.2, 5.5 | `windows.publish.project.target-open`、`windows.recovery.project.reboot` | `BLOCKER` |
| Resource/Termbase / `resource_artifact_save.py:105-369`; `resource_package.py:609,665`; `resource_portability.py:1179-1199`; `resource_receipt_ledger.py:213-330`; `resource_repository.py:460`; `resource_importer.py:744`; `termbase_store.py:1050-1077` | save/import/package/repository/receipt 重复 POSIX parent binding、identity 和 publish | `language-resource-portability` / WA-04 / source `2.4a,2.5a,3.4a,4.4a,5.4a`；frozen `5.5a`；WR-02 | portability/receipt/column mapping 不变；rooted import、bound publish、ledger/repository、junction/swap/lock/recovery | source 4.2, 5.1, 5.3, 5.5；frozen 1.6, platform 7.4 candidate, 7.4a-7.5 | `windows.publish.resource.receipt`、`windows.frozen.resource.bundle-root` | `BLOCKER` |
| TMX source/writer / `tmx_artifact_save.py:56-475`; `tmx_context_interchange.py:79-86`; `tmx_resource_package_handler.py:158-169` | source、resource handler 与 canonical target 依赖 POSIX rooted/publish primitives | `tmx-context-interchange` / WA-05 / source `2.3a,3.4a,3.5a,5.3a`；frozen `5.4a` | locale/conflict/count；invalid/escaped/reparse/swap source 零目标变化；canonical bytes/restart | source 4.2, 5.1, 5.4-5.5；frozen 1.6, platform 7.4 candidate, 7.4a-7.5 | `windows.source.tmx.rooted`、`windows.publish.tmx.canonical` | `BLOCKER` |
| TM reservation/locking / `tm_migration.py:4900-5181` | dynamic `fcntl`/`flock`、POSIX identity 与 persistent lock representation | `tm-storage-retrieval-index` / WA-06 / source `1.2a,5.3a,5.6a-5.9a,5.12a-5.14a,8.8a,9.1a,9.2a,9.6a`；frozen `9.6b` | W1 exact protocol-control object、two-creator、TerminateProcess release、single authority、stable conflict result | source 3.2-3.7, 6.1-6.2；frozen 1.6, platform 7.4 candidate, 7.4a-7.5 | `windows.lock.tm.initial-authority`、`windows.lock.tm.terminate-release` | `BLOCKER` |
| TM snapshot/publish/recovery / `tm_snapshot_artifacts.py`; `tm_snapshot_recovery.py:536,907`; `tm_activation_journal.py`; `tm_activation_recovery.py`; `tm_schema_upgrade.py`; `tm_sqlite_store.py`; `tm_resource_port.py:440-461`; `tm_engine.py`; `tm_json_importer.py`; `tm_benchmark*.py` | 广泛使用 `dir_fd`、`O_NOFOLLOW`、`st_dev/st_ino`、file/directory `fsync`，且 benchmark/import 存在 source/CWD assumptions | `tm-storage-retrieval-index` / WA-06 / source `1.2a,5.3a,5.6a-5.9a,5.12a-5.14a,8.8a,9.1a,9.2a,9.6a`；frozen `9.6b`；WR-01 | generation/phase/journal/LKG、retained readback→durable commit→terminal reproof、instruction fault/process termination/应用重启/正常OS reboot | source 3.4-3.7, 6.1-6.4；frozen 1.6, platform 7.4 candidate, 7.4a-7.5 | `windows.publish.tm.generation`、`windows.publish.ntfs.documented-reboot` | `BLOCKER` |
| TM private attestation / `tm_content_attestation.py`; `tm_stage_sealer.py`; `tm_benchmark_gate.py:2706-3585`; `tm_benchmark.py:704-759` | POSIX mode/uid/dev/ino/link proof 不能表示 W2/ADR-023 Windows security authority | `tm-storage-retrieval-index` / WA-06 source；WA-07 source `6.6a,6.7a`、frozen `6.6b` | owner envelope + nested `WindowsPrivateSecurityV2` proof；TokenUser/exact ACL/MIC/AccessCheck；FileId 只比较 live handles | source 3.3-3.7, 6.1, 6.3；frozen 1.6, platform 7.4 candidate, 7.4a-7.5 | `windows.private.tm.token-mic`、`windows.identity.tm.fileid-reuse` | `BLOCKER` |
| CapabilityHost/Gates / `capability_host.py:49,72,127-178,1429-2044,2080`; `matcher_validation.py`; `tm_retrieval_validation.py`; `tm_gate_a.py:52`; `tm_engine.py:511`; `logic_controller.py`; `deterministic_workload.py` | 以 checkout/source path、`lstat`、`O_NOFOLLOW` 等证明源码；matcher/retrieval validators 是 Gate A/C mandatory closure；frozen baseline缺少真实`.py`时退出 | `feature5-ui-integration` / WA-07 / source `3.5a,3.8a,5.4a,5.5a,6.6a,6.7a,7.2a,7.4a,7.6a`；frozen pre-build `3.6a`、post-build `6.6b,7.4b,7.6b,9.2a` | source先消费rooted authority；frozen再闭合Boot TCB、retained-handle exact bytes、loader attestation与无PYZ/bytecode duplicate | source platform 6.5-6.6b；frozen 1.6, 7.0-8.5 | `windows.source.capability-host`、`windows.frozen.source.exact-bytes` | `BLOCKER` |
| Qt/data paths / `qt_editor.py:648-654,692`; `qt_speaker_avatar.py:11`; `translation_runner.py:19-22`; `resource_package_contracts.py` | 资源默认从 module/checkout 相对位置解析；普通 PyInstaller hook 只证明 `qwindows.dll` 被收集 | `qt-editor-json-mvp-increment` / WA-08 / source `4.8a,5.2a,5.3a,5.4a`；frozen `5.2b,5.3b,5.4b`；WR-02/03 | source-owned resource root；frozen bundle authority；tm/terms/logo/benchmark/fixtures/critical `.py` 可见；avatar catalog index/decode/fallback；non-repository CWD | source platform 6.6a → WA-08 5.4a → platform 6.6b；frozen platform 7.4/7.4a → WA-08 5.4b | `windows.bundle.resources.qt`、`windows.ui.avatar.catalog-fallback` | `BLOCKER` |
| TM benchmark subprocess / `tm_benchmark_process.py:2350`; `tm_benchmark_query_process.py:2882`; `tm_benchmark.py:778` | 子进程 CWD/root 固定为 `__file__` 所在 checkout | WA-06 / `5.9a,5.12a,9.1a`；WR-01 | bundle-relative executable/data authority；clean user、non-repository CWD、无 checkout access | 6.1, 7.1-7.4, 8.3, 9.1 | `windows.frozen.tm-benchmark.nonrepo-cwd` | `NOT_OBSERVED` |

## 对抗与运行场景矩阵

| 场景 | 现有覆盖或基线事实 | Windows 所需观察 | Owner / task | Evidence key | 当前状态 |
|---|---|---|---|---|---|
| rooted component walk | Parser 有 POSIX-only symlink/ancestor swap tests（`tests/test_parser_wave4_safety.py:1120-1221`） | retained root/intermediate/final handles；junction/mount/reparse fail closed；final containment + live FileId | WA-01 / 3.2, 4.2 | `windows.fs.rooted.parser.reparse` | `COVERED_POSIX_ONLY` |
| hardlink 与 link count | POSIX negatives：`tests/test_tmx_artifact_save.py:169-179`、`tests/test_tm_activation_cluster_d_corrections.py:317-366,923,1221-2084`、`tests/test_tm_activation_journal.py:1342-1417` | 按 owner policy 区分只读 source 与 protocol/private/publish object，并记录 handle-bound link count | WA-01, WA-02, WA-06 / 3.2-3.3 | `windows.fs.identity.hardlink-policy` | `COVERED_POSIX_ONLY` |
| ancestor/final entry swap | POSIX race seams：`tests/test_tm_export.py:1069,2158,2248`、`tests/test_tm_snapshot_refresh.py:1816-2310`、`tests/test_tm_snapshot_recovery.py:2653,3346,4690-6250` | 真实 Windows reparse/rename race；retained handle 不漂移；失败不改变正文/目标 | WA-01, WA-03-06 / 3.2, 4.2, 5.5, 6.3 | `windows.fs.rooted.swap` | `COVERED_POSIX_ONLY` |
| target-open publish | baseline 观察“占用可阻止 replace”，但 share profile 未按 W1 实现 | caller-owned destination/reader handle 下 `REPLACE_UNDER_LOCK` 的明确 success/reject，candidate 可 readback/rename/close/reopen | W1 + WA-02-06 / 3.4-3.7, 4.3, 5.5 | `windows.publish.target-open` | `CHARACTERIZED_WINDOWS_ONLY` |
| candidate self-sharing | 未观察 W1 exact handle/share 生命周期 | publisher 自持 candidate/destination handles 不阻塞合法 rename/reopen，且不扩大对手 share | W1 / 3.4-3.7 | `windows.publish.candidate.self-sharing` | `NOT_OBSERVED` |
| process lock + kill release | baseline `msvcrt.locking` 只证明 byte-lock 特征 | W1 `LockFileEx` exact range；竞争者阻塞/超时；真实 `TerminateProcess` 后 OS 释放并可恢复 | W1 + WA-02/06 / 3.2-3.3, 3.7, 4.3, 6.2 | `windows.lock.process.terminate-release` | `CHARACTERIZED_WINDOWS_ONLY` |
| first lock-object initialization | 无 Windows 实现证据 | `CREATE_IF_ABSENT`/`ERROR_FILE_EXISTS` 后 LOCK-first；two-creator；create/write/flush/readback/close 每个 creator-crash 边界 | W1 + WA-02/06 / 3.2-3.3, 3.7 | `windows.lock.initialization.creator-crash` | `NOT_OBSERVED` |
| live identity / FileId reuse | `tests/test_tm_stage_sealer.py:1597-1644`、`tests/test_tm_activation_recovery.py:687`、`tests/test_tm_activation_journal.py:2084` 只有 device/inode analogue；历史 FileId 不得作 authority | VolumeSerial + FILE_ID_128 仅比较同时存活 handles；delete/recreate/restart 不能被历史 ID 放行 | W1/W2 + WA-06 / 3.2-3.4, 3.6-3.7, 6.3 | `windows.identity.tm.fileid-reuse` | `NOT_OBSERVED` |
| exact ACL/token/MIC | 无真实 Windows token matrix | provider-agnostic process-primary TokenUser SID、owner、V2 exact DACL/control bits、medium label/NO_WRITE_UP、mapped AccessCheck；standard/elevated positives、同SID low/restricted negatives、thread impersonation与service/AppContainer/impersonation fail-closed。domain/Entra仅optional非阻断 | W2 + ADR-023/024 + WA-06 / 3.3-3.4, 3.6-3.7, 6.3 | `windows.private.tm.token-mic` | `NOT_OBSERVED` |
| crash/process-death recovery | `tests/test_tm_snapshot_recovery.py:1578-1885`、`tests/test_tm_activation_publication.py:94-944` 包含 injected crash/child process death，不是断电 | 各 business owner 在真实进程终止后只产生 old/new/recovery-only，ambiguous residue fail closed | WA-02-06 / 4.3, 5.5, 6.2 | `windows.recovery.process-death` | `COVERED_POSIX_ONLY` |
| documented publish / normal reboot recovery | 现有所谓power-cut多为fault injection，不能证明硬件掉电资格 | 在local fixed NTFS上验证`WRITE_THROUGH`+`FlushFileBuffers`、handle-bound naming、candidate close、retained readback、owner commit与terminal reproof；instruction fault、process termination、应用重启与正常OS reboot后只接受old/new/recovery-only。forced-power-off不属于blocking矩阵 | W1 + ADR-025 + WA-03/06 / 3.5-3.7, 5.2, 5.5, 6.1 | `windows.publish.ntfs.documented-reboot` | `NOT_OBSERVED` |
| non-repository CWD | baseline frozen LocalCAT 因 `capability_host.py` 不可见失败 | source从轻量入口、frozen从EXE分别清除`PYTHONPATH`/Qt developer paths启动；二者均不以CWD解析资源，frozen不得读取checkout | source WA-07/08 / platform 6.6a-6.6b；frozen W3 + WA-07/08 / 1.6, 7.2-7.5, 8.1 | `windows.source.startup.nonrepo-cwd`、`windows.frozen.startup.nonrepo-cwd` | `BLOCKER` |
| native DLL closure/injection | baseline clean build受 ambient `PATH` 污染时曾复制外部 ICU/OpenSSL；净化后仅恢复到既有 source failure | native entry 在首次 Python/non-KnownDLL load 前排除 CWD/PATH；递归 static/delay/dynamic closure；pre-load handle proof + post-load identity；top-level/transitive early/late injection fail closed | W3 / 1.6, 7.2 | `windows.frozen.boot-tcb.pre-python` | `BLOCKER` |
| exact frozen source | baseline dist 没有 mandatory raw `.py`；普通 `module_collection_mode='py'` 不证明执行 bytes | `TrustedSourceLoader` 从 retained verified handle 读取、digest、direct compile；attestation/origin/co_filename 一致；无 `.pyc`/`__pycache__`/PYZ duplicate；swap/tamper fail closed | W3 + WA-07 / 1.6, 7.1-7.2, 8.5 | `windows.frozen.source.exact-bytes` | `BLOCKER` |
| fixture/data/resource visibility | baseline raw fixtures/data 缺失；`qwindows.dll` 单项存在 | source-owned resources先完成产品journey；frozen再证明manifest-bound fixture exact bytes与tm/terms/logo/benchmark/Qt plugins可见，missing/extra/tamper/checkout fallback均失败；不消费durability registry/power evidence | source WA-04/07/08 / platform 6.6a-6.6b；frozen W3 + WA-04/07/08 / 1.6, 7.1-7.5, 8.5 | `windows.source.resources.qt`、`windows.bundle.resources.qt` | `BLOCKER` |
| avatar behavior | 只属于 Qt Windows 功能回归，不改变 asset/ignore ownership | manifest声明 catalog 时真实 Qt 索引/解码/渲染；无 catalog/无匹配时显示既有 fallback | WA-08 / 7.4-7.5 | `windows.ui.avatar.catalog-fallback` | `NOT_OBSERVED` |
| SQLite FTS5/trigram | fresh source venv 已实际 create/MATCH，未绑定 published TM authority/dist | source 与同一 onedir dist 上对 canonical store create/write/MATCH/close/reopen，结果满足 Core 排序合同 | WA-06/07 / 6.4, 8.4, 10.1 | `windows.tm.fts5.reopen` | `CHARACTERIZED_WINDOWS_ONLY` |

## 静态扫描的非结论项

| 命中或零命中 | 处理规则 |
|---|---|
| evidence validator/tests 中的 `Path(__file__).resolve()` | harness 自定位，不是 production EXE consumer；只要它不进入发行 authority，就不计迁移 PASS/FAIL。 |
| `platform_fs_posix.py` 中的 `st_dev/st_ino` 或未来封装后的 POSIX primitive | 是批准的 POSIX adapter 事实；只有 consumer 直接依赖才是边界违规。 |
| Windows characterization probe 中 `msvcrt.locking` | 只说明 OS 有 byte-lock 行为；不代答 W1 的 LockFileEx range、share、payload、首次初始化和恢复合同。 |
| dist 中存在 `qwindows.dll` 或 PyInstaller build exit 0 | 只是一项 inventory/build 事实；不代答 visible Qt、Boot TCB、source proof、resources 或业务 E2E。 |
| production grep 对某 primitive 零命中 | 只能记录静态边界事实；在 owning Windows runtime scenario 成功前保持 `NOT_OBSERVED`。 |
| POSIX-only tests 全绿或 Windows mandatory case 被 skip | 不构成 Windows release PASS；mandatory skip 直接阻塞相应 capability/Feature GO。 |

## 阻塞顺序与迁移排期

1. Task 1.4已裁决stock PyInstaller entry为NO-GO；Task 1.5立即规划W3 custom in-process entry，Task 1.6等待W1 rooted contract冻结后裁决最小Boot TCB/TrustedSourceLoader是否可行，失败必须回到W3并停止所有frozen consumer与Task 7，但不阻断source主线。
2. Tasks 2-3 建立共享合同、POSIX parity 与 Windows W1/W2 backend；C3B按ADR-024/025验证provider-agnostic token profile与`WindowsDocumentedPublishV1`，不再建立硬件profile实验室门。没有该层时不得在 consumer 内复制 Win32/POSIX 分支。
3. WA-02 先消除 source Qt 的顶层 `fcntl` 阻塞；WA-01 同期完成 rooted Source/Writer，随后才能进入 Project/Resource/TMX。
4. WA-03/04/05 在 WA-01 后并行迁移；WA-06 还依赖 WA-02、W2、ADR-023 与完整 Windows backend。
5. WA-06 source后依次完成WA-07 source、platform 6.6a launcher、WA-08 5.4a与platform 6.6b里程碑；W3 spike全PASS后先合并pre-build roots/WA-07 3.6a并构建候选，platform 7.4后再完成各owner frozen revalidation与WA-08 packaged journey。
6. WR-01/02/03 只做无 contract delta 的回归登记；最终只接受同一 clean commit/dist 的 Qt、Project、TM、TMX、FTS5、锁/恢复、frozen visibility 全链路证据。
