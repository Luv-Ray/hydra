# Hydra DSL 参考(v2)

> **本文档以代码为准,持续维护。** 对照 `hydra/` 源码:字段与类型看 `models.py`,跨引用与图约束看 `validate.py`,依赖推导看 `dag.py`,调度与循环看 `executor.py`,注入与契约看 `inject.py`,表达式看 `expr.py`。
> `archive/` 下带日期的文件是早期设计快照,已归档,与现状不符,勿参考。

Hydra 用 YAML 定义多步骤 pipeline,以 Claude Agent SDK 为执行后端,输入输出均为本地文件,全程无人工干预。

v2 的控制流只有三个概念:

- **依赖**:step 在它依赖的一切完成后立即开跑(数据流调度,无关分支互不等待)。
- **`when`**:按 inputs 或上游 step 的 `verdict` 决定跑不跑——这就是**分支**。
- **`loops`**:声明式循环,重跑一组 step 直到 `until` 成立——这就是**重做**。

没有 goto、没有回跳、没有"只能被跳到"的 step。解析是**严格**的:任何未知键、错误类型都在加载时报错(`hydra validate` 能拦住 `outptu:` 这种拼写错误)。

---

## 顶层结构

```yaml
version: 2                     # 必填,只接受 2
name: "pipeline-name"          # 可选,缺省取文件名

inputs:
  issue_id: {description: "问题编号", required: true}
  severity: {default: "medium"}

defaults:                      # step 未写的字段取这里的值
  model: claude-sonnet-5-5
  retry: 1
  timeout: 1800
  allowed_tools: [Read, Edit, Write, Bash, Glob, Grep]

workdir: "./{{ inputs.issue_id }}"
mcp_servers: {}                # 原样透传给 Agent SDK
system_prompt: |               # 追加到引擎内置 system prompt 之后,作用于每个 step
  你是资深 Android 系统工程师,全程遵守证据分级。
user_preamble: |               # 前置到每个 step 的 user prompt 最前面
  产出文件写到当前工作目录,用相对路径。

steps: [...]                   # 必填,非空
loops: [...]                   # 可选
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `version` | 是 | 必须为 `2`;其他值(含缺省)报错 |
| `name` | 否 | pipeline 标识,缺省取文件名 |
| `inputs` | 否 | 参数定义,见下 |
| `defaults` | 否 | `model` / `retry` / `timeout` / `allowed_tools` 的默认值;step 写了同名字段就**整体覆盖**(不合并) |
| `workdir` | 否 | 工作目录,支持模板;相对路径相对于**启动时的当前目录** |
| `mcp_servers` | 否 | MCP server 映射,整体透传 |
| `system_prompt` | 否 | 追加到内置 system prompt 后,作用于每个 step;支持模板 |
| `user_preamble` | 否 | 前置到每个 step 的 user prompt(先于注入文件);支持模板 |
| `steps` | 是 | step 列表 |
| `loops` | 否 | 循环列表,见 [loops](#循环-loops) |

### inputs

```yaml
inputs:
  issue_id:
    description: "问题编号"   # 可选
    required: true           # 最终无值 → 启动报错
    default: "medium"        # 可选
