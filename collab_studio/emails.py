"""Transactional email. Every message is stored in the `emails` table (that is the development preview);
with SMTP configured it is also sent. A dedupe key stops the same event emailing twice."""
import smtplib
import sqlite3
from email.message import EmailMessage
from html import escape

from .db import DB, now

TEMPLATES = {
    "request_received": ("We've got your idea — {ref}",
                         "Thanks, {name}. Your idea is in our design queue.",
                         "We'll email you when your two concepts are ready. You can check progress any time on your "
                         "private design page.", "View my design page"),
    "designs_ready": ("Your designs are ready — {ref}",
                      "{name}, your concepts are ready.",
                      "Take a look, choose your favourite or ask for changes.", "Review my designs"),
    "revision_ready": ("Your revised design is ready — {ref}",
                       "{name}, your revision is ready.",
                       "We've applied your changes. Your earlier versions are still on the page.", "See the revision"),
    "proof_ready": ("Your final proof is ready — {ref}",
                    "{name}, your final proof is ready.",
                    "Check the details, pick your size and approve it when you're happy.", "Review my proof"),
    "payment_confirmed": ("Payment confirmed — {ref}",
                          "Thanks, {name}. Your payment is confirmed.",
                          "Your approved design is now being prepared for production. We'll email tracking as soon "
                          "as it ships.", "View my order"),
    "order_shipped": ("Your order is on its way — {ref}",
                      "{name}, your order has shipped.",
                      "{tracking}", "Track my order"),
    "new_link": ("Your private design link — {ref}",
                 "Here's your private design link, {name}.",
                 "We've issued a new link for your designs. Older links no longer work.", "Open my design page"),
}


def render(kind: str, business: str, ctx: dict):
    subject, heading, body, button = TEMPLATES[kind]
    subject = subject.format(**ctx)
    heading = heading.format(**ctx)
    body = body.format(**ctx)
    link = ctx["link"]
    text = f"{heading}\n\n{body}\n\n{button}: {link}\n\n— {business}\nThis link is private. Don't share it."
    html = f"""<!doctype html><html><body style="margin:0;background:#f3efe7;font-family:Helvetica,Arial,sans-serif;color:#1d1e20">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" style="max-width:560px;background:#fffdf8;border-radius:14px;overflow:hidden">
<tr><td style="background:#1d1e20;color:#f3efe7;padding:18px 28px;font-weight:800;letter-spacing:.12em;text-transform:uppercase">{escape(business)}</td></tr>
<tr><td style="padding:28px">
<h1 style="font-size:22px;margin:0 0 12px">{escape(heading)}</h1>
<p style="font-size:15px;line-height:1.6;margin:0 0 24px">{escape(body)}</p>
<a href="{escape(link)}" style="display:inline-block;background:#b3262e;color:#fff;text-decoration:none;padding:13px 22px;border-radius:999px;font-weight:700">{escape(button)}</a>
<p style="font-size:12px;color:#6b6a66;margin:28px 0 0">Reference {escape(ctx['ref'])}. This link is private — please don't share it.</p>
</td></tr></table></td></tr></table></body></html>"""
    return subject, html, text


class Mailer:
    def __init__(self, db: DB, env):
        self.db, self.env = db, env

    def send(self, kind, to_addr, ctx, business, request_id=None, dedupe_key=None):
        subject, html, text = render(kind, business, ctx)
        provider = self.env.email_provider
        try:
            eid = self.db.run(
                "INSERT INTO emails (request_id, kind, to_addr, subject, html, text, provider, status, dedupe_key, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (request_id, kind, to_addr, subject, html, text, provider,
                 "previewed" if provider == "preview" else "sending", dedupe_key, now()))
        except sqlite3.IntegrityError:
            return None                      # already sent for this event
        if provider == "smtp":
            try:
                msg = EmailMessage()
                msg["Subject"], msg["From"], msg["To"] = subject, self.env.email_from, to_addr
                msg.set_content(text)
                msg.add_alternative(html, subtype="html")
                with smtplib.SMTP(self.env.smtp_host, self.env.smtp_port, timeout=30) as s:
                    s.starttls()
                    if self.env.smtp_user:
                        s.login(self.env.smtp_user, self.env.smtp_password)
                    s.send_message(msg)
                self.db.run("UPDATE emails SET status='sent' WHERE id=?", (eid,))
            except (OSError, smtplib.SMTPException) as e:
                self.db.run("UPDATE emails SET status='failed', error=? WHERE id=?", (type(e).__name__, eid))
        return eid
