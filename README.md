# my code

一个基于 Python 从零开发的轻量级终端编程助手。它注重本地可靠性与可观察性:除了调用模型和工具,还提供持久会话、安全回退、跨会话记忆、可配置模型等能力,支持 CLI 和网页端两种交互方式。

## 项目定位

面向本地开发场景,核心设计原则:

- **会话优先**:会话可检查、回放、续跑、摘要
- **恢复优先**:文件编辑默认有 checkpoint,可预览回退
- **本地优先**:围绕真实仓库和本地工具构建,路径沙箱锁死项目目录
- **可观察**:运行时状态、就绪检查、修改 diff 都显式可见

## 功能特性

- **Agent 主循环**:think → act → observe 循环,phase 状态机 + verification 门禁 + `finish` 结构化结束 + 编辑后自动验证(`auto_verify`)
- **工具系统**:14 个内置工具(文件读写、命令执行、glob、符号搜索、记忆、精确编辑、apply_patch、verify、finish、只读子代理)+ MCP 接入 + 并行调用 + 输入校验 + JSON Schema
- **会话管理**:持久会话、checkpoint、rewind 安全回退、多文件 group 整体回退,会话落盘重启可恢复
- **记忆系统**:跨会话项目知识存储,自动注入上下文,按 workspace 隔离
- **上下文优化**:read_dedup 去重、大结果持久化、真实 token 驱动的上下文压缩、符号级代码索引
- **安全**:路径沙箱(锁死项目目录)、命令白名单 + shell=False、敏感工具权限确认、diff approve(编辑前确认)
- **模型管理**:支持 DeepSeek / OpenAI 兼容 / 本地模型 / Mock,配置持久化,一键切换,token 用量与成本追踪
- **可靠性**:API 重试 + 指数退避、模型 fallback、收尾逼迫
- **网页端**:左侧会话栏、流式打字、代码修改 diff 展示、撤销修改、停止生成、系统文件夹选择器、记忆管理、token 用量
- **测试**:130 个 pytest 用例(126 个单元测试 + 4 个集成测试)

## 快速开始

### 1. 克隆并安装

```bash
git clone <your-repo-url>
cd my-agent
python -m pip install -e .[dev]
```

### 2. 配置模型

```bash
# 方式一:DeepSeek
export DEEPSEEK_API_KEY=sk-xxx

# 方式二:本地模型(Ollama)
# 先运行 `ollama pull llama3`,然后:
export OPENAI_API_KEY=ollama
export OPENAI_BASE_URL=http://localhost:11434/v1
```

或参考 `.env.example`,在网页端"模型设置"里配置。

### 3. 运行 CLI

```bash
python main.py
```

### 4. 运行网页端

```bash
python server.py
# 浏览器打开 http://localhost:8000
```

### 5. 无 API key 时(测试模式)

```bash
MY_AGENT_MOCK=1 python server.py
```

## 使用说明

### CLI(main.py)

- 交互式对话,输入任务,agent 自动决定调用工具
- 支持本地命令:`/tools`、`/sessions`、`/resume`、`/history`、`/rewind`、`/rewind-preview`、`/cleanup`

### 网页端(server.py)

- **左侧会话栏**:切换、右键删除/重命名会话
- **切换项目**:系统文件夹选择器选项目目录,agent 只在该目录内操作
- **模型设置**:顶栏 ⚙ 按钮,切换 DeepSeek / OpenAI 兼容 / 本地模型
- **记忆管理**:顶栏 🧠 按钮,查看/添加/删除记忆(按当前 workspace 隔离)
- **流式输出**:逐字打字,可随时停止(真正停止后台 agent)
- **代码 diff**:修改文件后展示红绿高亮 diff(默认折叠)
- **撤销修改**:顶栏 ↩ 按钮,用 checkpoint 回退最近一次文件修改
- **token 用量**:顶栏 💰 按钮查看 token 统计与成本估算
- **diff approve(可选)**:`MY_AGENT_CONFIRM_EDIT=1 python server.py` 开启后,agent 每次改文件前弹窗展示 diff,批准后才真正写入

## 架构

```
main.py / server.py          入口(CLI / 网页端)
    │
    ▼
minicore/                    核心包
    ├── agent_loop.py        Agent 主循环(finish 门禁 / 自动验证 / 真实 token 压缩)
    ├── kernel.py            phase 状态机 + verification 门禁
    ├── tools.py             工具注册表(14 工具)+ MCP + 子代理 + 输入校验 + schema
    ├── model.py             模型接口(DeepSeek/OpenAI兼容/Mock)+ usage 追踪
    ├── session.py           会话持久化 + checkpoint + rewind + group 回退
    ├── memory.py            记忆系统
    ├── context_compactor.py 上下文压缩(force 模式)
    ├── read_dedup.py        重复读去重(按文件片段)
    ├── tool_cache.py        大结果持久化
    ├── path_safety.py       路径沙箱
    ├── permissions.py       权限管理(allow/allow_once/deny/deny_once 四态)
    ├── api_retry.py         API 重试 + 退避
    ├── model_switcher.py    模型 fallback
    ├── diff.py              diff 生成
    ├── mcp.py               MCP 客户端
    ├── patch.py             apply_patch(unified diff + fuzz 匹配)
    ├── code_index.py        符号级代码索引(ast)
    ├── dotenv.py            .env 加载
    ├── fsutil.py            原子写 + 损坏文件备份
    └── settings.py          模型配置持久化
```

## 目录结构

```
my-agent/
├── main.py              CLI 入口
├── server.py            网页端后端(FastAPI)
├── minicore/            核心包(见上)
├── static/index.html    网页端前端
├── tests/               测试(pytest,126 单元 + 4 集成)
├── pyproject.toml       打包 + 依赖 + ruff 配置
├── .github/workflows/   CI(Python 3.11-3.13)
├── .env.example         环境变量示例
└── pytest.ini           pytest 配置
```

## 测试

```bash
# 运行全部单元测试
python -m pytest

# 运行集成测试(需真实 API)
python -m pytest -m integration
```

## 技术栈

- Python 3.11+
- FastAPI + SSE(网页端)
- openai SDK(模型接入)
- pytest(测试)
- ruff(lint)
- 标准库 ast(符号级代码索引)

## License

MIT
