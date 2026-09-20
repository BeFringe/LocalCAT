# Core 受信输入的私有 publication 消费合同

本合同记录Task 9.6c已实现的私有接口，供Feature5消费；对应回归位于`tests/test_tm_gate_inputs.py`、`tests/test_tm_gate_input_composition.py`和`tests/test_tm_gate_publication.py`。下列既有提交/取消竞态语义不变；末节普通packaged消费修订为**用户明确批准的 `reassessment@3e41130` 合同**，不代表9.6e已实现。接口合同本身不授予真实frozen或100k性能资格。

## 普通引用槽的来源

`MemberDescriptorType` 本身不能证明可写引用存储：`datetime.timedelta.days` 同样属于该类型，却是只读 native 数值字段。准入现在只接受 Core 私有构造函数 `_publication_reference_type` 当次生成并登记的普通 Python 对象引用槽。它固定调用 `type.__new__(type, name, bases, namespace)`，返回原生 `type`；参数名虽然写作 `metaclass`，传入的是这个私有构造函数，不是一个自定义元类。因此原 Host 的 `type(value) is type` 精确构造器合同保持。

下游持有 handoff、notification/status 等预构建引用的类采用：

```python
from tm_retrieval_capability import _publication_reference_type

class References(metaclass=_publication_reference_type):
    __slots__ = ("handoff", "status")

    def __init__(self, handoff, status):
        self.handoff = handoff
        self.status = status
```

构造要求精确字符串名称、精确 namespace dict、精确 tuple 字符串 slots，以及空 bases 或唯一的原始 `object` 基类。调用不能传入自定义元类构造路径。登记只保存这次固定 `type.__new__` 产生、`__objclass__` 为新类且不在传入 namespace 原有 native descriptor 集合中的 descriptor。借入的 C member、非 object 基类、仅宣称 `__slots__` 的既有/native 类型均不能获得登记；`__dict__`/`__weakref__` 的 getset 也不属于准入的 member descriptor。类构造与所有候选初始化发生在短锁之前。

