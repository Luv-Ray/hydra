# Hydra DSL v1 设计规范

## 概述

Hydra 是一个轻量 AI agent 编排引擎，用 YAML DSL 定义多步骤 pipeline，以 Claude Agent SDK 为执行后端。DSL 设计目标：

- 团队内部使用，可读性优先
- 覆盖代码生成/修改、文档/报告两类场景
- 扁平 DAG 结构，并发自动推导
- 简单条件分支（success/failure 二选一）
- 文件为主的上下文传递
- 不硬绑定 Claude，DSL 层 provider 无关

---

## 顶层结构

```yaml
name: "pipeline-name"          # 必填，pipeline 标识
version: 1                     # DSL 版本号

inputs:                        # 可选，运行时参数
  bug_desc:
    description: "Bug 描述"
    required: true
  severity:
    description: "严重程度"
    default: "medium"

defaults:                      # 可选，step 属性默认值
  model: claude-sonnet-4-6
  retry: 2
  timeout: 300
  max_iterations: 10
  allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]

workdir: "."                   # 工作目录（绝对或相对于 YAML 文件）

steps:
  - ...
```

### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | pipeline 唯一标识 |
| `version` | 是 | DSL 版本，当前固定为 `1` |
| `inputs` | 否 | 参数定义，通过 CLI `--var` 传入 |
| `defaults` | 否 | step 级属性的默认值，step 同名字段覆盖（非合并） |
| `workdir` | 否 | 工作目录，支持绝对路径，不存在时自动 mkdir -p |
| `steps` | 是 | step 列表 |

### inputs 传入方式

```bash
hydra run task.yaml --var bug_desc="xxx" --var severity=critical
hydra run task.yaml --var-file vars.yaml
```

### defaults 覆盖规则

step 没写的字段用 defaults 的值，写了就覆盖。没有合并、没有追加。

---

## Step 定义

```yaml
steps:
  - id: investigate
    prompt: |
      排查 bug: {{ inputs.bug_desc }}
    output: reports/investigation.md

    # 依赖声明
    after: [step_a, step_b]

    # 上下文注入
    inject:
      - reports/investigation.md
      - reports/*.md
      - knowledge/checklist.md

    # 会话接续
    resume: parent_step_id

    # 执行控制（覆盖 defaults）
    model: claude-opus-4-6
    retry: 3
    timeout: 600
    allowed_tools: [Read, Edit, Write, Bash, Glob, Grep, WebSearch]
    max_turns: 50

    # 条件路由
    routes:
      - if: success
        goto: next_step
      - if: failure
        goto: fallback_step

    # 结果验证
    check:
      prompt: "测试是否通过？只回答 yes 或 no"
      success_pattern: "yes"

    # 条件执行
    when: "{{ inputs.severity == 'critical' }}"

    # 调度方式
    trigger: dag                # dag（默认）或 route（只通过 goto 触发）
```

### Step 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 唯一标识 |
| `prompt` | 是 | 发送给 LLM 的指令，支持 `{{ inputs.xxx }}` 模板 |
| `output` | 否 | 结果写入文件路径（相对于 workdir），父目录自动创建 |
| `after` | 否 | 显式依赖的 step id 列表 |
| `inject` | 否 | 注入文件列表，内容拼接在 prompt 前部，支持 glob |
| `resume` | 否 | 接续指定 step 的 session（1:1，不可多个下游共用） |
| `model` | 否 | 覆盖 defaults 的模型 |
| `retry` | 否 | 自动重试次数，覆盖 defaults |
| `retry_inject` | 否 | 重试时注入反馈：`last_output` / `check_output` / 不写（盲重试） |
| `timeout` | 否 | 超时秒数，覆盖 defaults |
| `allowed_tools` | 否 | 允许的工具列表，覆盖 defaults |
| `max_turns` | 否 | agent 最大交互轮数 |
| `max_iterations` | 否 | goto 回跳循环最大次数，覆盖 defaults |
| `routes` | 否 | 条件路由，见下文 |
| `check` | 否 | 结果验证，见下文 |
| `when` | 否 | 静态条件表达式，为 false 时跳过此 step |
| `trigger` | 否 | 调度方式：`dag`（默认，DAG 自动调度）或 `route`（只通过 goto 触发） |

