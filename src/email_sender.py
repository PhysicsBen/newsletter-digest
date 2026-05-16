"""
Send digest Markdown files to an email inbox via Gmail API.

Converts Markdown → HTML and sends a multipart/alternative email (plain + HTML).
Uses the same OAuth2 credentials as gmail_client.py (token.json / credentials.json).
Requires the gmail.send scope — token.json will be regenerated automatically if
it was issued with gmail.readonly only.
"""
from __future__ import annotations

import base64
import html as _html_lib
import logging
import re
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import markdown as md
from googleapiclient.discovery import build

from src.config import settings

log = logging.getLogger(__name__)

_SEND_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def _get_send_credentials():
    """Load OAuth2 credentials with gmail.send scope."""
    import os
    from typing import Optional
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds: Optional[Credentials] = None
    if os.path.exists(settings.gmail_token_path):
        creds = Credentials.from_authorized_user_file(settings.gmail_token_path, _SEND_SCOPES)
        if creds and (not creds.scopes or not set(_SEND_SCOPES).issubset(creds.scopes)):
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(
                settings.gmail_credentials_path, _SEND_SCOPES
            )
            creds = flow.run_local_server(port=0)

        with open(settings.gmail_token_path, "w") as f:
            f.write(creds.to_json())

    return creds

# Seconds to wait between sends to stay well within Gmail's sending rate limit.
_SEND_DELAY_S = 2


def _markdown_to_html(text: str) -> str:
    """Convert Markdown text to an HTML string."""
    return md.markdown(
        text,
        extensions=["tables", "fenced_code", "toc"],
        output_format="html",
    )


# ── Digest HTML email renderer ────────────────────────────────────────────────

_SECTION_RE = re.compile(r"^## (.+?)\s+·\s+(HIGH|MEDIUM|LOW)\s+([\d.]+)\s*$")
_CALLOUT_RE = re.compile(r"^>\s+\*\*What's new:\*\*\s+(.+)$")
_BULLET_RE = re.compile(r"^- (.+)$")
_SOURCE_RE = re.compile(r"^\s+\[Source\]\(([^)]+)\)(.*)?$")
_META_RE = re.compile(r"^\*Generated (.+)\*$")

_TIER_CONFIG: dict[str, dict] = {
    "breakthrough": {"label": "BREAKTHROUGH", "color": "#f47067"},
    "notable":      {"label": "NOTABLE DEVELOPMENTS", "color": "#e3b341"},
    "worth_knowing": {"label": "WORTH KNOWING", "color": "#4dabf7"},
}


def _tier_for(score: float) -> str:
    if score >= 8.0:
        return "breakthrough"
    if score >= 7.0:
        return "notable"
    return "worth_knowing"


def _domain_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc
        return netloc.removeprefix("www.") or "source"
    except Exception:  # noqa: BLE001
        return "source"