```

- 取值优先级:CLI `--var` > `--var-file` > `default`。
- **只接受已声明的 input**:`--var` / `--var-file` 传了未声明的名字直接报错(防拼写错误)。
- 解析后的值是唯一来源:模板 `{{ inputs.x }}` 和表达式里的 `inputs.x` 读的是同一份(含 default 与 var-file)。

### 模板

`{{ inputs.NAME }}` 在加载时展开,作用于:`workdir`、`system_prompt`、`user_preamble`,以及 step 的 `prompt` / `output` / `inject` / `verdict.prompt`。引用未定义的 input → 加载报错。

---

## Step 字段

以 `StepDef`(`models.py`)为准:

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | str | 必填 | 唯一;只能是字母、数字、下划线(要能写成 `steps.ID.verdict`)。与 loop id 共用一个命名空间 |
| `prompt` | str | 必填 | 发给模型的指令 |
| `output` | str | — | 把**回复正文**写入该文件(相对 workdir),并追加输出契约。见 [output](#输出捕获-output) |
| `after` | str \| list | `[]` | 显式依赖 |
| `inject` | str \| list | `[]` | 注入文件,内容拼在 prompt 前;支持 glob |
| `resume` | str | — | 接续某 step 的 session。见 [resume](#会话接续-resume--fork--compact_before_resume) |
| `fork` | bool | `false` | 配合 `resume`:在 session 的**副本**里继续,不改动原 session |
| `compact_before_resume` | int | — | 1..100;resume 目标的上下文占比超过它时先 `/compact` |
| `verdict` | list \| obj | — | 声明裁决值集合,见 [verdict](#裁决-verdict) |
| `when` | str | — | 条件表达式,为假则跳过,见 [when](#条件执行-when) |
| `model` | str | defaults | 模型 |
| `retry` | int | defaults(1) | **异常结果**的重试次数,见 [retry](#重试-retry) |
| `timeout` | int | defaults(不限) | 单次执行墙钟秒数;超时按异常结果处理 |
| `allowed_tools` | list | defaults | 模型**可见**的内置工具集合(SDK `tools`);缺省 `Read Edit Write Bash Glob Grep`。MCP 工具不受它约束 |
| `max_turns` | int | — | agent 最大轮数 |

---

## 依赖推导与调度

一个 step 依赖的东西取以下四者的**并集**(`dag.py`):

1. `after: [X]`
2. `resume: X`
3. `inject` 中的**精确路径**等于某 step 的 `output` → 依赖该 step
4. `when` 中引用的 `steps.X.verdict` → 依赖 X

规则:

- **与 YAML 书写顺序无关**。消费者写在生产者前面也照样推导出依赖;真有环就报错。
- **glob 不产生依赖**。inject 了 glob 又要等上游时,写 `after`(否则 validate 给 warning)。
- 依赖一个 loop 内的 step = 依赖**整个 loop**(看到的是最后一轮的产物与 verdict)。
- **数据流调度**:每个节点(step 或整个 loop)在其依赖全部完成后立即启动,不按"层"等待——慢分支不会拖住无关分支。`--concurrency N` 限制同时运行的 step 数(0 = 不限)。
- 任一节点失败 → 取消其余正在运行的节点,pipeline 失败。

---

## 上下文注入 inject

- 每个文件渲染为 `[File: <相对路径>]\n<内容>`,各段以 `\n\n---\n\n` 分隔,依次为:`user_preamble` → 注入文件 → step `prompt` → 契约(如有)。
- **glob**(含 `*` / `?`)→ `glob(recursive=True)` 展开,0 匹配静默跳过。
- **精确路径缺失 → 报错**,唯一例外是它的生产者"尚未产出":
  - 生产者被 `when` 跳过了;
  - 生产者在 loop 内、本轮还没跑到(例如第 1 轮的 body 注入 fix 的产物);
  - 就是本 step 自己的 `output`(第 1 轮读不到上一版)。
  没有任何 step 声明为 `output` 的文件缺失,一律报错——拼错的路径不会被悄悄吞掉。

---

## 输出捕获 output

声明 `output` 后,step 的**回复正文**(`ResultMessage.result`)在 step 成功时被**原样写入**该文件,父目录自动创建。捕获的是回复本身,不是 agent 用工具写的文件。

### 输出契约

只要声明了 `output`,`build_prompt` 就在 prompt 末尾追加一段 **output contract**:回复会被逐字存成文件、供后续 step 读取,所以回复**就是**文件内容,不要前言旁白,也**不要自己用 Write/重定向落盘**(那份会被回复覆盖)。

- 契约**不点名**目标文件:点名会诱使模型自己写盘,再回一行"文件已完成"——这行字被捕获后覆盖掉正确的文件。
- 契约是引导不是强制。对关键产物可再把 `allowed_tools` 收紧为只读(如 `[Read, Grep, Glob]`),模型就无法旁路写文件。
- 想让 agent 自己用工具写长文档,就**不要**写 `output`,在 prompt 里指明目标文件即可。这类文件没有生产者声明,不参与依赖推导,下游要用 `after` 等待。

---

## 裁决 verdict

`verdict` 声明一个**封闭的取值集合**。step 跑完后引擎从回复里读出其中一个值,供 `when` / `until` 使用。

```yaml
- id: review
  verdict: [ACCEPT, REJECT]               # 简写:从本 step 回复的最后一行读

