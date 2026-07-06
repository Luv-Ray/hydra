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

- **`trigger: route` 的 step 不进 DAG**,只通过 goto 触达;它们被排除在拓扑调度和回跳子图之外。
- **回跳循环的决策点在"汇总/审查方",不在"修正方"**。退出循环的判断(check + routes)应放在聚合结果的 step 上,修正 step 只负责改文件后跳回。这是审查者决策模式。
- **回跳时 route 发起者的重跑是有条件的**:普通 DAG step 会自动重跑;`trigger: route` 的发起者不自动重跑(否则死循环),由子图末步的 check/routes 决定去向。
- **循环耗尽的逃生出口**:发起者可写 `if: max_iterations` 的 route——惰性条件,只在 `max_iterations` 达上限时触发,让 pipeline 带未收敛结果前进而非整体 fail。不写则保持"抛错"的旧行为。逃生 goto 只能前进($end/$fail/未跑过的 step),指回循环内会报错。
- **并行 reviewer 共享 resume 目标**时,`compact_before_resume` 按目标加锁,只压缩一次。
- **`check.prompt` 可选**:省略时直接匹配 step 自身输出(适合让 step 自己在输出里给裁决信号如 ACCEPT/REJECT);写了则在同 session 追问。
- **inject 精确文件缺失**:前向运行报错(拼错路径不静默吞);仅回跳循环中(step 已在 `iteration_counts`)容忍缺失,支持第一轮上游未产出。glob 不参与依赖推导、0 匹配静默跳过。
- **prompt 只能引用本 step 看得到的东西**:每个 step 是独立 `query()`,上下文 = 它自己 inject 的文件 + 自己的 prompt。别的 step 的存在、名字、以及它们各自看到什么,本 step 一概不知。所以 prompt 里不要写"比 mini-review 更严"、"交给别的审查者"、"上一步已判断"这类**依赖 pipeline 结构**的话——那是把 step 看不到的编排泄漏进 prompt,agent 无从对照,只会困惑。把职责边界写成**自包含**形式:"X 不在本步范围内,不要做",而不是"X 交给别人"。引用别的产出只有一种合法方式:该文件在本 step 的 `inject` 里(如修正步引用 `inject` 进来的审查意见)。
- **持久化不覆盖**:循环每一轮进 `steps/<id>/run-<N>/`。

## 可观测性:两类日志分离

- **控制流**(step 起止、check 裁决、路由、回跳、压缩、终止原因)→ `events.jsonl` + stderr。这是排查执行逻辑的第一手材料。
- **详细 agent 日志**(prompt、工具/shell 调用、check I/O)→ `agent-trace.log`,`-v` 时才镜像到 stderr。
- 真实运行很贵(约 $50/20min),**不要为了加日志而重跑**;可观测性数据都是运行时免费落盘的。

## 工作方式约定(来自过往协作)

- **改代码前先读代码核对**,尤其涉及 SDK 行为(usage/context/compaction 这类)时,用 venv 起小脚本实测,不要凭假设下结论。
- **不确定就诚实说**,包括"我上一轮的建议错了"。给的数字/阈值要标明是实测还是外推。
- **本仓库无远程、不开分支**,到达自洽且测试通过的节点可直接在 master 上主动提交,不必每次等明确指示;破坏性或有争议的改动仍先说一声。
