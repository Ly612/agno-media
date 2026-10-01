"""邮件发送工具：把媒体监控日报（Word 附件）通过 SMTP 发到指定邮箱。

只用标准库 smtplib + email，无第三方依赖。结构对齐 tools.feishu_bot.FeishuBot：
一个类、send_report() 返回 {"ok": bool, ...}、任何失败都捕获不抛出（不打断
调用方流水线）、附 __main__ CLI 便于单独测试。

凭证从 config.email_settings（.env 的 MEDIA_MONITOR_SMTP_* 系列）读取，
也可在构造时显式传入。SMTP 服务器多为境内（QQ/163）或境外（Gmail）：
代码不写死代理，能否连通由 .env 里的 host/port 与本机网络决定。

用法:
    from utils.email_sender import EmailSender

    res = EmailSender().send_report("output/media_monitor/report_xxx.docx")

    # 命令行:
    python -m tools.email_sender <file_path>
    python -m tools.email_sender <file_path> --subject "媒体监控日报"
"""

from __future__ import annotations

import mimetypes
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.settings import email_settings

# Word .docx 的标准 MIME 类型
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_DEFAULT_BODY = "附件为今日媒体监控日报（Word 文档），请查收。"


class EmailSender(object):
    """SMTP 邮件发送器（发送带 Word 附件的日报）。

    Args:
        host:      SMTP 服务器地址（默认 config.email_settings.host）
        port:      SMTP 端口（默认 config.email_settings.port）
        user:      登录用户名（默认 config.email_settings.user）
        password:  登录密码 / 授权码（默认 config.email_settings.password）
        sender:    发件邮箱（默认 config.email_settings.sender，缺省回落 user）
        recipients: 收件邮箱列表（默认 config.email_settings.recipients）
        bcc:       隐藏抄送（密送）邮箱列表（默认 config.email_settings.bcc_recipients）；
                   不写入邮件头，收件人看不到密送名单
        use_ssl:   True 用 SSL(465)，False 用 STARTTLS(587)（默认取配置）
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        sender: Optional[str] = None,
        recipients: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        use_ssl: Optional[bool] = None,
    ):
        self.host = host or email_settings.host
        self.port = port or email_settings.port
        self.user = user or email_settings.user
        self.password = password or email_settings.password
        self.sender = sender or email_settings.sender
        self.recipients = recipients if recipients is not None else email_settings.recipients
        self.bcc = bcc if bcc is not None else email_settings.bcc_recipients
        self.use_ssl = email_settings.use_ssl if use_ssl is None else use_ssl

    # ── 底层：组装邮件 ─────────────────────────────────────

    def _build_message(
        self,
        subject: str,
        body: str,
        recipients: List[str],
        attachment: Optional[Path],
        attachment_name: Optional[str] = None,
    ) -> EmailMessage:
        """组装一封 EmailMessage（可带一个附件）。

        attachment_name 用于自定义收件人看到的附件文件名（与磁盘真实文件名解耦），
        缺省则用磁盘文件名。后缀不含 .docx 时会补上磁盘文件的后缀。
        """
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)

        if attachment is not None:
            data = attachment.read_bytes()
            if attachment.suffix.lower() == ".docx":
                maintype, subtype = _DOCX_MIME.split("/", 1)
            else:
                guessed, _ = mimetypes.guess_type(attachment.name)
                maintype, subtype = (guessed or "application/octet-stream").split("/", 1)
            filename = attachment_name or attachment.name
            # 自定义显示名若没带后缀，补上磁盘文件的后缀，避免收件端识别不出类型
            if attachment_name and not Path(attachment_name).suffix:
                filename = attachment_name + attachment.suffix
            msg.add_attachment(
                data, maintype=maintype, subtype=subtype, filename=filename
            )
        return msg

    # ── 底层：发送 ─────────────────────────────────────────

    def _send(self, msg: EmailMessage, envelope_recipients: List[str], timeout: int = 60) -> None:
        """连接 SMTP 并发送（SSL 或 STARTTLS）。失败抛异常，由上层捕获。

        envelope_recipients 是 SMTP 投递名单（To + Bcc 全部实际投递地址）；密送地址
        只出现在这里、不写进邮件头，所以收件人看不到密送名单，即实现隐藏抄送。
        """
        if self.use_ssl:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=timeout) as smtp:
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg, from_addr=self.sender, to_addrs=envelope_recipients)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=timeout) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                if self.user:
                    smtp.login(self.user, self.password)
                smtp.send_message(msg, from_addr=self.sender, to_addrs=envelope_recipients)

    # ── 公开：发送带 Word 附件的日报 ───────────────────────

    def send_report(
        self,
        file_path: str,
        subject: Optional[str] = None,
        body: Optional[str] = None,
        to: Optional[List[str]] = None,
        attachment_name: Optional[str] = None,
        bcc: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """把本地文件（如 .docx 日报）作为附件发送到收件邮箱。

        任何一步失败都返回 {"ok": False, "error": ...}，不抛出，避免打断调用方流水线。

        Args:
            file_path:       本地附件路径（.docx / 其他）
            subject:         邮件主题（默认「媒体监控日报」）
            body:            正文文本（默认一句附件说明）
            to:              收件人列表（默认用构造时的 self.recipients）
            attachment_name: 收件人看到的附件文件名（默认用磁盘真实文件名；
                             不含后缀时自动补磁盘文件的后缀）
            bcc:             隐藏抄送（密送）列表（默认用构造时的 self.bcc）；
                             不写入邮件头，收件人看不到密送名单

        Returns:
            {"ok": True, "recipients": [...], "bcc": [...]} 或 {"ok": False, "error": "..."}
        """
        recipients = to if to is not None else self.recipients
        bcc_list = bcc if bcc is not None else self.bcc
        if not self.host:
            return {"ok": False, "error": "MEDIA_MONITOR_SMTP_HOST 未配置，无法发送邮件"}
        if not self.sender:
            return {"ok": False, "error": "发件邮箱未配置（MEDIA_MONITOR_SMTP_MAIL_FROM / USER）"}
        if not recipients:
            return {"ok": False, "error": "MEDIA_MONITOR_SMTP_MAIL_TO 未配置，无收件人"}

        path = Path(file_path)
        if not path.exists():
            return {"ok": False, "error": f"附件不存在: {path}"}

        try:
            msg = self._build_message(
                subject=subject or "媒体监控日报",
                body=body or _DEFAULT_BODY,
                recipients=recipients,
                attachment=path,
                attachment_name=attachment_name,
            )
            # 密送地址只进投递名单、不进邮件头；去重保序
            envelope = recipients + [a for a in bcc_list if a not in recipients]
            self._send(msg, envelope)
            shown = attachment_name or path.name
            bcc_note = f"，密送 {len(bcc_list)} 人" if bcc_list else ""
            print(f"   ✓ 已发送邮件到 {', '.join(recipients)}{bcc_note}（附件: {shown}）")
            return {"ok": True, "recipients": recipients, "bcc": bcc_list}
        except (smtplib.SMTPException, OSError, ValueError) as e:
            print(f"   ✗ 邮件发送失败: {e}")
            return {"ok": False, "error": str(e)}

    # ── 公开：发送纯文本（Word 缺失时兜底 / 报警用）─────────

    def send_text(
        self,
        text: str,
        subject: Optional[str] = None,
        to: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """发送纯文本邮件（无附件）。

        bcc 为隐藏抄送（密送）列表，默认用构造时的 self.bcc；不写入邮件头。

        Returns:
            {"ok": True, "recipients": [...], "bcc": [...]} 或 {"ok": False, "error": "..."}
        """
        recipients = to if to is not None else self.recipients
        bcc_list = bcc if bcc is not None else self.bcc
        if not self.host:
            return {"ok": False, "error": "MEDIA_MONITOR_SMTP_HOST 未配置，无法发送邮件"}
        if not self.sender:
            return {"ok": False, "error": "发件邮箱未配置（MEDIA_MONITOR_SMTP_MAIL_FROM / USER）"}
        if not recipients:
            return {"ok": False, "error": "MEDIA_MONITOR_SMTP_MAIL_TO 未配置，无收件人"}

        try:
            msg = self._build_message(
                subject=subject or "媒体监控日报",
                body=text,
                recipients=recipients,
                attachment=None,
            )
            envelope = recipients + [a for a in bcc_list if a not in recipients]
            self._send(msg, envelope)
            return {"ok": True, "recipients": recipients, "bcc": bcc_list}
        except (smtplib.SMTPException, OSError, ValueError) as e:
            print(f"   ✗ 邮件发送失败: {e}")
            return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    """命令行入口，手动测试发送。

    用法:
        python -m tools.email_sender <file_path>
        python -m tools.email_sender <file_path> --subject "主题"
        python -m tools.email_sender --text "内容"
    """
    args = sys.argv[1:]
    sender = EmailSender()

    if not args:
        print("用法: python -m tools.email_sender <file_path> [--subject 主题] | --text <内容>")
        sys.exit(1)

    subject = None
    if "--subject" in args:
        i = args.index("--subject")
        if i + 1 < len(args):
            subject = args[i + 1]

    if args[0] == "--text":
        text = args[1] if len(args) > 1 else "邮件发送测试消息"
        result = sender.send_text(text, subject=subject)
    else:
        result = sender.send_report(args[0], subject=subject)

    if result.get("ok"):
        print(f"✅ 发送成功 → {', '.join(result.get('recipients', []))}")
        sys.exit(0)
    else:
        print(f"❌ 发送失败: {result.get('error')}")
        sys.exit(1)
