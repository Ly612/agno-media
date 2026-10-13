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
uv pip install "agno[os,sqlite]" openai pyyaml python-dotenv google-search-results openpyxl greenlet requests apscheduler "mcp>=1.2.0,<2"
```

或用 pip：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install "agno[os,sqlite]" openai pyyaml python-dotenv google-search-results openpyxl greenlet requests apscheduler "mcp>=1.2.0,<2"
```

> 也可直接按 `pyproject.toml` 一次性装齐（含可选 extra）：
> ```bash
> uv pip install -e ".[daily-report]"
> ```
> `daily-report` extra 含 `pypandoc`（Markdown→Word）与 `playwright`（截图）。
> **注意**：`pypandoc` 只是 pandoc 的 Python 壳，**还需另装系统 pandoc 二进制**，见下。

### 2.5 安装 Word 转换的系统依赖 pandoc（生成日报 Word 必需）

日报的 Markdown→Word 转换依赖系统 `pandoc` 二进制。**只装 `pypandoc` 不够**：
pypandoc 只是调用 pandoc 的壳，缺 pandoc 时转换会静默跳过，表现为
**只生成 `.md`、没有 `.docx`**。

```bash
# CentOS / RHEL
sudo yum install -y pandoc        # CentOS 7/8；较新版用 dnf
# Debian / Ubuntu
sudo apt install -y pandoc

# 验证
pandoc --version
```

> 若 `yum` 源里 pandoc 版本过老，可从 https://github.com/jgm/pandoc/releases
> 下载对应架构的静态包解压后把 `pandoc` 放进 PATH。
>
> 装好后可单独验证转换：
> ```bash
> .venv/bin/python -m utils.word output/daily_report/report_*.md
> ```
> 成功会打印 `✓ 转换完成` 并生成同名 `.docx`。

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

日报 .docx 的下载**复用 AgentOS 后端（7777）的 `/download/{filename}` 端点**，
与 `export_report` 导出的 Excel **完全同一机制**（同一个端点、同一份
`server.public_base_url` 配置），不再使用 MCP Server 自带的下载端点。

因此对外地址只需在 `config.yaml` 的 **`server` 段**填一次：

```yaml
server:
  host: 0.0.0.0
  port: 7777
  public_base_url: "http://SERVER_IP:7777"   # 日报与 Excel 共用；或 http://你的域名
```

> 由于 WordBuddy 等客户端通常与服务**不在同一台机器**，文件卡片用的是 http 链接
> 而非本机 `file://`。留空会回退到 `localhost:7777`，跨机点击下载会失败，务必填成
> 客户端能访问到的地址。

> **注意**：该端点由后端（7777）提供，所以**日报下载需要后端在运行**。若只想跑
> MCP（8000）、不跑后端，则点击日报下载会失败——此场景请改回用 MCP 自带端点。

> `mcp.public_base_url` 已**不再需要**（MCP 服务不再自带下载端点），可留空或删除。

> 验证鉴权：MCP 的 `/mcp` 端点仍受 `MCP_AUTH_TOKEN` 保护——不带 Header 请求会返回
> 401，带正确 Bearer token 返回 200。日报下载端点（后端 7777）与 Excel 一致，不校验
> MCP token。

---

## 四点八、浏览器截图初始化（Chromium 与 AdsPower，可选）

日报流水线的「信源网站首页截图」板块（`utils/site_screenshot.py`）需要一个浏览器。
它有**两条路径，自动择优回退**：

1. **首选 AdsPower 指纹浏览器**：各站点带自己的代理/指纹，翻墙更稳。通过本地 API
   启动固定 profile，再用 Playwright `connect_over_cdp` 接管。
2. **回退本地 Chromium**（Playwright 自带）：AdsPower 不可用（未启用 / 未装 /
   user_id 无效 / 客户端没开）时自动走这条，保证截图板块不缺失。

> 截图整个板块都是**可选**的：两条路径都不可用时，该阶段会被跳过，日报照常生成
> （只是没有截图板块）。也可用 `config.yaml` 的 `daily_report.skip_screenshot: true`
> 彻底关掉截图。

### A. 本地 Chromium（回退路径）初始化

这是最简单、服务器上最稳的方式，**无需 AdsPower**。

```bash
cd /root/agno-media
source .venv/bin/activate

# 1) 装 playwright（若装依赖时没带上）
uv pip install playwright        # 或 pip install playwright

# 2) 下载 Chromium 浏览器二进制
playwright install chromium

# 3) CentOS/Linux 还需装 Chromium 运行所需的系统库（关键，否则启动即崩）
playwright install-deps chromium
# 若上面命令在老版 CentOS 不可用，手动装常见依赖：
# sudo yum install -y nss nspr atk at-spi2-atk cups-libs libdrm libxkbcommon \
#   at-spi2-core libXcomposite libXdamage libXrandr mesa-libgbm pango alsa-lib
```

