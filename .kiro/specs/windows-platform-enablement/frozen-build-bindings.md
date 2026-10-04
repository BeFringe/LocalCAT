# W3 构建输入的绑定规则

本文件补充[构建输入接口](frozen-build-inputs.md)。CLI、`localcat-frozen-build-inputs-v1` 字段、owner roots、Core grammar/digest/codec 和原 W3 7.2/7.3 分工保持原合同；下列校验适用于 W3 开发闭包和生产输入的实际静态解析，不是普通 frozen 的构建前置。

## 动态目标与绑定

模块常量仅在调用所在作用域可证明没有遮蔽时使用。参数、局部赋值、推导式变量、可见的 global/条件重绑定以及反射修改导致无法证明的目标会拒绝。动态 import callable 的绑定与目标使用同一绑定提取规则：`importlib` 或 `import_module` 必须具有唯一匹配导入，另一个 import 别名、except、match、定义、参数、赋值或 nonlocal 同名绑定均不能冒充此导入。

worker mapping 的字典 `**` 展开按 Python 左到右更新顺序解析，后项覆盖前项；所有最终值递归进入声明闭包。展开不能证明为有限字典时拒绝。生成器只读 AST 和原始文件字节，不执行 owner 模块，不尝试猜测未知目标，也不要求手写一份替代业务映射。

## 构建 helper

`.spec` 和 `.py` 进入同一依赖扫描器。`from package import child`、相对导入和有界动态 import 必须解析到真实 helper；递归得到的每个 helper 均须出现在 `build_sources`，以实际 bytes/SHA 与 clean tracked 内容匹配。漏项、未声明动态目标均拒绝，补齐真实传递依赖后仍可生成。字段或固定必需源路径没有增加。

## 关键源码的竞争来源

生产 input tree、native bundle destination、实际 `validate_collection` 成员以及 ZIP/wheel 成员统一比较 Windows 模块身份，覆盖大小写、extension ABI 后缀、package `__init__`、重定向路径、字节码及 PYZ 名称。与关键项目模块同名的 `.pyd` 或第二份代码拒绝，不能用未来 loader 优先级代替此检查；不与关键模块竞争的第三方 native extension 正控继续通过。

W3 packager 消费 `validate_collection(generated, actual_members, pyz_modules=actual_toc)`，其中成员与 TOC 必须来自实际收集结果。原 W3 7.1 不产生真实 PE closure，不证明 MZ payload 可加载，不执行合成 native bytes；真实 PE/native producer、完整输入和产品构建属于原 W3 后续范围，不能由合成 fixture 正例代答。
