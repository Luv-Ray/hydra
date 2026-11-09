# CLAUDE.md — Hydra

AI agent 编排引擎:YAML DSL 定义多步骤 pipeline,Claude Agent SDK 为执行后端,全自动无人工干预。

## 环境与命令

**必须用 venv 里的解释器**,系统 `python` 没有依赖:

```bash
.venv/bin/python -m pytest tests/ -q        # 跑全部测试
.venv/bin/python -m pytest tests/test_compaction.py -q
```

`hydra` 是 editable 安装,指向工作目录源码。

> ⚠️ **editable 安装无热重载**:长驻进程(如一次 `hydra run`)在启动时就把 executor 等模块载入内存,期间改代码**不会**生效。改完代码要重跑才算数。判断依据:看日志是否跑到了新代码才有的输出行。

## 权威文档位置

- **DSL 完整关键字与语义**:`docs/specs/dsl-reference.md`(以代码为准,持续维护)。
- `docs/specs/archive/` 下带日期的文件是早期设计快照,**已归档、与现状有偏差,勿参考**。
- 事实来源永远是代码:关键字看 `models.py`,约束看 `validate.py`,依赖推导看 `dag.py`,注入看 `inject.py`。改了 DSL 语义务必同步更新 `dsl-reference.md`。

## 架构分层(执行顺序)

```
loader.py → validate.py → dag.py → executor.py
                                      ├── provider.py    (Claude SDK query() 封装)
                                      ├── inject.py      (文件注入)
                                      ├── events.py      (结构化事件流,可观测性)
                                      └── persistence.py (.hydra/runs/ 持久化)
```

## 关键设计约定(非显而易见,容易踩坑)

- **DSL v2,无 goto**:控制流只有依赖(数据流调度)、`when`(分支,读 inputs 或上游 `verdict`)、`loops`(声明式重做)。不要重新引入 goto / 回跳 / route-only step——v1 的大部分补丁都是它们带来的。v1 已删除,不保留兼容。
- **循环的决策点在审查方**:loop 的 `until` 读 body 里审查/汇总 step 的 verdict;`fix` 只负责改,不做判断。
- **verdict 是答案,异常不是**:报错/空回复/最后一行读不出恰好一个 verdict 值 → 按 `retry` 重试后 pipeline 失败,绝不当成 REJECT。`retry` 只管异常;"不满意就重做"用 loop。
- **resume 默认追加进目标 session(SDK 实测 id 不变)**,`fork: true` 才在副本上继续。可能并发的同目标 resume 必须全部 fork(validate 强制)。追加后引擎刷新目标的 session/上下文大小,`compact_before_resume` 才看得到增长;并发 forked resumer 按目标加锁只压缩一次。
- **inject 精确文件缺失**:报错,除非它的生产者(某 step 声明了这个 `output`)尚未产出——被跳过、loop 本轮还没跑到、或就是自己的上一版。glob 不参与依赖推导、0 匹配静默跳过。
- **依赖推导与 YAML 顺序无关**;依赖 loop 内的 step = 依赖整个 loop。一个 output 只能有一个声明者;agent 用工具改写的文件不是"声明的产物",下游要用 `after` 等。
- **解析严格**:未知键/错误类型/YAML 布尔(`NO`→False)都在加载期报错。加字段要同时改 `models.py` 的 `*_KEYS`。
- **prompt 只能引用本 step 看得到的东西**:每个 step 是独立 `query()`,上下文 = 它自己 inject 的文件 + 自己的 prompt。别的 step 的存在、名字、以及它们各自看到什么,本 step 一概不知。所以 prompt 里不要写"比 mini-review 更严"、"交给别的审查者"、"上一步已判断"这类**依赖 pipeline 结构**的话——那是把 step 看不到的编排泄漏进 prompt,agent 无从对照,只会困惑。把职责边界写成**自包含**形式:"X 不在本步范围内,不要做",而不是"X 交给别人"。引用别的产出只有一种合法方式:该文件在本 step 的 `inject` 里(如修正步引用 `inject` 进来的审查意见)。
- **持久化不覆盖**:循环每一轮进 `steps/<id>/run-<N>/`;loop 结果记在 `loops/<id>.json`,`--resume` 只整体复用已结束的 loop。

## 可观测性:两类日志分离

- **控制流**(step 起止、check 裁决、路由、回跳、压缩、终止原因)→ `events.jsonl` + stderr。这是排查执行逻辑的第一手材料。
- **详细 agent 日志**(prompt、工具/shell 调用、check I/O)→ `agent-trace.log`,`-v` 时才镜像到 stderr。
- 真实运行很贵(约 $50/20min),**不要为了加日志而重跑**;可观测性数据都是运行时免费落盘的。

## 工作方式约定(来自过往协作)

- **改代码前先读代码核对**,尤其涉及 SDK 行为(usage/context/compaction 这类)时,用 venv 起小脚本实测,不要凭假设下结论。
- **不确定就诚实说**,包括"我上一轮的建议错了"。给的数字/阈值要标明是实测还是外推。
- **本仓库无远程、不开分支**,到达自洽且测试通过的节点可直接在 master 上主动提交,不必每次等明确指示;破坏性或有争议的改动仍先说一声。