---

## 依赖与并发

### 依赖推导规则（优先级叠加取并集）

1. **`resume: X`** — 依赖 X
2. **`after: [X, Y]`** — 显式依赖 X 和 Y
3. **inject 精确文件反推** — inject 中的文件如果精确匹配某个 step 的 `output` 路径，自动产生依赖

glob 模式（如 `reports/*.md`）不参与依赖推导。如果 inject 了 glob 且需要等某些 step 完成，必须显式写 `after`。

### inject 文件不存在时的行为

- 精确文件路径：默认报错终止
- glob 模式：匹配到 0 个文件时静默跳过（不报错）
- 被 `goto` 回跳触发的 step：inject 的精确文件如果不存在，静默跳过（第一次循环时上游还没产出）

### validate warning

如果 inject 包含 glob pattern，且该 step 没有 `after` 任何 output 路径可能匹配该 glob 的 step → 输出 warning（不阻塞执行）。

### 并发规则

- 无依赖关系的 step 自动并发执行
- 无需显式声明并行——DAG 拓扑自然决定
- `--concurrency N` 限制最大并发数

---

## 会话接续（resume）

```yaml
steps:
  - id: investigate
    prompt: "排查 bug，阅读相关代码"
    output: reports/investigation.md

  - id: fix
    resume: investigate
    inject: [reports/review.md]
    prompt: "根据审查意见，实现修复"
```

### 语义

- 继承指定 step 的完整 Agent SDK session，在同一会话中继续对话
- 映射到 Agent SDK 的 `resume=session_id`
- 可与 `inject` 组合：resume 保留上下文，inject 补充新信息

### 约束

- 1:1 限制——同一个 step 只能被一个下游 resume
- 多个 step 都 `resume` 同一个 parent 时，validate 阶段报错
- 只有支持 session 的 provider 才能使用 resume

---

## 条件路由

```yaml
routes:
  - if: success
    goto: verify
  - if: failure
    goto: manual_fix
```

### 条件值

- `success` — step 成功完成
- `failure` — retry 耗尽后仍失败

### goto 目标

- `step_id` — 跳转到指定 step
- `[step_a, step_b]` — 并行触发多个 step，等全部完成后继续 goto 发起者的后续依赖链
- `$end` — 终止 pipeline，标记成功
- `$fail` — 终止 pipeline，标记失败

### 默认行为（不写 routes 时）

- 成功 → 继续 DAG 中的后续节点
- 失败 → retry 耗尽后终止整个 pipeline

### 循环保护

- 路由形成循环时，全局 `max_iterations: 10`（可在 defaults 或 step 上覆盖）
- 超过后自动走 `$fail`

### goto 回跳语义

`goto` 可以指向 DAG 中已执行过的 step（回跳），形成循环。规则：

1. 被回跳到的 step 重新执行（全新 session）
2. 该 step 的 DAG 下游（直到 goto 发起者自身）也全部重新执行
3. 每次重跑时，inject 读取的是文件的最新内容（上一轮的产出）
4. `max_iterations` 计数器在 goto 发起者上，每次回跳 +1

示例：review → revise 循环

```yaml
steps:
  - id: draft
    prompt: "生成设计文档"
    output: reports/draft.md

  - id: security_review
    after: [draft]
    inject: [reports/draft.md]
    prompt: "从安全角度审查"
    output: reports/security-feedback.md

  - id: perf_review
    after: [draft]
    inject: [reports/draft.md]
    prompt: "从性能角度审查"
    output: reports/perf-feedback.md

  - id: revise
    after: [security_review, perf_review]
    inject: [reports/draft.md, reports/security-feedback.md, reports/perf-feedback.md]
    prompt: "根据审查意见修改文档"
    output: reports/draft.md
    max_iterations: 3
    check:
      prompt: "审查意见是否都已解决？yes/no"
      success_pattern: "yes"
    routes:
      - if: success
        goto: $end
      - if: failure
        goto: [security_review, perf_review]
```

