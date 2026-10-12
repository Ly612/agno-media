# CentOS 服务器部署指南（IP + 端口直连）

本指南在一台 CentOS 服务器上部署 **后端 agno-media** 与 **前端 agent-ui**，通过 `http://服务器IP:端口` 直接访问，使用简单后台运行（nohup）。

- 后端 AgentOS：端口 **7777**，提供 Agent API 与 Excel 下载。
- 前端 AgentUI：端口 **3000**，图形聊天界面。
- 假设服务器公网/内网 IP 为 `SERVER_IP`（下文请替换为真实 IP，例如 `1.2.3.4`）。

---

## 零、部署前的关键配置说明

后端生成的 Excel 下载链接需要能被浏览器访问，因此必须告诉后端「对外地址」。
在 `config.yaml` 的 `server.public_base_url` 填服务器地址（见第二节第 4 步），否则下载链接会指向 `localhost` 而在别人电脑上打不开。

---

## 一、安装基础环境

### 1. 安装 Python 3.10+

先看系统自带版本：

```bash
python3 --version
```

**情况 A：CentOS 8 / Stream / Rocky / Alma**

```bash
sudo dnf install -y python3.11 python3.11-pip git
# 后续命令用 python3.11
```

**情况 B：CentOS 7（自带 Python 3.6，过旧，需装新版）**

用 SCL 或源码编译。推荐 SCL：

```bash
sudo yum install -y centos-release-scl
sudo yum install -y rh-python38 git
scl enable rh-python38 bash   # 进入 python3.8 环境
python --version
```

> CentOS 7 若需要 3.10+，建议源码编译或用 pyenv；agno 要求 >=3.10，3.8 可能不满足，优先考虑升级系统或用 conda。

### 2. 安装 uv（快速包管理器，可选但推荐）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

### 3. 安装 Node.js 18+（前端需要）

```bash
curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
sudo yum install -y nodejs   # CentOS 7 用 yum，CentOS 8 用 dnf
node --version
```

---

## 二、部署后端 agno-media

### 1. 克隆代码

```bash
cd /opt   # 或你习惯的目录
git clone https://github.com/Ly612/agno-media.git
cd agno-media
git checkout dev   # 若功能在 dev 分支
```

### 2. 创建虚拟环境并装依赖

用 uv：

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install "agno[os,sqlite]" openai pyyaml python-dotenv google-search-results openpyxl greenlet
```

或用 pip：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install "agno[os,sqlite]" openai pyyaml python-dotenv google-search-results openpyxl greenlet
```

### 3. 配置 token（.env）

```bash
cp .env.example .env
vi .env
```

填入真实密钥：

```
AI_API_KEY=你的网关token
SERPAPI_API_KEY=你的serpapi_key
```

### 4. 配置对外地址与监听地址（关键）

编辑 `config.yaml`：

```yaml
server:
  host: 0.0.0.0          # 改成 0.0.0.0，允许外部访问
  port: 7777
  reload: false          # 生产环境务必关掉，避免热重载卡死
  tracing: true
  public_base_url: "http://SERVER_IP:7777"   # 换成真实服务器 IP
```

> `host: 0.0.0.0` 让后端监听所有网卡；`public_base_url` 决定下载链接前缀。两者缺一不可。

### 5. 后台启动后端

```bash
mkdir -p logs
nohup .venv/bin/python main.py > logs/backend.log 2>&1 &
```

验证：

```bash
tail -f logs/backend.log        # 看到 Application startup complete 即成功
curl http://127.0.0.1:7777/config   # 返回 JSON 即正常
```

---

## 三、部署前端 agent-ui

### 1. 克隆并安装

```bash
cd /opt
git clone https://github.com/agno-agi/agent-ui.git
cd agent-ui
npm install
```

> 若国内服务器 npm 慢：`npm config set registry https://registry.npmmirror.com`

### 2. 构建并后台启动（生产模式）

```bash
npm run build
nohup npm run start -- -H 0.0.0.0 -p 3000 > start.log 2>&1 &
```

> `-H 0.0.0.0` 让前端监听所有网卡，外部才能访问。

验证：

```bash
tail -f start.log     # 看到 Ready 即成功
```

---

## 四、开放防火墙端口

CentOS 默认 firewalld：

```bash
sudo firewall-cmd --permanent --add-port=7777/tcp
sudo firewall-cmd --permanent --add-port=3000/tcp
sudo firewall-cmd --reload
```

> 若服务器在云厂商（阿里云/腾讯云等），还需在**云控制台的安全组**放行 7777 和 3000 端口。

---

## 四点五、部署 MCP Server（供豆包等 MCP Client 接入，可选）

若需让豆包通过「自定义连接器」调用新闻搜索/导出能力，额外启动 MCP Server（默认端口 8000）。

### 1. 启动

```bash
cd /root/agno-media
source .venv/bin/activate
nohup .venv/bin/python mcp_server.py > logs/mcp.log 2>&1 &
tail -f logs/mcp.log   # 看到 Uvicorn running on http://0.0.0.0:8000 即成功
```

### 2. 放行 8000 端口

