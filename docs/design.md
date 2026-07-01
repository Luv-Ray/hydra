# Hydra — AI Agent 编排引擎

## 背景

全自动化 AI agent 编排系统：
- 输入输出都是本地文件，无人工干预
- 文件为主的上下文传递，session resume 做 1:1 接续
- 任务流程用 YAML DSL 定义，支持条件路由和循环重试
- 单步执行质量对齐 Claude Code

## 技术栈

- **语言**：Python 3.10+
- **执行引擎**：Claude Agent SDK（Anthropic 官方）
- **DSL 解析**：PyYAML
- **CLI**：Click
- **并发**：asyncio

## 设计文档

详细 DSL 规范见 `docs/specs/2026-07-01-dsl-design.md`。

## 架构

```
YAML pipeline
     │
     ▼
  loader.py     ← 解析 + 模板展开 + defaults 合并
     │
     ▼
  validate.py   ← 静态校验
     │
     ▼
  dag.py        ← 依赖推导 + 拓扑排序
     │
     ▼
  executor.py   ← DAG 执行 + routes + retry + goto
     │
     ├── provider.py    ← Claude Agent SDK 调用
     ├── inject.py      ← 文件注入
     ├── check.py       ← 结果验证
     └── persistence.py ← .hydra/runs/ 持久化
```

## 核心概念

### 上下文传递：文件优先

- `output: path` — step 结果写入文件
- `inject: [paths]` — 读文件内容注入 prompt
- `resume: step_id` — 1:1 session 接续（继承完整对话历史）

### 执行模型

1. 解析 YAML → Pipeline 对象
2. 求值 `when` 条件，标记跳过的 step
3. 构建 DAG（`trigger: dag` 的 step）
4. 按拓扑层并发执行
5. 每步执行后：check → routes → 可能 goto（前跳或回跳）
6. `trigger: route` 的 step 不在 DAG 中，仅通过 goto 触达

### 条件路由 & 循环

- `routes` 基于 success/failure 跳转
- goto 回跳形成循环（每次全新 session + 文件为最新内容）
- `max_iterations` 防止无限循环
- `retry` + `retry_inject` 做带反馈的重试