def _parse_digest(text: str) -> dict:
    """Parse a digest .md file into structured data for HTML rendering."""
    result: dict = {"title": "", "meta": "", "overview": "", "sections": []}
    lines = text.split("\n")
    n = len(lines)
    i = 0

    # Title
    while i < n and not lines[i].startswith("# "):
        i += 1
    if i < n:
        result["title"] = lines[i][2:].strip()
        i += 1

    # Meta (italic *Generated ...*)
    while i < n:
        stripped = lines[i].strip()
        if stripped.startswith("*Generated") and stripped.endswith("*"):
            result["meta"] = stripped.strip("*").strip()
            i += 1
            break
        i += 1

    # Overview: lines between meta and the first --- or ##
    overview_lines: list[str] = []
    while i < n:
        stripped = lines[i].strip()
        if stripped == "---" or _SECTION_RE.match(lines[i]):
            if stripped == "---":
                i += 1
            break
        if stripped:
            overview_lines.append(stripped)
        i += 1
    result["overview"] = " ".join(overview_lines)

    # Sections
    current: dict | None = None
    current_bullet: str | None = None

    while i < n:
        line = lines[i]

        m = _SECTION_RE.match(line)
        if m:
            if current and current_bullet is not None:
                current["bullets"].append({"text": current_bullet, "url": "", "flags": []})
                current_bullet = None
            current = {
                "title": m.group(1).strip(),
                "badge": m.group(2),
                "score": float(m.group(3)),
                "callout": None,
                "bullets": [],
            }
            result["sections"].append(current)
            i += 1
            continue

        if current is None:
            i += 1
            continue

        m = _CALLOUT_RE.match(line)
        if m:
            current["callout"] = m.group(1).strip()
            i += 1
            continue

        m = _BULLET_RE.match(line)
        if m:
            if current_bullet is not None:
                current["bullets"].append({"text": current_bullet, "url": "", "flags": []})
            current_bullet = m.group(1).strip()
            i += 1
            continue

        m = _SOURCE_RE.match(line)
        if m and current_bullet is not None:
            flags_raw = m.group(2) or ""
            flags: list[str] = []
            if "[paywalled]" in flags_raw:
                flags.append("paywalled")
            stale_m = re.search(r"\[stale\s*[—-]\s*published\s*([\d-]+)\]", flags_raw)
            if stale_m:
                flags.append(f"stale:{stale_m.group(1)}")
            current["bullets"].append({"text": current_bullet, "url": m.group(1), "flags": flags})
            current_bullet = None
            i += 1
            continue

        i += 1

    if current and current_bullet is not None:
        current["bullets"].append({"text": current_bullet, "url": "", "flags": []})

    return result