执行流程：draft → (security_review, perf_review 并发) → revise → check 失败 → 回跳到 [security_review, perf_review] 重跑 → revise 再次执行 → ... 直到 check 通过或 max_iterations 耗尽。

---

## 成功/失败判定

三层判定，逐层递进：

| 层 | 判定方式 | 触发场景 |
|---|---|---|
| 执行层 | Agent SDK `query()` 是否正常返回 | API 错误、超时 |
| 输出层 | output 文件是否成功写入 | agent 拒绝、无内容 |
| 语义层 | `check` 验证结果内容 | 需要根据内容决定路由 |

不写 `check` 时只看前两层。

### check 字段

```yaml
check:
  prompt: "测试是否全部通过？只回答 yes 或 no"
  success_pattern: "yes"
```

- step 主体完成后，用 `check.prompt` 在同一 session 追问
- 回复匹配 `success_pattern`（正则）→ success，否则 → failure
- check 失败也会触发 retry

### check 多路由模式

当需要根据结果内容做多分支路由（不只是 success/failure）时：

```yaml
check:
  prompt: "分析结果属于哪种情况？回答 fixable / needs_refactor / wont_fix"
  routes:
    - match: "fixable"
      goto: apply_fix
    - match: "needs_refactor"
      goto: refactor
    - match: "wont_fix"
      goto: close_issue
```

- `check.routes` 中的 `match` 是正则匹配
- 第一个匹配的 route 生效
- 如果都不匹配 → 视为 failure，触发 retry 或 step 级 routes 的 failure 分支
- `check.routes` 和 `check.success_pattern` 互斥，不能同时写
- 使用 `check.routes` 时，step 级的 `routes` 仍可作为兜底（处理 retry 耗尽的情况）

---

## 重试

```yaml
defaults:
  retry: 2

steps:
  - id: fix
    retry: 3    # 覆盖 defaults
    routes:
      - if: failure
        goto: manual_fix
```

### 语义

- `retry: N` 表示失败后自动重跑同一 step，最多 N 次
- 每次重试是一个全新的 session（除非有 resume）
- 有 `resume: X` 时，每次重试都从 X 的 session 重新 resume（干净起点 + 完整上下文）
- retry 全部耗尽后才触发 routes 的 `failure` 分支
- 不写 routes 且 retry 耗尽 → pipeline 终止

### retry_inject — 重试时的反馈注入

```yaml
- id: fix
  prompt: "修复 bug"
  retry: 3
  retry_inject: last_output
  check:
    prompt: "测试通过了吗？只回答 yes 或 no"
    success_pattern: "yes"
```

可选值：
- `last_output` — 注入上一次 step 的完整输出（包含 check 回复）
- `check_output` — 只注入 check 的回复
- 不写 — 盲重试

注入格式（追加在 prompt 前部）：
```
[Output from previous attempt]
...上次输出内容...
[Retry with the above feedback]

原始 prompt 内容...
```

---

## 条件执行（when）

```yaml
- id: deep_analysis
  when: "{{ inputs.severity == 'critical' }}"
  prompt: "深度分析..."
```

- 基于 inputs 在 pipeline 启动时静态求值
- 为 false 时整个 step 跳过，不参与 DAG 执行
- 依赖该 step 的下游：如果 step 被跳过且下游只通过 inject 文件依赖它，inject 时文件不存在会报错——所以被跳过的 step 的下游也应该有对应的 `when` 或用 `after` 显式控制

---

## 执行后端

### Provider 抽象

```python
class Provider(Protocol):
    async def run(self, prompt: str, options: StepOptions) -> StepResult:
        ...
```

- 当前实现：`ClaudeProvider`（调 Agent SDK `query()`）
- DSL 层面通过 `defaults.provider` 声明，当前只支持 `claude`
- 同一 pipeline 内所有 step 用同一 provider，只有 model 可以不同
- 未来扩展新 provider 时只需新增实现类

