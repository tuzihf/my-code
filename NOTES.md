# my-agent 学习笔记

> 一个从零搭建的迷你版 terminal coding agent,用于深入理解 MiniCode / Claude Code 类 agent 的核心架构。
> 通过"复刻一个项目来理解它",并踩了 6+ 个真实 bug 后修好。

---

## 一、架构总览

```
用户输入
   │
   ▼
main.py ──────────────── 入口:组装工具/模型/会话/记忆/权限,交互循环
   │                       支持 /sessions /resume /rewind 等本地命令
   ▼
agent_loop.py ────────── 主干循环(心脏):
   │                        while 有步数:
   │                          ① 推导 phase (kernel.derive_phase)
   │                          ② 上下文压缩 (context_compactor.compact)
   │                          ③ 问模型 (model.next) → 失败则 fallback (model_switcher)
   │                          ④ 模型要调工具? 过权限 (permissions) → 执行 (tools.execute)
   │                          ⑤ read_dedup 去重读 → 结果回填 → 回到①
   │                          收尾:verify 阶段强制给结论 (门禁)
   ▼
tools.py ─────────────── 工具注册表 + 6 个工具(带输入校验)
kernel.py ────────────── phase 状态机 (explore/execute/verify) + verification 门禁
session.py ───────────── 会话存盘 + checkpoint + rewind + 可读对话统计
memory.py ────────────── 项目记忆存盘 + 注入 system prompt
context_compactor.py ─── 上下文超阈值时压缩旧历史(保留用户提问)
read_dedup.py ────────── 同一文件重复读 → 返回占位,省 token
permissions.py ───────── 敏感工具(写/命令/记忆)执行前问用户
model_switcher.py ────── 主模型失败 → 自动切备用模型
api_retry.py ─────────── API 超时/限流/5xx → 指数退避重试
model.py ─────────────── 模型接口 + DeepSeek 真实实现 + Mock 假实现
```

## 二、每个模块的职责(一句话)