def _render_email_html(markdown_text: str) -> str:
    """
    Render Markdown as a fully styled HTML email.

    Detects digest format and produces a dark-first mobile email.
    Falls back to a simple styled wrapper for non-digest content.
    """
    if not markdown_text.lstrip().startswith("# AI Newsletter Digest"):
        body = _markdown_to_html(markdown_text)
        h = _html_lib.escape
        return (
            "<!DOCTYPE html><html><head>"
            '<meta charset="UTF-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "</head>"
            '<body style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
            "max-width:600px;margin:0 auto;padding:20px;background:#0d1117;color:#e6edf3;\">"
            f"{body}</body></html>"
        )

    data = _parse_digest(markdown_text)
    h = _html_lib.escape

    # ── CSS (in-body; Gmail strips <head> styles but honors @media in body) ──
    css = """\
<style>
  a{color:#58a6ff}
  @media(prefers-color-scheme:light){
    .bg-page{background-color:#f0f2f5!important}
    .bg-card{background-color:#ffffff!important}
    .bg-inner{background-color:#f6f8fa!important}
    .bg-callout{background-color:#f0ebff!important}
    .text-primary{color:#1c2128!important}
    .text-secondary{color:#57606a!important}
    .text-muted{color:#6e7781!important}
    .border-subtle{border-color:#d0d7de!important}
  }
</style>"""

    # ── Header banner ─────────────────────────────────────────────────────────
    # Strip "AI Newsletter Digest — " prefix to get just the date range
    date_range = h(data["title"].replace("AI Newsletter Digest \u2014 ", ""))
    header = f"""\
<tr>
  <td style="background:linear-gradient(135deg,#0d1117 0%,#1a1a3e 55%,#2d1b6e 100%);
             padding:32px 24px 26px;border-radius:12px 12px 0 0;">
    <p style="margin:0 0 6px;color:#a78bfa;font-size:11px;font-weight:700;
              letter-spacing:3px;text-transform:uppercase;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      AI&nbsp;NEWSLETTER
    </p>
    <p style="margin:0 0 8px;color:#ffffff;font-size:26px;font-weight:700;line-height:1.2;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      Weekly Digest
    </p>
    <p style="margin:0;color:#c4b5fd;font-size:13px;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      {date_range}
    </p>
  </td>
</tr>"""

    # ── Meta bar ──────────────────────────────────────────────────────────────
    meta_row = ""
    if data["meta"]:
        meta_row = f"""\
<tr>
  <td class="bg-card border-subtle"
      style="background-color:#161b22;padding:10px 24px;border-bottom:1px solid #21262d;">
    <p class="text-secondary"
       style="margin:0;color:#8b949e;font-size:12px;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      {h(data['meta'])}
    </p>
  </td>
</tr>"""

    # ── Overview ──────────────────────────────────────────────────────────────
    overview_row = ""
    if data["overview"]:
        overview_row = f"""\
<tr>
  <td class="bg-card"
      style="background-color:#161b22;padding:20px 24px 16px;">
    <p class="text-primary"
       style="margin:0;color:#cdd9e5;font-size:14px;line-height:1.75;font-style:italic;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      {h(data['overview'])}
    </p>
  </td>
</tr>"""

    # ── Sections ──────────────────────────────────────────────────────────────
    sections_html = ""
    current_tier: str | None = None

    for section in data["sections"]:
        tier = _tier_for(section["score"])
        tier_cfg = _TIER_CONFIG[tier]
        tc = tier_cfg["color"]

        # Tier divider band on first section of each tier
        if tier != current_tier:
            current_tier = tier
            sections_html += f"""\
<tr>
  <td class="bg-card"
      style="background-color:#161b22;padding:20px 24px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="border-bottom:2px solid {tc};padding-bottom:8px;">
          <span style="color:{tc};font-size:10px;font-weight:700;
                       letter-spacing:3px;text-transform:uppercase;
                       font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
            &#9650;&nbsp;&nbsp;{tier_cfg['label']}
          </span>
        </td>
      </tr>
    </table>
  </td>
</tr>
"""

        # Callout block
        callout_html = ""
        if section["callout"]:
            callout_html = f"""\
<table width="100%" cellpadding="0" cellspacing="0" style="margin:10px 0 6px;">
  <tr>
    <td class="bg-callout"
        style="background-color:#1e1535;border-left:3px solid #a78bfa;
               padding:10px 12px;border-radius:0 6px 6px 0;">
      <p style="margin:0;font-size:13px;line-height:1.65;color:#cdd9e5;
                font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
        <span style="color:#a78bfa;font-weight:700;">What&#8217;s new:&nbsp;</span>{h(section['callout'])}
      </p>
    </td>
  </tr>
</table>"""

        # Bullet entries
        bullets_html = ""
        for bullet in section["bullets"]:
            flag_html = ""
            for flag in bullet["flags"]:
                if flag == "paywalled":
                    flag_html += ' <span style="color:#e3b341;font-size:11px;font-weight:600;">[paywalled]</span>'
                elif flag.startswith("stale:"):
                    flag_html += f' <span style="color:#8b949e;font-size:11px;">[stale: {h(flag[6:])}]</span>'

            source_html = ""
            if bullet["url"]:
                domain = h(_domain_from_url(bullet["url"]))
                url = h(bullet["url"])
                source_html = (
                    f'<p style="margin:3px 0 10px;font-size:12px;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;">'
                    f'<a href="{url}" style="color:#58a6ff;text-decoration:none;">&#8599;&nbsp;{domain}</a>'
                    f"{flag_html}</p>"
                )
            elif flag_html:
                source_html = f'<p style="margin:0 0 10px;font-size:12px;">{flag_html}</p>'

            bullets_html += (
                f'<p class="text-primary" style="margin:10px 0 3px;font-size:13px;line-height:1.65;'
                f"color:#cdd9e5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;\">"
                f"{h(bullet['text'])}</p>"
                f"{source_html}"
            )

        sections_html += f"""\
<tr>
  <td class="bg-card" style="background-color:#161b22;padding:4px 16px 4px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-left:3px solid {tc};background-color:#0d1117;
                  border-radius:0 8px 8px 0;margin-bottom:8px;">
      <tr>
        <td style="padding:14px 14px 10px;">
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:4px;">
            <tr>
              <td class="text-primary"
                  style="color:#e6edf3;font-size:15px;font-weight:600;line-height:1.4;
                         font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
                {h(section['title'])}
              </td>
              <td width="1" nowrap="" style="padding-left:8px;vertical-align:top;">
                <span style="display:inline-block;background:{tc};color:#0d1117;
                             font-size:10px;font-weight:800;padding:3px 8px;
                             border-radius:12px;white-space:nowrap;
                             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
                  {section['score']:.1f}
                </span>
              </td>
            </tr>
          </table>
          {callout_html}
          {bullets_html}
        </td>
      </tr>
    </table>
  </td>
</tr>
"""

    # ── Footer ────────────────────────────────────────────────────────────────
    footer = """\
<tr>
  <td style="background-color:#0d1117;padding:20px 24px;
             border-radius:0 0 12px 12px;border-top:1px solid #21262d;">
    <p style="margin:0;color:#484f58;font-size:11px;text-align:center;
              font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
      Generated by AI Newsletter Digest pipeline
    </p>
  </td>
</tr>"""

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <meta name="color-scheme" content="dark light">
  <meta name="supported-color-schemes" content="dark light">
