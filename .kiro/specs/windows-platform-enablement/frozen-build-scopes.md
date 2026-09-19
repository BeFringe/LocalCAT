# W3 构建输入的作用域规则

沿用[构建输入接口](frozen-build-inputs.md)与[绑定规则](frozen-build-bindings.md)的 CLI、`localcat-frozen-build-inputs-v1` 字段、source-only collection 规则、owner roots 与 W3 7.2/7.3 边界。本补充收紧不可证明动态绑定，修复跨函数 global 写入遗漏，并恢复被旧近似作用域错误拒绝的合法正控。

## 统一绑定来源

`_ScopeBindings` 建立 AST 节点的词法作用域、global/nonlocal 声明和有效绑定记录。普通赋值/删除、参数、import alias、函数/类定义、except/except*、match capture 等统一通过 `_node_bindings` 后路由到实际 owning scope；模块常量与动态 callable 不再各自维护一部分绑定语法。

global 写入归模块，nonlocal 写入归最近的有效外层函数绑定；其他函数中的写入也进入对应记录。因此任意可见 global 重绑定会使模块常量不可证明，动态 callable 必须在有效作用域中具有唯一正确导入且没有可见变更。`__import__` 同样检查有效全局写入。mutable mapping 的局部直接别名使用相同作用域身份追踪可见修改，不能仅因别名是局部变量就继续把原 owner 映射当常量。

默认值/装饰器、推导式第一个 iterable 与内部变量的作用域分开处理；推导式 walrus 绑定到外层非推导式 block。方法不会把 class namespace 作为闭包；显式 global 可以越过外层局部同名值。绑定语义依据 [Python 3.14 执行模型](https://docs.python.org/3/reference/executionmodel.html) 和 [表达式作用域说明](https://docs.python.org/3/reference/expressions.html#displays-for-lists-sets-and-dictionaries)。

这是有限静态证明，不执行应用模块。非模块动态值、未知/可变声明、无法解析的 nonlocal 或检测到的反射变更保守拒绝；本生成器尚未实现 annotation/type-parameter scope 的完整求值证明，遇到该范围中的动态 import 明确返回 `unproven dynamic annotation scope`，不会套用同名模块常量。无新增手工动态目标名单。

## 拒绝与后续消费

review 的 `global NAME; from chosen import TARGET as NAME` 和 `global NAME; match ... case NAME` 均在读取过期常量前得到 `nonliteral or cyclic declaration`。callable global 重绑定得到 `shadowed dynamic callable binding`。生产 CLI 仍统一返回 exit 2，缺失/不可证明输入不产生成功清单；完整、无重绑定的生产 fixture 正控继续允许。

原 F2 的 spec child/dynamic helper 递归内容寻址、原 F3 的关键模块 native/bytecode/PYZ 竞争来源拒绝保持。原 `validate_collection(generated, actual_members, pyz_modules=actual_toc)` 接口不变，必须消费真实收集内容和 TOC。

`remediation-r2-evidence.py replay <新标签>` 只重定位未修改的 `review/round2/replay_probes.py`，在新目录建立绑定当前 generator 的 clean Git fixture。两个全局反例的行为 subprocess 仅执行合成 Python 模块，记录完整命令、stdout/stderr 和源前后 SHA，证明真实 hidden 目标；不执行 owner/native。最终原配方结果路径为 `remediation-r2-replay-final/replay-results.json`，行为证据为同目录 `behavior-results.json`。

所有最终 run 在七份维护 Python 固定后产生，记录相同输入集合和 SHA。freeze/index 使用新 `remediation-r2-*`，不覆写旧证据，不收录可再生 fixture/.git 或静态 baseline。生产真实 checkout 仍缺 future build inputs，不能用上述合成 fixture 正控充当真实 candidate、W3 PASS、PE closure 或 GO。
