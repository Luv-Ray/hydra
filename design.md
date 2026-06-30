# Hydra — AI Agent 编排引擎

## 背景

需要一个全自动化的 AI agent 编排系统：
- 输入输出都是本地文件，无人工干预
- 上下文可树形分叉（一次推理结果 fork N 条独立路径）
- 任务流程用 DSL 文件定义，支持多轮交互模拟
- 单步执行质量对齐 Claude Code

## 技术栈

- **语言**：Python 3.10+
- **执行引擎**：Claude Agent SDK（Anthropic 官方，Claude Code 的可编程版本）
- **MCP**：Agent SDK 内置
- **DSL 解析**：PyYAML
- **并发**：asyncio

## 为什么选 Claude Agent SDK

Claude Agent SDK 是 Anthropic 官方提供的库，将 Claude Code 的完整能力以 Python/TypeScript SDK 形式暴露：
- 同一套 agent loop、工具、上下文管理
- 原生 `resume` / `fork_session` 支持——完美匹配 Hydra 的 context/continue 语义
- 50+ 内置工具（Read, Edit, Write, Bash, Glob, Grep, WebSearch...）
- MCP client 一行配置
- Session 持久化到 JSONL，支持跨进程恢复
- Hooks 系统（PreToolUse, PostToolUse, Stop...）
- Subagent 支持

Hydra 只需写编排层（~300 行 Python），执行质量由 Agent SDK 保证。

---

## 架构

```
task.yaml (DSL)
    ↓ PyYAML 解析
Hydra Orchestrator (~300 行 Python)
    ├── DSL 解析 → List[StepDef]
    ├── DAG 构建（从 context/continue/inject 引用推导依赖）
    ├── 拓扑排序 + asyncio 并发执行无依赖 step
    ├── context/continue/inject 语义实现
    └── 调用 ↓

Claude Agent SDK (已有，无需修改)
    ├── Agent loop (query → tools → continue)
    ├── 50+ 内置工具
    ├── MCP client
    ├── Session 持久化 + resume + fork
    └── Context 管理 + compact
```

---

## 核心设计

### 上下文控制模型

每个 step 通过两个互斥关键字声明上下文来源：

| 关键字 | 语义 | Agent SDK 映射 | 适合场景 |
|---|---|---|---|
| `continue: step_id` | 继承完整对话历史 | `resume=session_id, fork_session=True` | 接续未完成的工作 |
| `context: step_id` | 只注入该步骤的输出文本 | 新 session，output 拼入 prompt | 基于结论做新任务 |
| 都不写 | 空上下文 | 新 session | 完全独立的任务 |

补充原语：

| 关键字 | 语义 |
|---|---|
| `inject` | 额外注入内容（文件、其他 step 的输出、自定义文本） |
| `compact` | 对继承的上下文做压缩（仅与 `continue` 配合，未来实现） |

规则：
1. `context` 和 `continue` 互斥
2. 多个 step 引用同一个 parent → 各自独立 fork，互不干扰
3. `inject` 可与任何模式组合
4. 无依赖关系的 step 自动并发

### 为什么区分 context 和 continue

| 模式 | Agent SDK 行为 | 信息量 | 风险 |
|---|---|---|---|
| `continue` | fork_session=True，完整 JSONL 历史 | 大 | 上下文长、注意力稀释 |
| `context` | 新 session，仅 output 文本 | 小 | 丢失探索过程细节 |

对抗性审查等场景应该用 `context`（审查者像外部评审看方案文档），代码修复接续应该用 `continue`（保留完整探索路径）。

---

## DSL 格式