```bash
sudo firewall-cmd --permanent --add-port=8000/tcp
sudo firewall-cmd --reload
```

> 腾讯云等还需在**安全组**放行 8000。

### 3. 在豆包中接入

先在 `.env` 设置鉴权 token（强烈建议，防止 8000 端口被他人盗用消耗 SerpAPI 额度）：

```
MCP_AUTH_TOKEN=自定义一个足够复杂的密钥
```

> 留空则不校验（任何人可调用，不推荐对外暴露时留空）。改动后需重启 MCP Server。

豆包 →「新建自定义连接器」：
- 传输类型：**HTTP**
- 服务器 URL：`http://SERVER_IP:8000/mcp`
- 自定义 Headers：点「添加」，填
  - Key：`Authorization`
  - Value：`Bearer 你在.env里设置的MCP_AUTH_TOKEN`

保存后豆包即可调用以下工具：`google_news`、`google_search`、`facebook_profile_posts`、`export_report`、`latest_daily_report`、`trigger_daily_report`。

> `trigger_daily_report`：**手动触发**一次日报生成。后台异步执行（抓取+LLM，通常
> 数分钟），调用后立即返回「已开始」；同一时刻只跑一个任务（加锁）。生成完成后用
> `latest_daily_report` 取结果。适合用户想「现在立刻刷新一份」、不等定时任务时。

> `latest_daily_report`：**不运行流水线**，直接返回 `output/daily_report/` 下最近
> 生成的 Word 日报。日报由进程内定时任务（每天 13:00、16:00）在后台产出，MCP 侧
> 只做读取。返回**富内容**：一个文件卡片（`ResourceLink`，指向 http 下载链接）+
> 一段 JSON 元信息（含 `download_url`）。客户端会把它渲染成**可点击的下载组件**，
> 用户无需手动复制路径。

#### 日报下载端点与跨机部署（重要）

日报 .docx 的下载由 **MCP Server（8000）自带**的端点提供，路径
`/download/daily_report/{filename}`，**不依赖** AgentOS 后端（7777）。

由于 WordBuddy 等客户端通常与 MCP Server **不在同一台机器**，文件卡片用的是
http 链接而非本机 `file://`，因此必须在 `config.yaml` 的 `mcp` 段填**对外可访问**
的基础地址：

```yaml
mcp:
  host: 0.0.0.0
  port: 8000
  public_base_url: "http://SERVER_IP:8000"   # 或 http://你的域名（配了 Nginx 反代时）
```

> 留空会回退到 `localhost:8000`，跨机点击下载会失败，务必填成客户端能访问到的地址。

下载端点同样受 `MCP_AUTH_TOKEN` 保护，支持两种带 token 的方式：
`Authorization: Bearer <token>` 头，或 URL 后加 `?token=<token>`。配了 token 时
`download_url` 会**自动带上 `?token=`**，文件卡片点一下即可下载。放行 8000 端口后
即可生效。

> 验证鉴权：不带 Header / token 请求 `/mcp` 会返回 401，带正确 Bearer token 返回 200。

> 说明：`export_report` 的 Excel 下载链接仍由 AgentOS 后端（7777）的 `/download`
> 提供；若只用日报功能，可只跑 MCP Server（8000）。

---

## 五、访问与使用

1. 浏览器打开：`http://SERVER_IP:3000`
2. 左侧 endpoint 填：`http://SERVER_IP:7777`
3. 选择 **News Agent**，提问：
   - `给我5条最新的AI新闻`（数量控制）
   - 返回末尾的 **[下载 Excel 报告]** 链接可点击下载（链接指向 `http://SERVER_IP:7777/download/...`）

---

## 六、日常运维

### 查看进程

```bash
ps -ef | grep -E "main.py|next" | grep -v grep
```

### 停止服务

```bash
pkill -f main.py        # 停后端
pkill -f "next start"   # 停前端
```

### 更新代码后重启

```bash
cd /opt/agno-media
git pull
source .venv/bin/activate
pip install "agno[os,sqlite]" openai pyyaml python-dotenv google-search-results openpyxl greenlet  # 依赖有更新时
pkill -f main.py
nohup .venv/bin/python main.py > logs/backend.log 2>&1 &
```

---

## 七、常见问题

| 现象 | 排查 |
|------|------|
| 外部打不开 3000/7777 | 检查 `host` 是否为 `0.0.0.0`、防火墙、云安全组是否放行 |
| 下载链接点击 404/打不开 | 检查 `config.yaml` 的 `public_base_url` 是否填对服务器 IP |
| 后端启动报 401 | `.env` 的 `AI_API_KEY` / `SERPAPI_API_KEY` 未填或无效 |
| 启动报 `No module named 'greenlet'` | 缺 greenlet 依赖，执行 `pip install greenlet`（或 `pip install "sqlalchemy[asyncio]" greenlet`）后重启 |
| 前端连不上后端 | endpoint 必须填 `http://SERVER_IP:7777`，不是 localhost |
| 进程重启后丢失 | nohup 仅后台运行，重启服务器需手动再启；需开机自启请改用 systemd |