- id: test
  verdict:
    values: [pass, fail]
    prompt: "所有测试都通过了吗?"          # 写了 prompt:在同一 session 追问,从追问的回复里读
```

解析规则(`executor.parse_verdict`):

- 只看**最后一个非空行**。模型先写 ACCEPT、下面又改口 REJECT,以最后一行为准。
- 大小写不敏感,按**整词**匹配:`ACCEPT` 不匹配 `ACCEPTED` / `UNACCEPTABLE`;`**REJECT**`、`Verdict: reject` 都能匹配。
- 最后一行恰好命中**一个**值才算数;一个都没有或命中多个 → **异常结果**(走 retry,用尽则 pipeline 失败)。它**永远不会**被当成某个 verdict。
- 引擎在读 verdict 的那条消息末尾追加 **verdict contract**,要求模型以只含一个值的一行结尾。有 `output` 时,这行也是文件内容的一部分。
- 解析出的值是声明里的**规范写法**(`verdict: [pass, fail]` + 回复 `PASS` → `'pass'`),表达式里就写规范写法。
- 值必须互不相同(大小写不敏感)。**用 ASCII 词作值**;中文值之间若互为子串(如"通过"/"不通过")会判为歧义。

> ⚠️ **YAML 布尔陷阱**:不加引号的 `yes` / `no` / `on` / `off` / `true` / `false` 会被 YAML 解析成布尔。`verdict: [OK, NO]` 实际是 `["OK", False]`,加载时会报错提示你加引号:`[OK, 'NO']`。

---

## 表达式(when / until)

`ast` 安全求值(`expr.py`),外层 `{{ }}` 可选。**只允许**:

| 类别 | 写法 |
|---|---|
| 引用 | `inputs.NAME`(字符串,未赋值的可选 input 为 `""`);`steps.ID.verdict`(该 step 的 verdict,没产出过则为 `None`) |
| 字面量 | 字符串、数字、`True` / `False` / `None`、由字面量组成的 `[列表]` / `(元组)` |
| 运算 | `==` `!=` `in` `not in`;`and` `or` `not` |

其余(函数调用、下标、算术、`<` 等)→ 报错。validate 额外检查:

- `inputs.NAME` 必须是已声明的 input;
- `steps.ID` 必须存在且声明了 `verdict`;
- 与 `steps.ID.verdict` 比较的字面量必须是该 step 声明的值之一(`== 'ACCPET'` 直接报错,而不是永远不匹配)。

## 条件执行 when

```yaml
- id: triage
  verdict: [fixable, refactor, wontfix]
- id: fix
  when: "steps.triage.verdict == 'fixable'"
- id: redesign
  when: "steps.triage.verdict == 'refactor'"
- id: summary
  inject: [fix.md, redesign.md]           # 汇合点:被跳过分支的产物缺失是允许的
- id: deep_dive
  when: "inputs.severity == 'critical'"
```

- 在该 step **即将运行时**求值(此时它引用的上游已完成)。为假 → 跳过,记 `skip` 事件,verdict 为 `None`。
- **跳过不向下游传播**。下游照常运行;它注入的被跳过 step 的产物按"尚未产出"容忍缺失。想让下游一起跳过,就给下游写同样的条件——每个 step 跑不跑,只看它自己的 `when`。
- 例外:`resume` 一个被跳过的 step 无 session 可接 → 报错。resume 方要么带上同样的 `when`,要么别依赖可能被跳过的目标。

---

## 循环 loops

```yaml
loops:
  - id: review_loop
    body: [review]                         # 每轮重跑的 step(按 body 内的依赖排序,无依赖的并发)
    fix: revise                            # 可选:两轮之间运行的修正 step
    until: "steps.review.verdict == 'ACCEPT'"
    max_rounds: 3                          # body 最多跑几轮(>= 1)
    on_exhausted: continue                 # fail(默认)| continue