---

## CLI 接口

```bash
# 执行
hydra run pipeline.yaml
hydra run pipeline.yaml --var bug_desc="xxx" --var severity=critical
hydra run pipeline.yaml --var-file vars.yaml

# 校验（不执行）
hydra validate pipeline.yaml

# 干跑（展示 DAG + 参数解析结果）
hydra run pipeline.yaml --dry-run --var bug_desc="xxx"

# 断点续跑
hydra resume .hydra/runs/<timestamp>/

# 限制并发
hydra run pipeline.yaml --concurrency 3
```

### 运行时目录结构

```
.hydra/runs/<timestamp>/
├── meta.json           # pipeline 名称、参数、开始时间
├── steps/
│   ├── investigate/
│   │   ├── output.md   # step 输出
│   │   ├── session.jsonl  # Agent SDK session
│   │   └── meta.json   # 状态、耗时、token 用量
│   ├── security_review/
│   └── ...
└── result.json         # 最终状态（success/failure）
```

### 退出码

| 码 | 含义 |
|---|---|
| 0 | pipeline 成功完成 |
| 1 | pipeline 失败 |
| 2 | DSL 校验失败 / 参数缺失 |

---

## validate 检查项

- id 唯一性
- `after` / `resume` 引用的 step 存在
- `resume` 只被一个下游使用
- `trigger: route` 的 step 至少被一个 `goto` 引用（否则警告：永远不会执行）
- routes 循环有 `max_iterations` 保护
- `inputs` 中 `required: true` 的参数在运行时有对应 `--var`
- `when` 表达式语法合法
- workdir 路径合法

---

## 完整示例

```yaml
name: bug-fix-pipeline
version: 1

inputs:
  bug_desc:
    description: "Bug 描述"
    required: true

defaults:
  model: claude-sonnet-4-6
  retry: 2
  timeout: 300
  allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]

workdir: /home/user/project

steps:
  - id: investigate
    model: claude-opus-4-6
    prompt: |
      排查以下 bug 的根因，阅读相关代码，输出分析结论和修复方案。
      Bug: {{ inputs.bug_desc }}
    output: reports/investigation.md

  - id: security_review
    inject: [reports/investigation.md]
    prompt: "从安全角度审查上述分析和修复方案。"
    output: reports/security.md

  - id: perf_review
    inject: [reports/investigation.md]
    prompt: "从性能角度审查修复方案。"
    output: reports/perf.md

  - id: fix
    resume: investigate
    after: [security_review, perf_review]
    inject: [reports/security.md, reports/perf.md]
    prompt: "综合审查意见，实现最终修复。"
    retry: 3
    check:
      prompt: "运行测试，是否全部通过？只回答 yes 或 no"
      success_pattern: "yes"
    routes:
      - if: success
        goto: gen_changelog
      - if: failure
        goto: failure_report

  - id: gen_changelog
    trigger: route
    inject: [reports/investigation.md]
    prompt: "根据本次修复更新 CHANGELOG.md。"
    output: CHANGELOG.md

  - id: failure_report
    trigger: route
    inject: [reports/investigation.md, reports/security.md, reports/perf.md]
    prompt: "自动修复失败，生成问题总结报告供人工排查。"
    output: reports/failure.md
```

### 执行拓扑

```
investigate ──→ security_review ──┐
           └──→ perf_review ─────┤ (并发)
                                  ↓
                fix (resume investigate, 注入审查结果)
                 ├─ success → gen_changelog → $end
                 └─ failure → failure_report → $end
```

---

## 已知限制与未来扩展

| 当前限制 | 未来可扩展方向 |
|---|---|
| 条件只支持 success/failure | 自定义条件（基于 output 内容匹配） |
| when 只基于 inputs 静态求值 | 运行时动态条件 |
| 单 provider | 多 provider 支持 |
| 无变量模板（除 inputs） | step 间变量传递模板 |
| 无 webhook/cron 触发 | 外部触发机制 |
