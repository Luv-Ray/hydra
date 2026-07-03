# Hydra DSL 参考

> **本文档以代码为准,持续维护。** 关键字与语义直接对照 `hydra/` 源码
> (`models.py` / `validate.py` / `dag.py` / `inject.py` / `expr.py` / `cli.py` / `persistence.py`)。
> 带日期的 [`archive/2026-07-01-dsl-design.md`](archive/2026-07-01-dsl-design.md) 是早期设计快照,已归档,与现状有偏差,勿参考。

Hydra 是一个全自动 AI agent 编排引擎:用 YAML DSL 定义多步骤 pipeline,以 Claude Agent SDK 为执行后端。输入输出均为本地文件,无人工干预。

---

## 顶层结构

```yaml
name: "pipeline-name"          # 必填,pipeline 标识
version: 1                     # DSL 版本号,当前为 1

inputs:                        # 可选,运行时参数
  issue_id:
    description: "问题编号"
    required: true
  severity:
    description: "严重程度"
    default: "medium"

defaults:                      # 可选,step 属性默认值
  model: claude-sonnet-4-6
  retry: 0
  timeout: 1800        # 可选;省略则不限墙钟
  max_iterations: 10
  allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]

workdir: "./{{ inputs.issue_id }}"   # 工作目录,支持模板与相对路径

mcp_servers:                   # 可选,MCP server 配置,原样传给 Agent SDK
  my-server:
    command: "..."

steps:
  - ...
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | pipeline 标识(缺省时取 YAML 文件名) |
| `version` | 否 | DSL 版本,默认 `1` |
| `inputs` | 否 | 参数定义,通过 CLI `--var` / `--var-file` 传入 |
| `defaults` | 否 | step 级属性默认值,step 同名字段**覆盖**(非合并) |
| `workdir` | 否 | 工作目录,支持 `{{ inputs.xxx }}` 模板;相对路径相对于**当前工作目录**解析 |
| `mcp_servers` | 否 | MCP server 映射,整体透传给 provider |
| `steps` | 是 | step 列表,至少一个 |

### inputs 定义

```yaml
inputs:
  issue_id:
    description: "问题编号"   # 说明文字,可选
    required: true           # 缺失时启动报错,默认 false
    default: "medium"        # 默认值,可选