```

每一轮:

1. 运行 `body`(其内部依赖决定顺序;互不依赖的 body step 并发)。
2. 求值 `until`。成立 → loop **converged**,结束。
3. 不成立:若还有剩余轮次 → 运行 `fix`(若有),进入下一轮;若这是第 `max_rounds` 轮 → 按 `on_exhausted`:
   - `fail`:pipeline 失败。
   - `continue`:loop 记为 **exhausted**,下游带着最后一轮的产物继续;终止原因里会注明。

最后一轮失败后**不再跑 fix**(修完也没有下一轮去检验)。

约束(validate):

- `body` 非空;`fix` 不能同时在 `body` 里;一个 step 最多属于一个 loop;不支持嵌套。
- `until` 必须读取 body step 的 verdict,且**只能**读 body step 的 verdict。
- body 必须**闭合**:若某个 loop 外的 step 夹在两个 body step 之间(依赖前者、被后者依赖),会形成环并报错——把它也放进 body。
- body step 不能 `resume` 本 loop 的 fix(第 1 轮 fix 还没有 session)。
- 依赖方向:body 与 fix 之间的数据边(如 body 注入 fix 的产物)只表示"读最新版本",不产生调度依赖;顺序由 loop 固定为 body → fix → body。

### 典型模式:审查者决策

```yaml
steps:
  - {id: analyze, prompt: "...", output: root-cause.md}
  - id: review
    inject: [root-cause.md]
    output: review.md
    verdict: [ACCEPT, REJECT]
  - id: revise
    resume: analyze                        # 修正者就是原分析者,记得自己的推理
    inject: [review.md]
    prompt: "按审查意见修正 root-cause.md"
  - {id: report, after: [review], inject: [root-cause.md]}
loops:
  - {id: review_loop, body: [review], fix: revise,
     until: "steps.review.verdict == 'ACCEPT'", max_rounds: 3, on_exhausted: continue}
```

退出判断在审查方(`until` 读 review 的 verdict),修正方只负责改。多个审查者并行、再由汇总 step 给 verdict 时,把它们都放进 `body` 即可;一个廉价的预审 loop 放在昂贵的审查 loop 之前时,让昂贵审查者 `after` 预审 step——它们依赖整个预审 loop,只会在预审收敛后运行一次,不会被拖进预审的每一轮。

---

## 会话接续 resume / fork / compact_before_resume

```yaml
- id: revise
  resume: analyze              # 在 analyze 的 session 上继续(完整对话历史)
  inject: [review.md]          # 可叠加 inject 补充新信息
  compact_before_resume: 50
- id: second_opinion
  resume: analyze
  fork: true                   # 在副本上继续,不改动 analyze 的 session
