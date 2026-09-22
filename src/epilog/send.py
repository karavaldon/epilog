import imaplib
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from .config import Config

EPILOG_MSGID_DOMAIN = "epilog.local"


def check_gmail(address: str, app_password: str) -> str | None:
    """Logs in to Gmail (sending and reading) without sending anything.
    Returns None if both work, otherwise a readable reason."""
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(address, app_password)
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(address, app_password)
        imap.logout()
    except smtplib.SMTPAuthenticationError:
        return "Gmail didn't accept that address and app password."
    except imaplib.IMAP4.error as e:
        return f"Sending works, but reading replies doesn't: {e}"
    except OSError as e:
        return f"Couldn't reach Gmail: {e}"
    return None


def send_email(cfg: Config, subject: str, text: str, html: str | None = None,
               images: dict[str, bytes] | None = None, in_reply_to: str | None = None) -> None:
    if not (cfg.gmail_address and cfg.gmail_app_password):
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Epilog", cfg.gmail_address))
    msg["To"] = cfg.digest_to
    msg["Message-ID"] = make_msgid(domain=EPILOG_MSGID_DOMAIN)
    if in_reply_to:  # keeps the confirmation in the same Gmail thread
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
        html_part = msg.get_payload()[1]
        for cid, data in (images or {}).items():
            html_part.add_related(data, maintype="image", subtype="jpeg", cid=f"<{cid}>",
                                  disposition="inline", filename=f"{cid.split('@')[0]}.jpg")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as smtp:
        smtp.login(cfg.gmail_address, cfg.gmail_app_password)
        smtp.send_message(msg)