```

传入优先级:CLI `--var` > `--var-file` > `default`。`required: true` 且最终无值 → 启动时 `ValidationError`(退出码 2)。

### defaults 覆盖规则

step 未显式设置的字段,取 defaults 的值;显式写了就覆盖。**没有合并、没有追加。** 可作为默认的字段:`model` / `retry` / `timeout` / `max_iterations` / `allowed_tools`。

> 注意:`defaults` 没有 `provider` 字段。当前实现固定使用 `ClaudeProvider`。

---

## Step 全字段

以 `StepDef`(`models.py`)为准,共 18 个字段:

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | str | 必填 | 唯一标识 |
| `prompt` | str | 必填 | 发送给 LLM 的指令,支持 `{{ inputs.xxx }}` |
| `output` | str | — | 捕获**回复正文**写入文件路径(相对 workdir),父目录自动创建;声明后自动注入输出契约,见 [output 章节](#输出捕获-output) |
| `after` | str \| list | `[]` | 显式依赖的 step id;单个字符串会自动包成列表 |
| `inject` | str \| list | `[]` | 注入文件列表,内容拼在 prompt 前部,支持 glob |
| `resume` | str | — | 接续指定 step 的 session(数量约束见 [resume 章节](#会话接续-resume--压缩)) |
| `compact_before_resume` | int | — | resume 前压缩阈值(窗口百分比),见 [resume 章节](#会话接续-resume--压缩) |
| `model` | str | defaults | 覆盖模型 |
| `retry` | int | defaults(0) | 自动重试次数 |
| `retry_inject` | str | — | 重试反馈注入:`last_output` / `check_output` / 不写(盲重试) |
| `timeout` | int | — | 单次执行墙钟超时(秒),**可选**;不写(且 defaults 也不写)则**不限**。写了则由 provider 用 `asyncio.wait_for` 强制,超时→该次执行按失败处理(走 retry/route) |
| `allowed_tools` | list | defaults | 允许的工具列表 |
| `max_turns` | int | — | agent 最大交互轮数 |
| `max_iterations` | int | defaults(10) | goto 回跳循环最大次数 |
| `routes` | list | — | 条件路由,见 [routes 章节](#条件路由--goto) |
| `check` | obj | — | 结果验证,见 [check 章节](#结果验证-check) |
| `when` | str | — | 静态条件,为 false 时跳过该 step |
| `trigger` | str | `dag` | 调度方式:`dag`(默认,DAG 自动调度)或 `route`(仅 goto 触达) |

---

## 依赖推导与并发

依赖来源取**并集**(`dag.py`):

1. **`resume: X`** → 依赖 X
2. **`after: [X, Y]`** → 显式依赖 X、Y
3. **inject 精确文件反推** → inject 中的**精确路径**若匹配某 step 的 `output`,自动产生依赖

关键约束:

- **glob 不参与依赖推导**(`reports/*.md` 不会自动等待任何 step)。inject 了 glob 又需要等上游产出时,必须显式写 `after`。
- inject 反推**只在生产者在 YAML 中声明于消费者之前**时生效。这是为了避免 goto 回跳场景里产生环(回跳目标往往声明在后面)。

### 并发

- 无依赖关系的 step 自动并发执行,无需显式声明——拓扑层自然决定。
- 同一拓扑层的多个 step 通过 `asyncio.gather` 并发。
- `hydra run --concurrency N` 用信号量限制最大并发数(`0` = 不限制)。
- `trigger: route` 的 step **不进入 DAG**,不参与拓扑调度,仅通过 goto 触达。
- 检测到环 → `ValueError`(拓扑排序阶段)。

---

## 上下文注入 inject

`inject.py` 的实际行为:

- 每个注入文件渲染为 `[File: <相对路径>]\n<内容>`,多个文件之间及与 prompt 之间用 `\n\n---\n\n` 分隔,整体拼在 prompt **前部**。
- **精确文件不存在** → **前向运行时报错**(`FileNotFoundError`),避免拼错的 inject 路径被悄悄吞掉。**唯一例外**:回跳循环中(该 step 已进入 `iteration_counts`)容忍缺失并静默跳过,因为第一轮上游可能还没产出该文件。
- **glob 模式**(含 `*` 或 `?`)→ `glob.glob(recursive=True)` 展开,匹配到 0 个文件时静默跳过(glob 本就不"期待"具体文件)。
- 相对路径相对 workdir 解析;显示路径尽量取相对 workdir 的形式。

> 校验期 warning:inject 含 glob 且该 step 没有任何 `after` 依赖 → 提示"文件运行时可能不存在"(不阻塞执行)。

---

## 输出捕获 output

`output` 字段声明后,该 step 的**回复正文**(`ResultMessage.result`,即 agent 跑完后返回的最后一段消息)会被**原样写入**指定文件(`executor.py` → `_write_output`);父目录自动创建,相对路径相对 workdir 解析。

注意捕获的是**回复本身**,不是 agent 用工具写的某个文件。这带来一个易踩的坑:

- step 若几乎不用工具、prompt 就是"给出结论/审查/裁决",回复本身即答案 → `output` 抓得准。
- step 若做了大量工具操作(多轮 Read/Write/Bash),回复容易退化成"我做了什么"的对话式总结 → `output` 抓到的是工作日志,而非预期产物。

### 自动注入的输出契约

为消除上述错配,只要 step 声明了 `output`,`build_prompt`(`inject.py`)会在拼好的 prompt **末尾**自动追加一段 **output contract**(英文),向模型说明:这条回复会被逐字存成该文件、并被后续 step 当输入读取,因此它是**文件内容本身而非给读者的消息**,应直接输出文件该有的内容、不加任何前言或旁白。

要点:

- 契约由框架统一注入,**无需在每条 prompt 里手写**这类要求。
- 契约会点名具体的目标文件路径。
- 仅在 `output` 存在时注入;不写 `output` 则不注入。
- 契约是**引导而非强制**。对关键产物想上硬保险,可再叠一层:把该 step 的 `allowed_tools` 收紧为只读(如 `[Read, Grep, Glob]`),模型无法旁路写别的文件,只能把产物作为回复输出。
- 若更希望 agent 自己用 Write 落盘长文档(而非经由回复捕获),则**不要**写 `output`,改在 prompt 里指明目标文件——此时不注入契约,两种模式互不干扰。

---

## 会话接续 resume + 压缩

```yaml
- id: analyze
  prompt: "分析根因"
  output: root-cause.md

- id: review
  resume: analyze          # 继承 analyze 的完整 session
  inject: [checklist.md]   # 可与 inject 组合:session 保上下文,inject 补新信息
  compact_before_resume: 50
  prompt: "审查上述分析"
```

### 语义

- 继承指定 step 的完整 Agent SDK session,在同一会话中继续对话(映射到 SDK 的 `resume=session_id`)。
- 可与 `inject` 组合:resume 保留对话历史,inject 补充新文件。

### 约束(`validate.py`)

- **resume 数量约束**——`resume` 是往目标的磁盘 session **追加**、不是 fork 副本,并发 resume 会互相写坏 transcript。因此:同一 target 最多一个**非 route** step 可以 resume(DAG 同层会并发);`trigger: route` step 靠 goto 串行触发、不会重叠,数量不限(如 `mini-revise` 和 `revise` 都 resume `analyze`)。违反前者 → 校验报错。
- resume 引用的 step 必须存在。

### compact_before_resume

- 取值为**上下文窗口百分比**(整数)。resume 目标的上下文占比超过该阈值时,先对其执行一次 `/compact`(等价 CLI 的压缩),再进行 resume,使本步在更轻的上下文上继续。
- **免费判定**:占比直接取 resume 目标已完成结果里的 `input_tokens / context_window`(来自 `model_usage`),不额外消耗 API 调用;只有真正超阈值才付费执行 `/compact`。
- **并行安全**:多个 reviewer 同时 resume 同一 session 时(如三个 challenge 步),按 resume 目标加锁,保证只压缩一次,其余重新读取已压缩后的大小并跳过。
- 压缩会持久化到磁盘 session,后续 resume 同一 id 的步骤自然继承更轻的对话。
- 不写该字段 → 永不压缩(行为与旧 pipeline 完全一致)。
- 事件日志(`events.jsonl` / stderr)会记录压缩前后 token 数,便于调参。推荐阈值取决于窗口大小,详见 [附录:压缩阈值](#附录压缩阈值参考)。

---

## 条件路由 routes + goto

```yaml
routes:
  - if: success
    goto: verify
  - if: failure
    goto: manual_fix
  - if: max_iterations      # 可选:回跳循环耗尽时的逃生出口
    goto: final_report
```

### 条件值

- `success` — step 成功完成(含 check 通过)
- `failure` — retry 耗尽后仍失败,或 check 判定失败
- `max_iterations` — **惰性条件**:不参与 success/failure 匹配,仅当该 step 作为回跳发起者、循环达到 `max_iterations` 上限时触发。用于"改了 N 轮仍不收敛,就带着当前结果前进"而非整体失败。目标受"只能前进"约束(见 [回跳循环](#goto-回跳循环) 第 6 条)。

### goto 目标

- `step_id` — 跳转到指定 step
- `[step_a, step_b]` — 并行触发多个 step
- `$end` — 终止 pipeline,标记成功
- `$fail` — 终止 pipeline,标记失败(抛 `ExecutionError`)

### 默认行为(不写 routes)

- 成功 → 继续 DAG 后续节点
- 失败 → retry 耗尽后**终止整个 pipeline**(报错"failed and has no failure route")

### goto 回跳循环

`goto` 指向 DAG 中已执行过的 step(回跳)即形成循环。规则(`executor.py`):

1. 判定回跳:任一目标已在 `results` 中即视为回跳。
2. 计算**子图**——从目标到发起者之间的所有 DAG step(`_find_subgraph`);`trigger: route` 的 step 被排除在子图外。
3. 清除子图 step(及发起者)的 results,按拓扑序重跑子图。
4. 重跑时 inject 读取的是文件**最新内容**(上一轮产出)。
5. `max_iterations` 计数器在**发起者**上,每次回跳 +1;达到上限时:
   - **发起者的** `routes` 里若有 `if: max_iterations` 分支 → 走该 goto(**逃生路由**),让 pipeline 带着最后一版未收敛结果继续,而不是整体失败。
   - 否则 → 抛错 "loop did not converge"(默认行为,向后兼容)。
   - ⚠️ 逃生路由必须写在**发起回跳的 step**(计数器所在者)上,不是写在审查/裁决 step 上。当"审查"与"修正"分属两个 step 时(如 `mini-review` 判定、`mini-revise` 回跳),`max_iterations` 和逃生路由都在**修正 step**(`mini-revise`)上。写错位置不报校验错,但运行时逃生静默失效、循环照旧失败。
6. **逃生路由只能前进**:目标必须是 `$end`、`$fail`,或尚未执行的前向 step。若指向循环内已跑过的 step(会重新武装已耗尽的循环)→ 报错。常见用法:`goto: <汇总/发布 step>`(强行放行)或 `goto: $end`(停在当前结果)。
7. 发起者自身仅当它是普通 DAG step 时才自动重跑;若发起者是 `trigger: route`(经显式 goto 触达),则由子图末步的 check/routes 决定去向,不自动重跑(否则死循环)。

> 校验保护(`validate.py`):任何 step 的 route 目标是"声明在自己之前"的 step(回跳),该 step **必须**设置 `max_iterations`,否则校验报错。

---

## 结果验证 check

三层判定,逐层递进:

| 层 | 判定 | 场景 |
|---|---|---|
| 执行层 | SDK `query()` 是否正常返回 | API 错误、超时 |
| 输出层 | output 文件是否写入 | agent 拒绝、无内容 |
| 语义层 | `check` 验证结果内容 | 按内容决定路由 |

不写 `check` 时只看前两层。

### 两种形态(互斥,必居其一)

**A. success_pattern —— 二元判定**

```yaml
check:
  prompt: "测试是否全部通过?只回答 yes 或 no"   # 可选
  success_pattern: "yes"                        # 正则,大小写不敏感
```

匹配 → success,否则 → failure(触发 retry 或 routes 的 failure 分支)。

**B. routes —— 多分支路由**

```yaml
check:
  prompt: "属于哪种情况?回答 fixable / needs_refactor / wont_fix"
  routes:
    - match: "fixable"
      goto: apply_fix
    - match: "needs_refactor"
      goto: refactor
    - match: "wont_fix"
      goto: close_issue
```

`match` 为正则,第一个匹配的 route 生效并直接 goto;都不匹配 → failure。

### check.prompt 可选

- **写了 `prompt`**:step 主体完成后,在同一 session 追问该 prompt,对**追问的回复**做匹配。
- **省略 `prompt`**:直接对 step **自身的输出**(`output_text`)做匹配。适用于让 step 自己在输出里给出裁决信号(如末尾输出 `ACCEPT` / `REJECT`)。

### 校验约束(`validate.py`)

- `success_pattern` 与 `routes` **互斥**,不能同时写。
- 二者**必居其一**,`check` 不能为空。

---

## 重试 retry

```yaml
- id: fix
  prompt: "修复 bug"
  retry: 3
  retry_inject: last_output
  check:
    prompt: "测试通过了吗?只回答 yes 或 no"
    success_pattern: "yes"
  routes:
    - if: failure
      goto: manual_fix
```

- `retry: N` — 失败后自动重跑,最多 N 次。check 判定失败也算失败,触发重试。
- 有 `resume: X` 时,每次重试从 X 的 session **重新 resume**(干净起点 + 完整上下文);无 resume 时每次全新 session。
- retry 全部耗尽后才触发 routes 的 `failure` 分支;无 routes 且耗尽 → pipeline 终止。

### retry_inject —— 重试反馈注入

| 取值 | 注入内容 |
|---|---|
| `last_output` | 上一次 step 的完整输出 |
| `check_output` | 仅 check 的回复 |
| 不写 | 盲重试 |

注入格式(拼在 prompt 前部):

```
[Output from previous attempt]
...上次输出...

[Retry with the above feedback]

...原始 prompt...
```

---

## 条件执行 when

```yaml
- id: deep_analysis
  when: "{{ inputs.severity == 'critical' }}"
  prompt: "深度分析..."
```

- 基于 inputs 在 pipeline 启动时**静态求值**;为 false 时整个 step 跳过,不参与 DAG。
- 表达式经 `ast` 安全求值(`expr.py`),外层 `{{ }}` 可选。**仅支持**:
  - 比较:`==` `!=` `in` `not in`
  - 布尔:`and` `or` `not`
  - 值:字面常量、`inputs.xxx`、`True` / `False`
- 其他语法(函数调用、算术、下标等)→ `TemplateError`。
- 未定义的 `inputs.xxx` 在 when 中取空字符串;在 prompt/output/workdir 模板中则报错。

> 注意:被跳过的 step 若有下游只靠 inject 文件依赖它,运行时文件不存在会中断——下游应有对应 `when` 或改用显式 `after` 控制。

---

## CLI

当前仅两个命令(`cli.py`)。

```bash
# 执行
hydra run pipeline.yaml
hydra run pipeline.yaml --var issue_id=ISS-001 --var severity=critical
hydra run pipeline.yaml --var-file vars.yaml
hydra run pipeline.yaml --dry-run              # 只打印执行计划,不运行
hydra run pipeline.yaml --concurrency 3        # 限制并发
hydra run pipeline.yaml -v                     # 详细日志(含 assistant 文本)

# 校验(不执行)
hydra validate pipeline.yaml
```

`run` 选项:`--var`(可多次)、`--var-file`、`--concurrency`(默认 0=不限)、`--dry-run`、`-v/--verbose`。

> 早期设计里的 `hydra resume <run_dir>` 断点续跑命令**尚未实现**,勿依赖。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | pipeline 成功 |
| 1 | pipeline 失败 / 执行错误 |
| 2 | 校验失败 / 参数缺失 |

---

## 运行时目录与可观测性

`persistence.py` + `events.py` 的实际布局:

```
<workdir>/.hydra/runs/<timestamp>-<hash>/
├── meta.json           # pipeline 名、参数、开始时间、workdir
├── result.json         # 最终状态 success/failure + 结束时间
├── events.jsonl        # 结构化控制流事件(见下)
├── agent-trace.log     # 详细 agent 日志:prompt 预览、工具/shell 调用、check I/O
└── steps/
    └── <step_id>/
        ├── output.md   # step 输出(有 output 字段时)
        ├── check.txt   # check 回复(有 check 时)
        ├── meta.json   # 状态、耗时、cost、turns、session_id、iteration
        └── run-<N>/    # 第 N 次执行(循环回跳/重跑,不覆盖历史轮)
            └── ...
```

- run 目录名带随机 hash 后缀,避免同秒并发碰撞。
- **两类日志分离**:控制流(step 起止、check 裁决、路由、回跳、压缩、终止原因)走 `events.jsonl` + stderr;详细 agent 日志走 `agent-trace.log`(`-v` 时也镜像到 stderr)。
- 循环中每一轮的产出保存在独立 `run-<N>/` 子目录,不覆盖。

---

## validate 检查项

`hydra validate` 执行以下检查(`validate.py`):

1. id 唯一性
2. `after` / `resume` 引用的 step 存在
3. `resume` 1:1——不被多个下游共用
4. `routes` / `check.routes` 的 goto 目标存在(或为 `$end` / `$fail`)
5. `trigger: route` 的 step 至少被一个 goto 引用(否则 **warning**:永不执行)
6. 回跳 route 的发起 step 必须有 `max_iterations`
7. `when` 表达式语法合法
8. `check` 的 `success_pattern` 与 `routes` 互斥且必居其一
9. inject 含 glob 但无相关 `after` → **warning**

硬错误抛 `ValidationError`(退出码 2);warning 打印但不阻塞。

---

## 附录:压缩阈值参考

`compact_before_resume` 的取值没有"标准答案",研究表明推理质量的衰减主要跟**绝对 token 数**和任务类型相关,而非窗口百分比。经验值:

| 窗口 | 建议 `compact_before_resume` | 约触发 token | 依据 |
|---|---|---|---|
| 200k | 50–60% | 100–120k | 多数模型在此之前推理仍稳 |
| 1M | 50% 左右 | ~500k | 2026 年实测同代 Claude 多跳推理拐点在 512K 附近 |

务必用 `events.jsonl` 记录的压缩前后 token 数,结合自己 pipeline 的裁决质量微调,而非照搬。