此来源保证对应固定 CPython 3.14.7：普通声明 slot 由类型构造实现为对象引用字段，setter 直接替换引用；其他 native member 可以带只读标志或数值转换。参见 [CPython 类型构造源码](https://raw.githubusercontent.com/python/cpython/v3.14.7/Objects/typeobject.c) 与 [CPython member setter 源码](https://raw.githubusercontent.com/python/cpython/v3.14.7/Python/structmember.c)。实现不使用 ctypes、候选试写或按 native 类型名称排除。

该登记证明内存布局，不能产生 production receipt、输入 provenance 或 Gate 资格。消费方的实际引用 holder 必须采用此协议，并通过原 Host/通知观察锁提交。没有登记的任意 holder 将被拒绝；原 source publisher 不带 `_publication_window` 的调用仍兼容。

## 真实证明与最终提交

`tm_retrieval_capability.py` 仍只依赖标准库和 `tm_contracts`；纯 lifetime/window/plan 不执行 input、platform、Qt 或 native 操作。真实 `_GateInputOwner` 持有可撤销 lifetime；owner/session 自身也由上述函数构造，捕获原始 owner/epoch/window、completed/active/aborted 引用槽。原 Gate D 继续核验 exact session、receipt、同 epoch、fingerprint 和原 15 项 sealed publication bindings，不接受 callback 或 bool 冒充 proof。

1. 使用同一真实 owner 的当前有效 session 读取、复证并预构建候选、DTO、通知和结果对象；保持原观察锁覆盖准备与最终安装。
2. active session 调用 `_prepare_reference_publication(assignments, result)`。精确 tuple 的每项为 `(已登记的原 descriptor, 精确目标实例, 旧引用, 新引用)`；重复槽、未登记/native/property/custom descriptor 和任意 callback 全部拒绝，且不执行候选转换。
3. 先完成下游可失败的 native/Qt 准备；一般 consumer 调用 `session._prepare_publication()`，由该方法在 session 仍 active 时完成剩余 terminal 和 proof-window 成功退出，无需下游手动重复退出窗口。原 Gate D prepared callback 返回计划后由 Core 调用此准备方法。所有这些可失败步骤完成后才可 commit。共享 owner 只完成当前窗口，后续合法窗口继续可用；公开 retained Gate D 的一次性 native close 在提交前完成。
4. 原 Host/owner 观察锁仍持有时调用计划 commit；Gate D 原 publisher 在自己的原锁内追加精确 snapshot 槽，组成同一事务。最内层 lifetime mutex 只检查取消、同 owner/epoch/window 内存状态、原引用一致性并安装预构建引用。所有旧/新对象由计划/赋值 tuple 持有至短锁外，不通过析构调用 I/O。
5. 安装异常先设置 failed，然后尝试恢复全部槽；单个恢复异常不会跳过其余槽。若恢复也异常，则保留原安装异常及全部恢复异常为 `BaseExceptionGroup`，撤销 lifetime，禁止后续 owner 窗口。合法协议下的普通槽赋值不触发 native 数值转换或只读 setter；该恢复分支是防御措施。

计划准入异常会使当前窗口 failed，已有计划不能完成或重新准备，该 session 不能继续读/签发 receipt；无恢复故障时，共享 owner 可创建后续新窗口。完成、foreign、过期、aborted、重复及 epoch 漂移仍按原边界拒绝。memory-window 构造失败必须退出已进入的 proof-window。

## 取消与锁

调度线程调用真实 owner `_revoke_publication()`，只取得同一短 mutex 写撤销状态，释放后再唤醒调度器或安排原线程 native close。取消先取得锁则零安装；commit 先取得锁则完整安装并记录一次完成，随后撤销不把已完成 session 的外层退出倒判失败。

禁止在短锁内调用 `_require_epoch`、`_completed_owner`、任何 proof/native/Qt/I/O/import、任意 setter/rollback callback、等待、join 或额外阻塞锁。原 publisher 锁、原观察锁不能由 lifetime mutex 替代。Feature5 后续需要独立验证实际 notification/status 多引用、调度取消和 close 绑定；Core 本轮仅提供并验证这一私有协调协议。

`tm_gate_inputs` 实际调用纯 publication 模块，因此此前已补齐 Matcher build inventory 的 `tm_retrieval_capability.py` 仍保留。整改只按原 path/SHA 聚合算法重算受影响摘要，未再增加 paths、修改 cohort/阈值或 benchmark contract。

## 普通packaged的消费分工（已批准；待实施）

来源与身份的唯一定义见[Core Design](design.md)的「R4已完成范围与普通packaged消费修订」「检索兼容身份与单次运行身份」，平台候选身份只作当前运行关联；本合同不另定义profile、candidate格式或qualification key。普通输入由现有组合入口建立有限owner/session，使用受控构建所关联的owner输入与当前候选实际执行；不是旧source/native authority的别名。

当前session显式贯通Gate D、oracle及两个worker执行owner。纯DTO和query证据配对只检查传入不可变事实，不从ambient checkout重算fingerprint，也不持有活authority。所有需要当前运行验证的读取由实际持有session的Core owner完成；完成后的receipt仍按原publisher和同epoch协议消费，不能由候选id或构建digest重铸。

普通组合的terminal准备检查当前owner/epoch/window、已消费输入与兼容事实、候选关联和撤销状态，不要求完整source/AST/native复证。snapshot缓存不越过epoch或撤销，也不能将旧evidence解释成当前执行。所有可失败的输入检查与下游准备均在原commit短锁前完成，最内层锁仍只做既有内存状态和引用安装；不改sealed publication bindings或提交胜负规则。

Host/Qt请求取消后，Core依原短锁撤销publication，平台parent transport另行回收其创建的child与pipe。取消先赢则零安装；合法commit先赢则完整安装并记录完成，不能事后倒判成未发生。child退出、句柄close或有界等待都在锁外，Qt不阻塞join。只有这两项义务均有真实观察结果，才能报告取消/关闭清理完成；既有publication单测不能代替同EXE child退出与无晚到授权的候选验证。
