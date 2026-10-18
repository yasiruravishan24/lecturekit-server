"""The two emails: the confirmation code and the welcome message. Each has a plain-text and an HTML version."""
from html import escape

BRAND = "LectureKit"
COLOR = "#4f46e5"


def _page(title: str, body_html: str) -> str:
    """One shared layout. Tables and inline styles only: that is what email programs support."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:12px;overflow:hidden;">
<tr><td style="background:{COLOR};padding:24px 32px;color:#ffffff;font-size:20px;font-weight:700;letter-spacing:.3px;">{BRAND}</td></tr>
<tr><td style="padding:32px;color:#111827;font-size:16px;line-height:1.6;">{body_html}</td></tr>
<tr><td style="padding:20px 32px;background:#f9fafb;color:#6b7280;font-size:12px;line-height:1.5;border-top:1px solid #e5e7eb;">
You are receiving this email because this address was used to sign up for {BRAND}. If that was not you, you can ignore it.</td></tr>
</table>
</td></tr></table>
</body></html>"""


def verification_email(name: str, code: str, minutes: int) -> tuple[str, str, str]:
    """(subject, text, html) for the sign-up confirmation code."""
    who = name or "there"
    subject = f"Verify your {BRAND} email address"
    text = (f"Hi {who},\n\nThanks for signing up for {BRAND}. Enter this code to verify your email address:\n\n"
            f"    {code}\n\nThe code is valid for {minutes} minutes. Do not share it with anyone.\n\n"
            f"If you did not create an account, you can ignore this email.\n\n{BRAND}\n")
    spaced = "&nbsp;".join(escape(code))
    html = _page(subject, f"""
<p style="margin:0 0 16px;font-size:18px;font-weight:600;">Hi {escape(who)},</p>
<p style="margin:0 0 24px;">Thanks for signing up for {BRAND}. Enter this code to verify your email address:</p>
<table role="presentation" align="center" cellpadding="0" cellspacing="0" style="margin:0 auto 24px;"><tr>
<td style="background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;padding:16px 28px;font-family:'SFMono-Regular',Menlo,Consolas,monospace;font-size:34px;font-weight:700;letter-spacing:6px;color:{COLOR};text-align:center;">{spaced}</td>
</tr></table>
<p style="margin:0 0 8px;">This code is valid for <strong>{minutes} minutes</strong>.</p>
<p style="margin:0;color:#6b7280;font-size:14px;">For your security, never share this code with anyone.</p>""")
    return subject, text, html


def welcome_email(name: str, app_url: str = "", daily_limit: int = 5, keep_days: int = 7) -> tuple[str, str, str]:
    """(subject, text, html) sent once the email address is verified. `app_url` adds an "Open LectureKit" button."""
    first = (name or "").split(" ")[0] or "there"
    subject = f"Welcome to {BRAND}, {first}!" if name else f"Welcome to {BRAND}!"
    intro = (f"Thanks for joining {BRAND}. Your email is verified and your account is ready. {BRAND} turns your lecture recordings "
             "and slides into clear notes and a practice quiz, so you spend less time writing and more time learning.")
    steps = [("Create a course and a module", "for example a subject and its weeks."),
             ("Add a lecture", "upload a recording, or record it live. Slides are optional but make the notes better."),
             ("Study", "when processing finishes, read the notes and take the quiz.")]
    features = [("Accurate transcript", "speech-to-text for lectures in audio or video form."),
                ("Structured notes", "full notes with headings and key points, plus a short summary."),
                ("Practice quiz", "multiple-choice questions with explanations, and your score history."),
                ("Works with your slides", "PDF, PowerPoint and Word files are supported.")]
    good_to_know = [f"You can process up to {daily_limit} lectures per day.",
                    f"Uploaded recordings are removed from the server after {keep_days} days; your notes and quizzes stay.",
                    "Your lectures are private: only you can see them."]

    text = (f"Hi {first},\n\n{intro}\n\n" + (f"Open {BRAND}: {app_url}\n\n" if app_url else "") + "Get started in 3 steps\n"
            + "".join(f"  {i}. {h}: {d}\n" for i, (h, d) in enumerate(steps, 1))
            + "\nWhat you get\n" + "".join(f"  - {h}: {d}\n" for h, d in features)
            + "\nGood to know\n" + "".join(f"  - {g}\n" for g in good_to_know)
            + f"\nHappy studying!\nThe {BRAND} team\n")

    def step_row(i: int, head: str, desc: str) -> str:
        return (f'<tr><td valign="top" style="padding:0 14px 14px 0;"><div style="width:28px;height:28px;border-radius:14px;background:{COLOR};color:#ffffff;'
                f'font-weight:700;font-size:14px;line-height:28px;text-align:center;">{i}</div></td>'
                f'<td style="padding:0 0 14px;font-size:15px;line-height:1.5;"><strong>{escape(head)}</strong><br><span style="color:#4b5563;">{escape(desc)}</span></td></tr>')

    def feature_row(head: str, desc: str) -> str:
        return (f'<tr><td valign="top" style="padding:0 10px 10px 0;color:{COLOR};font-weight:700;font-size:15px;">&#10003;</td>'
                f'<td style="padding:0 0 10px;font-size:15px;line-height:1.5;"><strong>{escape(head)}</strong> '
                f'<span style="color:#4b5563;">&ndash; {escape(desc)}</span></td></tr>')

    button = (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 28px;"><tr><td style="background:{COLOR};border-radius:8px;">'
              f'<a href="{escape(app_url, quote=True)}" style="display:inline-block;padding:12px 28px;color:#ffffff;font-size:16px;font-weight:600;'
              f'text-decoration:none;">Open {BRAND}</a></td></tr></table>') if app_url else ""
    notes = "".join(f'<li style="margin:0 0 6px;">{escape(g)}</li>' for g in good_to_know)
    html = _page(subject, f"""
<p style="margin:0 0 12px;font-size:24px;font-weight:700;line-height:1.3;">Welcome aboard, {escape(first)}!</p>
<p style="margin:0 0 24px;">{escape(intro)}</p>
{button}
<p style="margin:0 0 14px;font-size:17px;font-weight:700;">Get started in 3 steps</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 24px;">{"".join(step_row(i, h, d) for i, (h, d) in enumerate(steps, 1))}</table>
<p style="margin:0 0 14px;font-size:17px;font-weight:700;">What you get</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 24px;">{"".join(feature_row(h, d) for h, d in features)}</table>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 24px;"><tr><td style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:16px 20px;font-size:14px;line-height:1.5;color:#374151;">
<strong style="display:block;margin-bottom:8px;">Good to know</strong><ul style="margin:0;padding-left:18px;">{notes}</ul></td></tr></table>
<p style="margin:0;">Happy studying!<br><span style="color:#6b7280;">The {BRAND} team</span></p>""")
    return subject, text, html
