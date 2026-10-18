"""Sending the verification email. SMTP when SMTP_HOST is set (for example Gmail), otherwise the text goes to the log."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from .settings import Settings

logger = logging.getLogger("lecturekit.server")


class SmtpMailer:
    def __init__(self, settings: Settings):
        self.s = settings

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"] = self.s.smtp_from or self.s.smtp_user
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            if self.s.smtp_port == 465:
                with smtplib.SMTP_SSL(self.s.smtp_host, 465, timeout=20, context=ssl.create_default_context()) as smtp:
                    self._deliver(smtp, msg)
            else:
                with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=20) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    self._deliver(smtp, msg)
        except Exception:                                   # never break a request because the mail failed
            logger.exception("could not send an email to a user (check the SMTP_* settings)")

    def _deliver(self, smtp: smtplib.SMTP, msg: EmailMessage) -> None:
        if self.s.smtp_user:
            smtp.login(self.s.smtp_user, self.s.smtp_password)
        smtp.send_message(msg)


class LogMailer:
    """No SMTP configured (local testing): the message, with its link, is written to the log."""

    def send(self, to: str, subject: str, text: str, html: str | None = None) -> None:
        logger.warning("SMTP is not configured; email to %s, subject %r:\n%s", to, subject, text)


def make_mailer(settings: Settings):
    return SmtpMailer(settings) if settings.smtp_host else LogMailer()
