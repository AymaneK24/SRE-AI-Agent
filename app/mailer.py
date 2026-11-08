"""Sends email over SMTP, such as Google Workspace or Gmail with an app password."""
from __future__ import annotations

import asyncio
import html
import logging
import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import certifi

from .config import Config

log = logging.getLogger("rcc.mail")

_LOGO_PATH = Path(__file__).parent / "static" / "email-logo.png"
_LOGO = _LOGO_PATH.read_bytes() if _LOGO_PATH.exists() else b""
# Email programs ignore most modern CSS, so this layout uses tables and inline styles.
FONT = "font-family:'Segoe UI',Arial,Helvetica,sans-serif"
INK = "#17181c"
BODY = "#33353c"
MUTED = "#6f727a"
# The stripe under the header. Red comes from the logo.
TONES = {"default": "#e5484d", "critical": "#e5484d", "warning": "#e09a00", "resolved": "#2f9e44"}


class MailError(Exception):
    pass


def _tls_context() -> ssl.SSLContext:
    """Trusts the system's certificates, such as a company's own, plus the public ones, which a minimal image may not include."""
    context = ssl.create_default_context()
    context.load_verify_locations(certifi.where())
    return context


def paragraph(text: str, color: str = BODY, size: int = 15) -> str:
    return f'<p style="margin:0 0 16px;color:{color};font-size:{size}px;line-height:1.6;{FONT}">{html.escape(text).replace(chr(10), "<br>")}</p>'


class Mailer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.outbox: list[EmailMessage] = []  # Kept only when no SMTP server is configured.

    @property
    def configured(self) -> bool:
        return self.config.smtp_configured

    def _send(self, message: EmailMessage) -> None:
        cfg = self.config
        try:
            if cfg.smtp_tls == "ssl":
                client: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20, context=_tls_context())
            else:
                client = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
            with client:
                if cfg.smtp_tls == "starttls":
                    client.starttls(context=_tls_context())
                if cfg.smtp_username:
                    client.login(cfg.smtp_username, cfg.smtp_password)
                client.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            raise MailError(f"The email server didn't accept the message: {error}") from error

    def _notices(self, company: str, security_notice: bool, reason: str = "") -> list[str]:
        notices = []
        if reason:
            notices.append(reason)
        if security_notice:
            # Only for emails about an account. An alert has nothing to do with passwords.
            notices.append(f"Security: {company} will never ask for your password by email. If you weren't expecting this message, don't use its links, and tell your administrator.")
        notices.append("Confidentiality: this message and anything attached to it are confidential and intended only for the person they were sent to. If you received it by mistake, delete it and don't share it.")
        notices.append(f"Automated message from Root Cause Console for {company}. Please don't reply.")
        return notices

    def _html(self, title: str, subtitle: str, content: str, link: str | None, link_label: str, company: str, notices: list[str], tone: str, has_logo: bool) -> str:
        safe = html.escape
        button = ""
        fallback = ""
        if link:
            url = safe(link, quote=True)
            button = (
                f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 22px"><tr><td style="background:{INK};border-radius:6px">'
                f'<a href="{url}" style="display:inline-block;padding:12px 24px;color:#ffffff;font-size:15px;font-weight:600;text-decoration:none;{FONT}">{safe(link_label)}</a></td></tr></table>'
            )
            fallback = (f'<p style="margin:0 0 4px;color:{MUTED};font-size:13px;line-height:1.5;{FONT}">If the button doesn\'t work, copy this address into your browser:</p>'
                        f'<p style="margin:0 0 24px;color:{BODY};font-size:13px;line-height:1.5;word-break:break-all;{FONT}">{url}</p>')
        logo = '<img src="cid:rcc-logo" width="44" height="44" alt="" style="display:block;margin:0 auto 14px;border:0">' if has_logo else ""
        sub = f'<p style="margin:10px 0 0;color:#d4d5db;font-size:15px;line-height:1.5;{FONT}">{safe(subtitle)}</p>' if subtitle else ""
        small = "".join(f'<p style="margin:0 0 10px;color:{MUTED};font-size:12px;line-height:1.5;{FONT}">{safe(item)}</p>' for item in notices)
        return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f1f2f4">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f2f4"><tr><td align="center" style="padding:28px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#ffffff;border:1px solid #dcdde1">
<tr><td align="center" style="padding:30px 32px 28px;background:{INK}">
{logo}<h1 style="margin:0;color:#ffffff;font-size:25px;line-height:1.25;font-weight:700;{FONT}">{safe(title)}</h1>{sub}
</td></tr>
<tr><td style="height:4px;line-height:4px;font-size:4px;background:{TONES.get(tone, TONES["default"])}">&nbsp;</td></tr>
<tr><td style="padding:30px 32px 26px">
{content}{button}{fallback}
<p style="margin:0;color:{BODY};font-size:15px;line-height:1.6;{FONT}"><b style="color:{INK}">Root Cause Console</b><br>For {safe(company)}</p>
</td></tr>
<tr><td style="padding:20px 32px 12px;background:#f8f8f9;border-top:1px solid #e6e7ea">
{small}
</td></tr>
</table></td></tr></table></body></html>"""

    async def send(
        self, to: str, subject: str, text: str, link: str | None = None, link_label: str = "", *,
        security_notice: bool = False, tone: str = "default", subtitle: str = "", title: str = "",
        body_html: str = "", images: dict[str, bytes] | None = None, attachments: list[dict[str, Any]] | None = None,
        reason: str = "",
    ) -> None:
        """`text` is the plain version, and also the styled one unless `body_html` is given. `body_html` must already be escaped."""
        company = self.config.company_name or "your organization"
        notices = self._notices(company, security_notice, reason)
        message = EmailMessage()
        message["From"] = f"Root Cause Console <{self.config.smtp_from or 'no-reply@localhost'}>"
        message["To"] = to
        clean_subject = subject.replace("\n", " ").replace("\r", " ")[:200]
        message["Subject"] = clean_subject
        message["Auto-Submitted"] = "auto-generated"
        link_text = f"\n\n{link_label}: {link}" if link else ""
        message.set_content(f"{text}{link_text}\n\n--\n" + "\n\n".join(notices) + "\n")
        content = body_html or "".join(paragraph(part) for part in text.split("\n\n") if part.strip())
        heading = title or re.sub(r"^\[[A-Z]+\]\s*", "", clean_subject)
        message.add_alternative(self._html(heading, subtitle, content, link, link_label, company, notices, tone, bool(_LOGO)), subtype="html")
        html_part = message.get_body(preferencelist=("html",))
        if _LOGO:
            html_part.add_related(_LOGO, maintype="image", subtype="png", cid="<rcc-logo>", filename="logo.png", disposition="inline")
        for name, data in (images or {}).items():
            html_part.add_related(data, maintype="image", subtype="png", cid=f"<{name}>", filename=f"{name}.png", disposition="inline")
        for item in attachments or []:
            maintype, _, subtype = item["mime"].partition("/")
            message.add_attachment(item["data"], maintype=maintype, subtype=subtype, filename=item["filename"])
        if not self.configured:
            # Local use only. Hosted mode refuses to start without SMTP.
            self.outbox.append(message)
            log.warning("No email server is set up. Message to %s not sent. %s: %s", to, link_label or subject, link or "")
            return
        await asyncio.to_thread(self._send, message)