| 文件 | 作用 |
|---|---|
| `main.py` | 入口,交互循环,本地命令(`/sessions` `/resume` `/rewind` `/history`) |
| `agent_loop.py` | 主循环:问模型→调工具→回填→再来,带 phase/门禁/压缩/权限/fallback/**并行工具** |
| `kernel.py` | phase 状态机 + verification 门禁(模型说"做完"时检查证据) |
| `tools.py` | 工具注册表 + 6 工具 + **输入校验**(validate) |
| `session.py` | 会话存盘 JSON + checkpoint/rewind + `readable_conversation_count` |
| `memory.py` | 项目记忆存盘 + 注入 system prompt(跨会话记住) |
| `context_compactor.py` | 上下文超阈值→把旧历史浓缩成摘要(**保留用户提问**) |
| `read_dedup.py` | 同文件二次读→占位(`[read_dedup]`) |
| `permissions.py` | 敏感工具执行前 `input()` 问用户,`allow/deny` |
| `model_switcher.py` | 模型候选列表,失败切换 |
| `api_retry.py` | 可恢复错误(超时/限流/5xx)指数退避重试,不可恢复直接抛 |
| `model.py` | `Model` 接口 + `DeepSeekModel`(真,带重试) + `MockModel`(假) |
| `cleanup_sessions.py` | 按"可读对话占比"清理旧会话 |

## 三、核心机制

### 1. Agent 主循环(think → act → observe)
```
问模型 → 模型说"调工具"? → 执行工具 → 结果塞回对话 → 再问模型
        → 模型说"给结论"? → 结束
```
工具结果以 `role="tool"` 消息回填,模型靠这些"看见"外部世界。

### 2. 消息角色
- `system` — 系统提示词(角色设定 + 记忆注入)
- `user` — 用户提问
- `assistant` — 模型回答 / 模型发起的工具调用(tool_calls)
- `tool` — 工具执行结果

### 3. phase 状态机(`kernel.py`)
按步数把一轮对话分成三阶段,越靠后越"逼收尾":
- `explore`(前 30%)— 探索,别急着下结论
- `execute`(30%-60%)— 干活,保持推进
- `verify`(后 40%)— 收尾,禁止调工具,强制给结论

### 4. 压缩器保护对话核心
旧版 bug:压缩时把"用户提问"也浓缩进摘要,多轮后用户原话丢失。
修复:压缩时**保留用户真实提问,只浓缩工具结果/旧回答**。

### 5. 系统注入 vs 真实对话(关键设计)
- agent 会在对话里**注入**内部消息(压缩提示、收尾逼迫、门禁理由、fallback 提示)
- 这些消息带 `system_injected: True` 标记(或 `[系统]` 前缀兜底)
- **恢复会话时只显示真实对话(user/assistant),工具/系统注入一律不显示**
- 类似 Claude Code:给模型的是全量 `messages`,给用户恢复的是干净的 `conversationHistory`

### 6. 并行工具调用(`agent_loop.py`)
- 模型一次调多个工具时,**只读工具(read_file/list_files/grep)并发执行**,提速
- **写/命令工具串行执行** + 权限检查,防止并发写文件冲突
- 用 `ThreadPoolExecutor(max_workers=4)` 并发,结果按调用顺序回填
- 对应原版 `ToolScheduler`(它还会按错误率/延迟动态调并发数)

### 7. API 重试 + 指数退避(`api_retry.py`)
- **可恢复错误**(超时/429/5xx/连接重置)→ 自动重试,间隔递增(1s→2s→4s)
- **不可恢复错误**(认证/参数错误)→ 直接抛,不浪费重试
- 用 `with_retry(func, max_retries=3, base_delay=1.0)` 包住 API 调用

### 8. 工具输入校验(`tools.py`)
- `ToolDefinition` 带可选的 `validate` 函数,执行前校验
- 第一层:input 必须是 dict;第二层:每个工具的专属校验(必填字段/类型)
- 模型传错参数 → 优雅返回错误结果,不崩

## 四、踩坑记录(最有价值的部分)

### Bug 1:checkpoint 多步回退错位
**现象**:2 个 checkpoint 只回退 1 步,再回退停在 v2 而非 v1。
**根因**:回退时追加的"反向 checkpoint"(存回退前的内容)被当成可回退的编辑记录。
**修复**:给 checkpoint 加 `kind` 字段,`rewind` 只处理 `kind="edit"`,反向记录标 `kind="rewind"`。

### Bug 2:模型死循环 / 无限调工具
**现象**:模型一直调工具不给结论,耗尽 max_steps。
**根因**:MockModel 剧本缺陷 + 门禁太粗暴 + 无防卡死。
**修复**:(1) Mock 看到工具结果就给文本;(2) 门禁更精准(识别兜底文本);(3) 连续 3 次同工具调用 → 注入提示打断。

### Bug 3:开放任务无限探索
**现象**:"解释项目架构"这种开放任务,模型一直 read_file 永远不总结。
**根因**:没有机制逼模型收尾。
**修复**:verify 阶段禁止调工具,强制给结论(类似原版 turn_kernel 的 widening/stop 逻辑)。

### Bug 4:DeepSeek 多轮 400 报错
**现象**:第二轮请求报 `The reasoning_content in the thinking mode must be passed back to the API`。
**根因**:DeepSeek 思考模式要求回传 `reasoning_content`,我们存消息时丢了。
**修复**:`AgentStep` 加 `reasoning_content` 字段,存 assistant 消息时保留。

### Bug 5:Windows 中文输出乱码 / GBK 崩溃
**现象**:`run_command` 跑含中文的命令 → `UnicodeDecodeError: 'gbk' codec`。
**根因**:(1) subprocess 用 GBK 解码;(2) 子进程在 GBK 终端输出 GBK 字节。
**修复**:`encoding="utf-8"` + 给子进程设 `PYTHONIOENCODING=utf-8`。

### Bug 6:会话 messages 没同步 → 存空
**现象**:跑真模型对话,`/sessions` 显示 `消息:0`。
**根因**:`run_agent_turn` 返回新 messages 列表,但 `session.messages` 没同步,`save_session` 存的是旧空列表。
**修复**:`session.messages = list(messages)` 同步后再 save。

### Bug 7:压缩器吞掉用户提问(最隐蔽)
**现象**:恢复会话看不到用户真实提问。
**根因**:压缩器把 user 消息也浓缩进摘要,多轮后原话丢失。
**修复**:压缩时保留 user 提问,只浓缩工具/旧回答;已有摘要替换而非叠加(防嵌套)。

### Bug 8:系统注入消息被当用户对话
**现象**:恢复会话时,"门禁理由"等内部消息显示成 `[用户] ...`。
**根因**:靠文本前缀 `[系统]` 识别不可靠(存量数据无前缀)。
**修复**:注入消息时打 `system_injected: True` 标记,过滤检查字段而非前缀。

### Bug 9:恢复视图显示工具日志刷屏
**现象**:恢复会话看到一堆 `↳ [工具 run_command] ...`。
**根因**:恢复视图错误地显示了工具记录。
**修复**:像 Claude Code 的 conversationHistory 一样,**恢复只显示 user/assistant 文本对话**,工具/系统注入一律不显示。

### Bug 10:助手回答被截断
**现象**:恢复会话,助手回答只显示一小半。
**根因**:`main.py` 里 `text[:200]` 截断。
**修复**:去掉截断,恢复显示完整问答。

## 五、与 MiniCode 原版对照

| MiniCode 原版 | 我的迷你版 | 说明 |
|---|---|---|
| `agent_loop.py` | `agent_loop.py` | 主循环 |
| `tooling.py` + `tools/` | `tools.py` | 工具注册表 |
| `model_registry.py` + adapters | `model.py` | 模型接口 + 实现 |
| `session.py` | `session.py` | 会话 + checkpoint/rewind |
| `memory.py` / `working_memory.py` | `memory.py` | 记忆 + 注入 |
| `context_compactor.py` | `context_compactor.py` | 上下文压缩 |
| `turn_kernel.py` | `kernel.py` | phase + verification |
| `permissions.py` | `permissions.py` | 权限 |
| `model_switcher.py` | `model_switcher.py` | 模型 fallback |
| `api_retry.py` | `api_retry.py` | API 重试 + 退避 |
| `tooling.py` validator | `tools.py` validate | 工具输入校验 |
| `ToolScheduler`(agent_loop内) | `agent_loop.py` | 并行工具调用 |
| `read_dedup`(context_compactor内) | `read_dedup.py` | 重复读去重 |
| `cli_commands.py` `/sessions` | `main.py` `/sessions` `/history` | 会话列表/恢复/历史 |

**还没做的**:MCP、TUI、工具结果持久化、成本追踪、动态并发控制。

## 六、下一步可做

- [x] API 重试 + 指数退避(`api_retry.py`)
- [x] 并行工具调用(只读并发/写串行)
- [x] 工具输入校验(validate)
- [x] `/history` 随时看当前会话
- [ ] MCP 接入外部服务
- [ ] TUI 界面
- [ ] 工具结果持久化(大段输出挪磁盘)
- [ ] 成本追踪
- [ ] 动态并发控制(按错误率/延迟调 worker 数)