要点：
- 服务器上务必**无头模式**运行（流水线默认 `headless=True`，无需改）。
- 若 playwright 精确版本的浏览器下不动，可设环境变量
  `PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome` 指向任意已有的完整 Chromium，
  代码会优先用它（见 `_find_chromium_executable`）。
- 无显示器的服务器用无头即可，**不需要** Xvfb（无头 Chromium 自带虚拟渲染）。

验证：

```bash
.venv/bin/python -m utils.site_screenshot --local   # 强制走本地 Chromium 测一遍
```

### B. AdsPower（首选路径）初始化

AdsPower 是**带 GUI 的桌面客户端**，且 **Local API 需付费版**权限。服务器（无显示器）
上要用它，需以**无头 + api-key** 方式启动，并用 Xvfb 提供虚拟显示。

**1) 配置项**（`.env`）：

```
DAILY_REPORT_ADSPOWER_API_URL=http://local.adspower.net:50325  # 默认即可，本机回环
DAILY_REPORT_ADSPOWER_USER_ID=你的profile的user_id            # 必填，否则直接回退本地
DAILY_REPORT_ADSPOWER_API_KEY=你的APIKEY                       # 服务器headless/开启安全校验时必填
DAILY_REPORT_ADSPOWER_ENABLED=true                             # false 则跳过 AdsPower
```

> `DAILY_REPORT_ADSPOWER_API_KEY` 有值时，代码会给所有本地 API 请求自动带上
> `Authorization: Bearer <api-key>`。桌面 GUI 模式通常可留空；服务器 headless 模式
> 必填（与下面启动命令的 `--api-key` 一致）。

> `user_id` 是 AdsPower 里某个浏览器 profile 的唯一 ID（在客户端「环境管理」里
> 导入/创建 profile 后得到）。各站点的翻墙代理应配在这个 profile 上。

**2) 桌面环境（有显示器 / 本地 Mac）**：
装好 AdsPower 客户端 → 登录付费账号 → 「自动化 → API」确认接口状态正常（默认端口
50325）→ 创建/导入一个 profile 并记下 `user_id` → 保持客户端常驻运行即可。

**3) Linux 服务器（无显示器，无头模式）**：

```bash
# 装虚拟显示服务
sudo yum install -y xorg-x11-server-Xvfb      # 或 apt: sudo apt install -y xvfb

# 以无头 + api-key 启动 AdsPower 的 Local API（在 AdsPower 安装目录下执行）
xvfb-run -a adspower_global \
  --headless=true \
  --api-key=你的APIKEY \
  --api-port=50325 \
  --no-sandbox
# API key 在 AdsPower「自动化 → API」里 Generate 获取；启动后 CLI 会打印可用地址
```

> 鉴权说明：headless 模式（及开启安全校验时）Local API 必须带 api-key。本项目已
> 支持：只要在 `.env` 配了 `DAILY_REPORT_ADSPOWER_API_KEY`，`utils/adspower.py`
> 会自动给请求加 `Authorization: Bearer <api-key>`（与启动命令的 `--api-key` 填同
> 一个值）。不配 key 则不带该头（适用于桌面 GUI 模式）。

验证：

```bash
.venv/bin/python -m utils.adspower status        # 探活：✅ 在线 / ❌ 不可用
.venv/bin/python -m utils.adspower start         # 启动 profile，打印 CDP ws 端点
.venv/bin/python -m utils.site_screenshot        # 优先 AdsPower，失败回退本地
```

### C. 建议

服务器部署**优先用本地 Chromium（路径 A）**：无需付费、无需 GUI、无需 Xvfb、无代码
改动，最省心。AdsPower 适合「各站点需要不同代理/指纹才能稳定访问」的场景，且需接受
上面的代码限制或自行补 Bearer 头。

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
uv pip install -e ".[daily-report]"   # 依赖有更新时（含 pypandoc + playwright）
pip install "agno[os,sqlite]" openai pyyaml python-dotenv google-search-results openpyxl greenlet requests apscheduler "mcp>=1.2.0,<2"  # 或按需补齐
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
| **日报只生成 `.md`、没有 `.docx`** | Word 转换依赖系统 pandoc。先 `pandoc --version` 确认是否安装；没装则 `sudo yum install -y pandoc`（或 apt）。再确认 Python 侧 `pypandoc` 已装：`.venv/bin/python -c "import pypandoc"`。手动验证：`.venv/bin/python -m utils.word output/daily_report/report_*.md`，成功会打印 `✓ 转换完成`。此前的版本缺依赖时会**静默跳过**（日志里是`⚠ Word 转换跳过（依赖缺失）`），现已改为打印明确原因 |
| 前端连不上后端 | endpoint 必须填 `http://SERVER_IP:7777`，不是 localhost |
| 进程重启后丢失 | nohup 仅后台运行，重启服务器需手动再启；需开机自启请改用 systemd |
