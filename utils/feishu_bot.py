"""飞书自建应用机器人：向群里发送文件 / 文本消息。

飞书「群自定义机器人（webhook）」无法发送文件，只有「自建应用」通过
`im/v1` 接口才能上传并发送 .docx 等文件。本模块封装自建应用的完整调用链：

    app_id + app_secret
        → POST auth/v3/tenant_access_token/internal  取 tenant_access_token
        → POST im/v1/files (multipart)               上传文件拿 file_key
        → POST im/v1/messages (msg_type=file)         把 file_key 发到群

飞书是境内服务，直连即可，无需代理。

用法:
    python -m tools.feishu_bot <file_path>          # 发送文件到默认群
    python -m tools.feishu_bot --text "内容"         # 发送文本到默认群

凭证从 config.feishu_settings（.env 的 FEISHU_APP_ID / FEISHU_APP_SECRET /
FEISHU_CHAT_ID）读取，也可在构造时显式传入。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from utils.settings import feishu_settings

FEISHU_ENDPOINT = "https://open.feishu.cn/open-apis"


class FeishuBot(object):
    """飞书自建应用机器人（文件 / 文本发送）。

    Args:
        app_id:     应用 App ID（默认从 config.feishu_settings.app_id 读取）
        app_secret: 应用 App Secret（默认从 config.feishu_settings.app_secret 读取）
        chat_id:    默认目标群 chat_id（默认从 config.feishu_settings.chat_id 读取）
    """

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self.app_id = app_id or feishu_settings.app_id
        self.app_secret = app_secret or feishu_settings.app_secret
        self.chat_id = chat_id or feishu_settings.chat_id

    # ── 底层：取 token ─────────────────────────────────────

    def _get_tenant_access_token(self, timeout: int = 30) -> str:
        """用 app_id + app_secret 换取 tenant_access_token。

        Returns:
            tenant_access_token 字符串

        Raises:
            RuntimeError: 凭证缺失或飞书返回错误码时
        """
        if not self.app_id or not self.app_secret:
            raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET 未配置")

        resp = requests.post(
            f"{FEISHU_ENDPOINT}/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=timeout,
        )
        data = resp.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"获取 tenant_access_token 失败: code={data.get('code')} msg={data.get('msg')}")
        return data["tenant_access_token"]

    # ── 底层：上传文件拿 file_key ───────────────────────────

    def _upload_file(self, token: str, file_path: Path, timeout: int = 120) -> str:
        """上传文件到飞书，返回 file_key。

        Args:
            token:     tenant_access_token
            file_path: 待上传文件路径

        Returns:
            file_key 字符串

        Raises:
            RuntimeError: 飞书返回错误码时
        """
        with file_path.open("rb") as f:
            files = {
                "file_type": (None, "stream"),
                "file_name": (None, file_path.name),
                "file": (file_path.name, f, "application/octet-stream"),
            }
            resp = requests.post(
                f"{FEISHU_ENDPOINT}/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                files=files,
                timeout=timeout,
            )
        data = resp.json()
        file_key = (data.get("data") or {}).get("file_key")
        if data.get("code") != 0 or not file_key:
            raise RuntimeError(f"上传文件失败: code={data.get('code')} msg={data.get('msg')}")
        return file_key

    # ── 公开：发送文件 ─────────────────────────────────────

    def send_file(self, file_path: str, chat_id: Optional[str] = None) -> Dict[str, Any]:
        """把本地文件（如 .docx）发送到飞书群。

        任何一步失败都返回 {"ok": False, "error": ...}，不抛出，避免打断调用方流水线。

        Args:
            file_path: 本地文件路径
            chat_id:   目标群 chat_id（默认用构造时的 self.chat_id）

        Returns:
            {"ok": True, "message_id": "..."} 或 {"ok": False, "error": "..."}
        """
        target = chat_id or self.chat_id
        if not target:
            return {"ok": False, "error": "FEISHU_CHAT_ID 未配置，无法确定接收群"}

        path = Path(file_path)
        if not path.exists():
            return {"ok": False, "error": f"文件不存在: {path}"}

        try:
            token = self._get_tenant_access_token()
            file_key = self._upload_file(token, path)

            resp = requests.post(
                f"{FEISHU_ENDPOINT}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": target,
                    "msg_type": "file",
                    "content": _json_str({"file_key": file_key}),
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("code") != 0:
                return {"ok": False, "error": f"发送文件消息失败: code={data.get('code')} msg={data.get('msg')}"}
            message_id = (data.get("data") or {}).get("message_id", "")
            print(f"   ✓ 已推送文件到飞书群: {path.name}")
            return {"ok": True, "message_id": message_id}
        except (requests.RequestException, RuntimeError, ValueError) as e:
            print(f"   ✗ 飞书文件推送失败: {e}")
            return {"ok": False, "error": str(e)}

    # ── 公开：发送文本 ─────────────────────────────────────

    def send_text(self, text: str, chat_id: Optional[str] = None) -> Dict[str, Any]:
        """发送纯文本消息（Word 发送失败兜底 / 流水线报警用）。

        Returns:
            {"ok": True, "message_id": "..."} 或 {"ok": False, "error": "..."}
        """
        target = chat_id or self.chat_id
        if not target:
            return {"ok": False, "error": "FEISHU_CHAT_ID 未配置，无法确定接收群"}

        try:
            token = self._get_tenant_access_token()
            resp = requests.post(
                f"{FEISHU_ENDPOINT}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": target,
                    "msg_type": "text",
                    "content": _json_str({"text": text}),
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("code") != 0:
                return {"ok": False, "error": f"发送文本消息失败: code={data.get('code')} msg={data.get('msg')}"}
            message_id = (data.get("data") or {}).get("message_id", "")
            return {"ok": True, "message_id": message_id}
        except (requests.RequestException, RuntimeError, ValueError) as e:
            print(f"   ✗ 飞书文本推送失败: {e}")
            return {"ok": False, "error": str(e)}


def _json_str(obj: Dict[str, Any]) -> str:
    """飞书消息 content 字段要求是 JSON 字符串（而非对象）。"""
    return json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    """命令行入口，手动测试发送。

    用法:
        python -m tools.feishu_bot <file_path>       # 发送文件
        python -m tools.feishu_bot --text "内容"      # 发送文本
    """
    args = sys.argv[1:]
    bot = FeishuBot()

    if not args:
        print("用法: python -m tools.feishu_bot <file_path> | --text <内容>")
        sys.exit(1)

    if args[0] == "--text":
        text = args[1] if len(args) > 1 else "飞书机器人测试消息"
        result = bot.send_text(text)
    else:
        result = bot.send_file(args[0])

    if result.get("ok"):
        print(f"✅ 发送成功 message_id={result.get('message_id', '')}")
        sys.exit(0)
    else:
        print(f"❌ 发送失败: {result.get('error')}")
        sys.exit(1)
