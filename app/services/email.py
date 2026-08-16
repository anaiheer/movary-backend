import os
import re
from email.message import EmailMessage
from html import unescape
from typing import Optional

import aiosmtplib
from aiosmtplib.errors import SMTPConnectError


_HTML_BREAK_TAGS = re.compile(r"</?(?:br|p|div|li|ul|ol|h[1-6]|tr|table)[^>]*>", re.IGNORECASE)
_HTML_TAGS = re.compile(r"<[^>]+>")
_TEXT_NEWLINES = re.compile(r"\n{3,}")


class SmtpConfig:
    def __init__(
        self,
        host: str,
        port: int,
        username: Optional[str],
        password: Optional[str],
        sender: str,
        use_tls: bool,
        use_ssl: bool,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.use_tls = use_tls
        self.use_ssl = use_ssl


async def send_email(
    to_address: str,
    subject: str,
    html_body: str,
    text_body: Optional[str],
    config: SmtpConfig,
) -> None:
    if os.getenv("MAIL_SUPPRESS_SEND") == "1":
        return

    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = to_address
    message["Subject"] = subject

    resolved_text_body = (text_body or "").strip() or _html_to_text(html_body)
    if resolved_text_body:
        message.set_content(resolved_text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        await aiosmtplib.send(
            message,
            hostname=config.host,
            port=config.port,
            username=config.username,
            password=config.password,
            start_tls=config.use_tls,
            use_tls=config.use_ssl,
        )
        return
    except SMTPConnectError:
        if (
            config.host == "smtp.gmail.com"
            and config.port == 587
            and config.use_tls
            and not config.use_ssl
        ):
            await aiosmtplib.send(
                message,
                hostname=config.host,
                port=465,
                username=config.username,
                password=config.password,
                start_tls=False,
                use_tls=True,
            )
            return
        raise


def _html_to_text(html_body: str) -> str:
    plain = _HTML_BREAK_TAGS.sub("\n", html_body or "")
    plain = _HTML_TAGS.sub("", plain)
    plain = unescape(plain)
    lines = [line.strip() for line in plain.splitlines()]
    plain = "\n".join(line for line in lines if line)
    return _TEXT_NEWLINES.sub("\n\n", plain).strip()
