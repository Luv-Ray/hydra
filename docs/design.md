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

完整 DSL 关键字与语义参考见 [`specs/dsl-reference.md`](specs/dsl-reference.md)(以代码为准,持续维护)。

## 架构

```
YAML pipeline
     │
     ▼
  loader.py + models.py ← 严格解析(未知键/类型报错)+ inputs + defaults + 模板
     │
     ▼
  validate.py   ← 引用、表达式、loop 结构、环、并发 resume 安全
     │
     ▼
  dag.py        ← 依赖推导 → 调度图(节点 = step 或整个 loop)
     │
     ▼
  executor.py   ← 数据流调度 + when + loop 轮次 + verdict + 异常重试
     │
     ├── provider.py    ← Claude Agent SDK 调用(run / verdict 追问 / compact)
     ├── inject.py      ← 文件注入 + 输出契约 / verdict 契约
     ├── events.py      ← 结构化事件流(控制流可观测性)
     └── persistence.py ← .hydra/runs/ 持久化与接续
```

## 核心概念

### 上下文传递：文件优先

- `output: path` — step 回复正文写入文件
- `inject: [paths]` — 读文件内容注入 prompt
- `resume: step_id` — 接续某 step 的 session(默认追加,`fork: true` 在副本上继续)

### 执行模型

1. 解析 YAML → Pipeline 对象,校验
2. 推导依赖,把每个 loop 收成一个节点,得到无环调度图
3. 数据流调度:节点的依赖全部完成即启动
4. step 启动时求值 `when`,为假则跳过
5. step 跑完读出 `verdict`(如有),供下游 `when` 与 loop 的 `until` 使用
6. loop 节点按轮运行 body → 判定 until → fix,直到成立或轮次用尽

### 分支与循环

- 分支 = `when` 读上游 verdict;重做 = 声明式 `loops`。没有 goto。
- verdict 是答案;报错、空回复、读不出 verdict 是"没有答案",按 `retry` 重试后失败,不会被当成 REJECT。
