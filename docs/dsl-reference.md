# Hydra DSL 参考

## 顶层字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 任务名称，用于日志输出 |
| `workdir` | string | 否 | 工作目录，step 中的相对路径基于此解析 |
| `steps` | list | 是 | 步骤定义列表 |

## Step 字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 步骤唯一标识，其他 step 通过此 id 引用 |
| `prompt` | string | 是 | 发送给 agent 的指令 |
| `fork_from` | string | 否 | 继承指定 step 的完整对话历史（fork session） |
| `output_from` | string | 否 | 将指定 step 的输出文本作为上下文注入 prompt 前 |
| `after` | string 或 list | 否 | 等待指定 step 完成后再执行，不注入任何内容 |
| `inject` | list | 否 | 额外注入内容列表，拼接到 prompt 前 |
| `output` | string | 否 | 将 agent 返回的文本写入此路径（供人查看） |
| `model` | string | 否 | 覆盖默认模型（如 `claude-haiku-4-5`） |
| `max_turns` | int | 否 | agent loop 最大轮数（默认由 SDK 决定） |
| `allowed_tools` | list | 否 | 覆盖默认工具列表 |
| `compact` | bool | 否 | 仅与 `fork_from` 配合，压缩继承的上下文（未实现） |

### fork_from、output_from 与 after

三者都会产生依赖关系（被引用的 step 必须先完成），区别在于注入了什么：

| 字段 | 产生依赖 | 注入到 prompt |
|------|----------|---------------|
| `fork_from` | 是 | 完整对话历史（fork session） |
| `output_from` | 是 | 输出文本 |
| `after` | 是 | 无 |

`after` 支持单个 string 或 list：

```yaml
# 单个依赖
- id: run_tests
  after: final_fix
  prompt: "运行测试套件"

# 多个依赖
- id: deploy
  after: [run_tests, build]
  prompt: "部署到 staging"
```

适用：step 依赖前序 step 的副作用（写了文件、做了 git commit），但不需要读它的输出文本。

---

### fork_from

两者互斥，只能写一个。

**`fork_from: step_id`**

Fork 指定 step 的完整 session。agent 看到的是从第一条消息到该 step 结束的所有对话历史（包括工具调用、中间推理等）。相当于你在 Claude Code 里继续输入下一条消息。

适用：需要接续工作的场景。比如 step A 读了代码做了分析，step B 要在同一个上下文里继续修复。

代价：完整历史会占用上下文窗口。多个 step fork 同一个 parent = N 份完整上下文的 token 成本。

**`output_from: step_id`**

只取指定 step 的最终输出文本，作为新 session 的 prompt 前缀注入。agent 看到的是一段文本，不知道它是怎么产生的。

适用：基于结论做独立判断的场景。比如 step A 输出了一份分析报告，step B 作为安全专家审查这份报告。

优势：上下文短、无锚定偏见、token 成本低。

### inject

inject 是一个列表，每项为以下两种之一：

**`output_of: step_id`**

注入指定 step 的输出文本。从内存读取，不走磁盘。

```yaml
inject:
  - output_of: security_review
  - output_of: perf_review
```

**`file: path`**

注入磁盘上已存在的静态文件内容。路径相对于 `workdir`。

```yaml
inject:
  - file: "knowledge/owasp-checklist.md"
  - file: "specs/api-design.yaml"
```

### output

将 agent 的最终返回文本写入指定文件路径。

- 路径相对于 `workdir`
- 目录会自动创建
- 不影响步骤间的信息传递（其他 step 用 `output_of` 从内存获取）
- 纯粹是持久化产物，供人查看或后续流程使用

## 执行规则

1. **依赖推导**：从 `fork_from`、`output_from`、`inject.output_of` 自动推导 step 间依赖
2. **并发执行**：无依赖关系的 step 自动并发
3. **拓扑排序**：有循环依赖时报错
4. **按层执行**：同一层的 step 并发，层间串行

## 完整示例

```yaml
name: "bug-analysis-pipeline"
workdir: "."

steps:
  - id: investigate
    prompt: |
      阅读代码库，排查以下 bug 的根因：
      用户反馈：登录后偶发 session 丢失，约 5% 请求返回 401。
    output: "reports/investigation.md"

  - id: security_review
    output_from: investigate
    prompt: "你是一位安全专家。从安全角度审查上述分析和修复方案。"
    output: "reports/security.md"

  - id: perf_review
    output_from: investigate
    prompt: "从性能角度审查修复方案。高并发下是否有问题？"
    output: "reports/perf.md"

  - id: final_fix
    fork_from: investigate
    inject:
      - output_of: security_review
      - output_of: perf_review
    prompt: "综合审查意见，实现最终修复。"
```