```yaml
name: "bug-analysis-pipeline"
workdir: "."

mcp_servers:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/project"]
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"

steps:
  - id: investigate
    prompt: |
      阅读代码库，排查以下 bug 的根因：
      用户反馈：登录后偶发 session 丢失，约 5% 请求返回 401。
    output: "reports/investigation.md"

  - id: security_review
    context: investigate
    inject:
      - file: "knowledge/owasp-session-checklist.md"
    prompt: "从安全角度审查上述分析和修复方案。"
    output: "reports/security.md"

  - id: perf_review
    context: investigate
    prompt: "从性能角度审查修复方案。高并发下是否有问题？"
    output: "reports/perf.md"

  - id: correctness_review
    context: investigate
    prompt: "构造边界条件和极端场景，找出方案中的逻辑漏洞。"
    output: "reports/correctness.md"

  - id: final_fix
    continue: investigate
    inject:
      - output_of: security_review
      - output_of: perf_review
      - output_of: correctness_review
    prompt: "综合以上审查意见，实现最终修复。"

  - id: update_docs
    inject:
      - output_of: final_fix
    prompt: "根据修复内容更新 CHANGELOG。"
    output: "CHANGELOG.md"
```

### 执行拓扑（自动推导）

```
investigate
    ├── security_review   ─┐
    ├── perf_review       ─┼── 并发
    └── correctness_review─┘
                               ↓
final_fix（依赖 investigate + 三个 review）
    ↓
update_docs
```

---

## 实现

### 项目结构

```
hydra/
├── pyproject.toml
├── hydra/
│   ├── __init__.py
│   ├── __main__.py     # python -m hydra
│   ├── cli.py          # 入口: hydra task.yaml
│   ├── models.py       # DSL 数据结构 (dataclass)
│   ├── dag.py          # DAG 构建 + 拓扑排序
│   ├── executor.py     # 核心执行引擎 (asyncio + Agent SDK)
│   └── inject.py       # inject 内容组装
└── examples/
    └── bug-analysis.yaml
```

### 核心代码

executor.py 的 `execute_step`：

```python
async def execute_step(step, results, task):
    options = ClaudeAgentOptions(
        allowed_tools=step.allowed_tools or DEFAULT_TOOLS,
    )

    # continue 模式: fork 父会话
    if step.continue_from:
        parent = results[step.continue_from]
        options.resume = parent.session_id
        options.fork_session = True

    # 组装 prompt (context + inject + step.prompt)
    prompt = build_prompt(step, results, workdir=task.workdir)

    # 执行 Agent SDK
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            session_id = msg.session_id
            result_text = msg.result

    return StepResult(session_id=session_id, output_text=result_text)
```

DAG 并发执行：

```python
async def execute_dag(task):
    dag = build_dag(task.steps)
    layers = topological_layers(task.steps, dag)

    for layer in layers:
        if len(layer) == 1:
            results[layer[0]] = await execute_step(...)
        else:
            # 并发执行同层所有 step
            layer_results = await asyncio.gather(
                *[execute_step(step_map[sid], results, task) for sid in layer]
            )
```

---

## 扩展点（v2）

### 条件分支

```yaml
  - id: check_tests
    continue: fix
    prompt: "运行测试套件，报告是否全部通过"
    condition:
      on_success: deploy
      on_failure: retry_fix
```

### 循环重试

```yaml
  - id: fix_and_verify
    continue: investigate
    prompt: "修复 bug 并运行测试验证"
    retry:
      max_attempts: 3
      until: "所有测试通过"
```

### Per-step 模型选择

```yaml
  - id: quick_check
    model: "claude-haiku-4-5"
    prompt: "检查文件格式是否正确"

  - id: deep_analysis
    model: "claude-opus-4-6"
    prompt: "分析架构设计的合理性"
```

---

## 已知风险

1. **Agent SDK 计费** — 2026.6.15 起 Agent SDK 用量从独立额度扣除
2. **并发文件冲突** — Fork 分支同时修改代码时需文件系统隔离
3. **Token 成本** — `continue` fork N 路 = N 份完整上下文，用 `context` 控制成本
4. **Session 存储** — 默认存 `~/.claude/projects/`，大量 fork 会积累文件

---

## 运行

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
pip install -e .
hydra examples/bug-analysis.yaml
```

## 开发计划

| 阶段 | 内容 | 状态 |
|---|---|---|
| MVP | DSL 解析 + DAG 执行 + context/continue/inject | 已完成 |
| v0.2 | MCP + per-step model + compact | 下一步 |
| v0.3 | retry + condition + 变量模板 | 计划中 |
| v0.4 | 断点续跑 + 日志 | 计划中 |
