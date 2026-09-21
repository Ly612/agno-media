# agno-media

基于 Agno + AgentOS 的多 Agent 后端服务。每个 `agents/*_agent.py` 对应界面上的一个 Agent。

- 仓库地址：https://github.com/Ly612/agno-media
- 模型走第三方 OpenAI 兼容网关（在 `config.yaml` 配置），token 走 `.env`。

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
- （可选，仅图形界面需要）Node.js >= 18

---

## 快速开始（协作者看这里）

被邀请为协作者后，按以下步骤在本地启动后端。

### 1. 克隆项目

```powershell
git clone https://github.com/Ly612/agno-media.git
cd agno-media
```

### 2. 创建虚拟环境并安装依赖

```powershell
uv venv --python 3.12
.venv\Scripts\Activate.ps1
uv pip install -U "agno[os,sqlite]" openai pyyaml python-dotenv
```

> 未安装 uv 时，可用 pip：`python -m venv .venv` → `.venv\Scripts\Activate.ps1` → `pip install "agno[os,sqlite]" openai pyyaml python-dotenv`

### 3. 配置 token（关键，`.env` 不在仓库里，需自己建）

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，把占位符换成真实网关 token：

```
AI_API_KEY=你的真实token
```

网关地址与模型在 `config.yaml` 的 `ai` 段（默认已填好，通常无需改动）。

### 4. 启动后端服务

```powershell
python main.py
```

- 服务地址：http://localhost:7777
- API 文档（Swagger）：http://localhost:7777/docs

---

## 图形界面（可选）

后端本身只提供 API。如需图形聊天界面，使用官方开源前端 [AgentUI](https://github.com/agno-agi/agent-ui)（独立项目，与本仓库无关，无需修改）。

```powershell
# 在本仓库之外的目录克隆
git clone https://github.com/agno-agi/agent-ui.git
cd agent-ui
npm install
npm run dev
```

打开 http://localhost:3000 ，在左侧 endpoint 填 `http://localhost:7777` 即可连接本后端。

> 若直连 GitHub 不稳定，可用镜像克隆：
> `git clone https://gitclone.com/github.com/agno-agi/agent-ui.git`

**注意**：图形界面需同时运行两个服务——后端（7777）+ 前端（3000），各占一个终端。

---

## 日常开发流程

```powershell
# 1. 开发前先拉最新代码
git pull

# 2. 修改代码后提交
git add .
git commit -m "描述你的改动"

# 3. 推送到远程
git push
```

> 首次 push 时 GitHub 会要求认证：HTTPS 方式需使用 Personal Access Token（不是账号密码）。

## 新增 Agent

1. 在 `agents/` 下新建 `xx_agent.py`，实现 `build_agent()` 函数（返回一个 `Agent` 实例）。
2. 在 `config.yaml` 的 `agents:` 下加一段同名 key（如 `xx_agent`）的配置。

`main.py` 会自动发现所有 `*_agent.py`，无需改动入口代码。

## 常见问题

- **启动报 401 / 认证失败**：`.env` 里的 `AI_API_KEY` 未填或无效，检查 token。
- **启动报 404 / 找不到接口**：`config.yaml` 里 `ai.base_url` 应为 `https://gate.aidingqing.com/v1`（到 `/v1` 为止，不要带 `/chat/completions`）。
- **端口被占用**：改 `config.yaml` 里 `server.port`，或关掉占用 7777 的进程。
- **前端连不上后端**：确认后端已在 7777 运行，且前端 endpoint 填的是 `http://localhost:7777`。
