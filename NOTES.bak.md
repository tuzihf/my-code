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
   │                       支持 /sessions /resume /rewind /history /cleanup
   ▼
minicore/ (包,21 个模块)
   │
   ├── agent_loop.py ─── 主干循环(心脏):
   │     while 有步数:
   │       ① 推导 phase (kernel.derive_phase)
   │       ② 真实 token 超阈值才压缩 (context_compactor.compact, force)
   │       ③ 问模型 (model.next) → 失败则 fallback (model_switcher)
   │       ④ finish 工具 → 结构化结束(直接取 summary 返回)
   │       ⑤ 模型要调工具? 过权限(permissions)+ 编辑确认(confirm_edit)→ 执行
   │       ⑥ read_dedup 去重读 → 结果回填 → 回到①
   │       收尾:verify 阶段强制给结论(门禁)+ auto_verify 自动跑测试
   │
   ├── tools.py ───────── 工具注册表 + 14 工具 + MCP 集成 + 输入校验 + JSON Schema
   ├── kernel.py ──────── phase 状态机 (explore/execute/verify) + verification 门禁
   ├── session.py ─────── 会话存盘 + checkpoint + rewind + group 整体回退 + 可读对话统计
   ├── memory.py ──────── 项目记忆存盘 + 注入 system prompt(按 workspace)
   ├── context_compactor.py ─ 上下文压缩(保留用户提问,force 模式跳过估算)
   ├── read_dedup.py ──── 同一文件片段重复读 → 返回占位,省 token
   ├── tool_cache.py ──── 大段工具结果 → 磁盘缓存,对话留引用
   ├── permissions.py ─── 敏感工具四态权限(allow/allow_once/deny/deny_once)
   ├── model_switcher.py ─ 主模型失败 → 自动切备用模型
   ├── api_retry.py ────── API 超时/限流/5xx → 指数退避重试
   ├── model.py ────────── 模型接口 + DeepSeek(流式) + Mock + usage 追踪
   ├── mcp.py ──────────── MCP 客户端(stdio 连外部服务,带超时/防死锁)
   ├── patch.py ────────── apply_patch:unified diff 解析 + 应用 + fuzz 匹配
   ├── code_index.py ───── 符号级索引(ast):find_symbol / find_references
   ├── dotenv.py ───────── .env 加载(不引入 python-dotenv)
   ├── fsutil.py ───────── 原子写 + 损坏文件备份(.corrupt)
   ├── diff.py ─────────── diff 生成
   ├── fake_mcp_server.py ─ MCP 假服务端(add 计算器,测试用)
   └── cleanup_sessions.py ─ 按"可读对话占比"清理旧会话