```

- **默认是追加**:不带 `fork` 时,本 step 的对话**追加进目标 session**(SDK 实测:session id 不变)。之后再 resume 同一目标的 step 会看到这些追加内容——loop 里的 `revise` 第二轮记得第一轮改了什么,就靠这一点。引擎在追加后刷新目标的 session id 与上下文大小,`compact_before_resume` 按真实的当前大小判定。
- **`fork: true`**:SDK `fork_session`,得到新 session id,拥有目标的完整历史,但不写回目标。适合"多个人各自基于同一份分析发表意见"。
- **并发约束**(validate):可能**同时运行**的多个 step 若 resume 同一目标,必须**全部** `fork: true`——两个追加者并发写同一份 transcript 会互相破坏。能确定先后的(有依赖关系、或一个是 loop 的 fix)不受限。
- 被 resume 的目标若被跳过、或没产出 session(如超时)→ 报错。
- `verdict.prompt` 的追问总是在本 step **自己的** session 里进行。

### compact_before_resume

- 取值为**上下文窗口百分比**。resume 前若目标占比 ≥ 阈值,先对目标 session 执行一次 `/compact`,本 step 在压缩后的上下文上继续。
- **免费判定**:占比取自目标已记录的 usage(最后一轮输入侧 token / 窗口),不额外调用 API;超阈值才付费压缩。
- 压缩用**目标 step 的**模型、工具集、system prompt 与 cwd 运行——任何一项不一致都会让整份 transcript 缓存失效。
- **按目标加锁**:多个 forked step 同时 resume 同一目标时只压缩一次,其余在锁内读到压缩后的大小并跳过。
- 压缩写回磁盘 session,之后 resume 同一目标的 step 都继承更轻的对话。

---

## 重试 retry

`retry` 只处理**异常结果**——没有得到可用答案的执行:

- SDK / API 报错、超时、流中断;
- 契约要求有正文(声明了 `output`,或从自身回复读 verdict)却回复为空;
- verdict 读不出来(最后一行没有命中恰好一个值),或 verdict 追问本身失败。

`retry: N` = 最多再跑 N 次(默认 1),用尽则 pipeline 失败。**verdict 是答案不是失败**:`REJECT` 不会触发 retry,"不满意就重做"用 loop 表达。有 `resume` 时每次重试都重新 resume 目标。

---

## CLI

```bash
hydra run pipeline.yaml --var issue_id=ISS-001 --var-file vars.yaml
hydra run pipeline.yaml --dry-run          # 打印调度图(节点、依赖、loop 结构、when),不执行
hydra run pipeline.yaml --concurrency 3    # 最多同时运行 3 个 step
hydra run pipeline.yaml -v                 # agent 详细日志也镜像到 stderr
hydra run pipeline.yaml --resume-last      # 接着最近一次 run 继续
hydra run pipeline.yaml --resume RUN_ID    # 接着指定 run 继续(优先于 --resume-last)
hydra validate pipeline.yaml               # 只做静态校验
```

**接续(--resume)规则**:普通 step 的最新一次执行成功 → 复用(产物、verdict、session 都恢复),不重跑。loop **整体**复用,且仅当它在上次 run 中**结束**(converged / exhausted);中途中断的 loop 从第 1 轮重跑。上次被跳过的 step 重新求值 `when`。每次接续都写一个新的 run 目录,`meta.json` 记录 `resumed_from`。

退出码:`0` 成功;`1` 执行失败;`2` 校验失败 / 参数错误。

---

## 运行时目录与可观测性

```
<workdir>/.hydra/runs/<timestamp>-<hash>/
├── meta.json           # pipeline 名、inputs、开始时间、workdir、resumed_from
├── result.json         # 最终状态
├── events.jsonl        # 控制流事件
├── agent-trace.log     # agent 详细日志:prompt 预览、工具调用、verdict 追问
├── loops/<loop_id>.json   # {status: converged|exhausted|failed, rounds}
└── steps/<step_id>/
    ├── meta.json       # status、verdict、execution、耗时、cost、turns、session_id、retries_used
    ├── output.md       # 回复正文
    ├── verdict.txt     # verdict 追问的回复(有 verdict.prompt 时)
    ├── transcript.jsonl            # 完整 SDK 消息流(重试为 transcript.attempt-N.jsonl)
    └── run-<N>/        # 第 N 次执行(loop 的每一轮),不覆盖历史
```

- **两类日志分离**:控制流 → `events.jsonl` + stderr;agent 细节 → `agent-trace.log`(`-v` 时镜像到 stderr)。
- `events.jsonl` 事件:`step_start` `step_result` `step_abnormal` `verdict` `skip` `loop_round` `loop_done` `compact` `resumed` `pipeline_done`。

---

## validate 检查项

加载期(`models.py` / `loader.py`):未知键、类型错误、`version != 2`、YAML 布尔 verdict 值、重复 verdict 值、未声明的 input、缺失的必填 input、模板引用未定义 input。

校验期(`validate.py`),硬错误:

1. id 格式(标识符)与唯一性(step 与 loop 共用命名空间)
2. `after` / `resume` 引用存在;不能 resume 自己;`fork` / `compact_before_resume` 需要 `resume`;数值范围
3. 两个 step 写同一个 `output`
4. `when` / `until` 表达式:语法、input 已声明、step 存在且有 verdict、比较的字面量属于 verdict 值集合
5. loop 结构(见 [loops](#循环-loops) 约束)
6. 节点之间、以及每个 loop body 内部无环
7. 可能并发的同目标 resume 必须全部 `fork`

warning(不阻塞):声明了 verdict 却没有任何 `when` / `until` 读取它;inject 了 glob 但没有 `after`。

---

## 附录:压缩阈值参考

`compact_before_resume` 没有标准答案;推理质量的衰减主要与**绝对 token 数**和任务类型相关,而非窗口百分比。经验值:

| 窗口 | 建议值 | 约触发 token | 依据 |
|---|---|---|---|
| 200k | 50–60% | 100–120k | 多数模型在此之前推理仍稳 |
| 1M | 50% 左右 | ~500k | 2026 年实测同代 Claude 多跳推理拐点在 512K 附近 |

用 `events.jsonl` 里 `compact` 事件记录的压缩前后 token 数,结合自己 pipeline 的裁决质量调参,不要照搬。
