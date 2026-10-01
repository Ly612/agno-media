"""媒体监控 Agent：LLM 驱动的五阶段媒体监控流水线。

Phase 1: 首页抓取（委托 tools.MediaMonitorToolkit）
Phase 2: 话题挑选（LLM）
Phase 3: 详情总结（LLM + BrightData 正文抓取）
Phase 4: 社交平台抓取 + 贴文总结（委托 tools.ApifyScraperToolkit + LLM）
Phase 5: 简报渲染（LLM / 本地降级，含海外社交平台板块）

依赖:
    - agno (Agent, OpenAILike, BrightDataTools)
    - tools.MediaMonitorToolkit（Phase 1 抓取）
    - LLM API（环境变量 MEDIA_MONITOR_LLM_API_KEY 等）

用法:
    from utils.media_monitor_pipeline import MediaMonitorPipeline

    agent = MediaMonitorPipeline()
    result = agent.run_full_pipeline()
    agent.select_topics(manifest_file="scrape_manifest.json")
    agent.render_report(summaries_file="summaries.json")

    # 命令行:
    python -m utils.media_monitor_pipeline
    python -m utils.media_monitor_pipeline --skip-to topics
    python -m utils.media_monitor_pipeline --scrape-only
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from agno.agent import Agent
from agno.models.openai.like import OpenAILike

from utils.settings import apify_settings, media_monitor_settings, bright_data_settings
from utils.apify_scraper import ApifyScraperToolkit, _extract_urls
from utils.email_sender import EmailSender
from utils.feishu_bot import FeishuBot
from utils.media_monitor_tool import (BEIJING_TZ,
                                 DEFAULT_OUTPUT_DIR, MEDIA_SOURCES,
                                 TRUNCATE_LIMIT, MediaMonitorToolkit)

# ── 对外命名（邮件标题 / 附件显示名）─────────────────────────
# 磁盘文件仍是 report_YYYYMMDD_HHMMSS.md/.docx（word.py 的 glob/日期正则依赖此前缀，
# 不改）；只在发邮件时把标题与附件显示名改成「日报选题参考-YYYYMMDD-H点」。
# 小时取报告的运行时刻（24 小时制、去掉前导 0），而非发送时刻，
# 这样 --skip-to send 补发旧报告时标题也对得上报告本身的时间。
_REPORT_TS_RE = re.compile(r"report_(\d{8})_(\d{2})\d{4}")


def _report_display_name(report_path: str) -> str:
    """由报告文件名推出对外展示名「日报选题参考-YYYYMMDD-H点」（不含后缀）。

    文件名符合 report_YYYYMMDD_HHMMSS 时取其中的日期与小时；
    不符合（如用户自定义 output_file）时回落到当前北京时间。
    """
    m = _REPORT_TS_RE.search(Path(report_path).name)
    if m:
        date_part, hour_part = m.group(1), int(m.group(2))
    else:
        now = datetime.now(BEIJING_TZ)
        date_part, hour_part = now.strftime("%Y%m%d"), now.hour
    return f"日报选题参考-{date_part}-{hour_part}点"


# ── LLM Prompt 模板 ───────────────────────────────────────

TOPIC_SELECTION_INSTRUCTIONS = """\
你是一名资深新闻编辑，擅长跨媒体的选题判断。

你会收到 23 家国际主流媒体首页首屏的 Markdown 内容。你的任务是：

1. 从所有媒体首页内容中提取每条新闻的标题和链接
2. **时效性硬性要求（最重要）**：只挑选发布时间在「最近 2 个自然日内」的新闻，
   即仅限「今天」或「昨天」发布的新闻（具体日期见下方 prompt 给出的当前日期）。
   前天及更早（3 天前及以上）的新闻一律舍弃，绝不入选。
   - 无法从内容直接判断日期时，结合首屏时间线、"X hours ago / 小时前 / 今日" 等相对时间线索推断；
   - 明显是旧闻、回顾、往期专题的，一律舍弃；
   - 若某组满足时效要求的话题不足 5 个，宁可少于 5 个，也不要用旧闻凑数。
3. 综合以下指标，在满足上述时效要求的新闻中，分别挑选全球报道最重要话题 + 中国相关报道最重要话题（各至多 5 个）：
   - **版面位置优先级（重要权重）**：每家内容按网页首页从上到下的真实顺序排列，
     越靠前出现的新闻，版面越重要、越应优先入选。每家标题前还标注了 [第N条] 序号，
     N 越小越靠版面顶部。此外每家附带「布局」说明（如 level1=头版大头条、
     level2=次级头条、level3=其余），level 数字越小版面权重越高。
     选题时把「出现越靠前 / level 越小」当作重要性的强信号，优先选各站的头版头条（level1）。
   - 新闻重要性（社会影响、公众关注度）
   - 报道媒体数量（同一事件被越多主流媒体报道，权重越高）
   - 话题多样性（避免同一组内话题全部集中在同一领域）
4. 同一事件的不同报道聚合到同一个话题下（articles 字段包含多条原始新闻）

global_topics: 从综合新闻/中东/美国政治/财经/外交/深度杂志/智库等类别中挑选最重要的至多 5 个全球话题（须满足时效要求）。
china_topics: 从全部媒体的涉华报道中挑选至多 5 个最重要的中国相关话题（不限于中国专区，任何媒体的涉华新闻均可；须满足时效要求）。

topic 字段用一句中文概括事件，reason 字段简述选择理由。
articles 字段中必须严格使用原始内容中出现过的标题 / URL / 媒体名，不要杜撰。

最终必须严格输出以下 JSON 结构（global_topics 与 china_topics 各至多 5 个；满足时效要求的不足 5 个时可少于 5 个）：

{
  "global_topics": [
    {
      "topic": "一句话中文概括事件",
      "reason": "选择理由",
      "articles": [
        {"title": "原始英文标题", "url": "原文链接", "media": "媒体名"},
        ...
      ]
    },
    ...至多 5 个
  ],
  "china_topics": [
    {
      "topic": "一句话中文概括事件",
      "reason": "选择理由",
      "articles": [
        {"title": "原始英文标题", "url": "原文链接", "media": "媒体名"},
        ...
      ]
    },
    ...至多 5 个
  ]
}

只输出 JSON，不要添加任何其他文字或 markdown 代码块围栏。千万不要用 ```json 或 ``` 包裹。"""

TOPIC_SUMMARY_INSTRUCTIONS = """\
你是一名新闻分析师，擅长对同一事件的多方报道做整合摘要。

你会收到一个话题，包含多条不同媒体的报道（含标题、链接、媒体名）。请：

1. 使用 scrape_as_markdown 工具依次抓取每条链接的正文
2. 阅读后撰写一句话要点，不要与标题重复（one_liner，不超过 40 个汉字）
3. 撰写综合摘要（summary），严格控制在 200 个汉字以内
   摘要中必须体现不同媒体之间的观点、立场或事实差异，
   可以采用『A 媒体认为…；B 媒体则强调…』这样的句式
4. sources 填入引用过的媒体名称列表
5. article_links 填入每条引用报道的原始链接（与 sources 一一对应）
6. 原文里提到的专家需要保留具体的头衔等信息

如果某个链接抓取失败，跳过该链接，但仍需基于其他链接完成摘要。
禁止编造原文中没有的事实。禁止在 summary 中使用省略号（「…」「...」等）省略内容。

最终严格输出以下 JSON：

{
  "topic": "话题中文概括",
  "one_liner": "不超过40个字的一句话要点",
  "summary": "200字以内的综合摘要，体现不同媒体观点差异",
  "sources": ["媒体A", "媒体B"],
  "article_links": ["https://...", "https://..."]
}

只输出 JSON，不要添加任何其他文字或 markdown 代码块围栏。千万不要用 ```json 或 ``` 包裹。summary 内如含双引号请用中文引号「」替代。"""

# 用于 --fix-errors 补全：正文已由外部直接抓取（付费墙文章用英文公开搜索片段兜底），
# 无需再调用抓取工具。绕开 BrightDataTools 的 Agent 工具循环
# （付费墙文章会触发中文 Google 搜索回退，中文内容超过 302.ai 长度阈值即报 404）。
DIRECT_SUMMARY_INSTRUCTIONS = """\
你是一名新闻分析师，擅长对同一事件的多方报道做整合摘要。

你会收到一个话题，包含多条**已获取好的报道内容**（正文或公开搜索摘要，含媒体名、标题、链接）。
请直接阅读这些内容，**无需调用任何工具**：

1. 撰写一句话要点，不要与标题重复（one_liner，不超过 40 个汉字）
2. 撰写综合摘要（summary），严格控制在 200 个汉字以内
   摘要中必须体现不同媒体之间的观点、立场或事实差异，
   可以采用『A 媒体认为…；B 媒体则强调…』这样的句式
3. sources 填入引用过的媒体名称列表
4. article_links 填入每条引用报道的原始链接（与 sources 一一对应）
5. 原文里提到的专家需要保留具体的头衔等信息

如果某个链接的正文抓取失败，跳过该部分，但仍需基于其他正文完成摘要。
禁止编造正文中没有的事实。禁止在 summary 中使用省略号（「…」「...」等）省略内容。

最终严格输出以下 JSON：

{
  "topic": "话题中文概括",
  "one_liner": "不超过40个字的一句话要点",
  "summary": "200字以内的综合摘要，体现不同媒体观点差异",
  "sources": ["媒体A", "媒体B"],
  "article_links": ["https://...", "https://..."]
}

只输出 JSON，不要添加任何其他文字或 markdown 代码块围栏。千万不要用 ```json 或 ``` 包裹。summary 内如含双引号请用中文引号「」替代。"""

FINAL_OUTPUT_INSTRUCTIONS = """\
你是一名新闻简报编辑，负责把摘要数据渲染成 markdown。

输入是一段 JSON，字段结构为 { summaries: [ {topic, one_liner, summary, sources, article_links} ] }。
请输出一份结构清晰的中文 markdown 简报，遵循以下模板：

# 今日新闻速览

> 共 N 个话题，涵盖多家主流媒体的核心报道。

## 🌍 全球要闻

### 1. <topic>

**要点**：<one_liner>

<summary>

**来源**：
- 媒体A：链接A
- 媒体B：链接B

---

（依次列出 5 个全球话题）

## 🇨🇳 中国相关

### 6. <topic>

**要点**：<one_liner>

<summary>

**来源**：
- 媒体A：链接A
- 媒体B：链接B

---

（依次列出 5 个中国话题，最后一个话题末尾不需要分隔线）

## 格式规则

1. summary 原文照搬，禁止使用省略号（如「…」「...」）省略或截断内容
2. 来源部分必须直接列出每篇被引用报道的原始链接，格式为 `- 媒体名：https://...`，
   不要用 `[媒体名](链接)` 的 markdown 链接隐藏 URL
3. 只输出 markdown 内容本身，不要添加代码块围栏，不要添加额外解释
4. 原文里提到的专家需要保留具体的头衔等信息"""


# ── 社交平台贴文总结 Prompt ────────────────────────────────

# 302.ai 对中文内容长度敏感（约 1000+ 中文字符即报 404），此指令保持精炼，
# 候选贴文文本在 _fit_candidates_for_llm 里额外做中文长度预算控制。
SOCIAL_SUMMARY_INSTRUCTIONS = """\
你是海外社交舆情分析师，负责从社交平台抓取的贴文中挑选最有新闻价值和影响力的条目，
**优先关注中国相关的社会事件，而非官方大媒体转发的官网新闻**。

输入是一段 JSON：{"platform": "X/Facebook/Google News", "candidates": [贴文...]}。
每个贴文字段：
- author: 作者显示名
- handle: 作者账号（不含 @）
- followers_display: 已换算好的关注者表述，如 "219 万关注者"（可能为空）
- text: 贴文内容
- url: 原始链接
- media: video / image / none
- article_urls: 贴文内引用的文章链接列表（可能为空，直接照搬）

请挑选 2-3 条最值得关注、最具新闻价值的贴文，挑选优先级：
1. 优先选择**个人用户、小众媒体或独立媒体**（博主、记者、学者、NGO、地方媒体等）发布的贴文，
   尤其是带独家视角、一手资料或具体数据的内容
2. 优先选择与**中国社会事件、民生、科技、文化、经济、环境、公共话题**直接相关的贴文
3. 排除官方大媒体（BBC、CNN、Reuters、FT、WSJ、The Economist、SCMP 等）
   转发自家官网新闻的贴文——这类与新闻简报正文重复
4. content 请写**详细的中文描述**（80-150 字）：交代清楚事件主体、时间、地点、
   关键数据与影响，不要把贴文原文整段照抄，也不要只是一句口号

