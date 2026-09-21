# agno-media

基于 Agno + AgentOS 的多 Agent 后端服务。每个 `agents/*_agent.py` 对应界面上的一个 Agent。

## 目录结构

```
agno-media/
├── main.py              # 入口：自动发现 agents 并启动本地 OS
├── config.yaml          # 非敏感配置（网关地址/模型/DB/端口/agent 参数）
├── .env                 # 仅存 token（不进 git，需自行创建）
├── .env.example         # token 示例，复制为 .env 后填入自己的 token
├── pyproject.toml       # 依赖声明
├── agents/              # 每个文件 = 一个 Agent
│   ├── web_agent.py
│   └── media_agent.py
└── utils/               # 通用模块
    ├── config.py        # 加载 config.yaml
    ├── settings.py      # token 从 .env，其余从 yaml
    ├── model.py         # OpenAILike 接第三方网关
    └── db.py            # SqliteDb 单例
```

## 环境要求

- Python >= 3.10
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip

## 本地启动

### 1. 克隆并进入项目

```powershell
git clone <仓库地址>
cd agno-media
```

### 2. 创建虚拟环境并安装依赖

```powershell
uv venv --python 3.12
.venv\Scripts\Activate.ps1
uv pip install -U "agno[os,sqlite]" openai pyyaml python-dotenv
```

### 3. 配置 token

复制 `.env.example` 为 `.env`，填入自己的网关 token：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`：

```
AI_API_KEY=你的真实token
```

网关地址与模型在 `config.yaml` 的 `ai` 段配置（默认已填好）。

### 4. 启动后端服务

```powershell
python main.py
```

服务运行在 http://localhost:7777 ，API 文档见 http://localhost:7777/docs 。

## 图形界面（可选）

后端本身只提供 API。如需图形聊天界面，使用官方开源前端 [AgentUI](https://github.com/agno-agi/agent-ui)（独立项目，无需修改）：

```powershell
npx create-agent-ui@latest
cd agent-ui
npm run dev
```

打开 http://localhost:3000 ，在左侧 endpoint 填 `http://localhost:7777` 即可连接本后端。

## 新增 Agent

在 `agents/` 下新建 `xx_agent.py`，实现 `build_agent()` 函数，并在 `config.yaml` 的 `agents:` 下加一段同名配置。`main.py` 会自动发现，无需改动入口代码。