```

**结构说明**:所有模块收进 `minicore/` 包(平铺),和原版 `minicode/` 同构。模块间用 `from minicore.xxx import` 互引。

## 二、每个模块的职责(一句话)

| 文件 | 作用 |
|---|---|
| `main.py` | 入口,交互循环,本地命令(`/sessions` `/resume` `/rewind` `/history` `/cleanup`) |
| `minicore/agent_loop.py` | 主循环:问模型→调工具→回填→再来,带 phase/finish 门禁/压缩/权限/**confirm_edit 编辑确认**/**auto_verify 自动验证**/fallback/并行工具/流式 |
| `minicore/kernel.py` | phase 状态机 + verification 门禁(模型说"做完"时检查证据) |
| `minicore/tools.py` | 工具注册表 + 14 工具 + 输入校验 + JSON Schema + MCP 集成 + 子代理 |
| `minicore/session.py` | 会话存盘 JSON + checkpoint/rewind + group 整体回退 + `readable_conversation_count` |
| `minicore/memory.py` | 项目记忆存盘 + 注入 system prompt(跨会话记住) |
| `minicore/context_compactor.py` | 上下文压缩(保留用户提问,force 模式跳过估算) |
| `minicore/read_dedup.py` | 同文件片段二次读→占位(`[read_dedup]`) |
| `minicore/tool_cache.py` | 大段工具结果→磁盘缓存,对话留引用 |
| `minicore/permissions.py` | 敏感工具四态权限(allow/allow_once/deny/deny_once) |
| `minicore/model_switcher.py` | 模型候选列表,失败切换 |
| `minicore/api_retry.py` | 可恢复错误(超时/限流/5xx)指数退避重试,不可恢复直接抛 |
| `minicore/model.py` | `Model` 接口 + `DeepSeekModel`(流式/重试) + `MockModel` + usage 追踪 |
| `minicore/mcp.py` | MCP 客户端:stdio 连外部服务(带超时/防死锁) |
| `minicore/patch.py` | apply_patch:unified diff 解析 + 应用 + fuzz 匹配 |
| `minicore/code_index.py` | 符号级索引(ast):find_symbol / find_references |
| `minicore/dotenv.py` | .env 加载(不引入 python-dotenv) |
| `minicore/fsutil.py` | 原子写 + 损坏文件备份(.corrupt) |
| `minicore/diff.py` | diff 生成(行级) |
| `minicore/fake_mcp_server.py` | 假 MCP 服务端(add 计算器,测试用) |
| `minicore/cleanup_sessions.py` | 按"可读对话占比"清理旧会话 |

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

### 9. 流式输出(`model.py`)
- `DeepSeekModel.next(on_chunk=...)` 传回调时用 `stream=True`,逐 chunk 返回
- `on_chunk` 逐个打印 → "打字机"效果
- 工具调用也支持流式(分段累积 `tool_calls`)
- 对应原版 `on_stream_chunk`

### 10. MCP 接入(`mcp.py`)
- `StdioMcpClient`:用 `subprocess.Popen` 起服务端子进程,stdio 上 JSON-RPC 通信
- `tools/list` 列工具,`tools/call` 调工具
- MCP 工具包装成 `ToolDefinition`,和内置工具平级进 `ToolRegistry`
- 模型能自主发现并调用 MCP 工具(如 `add`)
- 对应原版 `create_mcp_backed_tools`

### 11. finish 结构化门禁(`agent_loop.py` + `tools.py`)
- 新增 `finish` 工具:模型用工具调用表达"完成"并给出 `summary`
- `agent_loop` 在 `model.next` 返回后拦截 `finish` 调用,直接取 summary 作为最终回答(走流式)
- 相比关键词启发式(`decide_assistant_turn` 的 markers),结构化结束更可靠、不易误判
- 现有 `decide_assistant_turn` 保留为兜底(模型没调 finish 时仍用 markers 判断)

### 12. 真实 token 驱动压缩(`agent_loop.py` + `model.py`)
- `model.py` 在每次 API 调用后累加真实 `usage`(非流式读 `response.usage`,流式加 `stream_options.include_usage`)
- `agent_loop` 用 `get_usage()` 差值累计 `real_tokens`,超阈值才触发 `compact(..., force=True)`
- 替换了纯启发式估算(`estimate_tokens`)作为压缩触发依据,更贴近真实上下文占用

### 13. 编辑后自动验证(`agent_loop.py` + `tools.py`)
- 新增 `verify` 工具(默认 `python -m pytest`)
- `run_agent_turn(auto_verify=True)`:本轮改过文件、模型要结束时,自动跑验证命令并把结果回喂(最多 2 次)
- 形成"改 → 验 → 修"闭环(TDD 循环)

### 14. diff approve(编辑前确认)
- 写工具(`write_file`/`edit_file`/`apply_patch`)加 `dry_run` 参数:只生成 diff 预览,不写文件、不打 checkpoint
- `run_agent_turn(confirm_edit=...)`:写工具执行前先 dry_run,回调返回 False 则拒绝
- 网页端:`MY_AGENT_CONFIRM_EDIT=1` 开启后,改文件前弹窗展示 diff,批准/拒绝

### 15. apply_patch(`patch.py`)
- 解析标准 unified diff(多 hunk),一次应用多处修改
- 匹配策略:按 hunk 头行号精确匹配 → 失败则全局搜索(fuzz 偏移)
- 失败返回原内容 + 错误信息

### 16. 符号级搜索(`code_index.py`)
- 用标准库 `ast` 做轻量静态分析,索引 function/class/assign/import/name_use/call
- `find_symbol`(查定义)/ `find_references`(查全部引用),结果按文件引用密度排序
- 目录级 mtime 缓存失效;跳过语法错误文件、>1MB 文件

### 17. 命令白名单 + shell=False(`tools.py`)
- `run_command` 从 `shell=True + 黑名单` 改为 `shell=False + 白名单` 四层防线:
  路径沙箱 → 拒绝 shell 元字符 → shlex 分词 → 命令名白名单
- 消除 shell 注入面,牺牲管道/重定向灵活性(agent 用内置 grep/find 工具替代)

### 18. group 整体回退(`session.py`)
- `FileCheckpoint` 加 `group_id`,`run_agent_turn` 每轮生成一个组号
- `rewind_group(group_id)` 一次回退整个 turn 内改的所有文件(一个事务)
- 网页端 `/rewind?group_id=` 支持整体回退

### 19. token 用量与成本追踪(`model.py` + `server.py`)
- `model.py` 维护模块级 `_USAGE` 累计 + `get_usage()`/`reset_usage()`
- `server.py` 暴露 `GET /usage`(含成本估算)、`POST /usage/reset`
- 网页端顶栏 💰 按钮查看

## 四、测试体系(pytest)

从"手写 verify 脚本"升级到 pytest,逐步淘汰了 30 个 verify_*.py:

```
tests/
├── conftest.py                       把项目根加入 sys.path + 沙箱兼容(basetemp)
├── test_tools.py                     工具 + 边界 + grep/glob/分页/dry_run/命令白名单
├── test_session.py                   会话存读 + 多步回退 + group 回退 + name + 损坏兜底
├── test_kernel_compactor_memory.py   phase/门禁/压缩(force)/去重/记忆损坏
├── test_permissions_switch_retry.py  权限四态/fallback/重试(含重试耗尽)
├── test_tool_cache_mcp.py            工具持久化 + MCP(含超时)
├── test_path_safety.py               路径沙箱逃逸拦截
├── test_code_index.py                符号索引
├── test_patch.py                     apply_patch
├── test_dotenv_fsutil.py             .env 加载 + 原子写 + 清理
├── test_regressions.py               修复回归(finish/真实token压缩/dry_run/confirm_edit/命令逃逸)
└── test_integration.py               真实API/性能/编码(标记 integration)
```

命令:
- `python -m pytest`                  # 126 单元测试(默认)
- `python -m pytest -m integration`   # 4 集成测试(真实 API/性能)
- `python -m pytest -m "not integration"`  # 等同默认

pytest 比 verify 脚本强在哪:
- **断言**:`assert 具体值`,失败自动标红(verify 只 print,人眼判断)
- **边界覆盖**:空/None/特殊字符/重试耗尽,verify 没测
- **回归基准**:压缩器不吞用户提问、摘要不嵌套,固化成测试
- **marker 分类**:integration 标记,按需跑真实 API

## 五、踩坑记录(最有价值的部分)

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

## 六、与 MiniCode 原版对照

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
| 工具结果持久化(compactor内) | `tool_cache.py` | 大结果挪磁盘 |
| `mcp.py` | `mcp.py` + `fake_mcp_server.py` | MCP 接入外部服务 |
| 流式输出(`on_stream_chunk`) | `model.py` | 打字机效果 |
| `cli_commands.py` `/sessions` | `main.py` `/sessions` `/history` | 会话列表/恢复/历史 |

**还没做的**:TUI、动态并发控制、Skills、真·沙箱隔离(容器/受限 token)、embedding 检索、可写并行子代理。

## 七、下一步可做

- [x] API 重试 + 指数退避(`api_retry.py`)
- [x] 并行工具调用(只读并发/写串行)
- [x] 工具输入校验(validate)
- [x] `/history` 随时看当前会话
- [x] MCP 接入外部服务(`mcp.py`)
- [x] 工具结果持久化(大段输出挪磁盘,`tool_cache.py`)
- [x] 流式输出(`model.py` on_chunk)
- [x] 测试统一到 pytest(130 用例,`tests/`)
- [x] 平铺包结构(`minicore/`)
- [x] git 仓库存档
- [x] 命令白名单 + shell=False(安全收敛)
- [x] 子代理委派(只读 delegate)
- [x] 成本追踪(token 用量 + 成本估算)
- [x] 符号级搜索(find_symbol / find_references)
- [x] apply_patch(unified diff + fuzz)
- [x] 结构化门禁(finish 工具)
- [x] 真实 token 驱动压缩
- [x] 编辑后自动验证(auto_verify)
- [x] diff approve(编辑前确认)
- [x] 多文件 group 整体回退
- [x] glob 工具 + read_file 分页 + 行号
- [x] 会话重命名持久化 + 记忆按 workspace
- [x] 原子写 + 损坏文件兜底
- [x] .env 加载
- [x] 打包(pyproject.toml)+ ruff + CI
- [ ] TUI 界面
- [ ] 动态并发控制(按错误率/延迟调 worker 数)
- [ ] Skills 技能包
- [ ] 真·沙箱隔离(容器/受限 token)
- [ ] embedding 检索增强
- [ ] 可写子代理 + 并行 fan-out
