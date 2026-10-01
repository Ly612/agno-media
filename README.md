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
│   ├── media_agent.py
│   ├── news_agent.py
│   └── media_monitor_agent.py   # 国际媒体监控（工具型 Agent）
└── utils/               # 通用模块
    ├── config.py        # 加载 config.yaml
    ├── settings.py      # token 从 .env，其余从 yaml
    ├── model.py         # OpenAILike 接第三方网关
    ├── db.py            # SqliteDb 单例
    ├── scheduler.py     # 进程内定时任务（APScheduler）
    └── media_monitor_*.py  # 媒体监控流水线及其工具（抓取/社交/截图/Word/推送）
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

### 分支协作约定

`main` 分支已启用轻量保护（Ruleset）：

- **禁止强推**（force push）：不能覆盖已有提交历史。
- **禁止删除** `main` 分支。

日常仍可直接 `git push` 到 `main`。请遵守：

- push 前先 `git pull`，避免与他人提交冲突。
- 不要使用 `git push --force`（会被拒绝）。
- 大的功能建议开分支开发，完成后合并回 `main`：

```powershell
git checkout -b feature/你的功能
# 开发、提交...
git push -u origin feature/你的功能
# 之后可在 GitHub 发起 Pull Request 合并，或本地合并后推送
```

## Media Monitor Agent（媒体监控）

从 `qf-toolkit` 移植的「LLM 驱动五阶段媒体监控流水线」，在界面上显示为一个
**Media Monitor Agent**。它抓取 23 家国际主流媒体首页，用 LLM 挑选全球与涉华
话题、生成多方观点摘要，渲染成 Markdown 日报并转 Word，可选推送飞书/邮件。

### 五个阶段

1. **首页抓取**（BrightData）：抓 23 家媒体首页首屏内容。
2. **话题挑选**（LLM）：从首页内容里挑出重要的全球 / 涉华话题。
3. **详情总结**（LLM + BrightData 正文抓取）：逐话题抓正文、生成多方观点摘要。
4. **社交平台板块**（Apify + LLM）：抓 Facebook / X / Google News 贴文并总结。
5. **简报渲染**：汇总为 Markdown 日报 → 转 Word → 可选推送飞书 / 邮件。

### 外部服务与开关（全部可选，无凭证则对应阶段自动跳过）

在 `.env` 里按需填入（参见 `.env.example`）：

| 服务 | 环境变量 | 缺失时 |
| --- | --- | --- |
| BrightData（核心抓取） | `BRIGHT_DATA_API_KEY` / `BRIGHT_DATA_ZONE` | 无法跑核心三阶段 |
| 专用 LLM 网关（可选） | `MEDIA_MONITOR_LLM_*` | 回落到 `AI_API_KEY` / config 的 `ai` 段 |
| Apify（社交板块） | `APIFY_API_KEY` | 跳过社交板块 |
| 飞书推送 | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_CHAT_ID` | 跳过飞书推送 |
| 邮件推送 | `MEDIA_MONITOR_SMTP_*` | 跳过邮件推送 |
| AdsPower（截图指纹浏览器） | `MEDIA_MONITOR_ADSPOWER_*` | 回退本地 Chromium 截图 |

> 截图与 Word 为重依赖（Playwright / pandoc），缺失时自动跳过。需要时安装：
> `uv pip install -e ".[media-monitor]"` 并 `playwright install chromium`；
> Word 转换另需系统 `pandoc` 二进制。

非敏感默认行为（是否推送、是否跳过某阶段、专用 LLM）在 `config.yaml` 的
`media_monitor` 段配置。

### 在聊天里使用

对 Media Monitor Agent 说「**运行今天的媒体监控**」即触发完整流水线；也可要求
分步执行（只抓首页 / 只挑话题 / 只渲染报告）。工具返回 JSON（含报告与 Word
文件路径），Agent 会用中文说明结果。

### 命令行使用（调试 / 手动跑）

```bash
python -m utils.media_monitor_pipeline                 # 完整流水线（含 Word）
python -m utils.media_monitor_pipeline --scrape-only   # 只爬首页
python -m utils.media_monitor_pipeline --skip-to topics  # 从话题挑选开始
python -m utils.media_monitor_pipeline --skip-social   # 跳过社交板块
python -m utils.media_monitor_pipeline --push-feishu   # 完整流水线 + 推送飞书
python -m utils.media_monitor_pipeline --daily         # 每日定时入口（含推送+异常报警）
```

产物默认落在 `output/media_monitor/`。

### 定时运行（进程内 APScheduler）

在 `config.yaml` 的 `scheduler` 段开启，随 `python main.py` 一起启动：

```yaml
scheduler:
  enabled: true
  cron: ""            # 五段式 "m h dom mon dow"，优先级高于下面的 hour/minute
  hour: 8
  minute: 0
  day_of_week: "*"    # 每天；工作日用 "mon-fri"
  timezone: "Asia/Shanghai"
```

> 默认 `enabled: false`，避免开发态误触发真实抓取。启用后需确保已配置
> BrightData key，否则调度器会打印警告、不注册任务。

## 新增 Agent

1. 在 `agents/` 下新建 `xx_agent.py`，实现 `build_agent()` 函数（返回一个 `Agent` 实例）。
2. 在 `config.yaml` 的 `agents:` 下加一段同名 key（如 `xx_agent`）的配置。

`main.py` 会自动发现所有 `*_agent.py`，无需改动入口代码。

## 常见问题

- **启动报 401 / 认证失败**：`.env` 里的 `AI_API_KEY` 未填或无效，检查 token。
- **启动报 404 / 找不到接口**：`config.yaml` 里 `ai.base_url` 应为 `https://gate.aidingqing.com/v1`（到 `/v1` 为止，不要带 `/chat/completions`）。
- **端口被占用**：改 `config.yaml` 里 `server.port`，或关掉占用 7777 的进程。
- **前端连不上后端**：确认后端已在 7777 运行，且前端 endpoint 填的是 `http://localhost:7777`。