只输出 JSON：
{"items": [{"author": "作者显示名", "handle": "账号", "followers_display": "关注者表述原样照搬", "platform_desc": "平台动作描述", "content": "中文详细描述", "url": "原始链接", "article_urls": ["文章链接1", "文章链接2"]}]}

要求：
1. content 为中文详细描述（80-150 字），保留人物、时间、事件、关键数据等核心信息，只依据给定 text 撰写，严禁编造
2. platform_desc 按平台填：
   - X：写 "官方推特主页发布帖文"，贴文有视频再加"配视频"、有图片再加"配图"
     （如 "官方推特主页发布帖文配视频"）
   - Facebook：媒体/机构账号写 "在脸书发布报道称"，个人账号写 "在脸书发布帖文"
   - Google News：写 "报道称"（author 为来源媒体名）
3. followers_display 直接照搬输入值，不要重新换算、不要改动空格
4. url 与 article_urls 直接照搬输入值，不要修改、不要省略
5. 只返回 JSON，不要代码块围栏"""


# ═══════════════════════════════════════════════════════════
# JSON 解析工具函数
# ═══════════════════════════════════════════════════════════

def _is_api_error(text: str) -> bool:
    """检测 LLM 返回的文本是否为 API 报错（而非有效回复）。"""
    if not text:
        # 内容为空 / None（服务端返回空 content、超时或限流）视为 API 报错
        return True
    error_markers = [
        "Parameter error", "Error code:", "API status error",
        "Unknown model error", "cloudflare_error",
        "Error in Agent run", "timed out", "timeout",
        "connection error", "connectionerror", "connection refused",
        "connection reset", "connection timed out",
    ]
    return any(m.lower() in text.lower() for m in error_markers)


# LLM 拒答话术（内容级问题，非传输错误）。上游有时会把整段摘要替换成
# 这类拒答，需当作无效内容触发重试 / 补全，而不是当成正常摘要写进报告。
_REFUSAL_MARKERS = (
    "我无法给到", "无法给到相关内容", "我无法提供", "无法提供相关",
    "我无法回答", "无法回答这个", "我无法协助", "无法协助",
    "我不能提供", "我不能回答", "作为一个ai", "作为一个人工智能",
    "抱歉，我无法", "很抱歉，我无法", "我无法讨论", "无法讨论该",
    "sorry, i can't", "sorry, i cannot", "i'm unable to", "i am unable to",
    "i cannot assist", "i can't assist", "i cannot provide", "i can't provide",
)


def _is_refusal(text: str) -> bool:
    """检测 LLM 返回是否为拒答话术（如「你好，我无法给到相关内容。」）。

    这类回复是有效 JSON / 正常文本，能被解析，却不含真实摘要内容，
    需与 API 报错一样触发重试与自动补全。
    """
    if not text:
        return False
    low = text.lower()
    return any(m.lower() in low for m in _REFUSAL_MARKERS)


def _extract_json_candidates(text: str) -> List[str]:
    """从文本中提取所有可能的 JSON 对象候选。"""
    candidates: List[str] = []
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                candidates.append(text[start:i + 1])
                start = -1
    candidates.sort(key=len, reverse=True)
    return candidates


def _escape_inner_quotes(raw: str) -> Optional[str]:
    """转义 JSON 字符串「值」内部未转义的引号。

    LLM 常在字符串值里写裸引号（如中文 "最大"生命线"），使 json 解析在此
    提前认为字符串结束、后续崩溃。本函数逐字符扫描：进入字符串后遇到 `"`，
    仅当其后（跳过空白）紧跟 JSON 结构符（`: , } ]` 或字符串末尾）时才认定
    为真正的闭合引号，否则视为字符串内部字符并转义成 `\\"`。
    """
    try:
        s = raw
        out: List[str] = []
        in_string = False
        escape_next = False
        n = len(s)
        for i, ch in enumerate(s):
            if escape_next:
                out.append(ch)
                escape_next = False
                continue
            if ch == '\\' and in_string:
                out.append(ch)
                escape_next = True
                continue
            if ch == '"':
                if not in_string:
                    in_string = True
                    out.append(ch)
                    continue
                # 已在字符串内，判断这个 " 是闭合还是内部裸引号
                j = i + 1
                while j < n and s[j] in ' \t\r\n':
                    j += 1
                if j >= n or s[j] in ',:}]':
                    in_string = False          # 真正的闭合引号
                    out.append(ch)
                else:
                    out.append('\\"')          # 字符串内部的裸引号 → 转义
                continue
            out.append(ch)
        return ''.join(out)
    except Exception:
        return None


def _repair_json(raw: str) -> Optional[str]:
    """尝试修复 LLM 常见 JSON 瑕疵。"""
    try:
        s = raw.strip()
        s = re.sub(r',(\s*[}\]])', r'\1', s)

        fixed: List[str] = []
        in_string = False
        escape_next = False
        for ch in s:
            if escape_next:
                fixed.append(ch)
                escape_next = False
                continue
            if ch == '\\' and in_string:
                fixed.append(ch)
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                fixed.append(ch)
                continue
            if in_string and ch in '\n\r\t':
                fixed.append(repr(ch)[1:-1])
                continue
            fixed.append(ch)

        result = ''.join(fixed)
        if result.count('"') % 2 != 0:
            result = result.rstrip() + '"'
        return result
    except Exception:
        return None


def _extract_json_field(text: str, field_name: str) -> Optional[str]:
    """从 JSON 文本中提取指定字段的值（容忍格式破损）。"""
    t = text.strip()
    t = re.sub(r'^```\w*\s*', '', t)
    t = re.sub(r'\s*```$', '', t)

    pattern = rf'"{re.escape(field_name)}"\s*:\s*"(.*?)(?:"\s*[,}}\]]|$)'
    m = re.search(pattern, t, re.DOTALL)
    if m:
        value = m.group(1)
        value = value.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
        return value.rstrip()
    return None


def _parse_json_response(response) -> dict:
    """从 Agent 返回内容中提取 JSON，容忍 LLM 常见的格式瑕疵。"""
    text = response.content if hasattr(response, "content") else str(response)

    if not text:
        # response.content 为 None/空（服务端返回空内容、超时或限流）
        raise ValueError("LLM 返回内容为空（response.content 为空/None），可能是超时、限流或服务端异常")

    if _is_api_error(text):
        snippet = text[:200].replace("\n", "\\n")
        raise ValueError(f"LLM 返回的是 API 报错而非有效回复: {snippet}")

    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()

    # 1) 直接解析
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass

    # 2) 修复常见瑕疵后解析：依次尝试 _repair_json、转义字符串内裸引号、二者组合
    for repaired in (
        _repair_json(t),
        _escape_inner_quotes(t),
        _repair_json(_escape_inner_quotes(t) or t),
    ):
        if repaired is not None:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    # 3) 正则找最外层 {...}
    for candidate in _extract_json_candidates(t):
        for cand in (
            candidate,
            _repair_json(candidate),
            _escape_inner_quotes(candidate),
            _repair_json(_escape_inner_quotes(candidate) or candidate),
        ):
            if cand is not None:
                try:
                    return json.loads(cand)
                except json.JSONDecodeError:
                    pass

    # 4) 字段级提取
    extracted: Dict[str, object] = {}
    for field in ("topic", "one_liner", "summary"):
        val = _extract_json_field(t, field)
        if val:
            extracted[field] = val
    src_m = re.search(r'"sources"\s*:\s*\[(.*?)\]', t, re.DOTALL)
    if src_m:
        extracted["sources"] = re.findall(r'"([^"]+)"', src_m.group(1))
    links_m = re.search(r'"article_links"\s*:\s*\[(.*?)\]', t, re.DOTALL)
    if links_m:
        extracted["article_links"] = re.findall(r'"([^"]+)"', links_m.group(1))

    if extracted.get("summary"):
        return extracted

    snippet = text[:500].replace("\n", "\\n")
    raise ValueError(f"无法从 LLM 返回中解析 JSON。原始返回前500字符: {snippet}")


def _response_text(response) -> Optional[str]:
    """安全取出 Agent 返回的文本内容（content 为 None 时返回 None，不崩溃）。"""
    if response is None:
        return None
    return response.content if hasattr(response, "content") else str(response)


def _run_agent_with_retry(agent, prompt, retries: int = 3, delay: float = 3.0):
    """运行 Agent，对空 content（服务端偶发空返回/超时/限流）自动重试。

    某些网关在长 prompt 下会偶发返回空 content（HTTP 正常但 message.content 为空），
    直接失败会中断整条流水线。这里对「空内容」重试若干次，指数退避。

    Returns:
        最后一次 response（可能仍为空内容，由上层解析逻辑判定并报错）
    """
    response = None
    for attempt in range(1, retries + 1):
        try:
            response = agent.run(prompt)
        except Exception as e:  # 传输层异常也重试
            print(f"   ⚠ LLM 调用异常（第 {attempt}/{retries} 次）: {e}")
            response = None
        else:
            if _response_text(response):
                return response
            print(f"   ⚠ LLM 返回空内容（第 {attempt}/{retries} 次），重试中...")
        if attempt < retries:
            time.sleep(delay * attempt)
    return response


def _strip_all_code_fences(text: str) -> str:
    """彻底移除所有 markdown 代码块围栏。"""
    text = text.strip()
    lines = text.split("\n")
    if lines and re.match(r'^```', lines[0].strip()):
        lines = lines[1:]
    if lines and re.match(r'^```$', lines[-1].strip()):
        lines = lines[:-1]
    result = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        result.append(line)
    return "\n".join(result).strip()


# 像新闻标题的链接才编号：过滤掉过短 / 明显是导航或功能入口的链接文字
_NAV_LINK_WORDS = {
    "sign in", "log in", "login", "subscribe", "register", "menu", "more",
    "search", "home", "newsletters", "newsletter", "sign up", "live", "video",
    "videos", "watch", "podcasts", "podcast", "sections", "all", "see all",
    "read more", "contact", "about", "help", "advertise", "jobs", "shop",
}


def _number_headline_links(md: str, min_title_len: int = 12) -> str:
    """给 Markdown 里像新闻标题的链接按出现顺序注入 [第N条] 序号（路线 A：版面位置优先）。

    只给「链接文字够长、且不是 Sign in/Subscribe 之类导航词」的链接编号，避免序号被
    首页顶部的导航 / 功能入口占满。N 反映该新闻在网页从上到下的出现先后——越小越靠
    版面顶部，交给 LLM 当作版面重要性的信号。不改动链接本身，仅在其前面加 `[第N条] `。
    """
    link_re = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
    counter = {"n": 0}

    def _sub(m: "re.Match") -> str:
        text = m.group(1).strip()
        low = text.lower()
        # 过短、或纯导航/功能词 → 不编号，原样返回
        if len(text) < min_title_len or low in _NAV_LINK_WORDS:
            return m.group(0)
        counter["n"] += 1
        return f"[第{counter['n']}条] {m.group(0)}"

    return link_re.sub(_sub, md)


def _flatten_source_links(md: str) -> str:
    """规整来源列表：markdown 链接展开为 `- 媒体名：url`，且每个来源网址各占一行。

    LLM 渲染时可能不遵守模板：既可能输出 `[媒体名](url)` 隐藏链接，也可能把
    多条来源挤在同一行（空格 / 顿号 / 分号分隔），导致报告里来源网址不换行。
    这里做确定性后处理：先展开隐藏链接，再把一行内的多个 URL 拆成各占一行，
    保证每个信源网址独立成行（pandoc 转 Word 时裸 URL 仍可点击）。
    """
    # 匹配一行里出现的所有 http(s) URL；URL 到下一个空白 / 顿号 / 分号 / 逗号为止
    url_re = re.compile(r"https?://[^\s，、；;,]+")
    md_link_re = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")

    lines = md.split("\n")
    out: List[str] = []
    in_sources = False  # 是否处于「**来源**：」之后的来源区块
    for line in lines:
        stripped = line.strip()

        # 进入 / 退出来源区块：遇到「来源」标记进入；遇到分隔线 / 新标题 / 空行退出
        if re.match(r"^\**\s*来源\s*[:：]?\s*\**$", stripped):
            in_sources = True
            out.append(line)
            continue
        if in_sources and (not stripped or stripped.startswith("#") or stripped.startswith("---")):
            in_sources = False
            out.append(line)
            continue

        is_list_item = bool(re.match(r"^\s*[-*•]\s+", line))

        # 整行就是单个隐藏链接列表项 → 展开为 `- 名：url`
        m = re.match(r"^\s*[-*•]\s+\[([^\]]+)\]\((https?://[^)]+)\)\s*$", line)
        if m:
            out.append(f"- {m.group(1)}：{m.group(2)}")
            continue

        urls = url_re.findall(line)
        # 只在「来源区块内」或「本行是列表项」时才拆分——避免误伤正文段落里的 URL。
        # 不满足条件、或 URL 不足 2 个 → 原样保留。
        if len(urls) <= 1 or not (in_sources or is_list_item):
            out.append(line)
            continue

        # 一行里有多个 URL 且确属来源：拆成每个来源各占一行。
        # 优先按「[名](url)」配对出媒体名；配不上就只列裸 URL。
        pairs = md_link_re.findall(line)
        if len(pairs) == len(urls):
            for name, url in pairs:
                out.append(f"- {name}：{url}")
        else:
            for url in urls:
                out.append(f"- {url}")
    return "\n".join(out)


def _fallback_render_markdown(summaries: List[dict]) -> str:
    """纯本地降级渲染（不依赖 LLM），生成 Markdown 简报。"""
    n = len(summaries)
    lines = ["# 今日新闻速览", "", f"> 共 {n} 个话题，涵盖多家主流媒体的核心报道。", ""]

    global_items = [s for s in summaries if s.get("group") == "global"]
    china_items = [s for s in summaries if s.get("group") == "china"]
    if not global_items and not china_items:
        global_items = summaries[:5]
        china_items = summaries[5:]

    def _render_group(items: List[dict], heading: str, start_num: int) -> None:
        if not items:
            return
        lines.append(heading)
        lines.append("")
        for i, s in enumerate(items, start_num):
            lines.append(f"### {i}. {s.get('topic', '')}")
            lines.append("")
            oneliner = s.get('one_liner', '')
            if oneliner:
                lines.append(f"**要点**：{oneliner}")
                lines.append("")
            summary_text = s.get('summary', '')
            if summary_text:
                lines.append(summary_text)
                lines.append("")
            lines.append("**来源**：")
            src_names = s.get('sources', [])
            src_links = s.get('article_links', [])
            for j, name in enumerate(src_names):
                link = src_links[j] if j < len(src_links) else ""
                if link:
                    lines.append(f"- {name}：{link}")
                else:
                    lines.append(f"- {name}")
            lines.append("")
            if i < start_num + len(items) - 1:
                lines.append("---")
                lines.append("")

    _render_group(global_items, "## 🌍 全球要闻", 1)
    _render_group(china_items, "## 🇨🇳 中国相关", len(global_items) + 1)
    return "\n".join(lines)


# ── 社交平台总结辅助 ───────────────────────────────────────

_SOCIAL_PLATFORM_HEADERS = {
    "X": "【X 平台】",
    "Facebook": "【脸书平台】",
    "Google News": "【Google News】",
}
_SOCIAL_ORDER = ("X", "Facebook", "Google News")

# 官方大媒体关键词（作者名或 handle 含这些词则视为官方媒体账号）。
# 它们通常把自家官网新闻转发到社交平台，与新闻正文重复，挑选时应排除。
_OFFICIAL_BIG_MEDIA_KEYWORDS = [
    "bbc", "cnn", "nyt", "new york times", "wsj", "wall street journal",
    "reuters", "associated press", "ap news", "the economist", "economist",
    "financial times", "the guardian", "guardian", "south china morning post",
    "scmp", "bloomberg", "al jazeera", "npr", "washington post", "washpost",
    "the telegraph", "telegraph", "the times", "time magazine", "forbes",
    "axios", "politico", "cnbc", "nbc news", "cbs news", "abc news",
    "sky news", "dw news", "france 24", "the hill", "business insider",
]


def _is_official_big_media(item: dict) -> bool:
    """判断贴文作者是否为官方大媒体账号。

    关键词分两类：
    - 2 字母短词（ft/ap/dw 等）要求全词匹配，避免误伤 'ftm' 之类 handle；
    - 其余关键词要求出现在某个词的词首（\b前缀），如 BBCWorld / BBCIndonesia / CNN 均命中。
    """
    name = ((item.get("author_name") or "") + " " + (item.get("author_handle") or "")).lower()
    for kw in _OFFICIAL_BIG_MEDIA_KEYWORDS:
        esc = re.escape(kw)
        if len(kw) <= 2:
            if re.search(rf"\b{esc}\b", name):
                return True
        else:
            if re.search(rf"\b{esc}", name):
                return True
    return False


def _social_candidate_rank(item: dict) -> tuple:
    """候选排序：官方大媒体压到最低；其余按互动量 + 小账号互动率加权降序。

    返回 (是否官方大媒体, 得分)：官方大媒体永远排在最后，
    让个人/小众媒体的高互动贴文优先进入 LLM 候选池。
    """
    if _is_official_big_media(item):
        return (0, 0.0)
    engagement = item.get("engagement") or 0
    followers = item.get("followers") or 0
    rate = (engagement / followers) if followers and followers > 0 else 0.0
    # 互动量为主，高互动率的小账号额外加权，让『小众声音』也有机会被看到
    score = engagement + 0.5 * engagement * rate
    return (1, score)


def _count_cjk(text: str) -> int:
    """统计中文字符数量（用于控制 302.ai 中文长度限制）。"""
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def _format_followers(followers: Any) -> str:
    """把关注者数字格式化为 '219 万关注者'；无数据显示空串。"""
    if not followers:
        return ""
    try:
        n = int(followers)
    except (TypeError, ValueError):
        return ""
    if n >= 10000:
        v = f"{n / 10000:.1f}".rstrip("0").rstrip(".")
        return f"{v} 万关注者"
    return f"{n} 关注者"


def _social_platform_desc(platform: str, post: dict) -> str:
    """本地降级用的平台动作描述。"""
    media = post.get("media", "none")
    media_suffix = {"video": "配视频", "image": "配图"}.get(media, "")
    if platform == "X":
        return f"官方推特主页发布帖文{media_suffix}"
    if platform == "Facebook":
        return f"在脸书发布帖文{media_suffix}" if media_suffix else "在脸书发布帖文称"
    return "报道称"


def _fallback_social_items(candidates: List[dict], platform: str, limit: int = 3) -> List[dict]:
    """无 LLM 时的本地降级挑选：按关注者/互动量取前 limit 条。"""
    items = []
    for c in candidates[:limit]:
        author = (c.get("author_name") or c.get("author_handle") or "").strip()
        handle = (c.get("author_handle") or "").strip()
        content = (c.get("text") or "").strip()
        url = (c.get("url") or "").strip()
        items.append({
            "author": author,
            "handle": handle,
            "followers_display": _format_followers(c.get("followers")),
            "platform_desc": _social_platform_desc(platform, c),
            "content": content[:300],
            "url": url,
            "article_urls": c.get("article_urls") or [],
        })
    return items


def _render_social_markdown(platform_data: dict) -> str:
    """把社交平台挑选结果渲染为报告板块 Markdown。

    每条贴文：标题行（作者@handle 关注者 平台动作：内容摘要）后，
    逐行直列原始链接——先列原贴 URL，再列贴文内引用的文章 URL（如有），不隐藏。
    """
    lines = ["## 📱 海外社交平台", "",
             "> 基于 Apify 抓取的最近 24 小时关键字数据（关键字：China）。", ""]
    for platform in _SOCIAL_ORDER:
        items = platform_data.get(platform) or []
        if not items:
            continue
        lines.append(_SOCIAL_PLATFORM_HEADERS[platform])
        lines.append("")
        for i, it in enumerate(items, 1):
            content = (it.get("content") or "").strip()
            if platform == "Google News":
                lines.append(f"{i}、{content}")
            else:
                author = (it.get("author") or "").strip()
                handle = (it.get("handle") or "").strip()
                followers = (it.get("followers_display") or "").strip()
                desc = (it.get("platform_desc") or "").strip()
                head = f"{i}、{author}"
                if handle:
                    head += f"@{handle}"
                if followers:
                    head += f"（{followers}）"
                if desc:
                    head += f"{desc}"
                lines.append(f"{head}：{content}")

            # 原贴 URL（Google News 即文章 URL）
            url = (it.get("url") or "").strip()
            if url:
                lines.append(url)
            # 贴文内引用的文章链接，全部直列
            for au in (it.get("article_urls") or []):
                au = str(au).strip()
                if au and au != url:
                    lines.append(au)
            lines.append("")
    return "\n".join(lines)


# ── 信源网站首页截图 ───────────────────────────────────────

def _render_screenshots_markdown(shots: Dict[str, Path]) -> str:
    """把信源网站首页截图渲染为报告板块 Markdown（图片用绝对路径）。

    pypandoc.convert_text 解析相对图片路径是相对进程 CWD 而非 md 文件目录，
    易失效，故图片一律写绝对路径，pandoc 无论从哪运行都能找到并嵌入 Word。
    """
    if not shots:
        return ""
    lines = ["## 📸 信源网站首页截图", "",
             "> 各信源网站首页首屏截图（抓取时间见报告日期）。", ""]
    for url, path in shots.items():
        domain = urlparse(url).netloc.replace("www.", "") or url
        abs_path = Path(path).resolve()
        lines.append(f"### {domain}")
        lines.append("")
        lines.append(f"![{domain}]({abs_path})")
        lines.append("")
    return "\n".join(lines)


# ── 付费墙检测 / 来源推断（--fix-errors 用）─────────────────

_PAYWALL_MARKERS = [
    "subscribe to unlock", "subscribe to read", "sign in to read",
    "sign in to continue", "log in to continue", "unlock this article",
    "subscriber exclusive", "subscribe now", "register to read",
    "you've reached your article limit", "unlock for free",
    "try unlimited access",
]


def _is_paywall_stub(content: str) -> bool:
    """判断直接抓取的结果是否为付费墙占位页（导航+订阅提示，无正文）。"""
    if not content:
        return True
    low = content.lower()
    hits = sum(1 for m in _PAYWALL_MARKERS if m in low)
    effective = len(re.sub(r"\s+", "", content))
    # 命中多个订阅词，或有效文本太少（纯导航），视为付费墙占位页
    if hits >= 2:
        return True
    if effective < 300:
        return True
    return False


def _guess_source(url: str) -> str:
    """从 URL 提取站点名（如 https://www.ft.com/... → ft.com）。"""
    m = re.search(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else ""


# ═══════════════════════════════════════════════════════════
# MediaMonitorAgent
# ═══════════════════════════════════════════════════════════

class MediaMonitorPipeline:
    """媒体监控流水线：LLM 驱动的五阶段流水线编排。

    Phase 1: 首页抓取（委托 MediaMonitorToolkit）
    Phase 2: 话题挑选（LLM）
    Phase 3: 详情总结（LLM + BrightData 正文抓取）
    Phase 4: 社交平台抓取 + 贴文总结（委托 ApifyScraperToolkit + LLM）
    Phase 5: 简报渲染（LLM / 本地降级，含海外社交平台板块）

    Args:
        brightdata_key:  BrightData API 密钥（默认从 config 读取）
        brightdata_zone:  BrightData zone 名称（默认从 config 读取）
        llm_api_key:      LLM API 密钥（默认从 MEDIA_MONITOR_LLM_API_KEY 环境变量读取）
        llm_base_url:     LLM API 地址（默认 https://api.302ai.com）
        llm_model:        模型 ID（默认 deepseek-v4-pro）
        apify_api_key:    Apify API 密钥（默认从 config 读取，env: APIFY_API_KEY）
        output_dir:       输出目录（默认项目根/output/media_monitor）
    """

    def __init__(
        self,
        brightdata_key: Optional[str] = None,
        brightdata_zone: Optional[str] = None,
        llm_api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        llm_model: Optional[str] = None,
        llm_max_tokens: Optional[int] = None,
        apify_api_key: Optional[str] = None,
        output_dir: Optional[str] = None,
    ):
        self.brightdata_key = brightdata_key or bright_data_settings.api_key
        self.brightdata_zone = brightdata_zone or bright_data_settings.zone
        self.llm_api_key = llm_api_key or media_monitor_settings.llm_api_key or os.getenv("302_API_KEY", "")
        self.llm_base_url = llm_base_url or media_monitor_settings.llm_base_url
        self.llm_model = llm_model or media_monitor_settings.llm_model
        self.llm_max_tokens = llm_max_tokens or media_monitor_settings.llm_max_tokens
        self.apify_key = apify_api_key or apify_settings.api_key
        self.output_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
        # 飞书推送（无凭证时 send_* 自行返回错误、不抛出）
        self.feishu = FeishuBot()
        # 邮件推送（无凭证时 send_* 自行返回错误、不抛出）
        self.email = EmailSender()

    # ── LLM 辅助 ──────────────────────────────────────────

    def _build_model(self) -> OpenAILike:
        """构建 LLM 模型实例。

        注意 max_tokens：q-claude-sonnet-5 等「推理/thinking 模型」会先消耗输出
        token 做思考，再输出正式内容。网关默认上限（约 4096）会被思考吃满，导致
        finish_reason=length 且 content 为空的「空返回」。因此显式调大输出上限。
        max_tokens（传统 completions）与 max_completion_tokens（推理模型）都设上，
        兼容不同网关。
        """
        return OpenAILike(
            id=self.llm_model,
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
            max_tokens=self.llm_max_tokens,
            max_completion_tokens=self.llm_max_tokens,
        )

    # ── Phase 1: 首页抓取（委托 Toolkit）────────────────────

    def scrape_all_homepages(
        self,
        output_dir: Optional[str] = None,
        retries: int = 2,
        delay: float = 1.0,
    ) -> Dict[str, Any]:
        """抓取 23 家国际主流媒体首页首屏。

        Args:
            output_dir: 输出目录
            retries:    失败重试次数
            delay:      请求间隔秒数

        Returns:
            同 MediaMonitorToolkit.scrape_all_homepages 的返回值
        """
        base = Path(output_dir) if output_dir else self.output_dir
        toolkit = MediaMonitorToolkit(
            brightdata_key=self.brightdata_key,
            brightdata_zone=self.brightdata_zone,
            output_dir=str(base),
        )

        raw_dir = base / "raw"
        results = toolkit._scrape_all_impl(raw_dir, retries=retries, delay=delay)

        # 保存清单
        manifest = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "results": results,
        }
        manifest_file = base / "scrape_manifest.json"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        ok_count = sum(1 for r in results.values() if r["status"] == "ok")
        return {
            "ok": ok_count,
            "total": len(MEDIA_SOURCES),
            "results": results,
            "raw_dir": str(raw_dir),
            "manifest_file": str(manifest_file),
        }

    # ── Phase 2: 话题挑选 ──────────────────────────────────

    # BBC Chinese 是中文站点，中文内容约 1155 字符即触发 302.ai 限制，
    # 需要更短的截断。其他源用 TRUNCATE_LIMIT。
    _PER_SOURCE_LIMITS: Dict[str, int] = {"BBC Chinese": 1000}

    # --fix-errors 直接抓正文时，单篇正文截断长度 / 累计上限
    _DIRECT_BODY_LIMIT = 8000
    _DIRECT_TOTAL_LIMIT = 40000

    def _build_topic_selection_prompt(self, now: datetime, scrape_results: dict) -> str:
        """构建话题挑选 prompt。

        路线 A（文档顺序优先）：内容按网页从上到下的真实顺序排列，并给每条 markdown
        链接注入 [第N条] 序号（N 越小越靠版面顶部），让 LLM 能以「出现越靠前 =
        版面越重要」作为选题权重——近似还原「首屏 / 头版头条优先」的版面位置优先级。
        编号在按字符截断之前完成，保证靠前的头条不会因截断而丢失序号。
        """
        parts: List[str] = []
        for src in MEDIA_SOURCES:
            name = src["name"]
            limit = self._PER_SOURCE_LIMITS.get(name, TRUNCATE_LIMIT)
            result = scrape_results.get(name, {})
            if result.get("status") == "ok":
                file_path = result.get("file", "")
                try:
                    content = Path(file_path).read_text(encoding="utf-8")
                    content = _number_headline_links(content)  # 先按出现顺序编号
                    if len(content) > limit:
                        content = content[:limit] + f"\n\n... (已截断，共 {len(content)} 字符)"
                except Exception:
                    content = "（文件读取失败）"
            else:
                content = f"（抓取失败: {result.get('error', '未知')}）"
            parts.append(
                f"### [{src['category']}] {name}  |  URL: {src['url']}\n"
                f"版面布局（level 越小版面权重越高）: {src['layout']}\n"
                f"（以下内容按网页首页从上到下的顺序排列，[第N条] 中 N 越小越靠版面顶部）\n\n"
                f"{content}"
            )
        raw_content = "\n\n---\n\n".join(parts)

        today = now.strftime('%Y-%m-%d')
        yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        return f"""\
现在时间是北京时间 {now.strftime('%Y-%m-%d %H:%M:%S')}。

以下是 23 家国际主流媒体首页首屏的 Markdown 内容（已预先抓取）。
每家内容均按网页从上到下的真实版面顺序排列，标题前的 [第N条] 表示其在该站首页
出现的先后（N 越小越靠版面顶部、越可能是头版头条）；每家还附了「版面布局」说明。

{raw_content}

---

请根据你的指令，从以上内容中提取新闻，挑选全球话题 + 中国相关话题，输出 JSON。

【版面位置优先级】优先挑选各站出现越靠前（[第N条] 的 N 越小）、布局 level 越小的新闻，
它们是该站编辑判定的头版头条；在时效达标的前提下，版面越靠前 / level 越小的越应入选。

【时效性硬性要求】只挑选发布日期为 {today}（今天）或 {yesterday}（昨天）的新闻，
{yesterday} 之前的新闻一律舍弃、绝不入选。全球与中国相关两组各至多 5 个话题；
满足时效要求的话题不足 5 个时，宁可少于 5 个，也不要用更早的旧闻凑数。"""

    def select_topics(
        self,
        scrape_manifest_file: Optional[str] = None,
        output_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """使用 LLM 从已抓取的首页内容中挑选 10 个最重要话题。

        Args:
            scrape_manifest_file: 抓取清单 JSON 文件路径
            output_file:          话题输出路径

        Returns:
            {"global_topics": [...], "china_topics": [...], "output_file": "..."}
        """
        base = self.output_dir
        manifest_path = Path(scrape_manifest_file) if scrape_manifest_file else base / "scrape_manifest.json"
        topics_path = Path(output_file) if output_file else base / "topics.json"

        if not manifest_path.exists():
            return {"error": f"抓取清单不存在: {manifest_path}，请先运行 scrape_all_homepages"}

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        scrape_results = manifest.get("results", manifest)
        now = datetime.now(BEIJING_TZ)

        print("📋 Phase 2/4: 话题挑选 — 从 23 家首页内容中挑选全球5个+中国5个话题...")
        print()

        agent = Agent(
            name="TopicSelector",
            model=self._build_model(),
            instructions=TOPIC_SELECTION_INSTRUCTIONS,
            add_datetime_to_context=True,
        )
        prompt = self._build_topic_selection_prompt(now, scrape_results)

        # 该网关对大 prompt 偶发「空返回」或「返回被截断成不合法 JSON」，
        # 两种情况都重试整轮（调用 + 解析），而非只重试空返回。
        topics = None
        last_err = None
        last_text = ""
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = agent.run(prompt)
            except Exception as e:
                last_err = e
                print(f"   ⚠ LLM 调用异常（第 {attempt}/{max_attempts} 次）: {e}")
                if attempt < max_attempts:
                    time.sleep(3 * attempt)
                continue
            last_text = _response_text(response) or ""
            try:
                topics = _parse_json_response(response)
                break
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                last_err = e
                reason = "空返回" if not last_text else f"解析失败: {e}"
                print(f"   ⚠ 话题挑选{reason}（第 {attempt}/{max_attempts} 次），重试中...")
                if attempt < max_attempts:
                    time.sleep(3 * attempt)
        if topics is None:
            return {"error": f"话题挑选 JSON 解析失败: {last_err}", "raw": last_text[:500]}

        global_topics = topics.get("global_topics", [])
        china_topics = topics.get("china_topics", [])
        all_topics = global_topics + china_topics
        if not all_topics and "topics" in topics:
            all_topics = topics["topics"]
            global_topics = all_topics[:5]
            china_topics = all_topics[5:10]
            topics = {"global_topics": global_topics, "china_topics": china_topics}

        topics_path.parent.mkdir(parents=True, exist_ok=True)
        topics_path.write_text(json.dumps(topics, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"   ✓ 全球话题 {len(global_topics)} 个，中国话题 {len(china_topics)} 个")
        for i, t in enumerate(global_topics, 1):
            print(f"      🌍 {i}. {t['topic']}（{len(t.get('articles', []))} 篇报道）")
        for i, t in enumerate(china_topics, 1):
            print(f"      🇨🇳 {i}. {t['topic']}（{len(t.get('articles', []))} 篇报道）")
        print(f"   📋 话题清单已保存: {topics_path}")
        print()

        return {
            "global_topics": global_topics,
            "china_topics": china_topics,
            "output_file": str(topics_path),
        }

    # ── Phase 3: 详情总结 ──────────────────────────────────

    def _fallback_summary(
        self,
        topic_name: str,
        articles: List[dict],
        reason: Optional[str] = None,
        raw_text: str = "",
    ) -> dict:
        """生成降级摘要。"""
        sources = [a["media"] for a in articles]
        links = [a["url"] for a in articles]

        summary_content = ""
        one_liner = ""

        if raw_text and not _is_api_error(raw_text) and not _is_refusal(raw_text):
            extracted_summary = _extract_json_field(raw_text, "summary")
            extracted_oneliner = _extract_json_field(raw_text, "one_liner")

            if extracted_summary:
                summary_content = extracted_summary
            else:
                cleaned = re.sub(r'^```\w*\s*', '', raw_text.strip())
                cleaned = re.sub(r'\s*```$', '', cleaned)
                content_lines = [
                    l for l in cleaned.split('\n')
                    if not l.strip().startswith('{')
                    and not l.strip().startswith('}')
                    and not l.strip().startswith('"topic"')
                    and not l.strip().startswith('"one_liner"')
                    and not l.strip().startswith('"summary"')
                    and not l.strip().startswith('"sources"')
                    and not l.strip().startswith('"article_links"')
                ]
                summary_content = '\n'.join(content_lines).strip()

            one_liner = extracted_oneliner[:40] if extracted_oneliner else f"（{', '.join(sources[:3])} 报道）{topic_name}"[:40]

        if not summary_content:
            one_liner = f"（API 不可用）{topic_name}"[:40]
            titles = "; ".join(a.get("title", "")[:60] for a in articles[:3])
            summary_content = f"话题: {topic_name}。涉及报道: {titles}" if titles else f"话题: {topic_name}"

        if reason:
            summary_content = f"[{reason}] {summary_content}"

        return {
            "topic": topic_name,
            "one_liner": one_liner,
            "summary": summary_content,
            "sources": sources,
            "article_links": links,
        }

    def _summarize_one_topic_impl(self, topic: dict, idx: int, total: int) -> dict:
        """对单个话题做详情总结（带重试+降级）。"""
        topic_name = topic["topic"]
        articles = topic.get("articles", [])

        if not articles:
            print(f"   [{idx}/{total}] {topic_name} — 无文章，跳过")
            return {"topic": topic_name, "one_liner": "", "summary": "", "sources": [], "article_links": []}

        article_urls = "\n".join(
            f"- [{a['media']}] {a['title']} → {a['url']}" for a in articles
        )
        print(f"   [{idx}/{total}] {topic_name}（{len(articles)} 篇文章）", end="", flush=True)

        max_retries = 2
        response = None
        for attempt in range(1 + max_retries):
            if attempt > 0:
                print(f"\n      ↩ 重试 {attempt}/{max_retries}...", end="", flush=True)

            try:
                from agno.tools.brightdata import BrightDataTools
                brightdata_tools = BrightDataTools(
                    api_key=self.brightdata_key,
                    web_unlocker_zone=self.brightdata_zone,
                )

                agent = Agent(
                    name="TopicSummarizer",
                    model=self._build_model(),
                    tools=[brightdata_tools],
                    instructions=TOPIC_SUMMARY_INSTRUCTIONS,
                    add_datetime_to_context=True,
                )

                prompt = f"""\
话题: {topic_name}

该话题包含以下报道（请用 scrape_as_markdown 逐篇抓取正文后分析）：

{article_urls}

请抓取各篇文章正文，阅读后输出综合摘要 JSON。"""

                response = agent.run(prompt)
                raw_text = response.content if hasattr(response, "content") else str(response)

                if _is_api_error(raw_text):
                    if attempt < max_retries:
                        time.sleep(3)
                        continue
                    print(f" ✗ 持续 API 报错，使用仅标题降级")
                    return self._fallback_summary(topic_name, articles, reason="API 持续报错")

                summary = _parse_json_response(response)
                summary.setdefault("topic", topic_name)
                # 解析成功但内容是拒答话术（如「我无法给到相关内容」）时，视为无效摘要，重试
                summary_blob = (summary.get("summary") or "") + "|" + (summary.get("one_liner") or "")
                if _is_refusal(summary_blob) or _is_refusal(raw_text):
                    if attempt < max_retries:
                        print(f"\n      ↩ LLM 拒答，重试...", end="", flush=True)
                        time.sleep(3)
                        continue
                    print(f" ✗ LLM 持续拒答，使用降级")
                    return self._fallback_summary(topic_name, articles, reason="LLM 拒答")
                print(f" ✓ {summary.get('one_liner', '')[:50]}")
                return summary

            except (json.JSONDecodeError, KeyError, ValueError) as e:
                raw_text = ""
                if response and hasattr(response, "content"):
                    raw_text = response.content if hasattr(response, "content") else str(response)
                if _is_api_error(raw_text):
                    if attempt < max_retries:
                        time.sleep(3)
                        continue
                    print(f" ✗ 持续报错，使用降级")
                    return self._fallback_summary(topic_name, articles, reason="API 报错")
                text = raw_text.strip()
                print(f" ⚠ JSON 格式错误，使用 LLM 原始文本降级")
                return self._fallback_summary(topic_name, articles, reason=None, raw_text=text)

        print(f" ✗ 全部重试耗尽")
        return self._fallback_summary(topic_name, articles, reason="重试耗尽")

    # ── API 报错补全（--fix-errors）────────────────────────

    def _summarize_one_topic_direct(self, topic: dict, idx: int, total: int) -> dict:
        """直接抓正文 + 无工具 LLM 补全单个话题摘要。

        绕开 BrightDataTools 的 Agent 工具循环（付费墙文章会触发中文 Google 搜索回退，
        导致 302.ai 中文超长报错）。改为用 BrightData API 直接抓每篇正文，
        把干净的正文塞进 prompt 交给无工具 Agent 总结。
        """
        topic_name = topic["topic"]
        articles = topic.get("articles", [])
        if not articles:
            return {"topic": topic_name, "one_liner": "", "summary": "", "sources": [], "article_links": []}

        print(f"   [{idx}/{total}] {topic_name} — 直接抓正文补全 ...", end="", flush=True)

        toolkit = MediaMonitorToolkit(
            brightdata_key=self.brightdata_key,
            brightdata_zone=self.brightdata_zone,
        )
        scraped: List[str] = []
        total_chars = 0
        readable = 0
        for a in articles:
            url = (a.get("url") or "").strip()
            if not url:
                continue
            status, content = toolkit._scrape_one_url(url)
            if status == "ok" and content and not _is_paywall_stub(content):
                body = content[: self._DIRECT_BODY_LIMIT]
                total_chars += len(body)
                scraped.append(
                    f"### [{a.get('media', '')}] {a.get('title', '')}\n{url}\n\n{body}"
                )
                readable += 1
                if total_chars >= self._DIRECT_TOTAL_LIMIT:
                    break
            else:
                scraped.append(f"### [{a.get('media', '')}] {a.get('title', '')}\n{url}\n\n（正文抓取失败或付费墙不可读）")
            time.sleep(0.3)

        # 所有文章都不可读（常见于付费墙）时，用英文公开搜索片段兜底，
        # 避免把付费墙占位页喂给 LLM 导致空摘要
        if readable == 0:
            query = ""
            for a in articles:
                t = (a.get("title") or "").strip()
                if re.search(r"[A-Za-z]", t):
                    query = t
                    break
            if not query:
                query = topic_name
            search_hits = self._web_search_snippets(query)
            if search_hits:
                for s in search_hits[:5]:
                    scraped.append(
                        f"### [{s.get('source') or s.get('url', '')}] {s.get('title', '')}\n{s.get('url', '')}\n\n{s.get('snippet', '')}"
                    )
                print(f"（直连不可读，改用搜索 {len(search_hits[:5])} 条公开报道）", end="", flush=True)

        if not scraped:
            print(" ✗ 无任何可用内容")
            return self._fallback_summary(topic_name, articles, reason="正文与搜索均无可用内容")

        prompt = f"""\
现在时间是北京时间 {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}。

话题: {topic_name}

以下是各篇报道的正文或公开搜索摘要（已预先获取，无需调用抓取工具）：

{chr(10).join(scraped)}

请阅读以上内容，撰写一句话要点与综合摘要，输出 JSON。"""

        response = None
        max_retries = 2
        for attempt in range(1 + max_retries):
            if attempt > 0:
                print(f"\n      ↩ 重试 {attempt}/{max_retries}...", end="", flush=True)
            try:
                agent = Agent(
                    name="TopicSummarizerDirect",
                    model=self._build_model(),
                    instructions=DIRECT_SUMMARY_INSTRUCTIONS,
                    add_datetime_to_context=True,
                )
                response = agent.run(prompt)
                raw_text = response.content if hasattr(response, "content") else str(response)
                if _is_api_error(raw_text):
                    if attempt < max_retries:
                        time.sleep(3)
                        continue
                    print(f" ✗ 持续 API 报错")
                    return self._fallback_summary(topic_name, articles, reason="API 持续报错")
                summary = _parse_json_response(response)
                summary.setdefault("topic", topic_name)
                # 解析成功但内容是拒答话术（如「我无法给到相关内容」）时，视为无效摘要，重试
                summary_blob = (summary.get("summary") or "") + "|" + (summary.get("one_liner") or "")
                if _is_refusal(summary_blob) or _is_refusal(raw_text):
                    if attempt < max_retries:
                        print(f"\n      ↩ LLM 拒答，重试...", end="", flush=True)
                        time.sleep(3)
                        continue
                    print(f" ✗ LLM 持续拒答，使用降级")
                    return self._fallback_summary(topic_name, articles, reason="LLM 拒答")
                print(f" ✓ {summary.get('one_liner', '')[:50]}")
                return summary
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                raw_text = ""
                if response and hasattr(response, "content"):
                    raw_text = response.content
                if _is_api_error(raw_text):
                    if attempt < max_retries:
                        time.sleep(3)
                        continue
                    print(f" ✗ 持续报错，使用降级")
                    return self._fallback_summary(topic_name, articles, reason="API 报错")
                print(f" ⚠ JSON 格式错误，使用 LLM 原始文本降级 ({e})")
                return self._fallback_summary(topic_name, articles, reason=None, raw_text=raw_text.strip())

        print(f" ✗ 全部重试耗尽")
        return self._fallback_summary(topic_name, articles, reason="重试耗尽")

    def _web_search_snippets(self, query: str, max_results: int = 6) -> List[dict]:
        """用 DuckDuckGo HTML 搜索英文公开报道片段，作为付费墙文章的后备正文。

        只取标题 + 摘要 + 真实 URL，返回结构化列表：
        [{"title", "url", "snippet", "source"}]
        搜索不可用或失败时返回空列表。
        """
        from urllib.parse import parse_qs, urlparse

        import html as _html

        try:
            r = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"},
                timeout=30,
            )
        except requests.RequestException:
            return []
        if r.status_code != 200:
            return []

        def _clean(s: str) -> str:
            return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()

        title_blocks = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.DOTALL)
        snip_blocks = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)

        results: List[dict] = []
        for i, (href, title) in enumerate(title_blocks[:max_results]):
            url = href
            qs = urlparse(href)
            if "duckduckgo.com" in (qs.netloc or ""):
                url = parse_qs(qs.query).get("uddg", [href])[0]
            snippet = _clean(snip_blocks[i]) if i < len(snip_blocks) else ""
            if not _clean(title) and not snippet:
                continue
            results.append({
                "title": _clean(title),
                "url": url,
                "snippet": snippet,
                "source": _guess_source(url),
            })
        return results

    def repair_failed_summaries(self, output_file: Optional[str] = None) -> Dict[str, Any]:
        """找出摘要中 API 报错的话题，用直连方式补全，写回 summaries.json。

        Args:
            output_file: 摘要输出路径（默认 self.output_dir/summaries.json）

        Returns:
            {"summaries": [...], "repaired": [话题序号...], "output_file": "..."}
        """
        base = self.output_dir
        summaries_path = Path(output_file) if output_file else base / "summaries.json"
        topics_path = base / "topics.json"

        if not summaries_path.exists():
            return {"error": f"摘要文件不存在: {summaries_path}"}
        summaries_data = json.loads(summaries_path.read_text(encoding="utf-8"))
        summaries = summaries_data.get("summaries", summaries_data)
        if isinstance(summaries, dict) and "summaries" in summaries:
            summaries = summaries["summaries"]

        # 定位报错条目（与 _fallback_summary 产出的标记对应）
        # 同时兼容 LLM 直连返回的英文报错（如 302.ai 上游 "Connection error."）
        error_markers = (
            "API 不可用", "API 报错", "API 持续报错", "重试耗尽", "分析失败", "正文抓取失败",
            "LLM 拒答",
            "connection error", "connectionerror", "connection refused",
            "connection reset", "connection timed out",
        )

        def _has_error_marker(s: dict) -> bool:
            haystack = ((s.get("one_liner") or "") + "|" + (s.get("summary") or "")).lower()
            if any(m.lower() in haystack for m in error_markers):
                return True
            # 拒答话术（如「我无法给到相关内容」）即便被当作正常摘要写入，也当作待补全
            return _is_refusal(haystack)

        failed_idx = [i for i, s in enumerate(summaries) if _has_error_marker(s)]
        if not failed_idx:
            print("✅ 未发现 API 报错话题，无需补全")
            return {"summaries": summaries, "repaired": [], "output_file": str(summaries_path)}

        print(f"🔧 发现 {len(failed_idx)} 个 API 报错话题，开始补全...")

        # 加载 topics.json 拿原始文章链接（序号对齐 summaries）
        topics: List[dict] = []
        if topics_path.exists():
            td = json.loads(topics_path.read_text(encoding="utf-8"))
            topics = td.get("global_topics", []) + td.get("china_topics", [])

        repaired: List[int] = []
        for i in failed_idx:
            s = summaries[i]
            print(f"\n🔧 话题 {i + 1}: {s.get('topic', '')}")
            topic = topics[i] if i < len(topics) else {}
            articles = topic.get("articles", [])
            # 若 topics.json 取不到文章，退回用 sources / article_links 组装
            if not articles:
                articles = [
                    {"title": s.get("topic", ""), "url": u, "media": m}
                    for m, u in zip(s.get("sources", []), s.get("article_links", []))
                    if u
                ]
            if not articles:
                print(f"   ⚠ 无文章链接，跳过")
                continue

            new_summary = self._summarize_one_topic_direct(
                {"topic": s.get("topic", ""), "articles": articles}, i + 1, len(summaries)
            )
            if new_summary.get("one_liner") and not _has_error_marker(new_summary):
                new_summary.setdefault("group", s.get("group"))
                summaries[i] = new_summary
                repaired.append(i + 1)
                print(f"   ✓ 补全成功")
            else:
                print(f"   ✗ 补全仍失败，保留原摘要")
            time.sleep(0.5)

        summaries_path.parent.mkdir(parents=True, exist_ok=True)
        summaries_path.write_text(
            json.dumps({"summaries": summaries}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n   ✓ 已写回 {summaries_path}，共补全 {len(repaired)} 条")
        return {"summaries": summaries, "repaired": repaired, "output_file": str(summaries_path)}

    def summarize_topic(self, topic: Dict[str, Any]) -> Dict[str, Any]:
        """对单个话题做深度摘要。

        Args:
            topic: {"topic": "话题名", "articles": [{"title": "...", "url": "...", "media": "..."}]}

        Returns:
            {"topic": "...", "one_liner": "...", "summary": "...", "sources": [...], "article_links": [...]}
        """
        return self._summarize_one_topic_impl(topic, 1, 1)

    def summarize_all_topics(
        self,
        topics_file: Optional[str] = None,
        output_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """对所有已挑选的话题逐篇抓取正文并撰写综合摘要。

        Args:
            topics_file: 话题 JSON 文件路径
            output_file: 摘要输出路径

        Returns:
            {"summaries": [...], "output_file": "..."}
        """
        base = self.output_dir
        topics_path = Path(topics_file) if topics_file else base / "topics.json"
        summaries_path = Path(output_file) if output_file else base / "summaries.json"

        if not topics_path.exists():
            return {"error": f"话题文件不存在: {topics_path}，请先运行 select_topics"}

        topics_data = json.loads(topics_path.read_text(encoding="utf-8"))
        global_topics = topics_data.get("global_topics", [])
        china_topics = topics_data.get("china_topics", [])
        topic_list = global_topics + china_topics
        if not topic_list:
            topic_list = topics_data.get("topics", [])

        total = len(topic_list)
        n_global = len(global_topics)
        print(f"📝 Phase 3/4: 详情总结 — {total} 个话题，逐篇直接抓取选题网站正文并撰写摘要（单次 LLM）...")
        print()

        summaries: List[dict] = []
        for i, topic in enumerate(topic_list, 1):
            try:
                # 直接抓选题网站正文 + 单次无工具 LLM 总结（不走 Agent 工具循环，大幅提速）；
                # 仅当整组文章都不可读（付费墙）时才在内部用搜索兜底
                s = self._summarize_one_topic_direct(topic, i, total)
                if n_global > 0 and i <= n_global:
                    s["group"] = "global"
                elif n_global > 0:
                    s["group"] = "china"
                summaries.append(s)
            except Exception as e:
                print(f"      ❌ 话题 {i} 失败: {e}")
                traceback.print_exc()
                summaries.append({
                    "topic": topic.get("topic", f"话题{i}"),
                    "one_liner": f"（分析失败: {e}）",
                    "summary": "",
                    "sources": [a["media"] for a in topic.get("articles", [])],
                    "article_links": [a["url"] for a in topic.get("articles", [])],
                })

            if i < total:
                time.sleep(0.5)

        result = {"summaries": summaries}
        summaries_path.parent.mkdir(parents=True, exist_ok=True)
        summaries_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n   ✓ {len(summaries)} 个话题总结完毕 → {summaries_path}")
        print()

        return {"summaries": summaries, "output_file": str(summaries_path)}

    # ── Phase 4: 社交平台抓取 + 贴文总结 ────────────────────

    def scrape_social_platforms(
        self,
        output_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """抓取 Facebook / X / Google News 关键字 China 的最近一天数据。

        委托 ApifyScraperToolkit，结果保存到 output_dir/social/*.json。
        未设置 APIFY_API_KEY 时返回空结果，不阻塞流水线。

        Args:
            output_dir: 输出目录（默认 self.output_dir）

        Returns:
            ApifyScraperToolkit.scrape_all_social 的返回值，或 {"ok": 0, ...}
        """
        base = Path(output_dir) if output_dir else self.output_dir
        if not self.apify_key:
            print("⚠ 未设置 APIFY_API_KEY，跳过社交平台抓取")
            return {"ok": 0, "total": 3, "results": {}, "dir": str(base / "social")}
        toolkit = ApifyScraperToolkit(apify_api_key=self.apify_key, output_dir=str(base / "social"))
        print(f"📡 Phase 4/5: 社交平台抓取（Facebook / X / Google News，关键字 China，最近 1 天）")
        return toolkit._scrape_all_social_impl()

    def _fit_candidates_for_llm(
        self,
        candidates: List[dict],
        budget: int = 400,
        text_len: int = 500,
    ) -> List[dict]:
        """把候选贴文压缩到 302.ai 可安全处理的中文长度预算内，并转成 prompt 需要的字段。

        Args:
            candidates: 按影响力排好序的候选贴文
            budget:     候选文本累计中文长度预算（字符）
            text_len:   每条贴文文本截断长度

        Returns:
            只含 author / handle / followers_display / text / url / media / article_urls
            的候选列表
        """
        kept: List[dict] = []
        total = 0
        for c in candidates:
            t = (c.get("text") or "").strip()[:text_len]
            cjk = _count_cjk(t)
            if kept and total + cjk > budget:
                break
            kept.append({
                "author": (c.get("author_name") or "").strip(),
                "handle": (c.get("author_handle") or "").strip(),
                "followers_display": _format_followers(c.get("followers")),
                "text": t,
                "url": (c.get("url") or "").strip(),
                "media": c.get("media", "none"),
                "article_urls": c.get("article_urls") or [],
            })
            total += cjk
        return kept

    def _summarize_one_platform(self, platform: str, candidates: List[dict]) -> List[dict]:
        """对单个平台的候选贴文调用 LLM 挑选 2-3 条并写摘要，失败时本地降级。"""
        # 记录 url → 原始候选，用于回填 article_urls（LLM 可能省略该字段）
        cand_by_url = {str(c.get("url") or ""): c for c in candidates if c.get("url")}
        candidates = self._fit_candidates_for_llm(candidates)
        payload = json.dumps({"platform": platform, "candidates": candidates}, ensure_ascii=False)
        prompt = f"""\
现在时间是北京时间 {datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}。

以下是 {platform} 平台关键字 "China" 的贴文候选，请挑选 2-3 条最值得关注的：

{payload}"""

        response = None
        max_retries = 2
        for attempt in range(1 + max_retries):
            if attempt > 0:
                print(f"\n      ↩ 重试 {attempt}/{max_retries}...", end="", flush=True)
            try:
                agent = Agent(
                    name="SocialSummarizer",
                    model=self._build_model(),
                    instructions=SOCIAL_SUMMARY_INSTRUCTIONS,
                    add_datetime_to_context=True,
                )
                response = agent.run(prompt)
                raw_text = response.content if hasattr(response, "content") else str(response)
                if _is_api_error(raw_text):
                    if attempt < max_retries:
                        time.sleep(3)
                        continue
                    print(" ✗ 持续 API 报错，使用本地降级")
                    return _fallback_social_items(candidates, platform)

                parsed = _parse_json_response(response)
                items = parsed.get("items") or []
                cleaned = [it for it in items
                           if isinstance(it, dict) and (it.get("content") or "").strip()]
                if not cleaned:
                    print(" ⚠ LLM 未返回有效条目，使用本地降级")
                    return _fallback_social_items(candidates, platform)
                # 回填 article_urls：LLM 未带时按 url 从原始候选取，保证原文章链接不丢
                for it in cleaned:
                    if not it.get("article_urls"):
                        src = cand_by_url.get(str(it.get("url") or ""))
                        if src:
                            it["article_urls"] = src.get("article_urls") or []
                    if not isinstance(it.get("article_urls"), list):
                        it["article_urls"] = []
                print(f" ✓ {platform} 挑选 {len(cleaned)} 条")
                return cleaned[:3]
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                raw_text = ""
                if response and hasattr(response, "content"):
                    raw_text = response.content
                if _is_api_error(raw_text):
                    if attempt < max_retries:
                        time.sleep(3)
                        continue
                    print(" ✗ 持续报错，使用本地降级")
                    return _fallback_social_items(candidates, platform)
                print(f" ⚠ JSON 解析失败，使用本地降级 ({e})")
                return _fallback_social_items(candidates, platform)

        print(" ✗ 全部重试耗尽，使用本地降级")
        return _fallback_social_items(candidates, platform)

    def summarize_social_posts(
        self,
        social_dir: Optional[str] = None,
        output_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """总结社交平台贴文，输出报告板块 Markdown。

        Args:
            social_dir:  社交平台原始数据目录（默认 self.output_dir/social）
            output_file: 摘要输出路径（默认 self.output_dir/social_summaries.json）

        Returns:
            {"platforms": {...}, "markdown": "...", "output_file": "..."}
        """
        base = self.output_dir
        social_path = Path(social_dir) if social_dir else base / "social"
        output_path = Path(output_file) if output_file else base / "social_summaries.json"

        platform_files = {
            "X": social_path / "x.json",
            "Facebook": social_path / "facebook.json",
            "Google News": social_path / "google_news.json",
        }
        candidates_by_platform: Dict[str, List[dict]] = {}
        for platform, f in platform_files.items():
            if not f.exists():
                print(f"   ⏭ {platform}: 无数据文件 {f.name}")
                continue
            data = json.loads(f.read_text(encoding="utf-8"))
            items = data.get("items", [])
            # 兼容旧数据：落盘文件缺少 article_urls 时从 text 兜底提取
            for it in items:
                if "article_urls" not in it:
                    it["article_urls"] = _extract_urls(it.get("text"))
            # 用 _social_candidate_rank 排序：官方大媒体压到最后，
            # 个人/小众媒体的高互动贴文优先进入候选池
            items.sort(key=_social_candidate_rank, reverse=True)
            candidates_by_platform[platform] = items[:16]

        if not candidates_by_platform:
            return {"error": f"未找到社交平台数据（{social_path} 下无平台 JSON），请先运行 scrape_social_platforms"}

        print(f"📝 Phase 4/5: 社交平台贴文总结（每平台 2-3 条）...")
        platforms_result: Dict[str, List[dict]] = {}
        for platform, candidates in candidates_by_platform.items():
            if not candidates:
                print(f"   ⏭ {platform}: 无候选贴文，跳过")
                continue
            try:
                platforms_result[platform] = self._summarize_one_platform(platform, candidates)
            except Exception as e:
                print(f"   ❌ {platform} 总结失败: {e}")
                traceback.print_exc()
                platforms_result[platform] = _fallback_social_items(candidates, platform)

        markdown = _render_social_markdown(platforms_result)
        payload = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "platforms": platforms_result,
            "markdown": markdown,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   ✓ 社交平台板块渲染完毕 → {output_path}")
        return {"platforms": platforms_result, "markdown": markdown, "output_file": str(output_path)}

    # ── Phase 4.5: 信源网站首页截图 ────────────────────────

    def capture_site_screenshots(
        self,
        output_dir: Optional[str] = None,
        headless: bool = True,
        output_file: Optional[str] = None,
    ) -> Dict[str, Any]:
        """截取各信源网站首页首屏，生成报告末尾的「信源网站首页截图」板块。

        委托 tools.site_screenshot，截图保存到 output_dir/screenshots/*.png，
        并落盘 screenshots.json（含 timestamp + url→路径 + markdown），
        便于 --skip-to report 复用。单站点失败自动跳过，不阻塞流水线；
        playwright 未安装时打印警告并返回空结果。

        Args:
            output_dir:  输出根目录（默认 self.output_dir）
            headless:    无头模式（默认 True，可在服务器 / 定时任务下运行）
            output_file: screenshots.json 输出路径（默认 output_dir/screenshots.json）

        Returns:
            {"shots": {url: path}, "dir": "...", "markdown": "...", "output_file": "..."}
        """
        base = Path(output_dir) if output_dir else self.output_dir
        shots_dir = base / "screenshots"
        json_path = Path(output_file) if output_file else base / "screenshots.json"

        print("📸 Phase 4.5/5: 信源网站首页截图 ...")
        try:
            from utils.site_screenshot import capture_all
            shots = capture_all(shots_dir, headless=headless)
        except ImportError as e:
            print(f"⚠ 未安装 playwright，跳过截图板块（pip install playwright && playwright install chromium）: {e}")
            return {"shots": {}, "dir": str(shots_dir), "markdown": "", "output_file": ""}
        except Exception as e:
            print(f"⚠ 截图阶段异常，跳过截图板块: {e}")
            return {"shots": {}, "dir": str(shots_dir), "markdown": "", "output_file": ""}

        markdown = _render_screenshots_markdown(shots)
        payload = {
            "timestamp": datetime.now(BEIJING_TZ).isoformat(),
            "shots": {url: str(Path(p).resolve()) for url, p in shots.items()},
            "markdown": markdown,
        }
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   ✓ 截图板块渲染完毕 → {json_path}")
        return {"shots": shots, "dir": str(shots_dir), "markdown": markdown, "output_file": str(json_path)}

    # ── Phase 5: 简报渲染 ──────────────────────────────────

    def render_report(
        self,
        summaries_file: Optional[str] = None,
        social_summaries_file: Optional[str] = None,
        screenshots_file: Optional[str] = None,
        output_file: Optional[str] = None,
        use_fallback: bool = False,
    ) -> Dict[str, Any]:
        """将摘要数据渲染为 Markdown 简报。

        Args:
            summaries_file:        摘要 JSON 文件路径
            social_summaries_file: 社交平台摘要 JSON 路径（存在时把板块追加到简报末尾）
            screenshots_file:      信源网站截图 JSON 路径（存在时把截图板块追加到简报最后）
            output_file:           输出 Markdown 文件路径
            use_fallback:          是否使用本地降级渲染（不调 LLM）

        Returns:
            {"report": "Markdown 内容", "output_file": "..."}
        """
        base = self.output_dir
        summaries_path = Path(summaries_file) if summaries_file else base / "summaries.json"

        if not summaries_path.exists():
            return {"error": f"摘要文件不存在: {summaries_path}，请先运行 summarize_all_topics"}

        summaries_data = json.loads(summaries_path.read_text(encoding="utf-8"))
        summaries = summaries_data.get("summaries", summaries_data)
        if isinstance(summaries, dict) and "summaries" in summaries:
            summaries = summaries["summaries"]

        now = datetime.now(BEIJING_TZ)
        print(f"📄 Phase 5/5: 渲染 Markdown 简报...")
        print()

        if use_fallback:
            content = _fallback_render_markdown(summaries)
        else:
            try:
                agent = Agent(
                    name="ReportRenderer",
                    model=self._build_model(),
                    instructions=FINAL_OUTPUT_INSTRUCTIONS,
                    markdown=True,
                    add_datetime_to_context=True,
                )
                summaries_json = json.dumps({"summaries": summaries}, ensure_ascii=False)
                prompt = f"""\
现在时间是北京时间 {now.strftime('%Y-%m-%d %H:%M:%S')}。

以下是话题摘要 JSON（包含全球要闻和中国相关两部分），请渲染为 Markdown 简报。
请务必：
- summary 原文照搬，不要用省略号省略
- 来源部分直接列出原始链接，格式为 `- 媒体名：https://...`，不要用 [媒体名](链接) 隐藏 URL
- 不要输出任何 JSON 或代码块（如 ```），只输出纯 Markdown

{summaries_json}"""

                response = _run_agent_with_retry(agent, prompt)
                content = _response_text(response)
                if not content:
                    raise ValueError("渲染返回内容为空")
                content = _strip_all_code_fences(content)
            except Exception as e:
                print(f"⚠ LLM 渲染失败: {e}，使用本地降级渲染")
                content = _fallback_render_markdown(summaries)

        # 确定性后处理：来源里的 `[媒体名](url)` 一律展开为 `- 媒体名：url`，
        # 保证来源 URL 直接可见（LLM 可能不遵守指令，这里兜底）
        content = _flatten_source_links(content)

        # 追加海外社交平台板块（如有）
        social_path = Path(social_summaries_file) if social_summaries_file else base / "social_summaries.json"
        if social_path.exists():
            try:
                social_data = json.loads(social_path.read_text(encoding="utf-8"))
                social_md = social_data.get("markdown", "")
                if social_md:
                    content = content.rstrip() + "\n\n" + social_md + "\n"
            except Exception as e:
                print(f"⚠ 读取社交板块失败: {e}")

        # 追加信源网站首页截图板块（如有）——始终放在报告最后。
        # screenshots_file 传空串 "" 表示「显式跳过截图板块」，绝不回落到旧缓存
        # （否则 --skip-screenshot 时上一次残留的 screenshots.json 会被塞进报告）；
        # 传 None 才回落到默认路径。
        if screenshots_file == "":
            screenshots_path = None
        else:
            screenshots_path = Path(screenshots_file) if screenshots_file else base / "screenshots.json"
        if screenshots_path is not None and screenshots_path.exists():
            try:
                shots_data = json.loads(screenshots_path.read_text(encoding="utf-8"))
                shots_md = shots_data.get("markdown", "")
                if shots_md:
                    content = content.rstrip() + "\n\n" + shots_md + "\n"
            except Exception as e:
                print(f"⚠ 读取截图板块失败: {e}")

        run_id = now.strftime("%Y%m%d_%H%M%S")
        report_file = Path(output_file) if output_file else base / f"report_{run_id}.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(content, encoding="utf-8")

        print(f"   ✓ 简报渲染完成 → {report_file}")

        return {"report": content, "output_file": str(report_file)}

    # ── Word 转换 ──────────────────────────────────────────

    def convert_report_to_word(self, report_file: str) -> Optional[str]:
        """把 Markdown 简报转换为 Word 文档（委托 tools.word.MarkdownToDocx）。

        依赖 pypandoc + 系统 pandoc 二进制。转换失败时打印警告并返回 None，
        不阻塞流水线其他阶段。

        Args:
            report_file: Markdown 报告路径

        Returns:
            生成的 docx 路径；pypandoc / pandoc 不可用或转换失败时返回 None。
        """
        try:
            from utils.word import MarkdownToDocx
            docx = MarkdownToDocx.transfer(report_file)
            print(f"   ✓ Word 转换完成 → {docx}")
            return docx
        except Exception as e:
            print(f"⚠ Word 转换失败（跳过）: {e}")
            return None

    # ── 完整流水线 ─────────────────────────────────────────

    def run_full_pipeline(
        self,
        output_dir: Optional[str] = None,
        skip_scrape: bool = False,
        skip_social: bool = False,
        skip_screenshot: bool = False,
        headless: bool = True,
        skip_word: bool = False,
        push_feishu: bool = False,
        push_email: bool = False,
    ) -> Dict[str, Any]:
        """一键运行完整的媒体监控五阶段流水线。

        Phase 1: 首页抓取 → Phase 2: 话题挑选 → Phase 3: 详情总结
             → Phase 4: 社交平台抓取+总结 → Phase 5: 简报渲染 → Word 转换

        Args:
            output_dir:  输出目录
            skip_scrape: 跳过 Phase 1（使用已有的抓取结果）
            skip_social: 跳过 Phase 4（社交平台板块，未设 APIFY_API_KEY 时自动跳过）
            skip_screenshot: 跳过 Phase 4.5（信源网站首页截图板块）
            headless:    截图是否无头模式（默认 True）
            skip_word:   跳过末尾的 Word 转换（pypandoc / pandoc 不可用时自动跳过）
            push_feishu: Word 生成后把文档推送到飞书群（需配置 FEISHU_* 凭证）
            push_email:  Word 生成后把文档作为附件发到邮箱（需配置 MEDIA_MONITOR_SMTP_* 凭证）

        Returns:
            {"report": "...", "report_file": "...", "word_file": "...",
             "topics_file": "...", "summaries_file": "...", "social_summaries_file": "...",
             "feishu_pushed": bool, "email_sent": bool}
        """
        if output_dir:
            self.output_dir = Path(output_dir)

        base = self.output_dir
        raw_dir = base / "raw"
        manifest_file = base / "scrape_manifest.json"
        topics_file = base / "topics.json"
        summaries_file = base / "summaries.json"
        social_file = base / "social_summaries.json"
        screenshots_file = base / "screenshots.json"

        if not self.brightdata_key:
            return {"error": "BrightData API key 未设置（请设置 BRIGHTDATA_API_KEY 环境变量）"}

        # Phase 1: 首页抓取
        if not skip_scrape:
            result = self.scrape_all_homepages(output_dir=str(base))
            if "error" in result:
                return result
        elif not manifest_file.exists():
            return {"error": f"清单文件不存在: {manifest_file}，无法跳过抓取"}

        # Phase 2: 话题挑选
        topics = self.select_topics(
            scrape_manifest_file=str(manifest_file),
            output_file=str(topics_file),
        )
        if "error" in topics:
            return topics

        # Phase 3: 详情总结
        summaries = self.summarize_all_topics(
            topics_file=str(topics_file),
            output_file=str(summaries_file),
        )
        if "error" in summaries:
            return summaries

        # Phase 4: 社交平台抓取 + 贴文总结（无 key 时自动跳过）
        if not skip_social:
            if not self.apify_key:
                print("⚠ 未设置 APIFY_API_KEY，跳过社交平台板块")
            else:
                social_manifest = self.scrape_social_platforms(output_dir=str(base))
                if social_manifest.get("ok", 0) > 0:
                    social = self.summarize_social_posts(
                        social_dir=str(base / "social"),
                        output_file=str(social_file),
                    )
                    if "error" in social:
                        print(f"⚠ 社交平台总结失败: {social['error']}")
        elif not social_file.exists():
            print("⏭ 已跳过社交平台阶段（--skip-social）")

        # Phase 4.5: 信源网站首页截图
        if not skip_screenshot:
            self.capture_site_screenshots(
                output_dir=str(base), headless=headless, output_file=str(screenshots_file)
            )
        elif not screenshots_file.exists():
            print("⏭ 已跳过截图阶段（skip_screenshot）")

        # Phase 5: 简报渲染
        # --skip-screenshot 时传空串 "" 显式跳过截图板块，避免旧的 screenshots.json 残留被塞进报告
        render_screenshots = "" if skip_screenshot else str(screenshots_file)
        report = self.render_report(
            summaries_file=str(summaries_file),
            social_summaries_file=str(social_file),
            screenshots_file=render_screenshots,
        )
        if "error" in report:
            return report

        # Phase 5.5: 检查报告内容，发现因 API 报错不完整的条目则补全后重渲染
        repaired = self.repair_failed_summaries(output_file=str(summaries_file))
        if repaired.get("repaired"):
            print("🔁 已补全 API 报错条目，重新渲染简报...")
            report = self.render_report(
                summaries_file=str(summaries_file),
                social_summaries_file=str(social_file),
                screenshots_file=render_screenshots,
            )
            if "error" in report:
                return report

        # Word 转换（pypandoc / pandoc 不可用时自动跳过，不阻塞）
        word_file = ""
        if not skip_word and report.get("output_file"):
            word_file = self.convert_report_to_word(report["output_file"]) or ""

        # 飞书推送：优先发 Word 文档；Word 缺失时兜底发一条文本说明
        feishu_pushed = False
        if push_feishu:
            print("📤 推送到飞书群 ...")
            if word_file:
                res = self.feishu.send_file(word_file)
            else:
                res = self.feishu.send_text(
                    f"今日媒体监控日报已生成，但 Word 转换不可用，Markdown 报告见: {report.get('output_file', '')}"
                )
            feishu_pushed = bool(res.get("ok"))
            if not feishu_pushed:
                print(f"⚠ 飞书推送失败: {res.get('error')}")

        # 邮件推送：优先发 Word 附件；Word 缺失时兜底发一条文本说明
        # 标题与附件名统一为「日报选题参考-YYYYMMDD-H点」（H 为报告运行时刻小时）
        email_sent = False
        if push_email:
            print("📧 发送邮件 ...")
            display_name = _report_display_name(report.get("output_file", ""))
            if word_file:
                res = self.email.send_report(
                    word_file, subject=display_name, attachment_name=display_name
                )
            else:
                res = self.email.send_text(
                    f"今日媒体监控日报已生成，但 Word 转换不可用，Markdown 报告见: {report.get('output_file', '')}",
                    subject=display_name,
                )
            email_sent = bool(res.get("ok"))
            if not email_sent:
                print(f"⚠ 邮件发送失败: {res.get('error')}")

        print(f"✅ 全流程完成")
        print(f"📁 产物: {raw_dir}/ | {topics_file.name} | {summaries_file.name} | {social_file.name if social_file.exists() else '(无社交板块)'} | {Path(word_file).name if word_file else '(无 Word)'}")

        return {
            "report": report.get("report", ""),
            "report_file": report.get("output_file", ""),
            "word_file": word_file,
            "topics_file": str(topics_file),
            "summaries_file": str(summaries_file),
            "social_summaries_file": str(social_file) if social_file.exists() else "",
            "screenshots_file": str(screenshots_file) if screenshots_file.exists() else "",
            "feishu_pushed": feishu_pushed,
            "email_sent": email_sent,
        }


# ═══════════════════════════════════════════════════════════
# 命令行入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    """直接运行时的命令行入口。

    用法:
        python -m utils.media_monitor_pipeline                          # 完整流水线（含 Word 转换）
        python -m utils.media_monitor_pipeline --scrape-only              # 只爬首页
        python -m utils.media_monitor_pipeline --skip-to topics           # 从话题挑选开始
        python -m utils.media_monitor_pipeline --skip-to summaries        # 从详情总结开始
        python -m utils.media_monitor_pipeline --skip-to report           # 只渲染简报（含 Word；无截图缓存时会自动补截一次）
        python -m utils.media_monitor_pipeline --skip-to send             # 不重新生成，直接把最新报告推送到飞书+邮箱（默认两者都发；也可用 --push-feishu / --push-email 指定其一）
        python -m utils.media_monitor_pipeline --skip-social              # 完整流水线但跳过社交平台板块
        python -m utils.media_monitor_pipeline --social-only              # 只抓社交平台+总结+渲染板块
        python -m utils.media_monitor_pipeline --skip-screenshot          # 完整流水线但跳过信源网站截图板块
        python -m utils.media_monitor_pipeline --headful                  # 截图使用有头模式（默认无头）
        python -m utils.media_monitor_pipeline --no-word                  # 完整流水线但不转 Word
        python -m utils.media_monitor_pipeline --fix-errors               # 补全最新报告里 API 报错的新闻
        python -m utils.media_monitor_pipeline --push-feishu              # 完整流水线 + Word + 推送飞书
        python -m utils.media_monitor_pipeline --push-email               # 完整流水线 + Word + 发送邮件
        python -m utils.media_monitor_pipeline --daily                    # 每日定时入口（完整流水线+Word+推送飞书+邮件，异常兜底报警）
    """
    skip_to: Optional[str] = None
    if "--skip-to" in sys.argv:
        idx = sys.argv.index("--skip-to")
        if idx + 1 < len(sys.argv):
            skip_to = sys.argv[idx + 1]
    scrape_only = "--scrape-only" in sys.argv
    skip_social = "--skip-social" in sys.argv
    social_only = "--social-only" in sys.argv
    skip_screenshot = "--skip-screenshot" in sys.argv
    headless = "--headful" not in sys.argv
    skip_word = "--no-word" in sys.argv
    fix_errors = "--fix-errors" in sys.argv
    push_feishu = "--push-feishu" in sys.argv
    push_email = "--push-email" in sys.argv
    daily = "--daily" in sys.argv

    agent = MediaMonitorPipeline()
    base = agent.output_dir
    base.mkdir(parents=True, exist_ok=True)

    raw_dir = base / "raw"
    manifest_file = base / "scrape_manifest.json"
    topics_file = base / "topics.json"
    summaries_file = base / "summaries.json"
    social_file = base / "social_summaries.json"
    screenshots_file = base / "screenshots.json"
    now = datetime.now(BEIJING_TZ)

    # ── 每日定时入口：完整流水线 + Word + 推送飞书（异常兜底发飞书文本报警）──
    if daily:
        print(f"🗓  每日定时任务启动 — {now.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
        try:
            result = agent.run_full_pipeline(
                push_feishu=True, push_email=True, skip_word=skip_word,
                skip_screenshot=skip_screenshot, headless=headless,
            )
            if "error" in result:
                print(f"❌ 流水线失败: {result['error']}")
                agent.feishu.send_text(f"⚠️ 今日媒体监控日报生成失败: {result['error']}")
                sys.exit(1)
            if not result.get("feishu_pushed"):
                print("⚠ 报告已生成但飞书推送失败，请检查 FEISHU_* 配置")
            if not result.get("email_sent"):
                print("⚠ 报告已生成但邮件发送失败，请检查 MEDIA_MONITOR_SMTP_* 配置")
            sys.exit(0)
        except Exception as e:
            print(f"❌ 每日任务异常: {e}")
            try:
                agent.feishu.send_text(f"⚠️ 今日媒体监控日报任务异常: {e}")
            except Exception:
                pass
            sys.exit(1)

    # ── 仅社交平台：抓取 + 总结 + 渲染板块 ──────────────────
    if social_only:
        if not agent.apify_key:
            print("❌ APIFY_API_KEY 未设置（请设置 APIFY_API_KEY 环境变量）")
            sys.exit(1)
        manifest = agent.scrape_social_platforms(output_dir=str(base))
        if manifest.get("ok", 0) == 0:
            print(f"❌ 社交平台抓取全部失败: {manifest.get('results', {})}")
            sys.exit(1)
        social = agent.summarize_social_posts(
            social_dir=str(base / "social"),
            output_file=str(social_file),
        )
        if "error" in social:
            print(f"❌ {social['error']}")
            sys.exit(1)
        print(f"\n{social['markdown']}\n")
        print(f"✅ 社交平台板块已保存: {social.get('output_file', '')}")
        sys.exit(0)

    # ── 补全 API 报错新闻：读取最新摘要，重新抓正文总结，再重渲染报告+Word ──
    if fix_errors:
        if not agent.brightdata_key:
            print("❌ BrightData API key 未设置（补全需要抓取正文，请设置 BRIGHTDATA_API_KEY 环境变量）")
            sys.exit(1)
        repaired = agent.repair_failed_summaries(output_file=str(summaries_file))
        if "error" in repaired:
            print(f"❌ {repaired['error']}")
            sys.exit(1)
        if not repaired.get("repaired"):
            print("✅ 无需补全，未生成新报告")
            sys.exit(0)
        # 重新渲染报告（新来源 URL 格式），并转 Word（复用已有截图板块，如有）
        result = agent.render_report(
            summaries_file=str(summaries_file),
            social_summaries_file=str(social_file),
            screenshots_file=str(screenshots_file),
        )
        if "error" in result:
            print(f"❌ {result['error']}")
            sys.exit(1)
        word_file = ""
        if not skip_word and result.get("output_file"):
            word_file = agent.convert_report_to_word(result["output_file"]) or ""
        print(f"✅ 补全完成，新报告: {result.get('output_file', '')}")
        if word_file:
            print(f"📄 Word 文档: {word_file}")
        sys.exit(0)

    # ── 只发送最新报告：不重新生成，直接把已有最新报告推送到飞书 / 邮箱 ──
    if skip_to == "send":
        from utils.word import _find_latest_report

        latest_md = _find_latest_report()
        if latest_md is None:
            print(f"❌ 未找到任何报告（{base} 下无 report_*.md），无法发送")
            sys.exit(1)
        print(f"📤 发送最新报告: {latest_md.name}")

        # 优先发已有同名 Word；缺失且未 --no-word 时补转一次
        word_file = ""
        docx_path = latest_md.with_suffix(".docx")
        if docx_path.exists():
            word_file = str(docx_path)
        elif not skip_word:
            word_file = agent.convert_report_to_word(str(latest_md)) or ""

        # 未显式指定推送目标时，飞书 + 邮箱都发（否则 --skip-to send 会是空操作）
        do_feishu = push_feishu or not (push_feishu or push_email)
        do_email = push_email or not (push_feishu or push_email)

        # 邮件标题与附件名用「日报选题参考-YYYYMMDD-H点」（H 取报告文件名里的运行时刻小时）
        display_name = _report_display_name(str(latest_md))
        text_fallback = f"今日媒体监控日报见: {latest_md}"
        any_ok = False

        if do_feishu:
            print("📤 推送到飞书群 ...")
            res = agent.feishu.send_file(word_file) if word_file else agent.feishu.send_text(text_fallback)
            if res.get("ok"):
                any_ok = True
            else:
                print(f"⚠ 飞书推送失败: {res.get('error')}")
        if do_email:
            print("📧 发送邮件 ...")
            res = (agent.email.send_report(word_file, subject=display_name, attachment_name=display_name)
                   if word_file
                   else agent.email.send_text(text_fallback, subject=display_name))
            if res.get("ok"):
                any_ok = True
            else:
                print(f"⚠ 邮件发送失败: {res.get('error')}")

        sys.exit(0 if any_ok else 1)

    # Phase 1: 首页抓取
    if skip_to is None or scrape_only:
        if not agent.brightdata_key:
            print("❌ BrightData API key 未设置（请设置 BRIGHTDATA_API_KEY 环境变量）")
            sys.exit(1)
        print(f"🕐 Phase 1/5: 首页抓取 — {now.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
        print(f"📡 目标: 23家国际主流媒体首页首屏 (含中国专区5家)")
        print()
        agent.scrape_all_homepages(output_dir=str(base))
        if scrape_only:
            print(f"📁 原始数据: {raw_dir}")
            print(f"📋 清单: {manifest_file}")
            sys.exit(0)
    elif not manifest_file.exists():
        print(f"❌ 未找到抓取清单: {manifest_file}")
        sys.exit(1)

    # Phase 2: 话题挑选
    if skip_to in (None, "topics"):
        result = agent.select_topics(
            scrape_manifest_file=str(manifest_file),
            output_file=str(topics_file),
        )
        if "error" in result:
            print(f"❌ {result['error']}")
            sys.exit(1)
        topics = result
    else:
        if not topics_file.exists():
            print(f"❌ 未找到话题清单: {topics_file}")
            sys.exit(1)
        topics = json.loads(topics_file.read_text(encoding="utf-8"))

    # Phase 3: 详情总结
    if skip_to in (None, "topics", "summaries"):
        if not agent.brightdata_key:
            print("❌ BrightData API key 未设置（Phase 3 需要抓取正文，请设置 BRIGHTDATA_API_KEY 环境变量）")
            sys.exit(1)
        if not isinstance(topics, dict) or not (topics.get("global_topics") or topics.get("china_topics") or topics.get("topics")):
            topics = json.loads(topics_file.read_text(encoding="utf-8"))
        result = agent.summarize_all_topics(
            topics_file=str(topics_file),
            output_file=str(summaries_file),
        )
        if "error" in result:
            print(f"❌ {result['error']}")
            sys.exit(1)
        summaries_list = result.get("summaries", [])
    else:
        if not summaries_file.exists():
            print(f"❌ 未找到摘要数据: {summaries_file}")
            sys.exit(1)
        summaries_data = json.loads(summaries_file.read_text(encoding="utf-8"))
        summaries_list = summaries_data.get("summaries", summaries_data)

    # Phase 4: 社交平台抓取 + 贴文总结
    # （--skip-social 或 --skip-to report（跳过到渲染）时跳过；无 key 时自动跳过）
    if not skip_social and skip_to is None:
        if not agent.apify_key:
            print("⚠ 未设置 APIFY_API_KEY，跳过社交平台板块")
        else:
            manifest = agent.scrape_social_platforms(output_dir=str(base))
            if manifest.get("ok", 0) > 0:
                social = agent.summarize_social_posts(
                    social_dir=str(base / "social"),
                    output_file=str(social_file),
                )
                if "error" in social:
                    print(f"⚠ 社交平台总结失败: {social['error']}")
    elif not social_file.exists():
        if skip_social:
            print("⏭ 已跳过社交平台阶段（--skip-social）")
        else:
            print("⏭ 已跳过社交平台阶段（--skip-to，直接复用已有 social_summaries.json）")

    # Phase 4.5: 信源网站首页截图
    # 完整流程每次重截；--skip-to report 时若已有 screenshots.json 则复用，
    # 没有则自动补截一次（否则报告里不会有截图板块）。--skip-screenshot 显式跳过。
    if skip_screenshot:
        print("⏭ 已跳过截图阶段（--skip-screenshot）")
    elif skip_to is None or not screenshots_file.exists():
        agent.capture_site_screenshots(
            output_dir=str(base), headless=headless, output_file=str(screenshots_file)
        )
    else:
        print(f"⏭ 复用已有截图 {screenshots_file.name}（--skip-to）")

    # Phase 5: 最终渲染
    # --skip-screenshot 传空串 "" 显式跳过截图板块，避免旧的 screenshots.json 残留被塞进报告
    if isinstance(summaries_list, dict) and "summaries" in summaries_list:
        summaries_list = summaries_list["summaries"]
    result = agent.render_report(
        summaries_file=str(summaries_file),
        social_summaries_file=str(social_file),
        screenshots_file="" if skip_screenshot else str(screenshots_file),
    )
    if "error" in result:
        print(f"❌ {result['error']}")
        sys.exit(1)

    # Word 转换（--no-word 跳过；pypandoc / pandoc 不可用时自动跳过）
    word_file = ""
    if not skip_word and result.get("output_file"):
        word_file = agent.convert_report_to_word(result["output_file"]) or ""

    print(f"✅ 报告已保存: {result.get('output_file', '')}")
    print(f"📁 中间产物: {raw_dir}/ | {topics_file.name} | {summaries_file.name} | {social_file.name if social_file.exists() else '(无社交板块)'} | {screenshots_file.name if screenshots_file.exists() else '(无截图板块)'}")
    if word_file:
        print(f"📄 Word 文档: {word_file}")
    elif skip_word:
        print("⏭ 已跳过 Word 转换（--no-word）")

    # 飞书推送（--push-feishu）：优先发 Word，缺失时发文本说明
    if push_feishu:
        print("📤 推送到飞书群 ...")
        if word_file:
            res = agent.feishu.send_file(word_file)
        else:
            res = agent.feishu.send_text(
                f"今日媒体监控日报已生成，Markdown 报告见: {result.get('output_file', '')}"
            )
        if not res.get("ok"):
            print(f"⚠ 飞书推送失败: {res.get('error')}")

    # 邮件推送（--push-email）：优先发 Word 附件，缺失时发文本说明
    # 标题与附件名统一为「日报选题参考-YYYYMMDD-H点」（H 为报告运行时刻小时）
    if push_email:
        print("📧 发送邮件 ...")
        display_name = _report_display_name(result.get("output_file", ""))
        if word_file:
            res = agent.email.send_report(
                word_file, subject=display_name, attachment_name=display_name
            )
        else:
            res = agent.email.send_text(
                f"今日媒体监控日报已生成，Markdown 报告见: {result.get('output_file', '')}",
                subject=display_name,
            )
        if not res.get("ok"):
            print(f"⚠ 邮件发送失败: {res.get('error')}")