</head>
<body class="bg-page" style="margin:0;padding:0;background-color:#0d1117;">
{css}
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       class="bg-page" style="background-color:#0d1117;min-height:100vh;">
  <tr>
    <td align="center" style="padding:20px 12px 40px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;">
        {header}
        {meta_row}
        {overview_row}
        {sections_html}
        {footer}
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _build_message(recipient: str, subject: str, markdown_body: str) -> dict:
    """Build a Gmail API message dict from a Markdown body."""
    html_body = _render_email_html(markdown_body)

    msg = MIMEMultipart("alternative")
    msg["To"] = recipient
    msg["From"] = "me"
    msg["Subject"] = subject

    msg.attach(MIMEText(markdown_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return {"raw": raw}


def _subject_from_path(path: Path) -> str:
    """Derive an email subject from the digest filename.

    E.g. digest_2026-05-05_2026-05-12.md → 'AI Digest: 2026-05-05 → 2026-05-12'
         digest_all-time_2026-05-12.md   → 'AI Digest: all-time → 2026-05-12'
    """
    stem = path.stem  # e.g. 'digest_2026-05-05_2026-05-12'
    parts = stem.split("_", 1)
    if len(parts) == 2:
        date_part = parts[1].replace("_", " \u2192 ")
        return f"AI Digest: {date_part}"
    return f"AI Digest: {stem}"


def send_digest_file(path: Path, recipient: str) -> None:
    """Send a single digest Markdown file as an email.

    Args:
        path: Path to the .md digest file.
        recipient: Destination email address.

    Raises:
        ValueError: If recipient is empty.
        FileNotFoundError: If the digest file does not exist.
    """
    if not recipient:
        raise ValueError(
            "digest_recipient_email is not set. Add DIGEST_RECIPIENT_EMAIL=you@example.com to .env"
        )
    if not path.exists():
        raise FileNotFoundError(f"Digest file not found: {path}")

    subject = _subject_from_path(path)
    body = path.read_text(encoding="utf-8")

    creds = _get_send_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    message = _build_message(recipient, subject, body)
    service.users().messages().send(userId="me", body=message).execute()
    log.info("Sent %s → %s", path.name, recipient)


def send_all_digests(digest_dir: Path | None = None, recipient: str | None = None) -> int:
    """Send all .md digest files in digest_dir, sorted oldest-first by filename.

    Returns the number of emails sent.
    """
    digest_dir = digest_dir or Path("output")
    recipient = recipient or settings.digest_recipient_email

    if not recipient:
        raise ValueError(
            "digest_recipient_email is not set. Add DIGEST_RECIPIENT_EMAIL=you@example.com to .env"
        )

    files = sorted(
        f for f in digest_dir.glob("*.md") if f.name != ".gitkeep"
    )
    if not files:
        log.info("No digest files found in %s", digest_dir)
        return 0

    creds = _get_send_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    sent = 0
    for path in files:
        subject = _subject_from_path(path)
        body = path.read_text(encoding="utf-8")
        message = _build_message(recipient, subject, body)
        service.users().messages().send(userId="me", body=message).execute()
        log.info("Sent %s → %s  (%d / %d)", path.name, recipient, sent + 1, len(files))
        sent += 1
        if sent < len(files):
            time.sleep(_SEND_DELAY_S)

    return sent
