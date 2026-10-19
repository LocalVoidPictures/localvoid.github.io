"""Alert delivery. Every channel is optional and enabled by its environment variables."""
from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

import requests

log = logging.getLogger(__name__)

KIND_LABEL = {
    "buy_now": "Buy-it-now deal",
    "ending_soon": "Ending within 24h, still cheap",
    "final_call": "Final call, still cheap",
    "price_drop": "Price dropped into deal range",
}


@dataclass
class Alert:
    kind: str
    auction_id: str
    url: str
    title: str
    price: int
    hours_left: float
    market: int | None
    ceiling: int
    verdict: str
    confidence: float
    summary: str
    hardware: str
    red_flags: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    reasoning: str = ""
    models: list[str] = field(default_factory=list)
    image: str | None = None

    @property
    def pct(self) -> str:
        return f"{self.price / self.market:.0%}" if self.market else "?"

    def headline(self) -> str:
        tag = "⚠️ " if self.verdict == "uncertain" else "🔥 " if self.verdict == "strong_deal" else ""
        return f"{tag}¥{self.price:,} ({self.pct} of market) · {self.title[:80]}"

    def body_markdown(self) -> str:
        lines = [
            f"**{KIND_LABEL.get(self.kind, self.kind)}** · verdict **{self.verdict}** "
            f"(confidence {self.confidence:.0%}) · [open listing]({self.url})",
            "",
            f"> {self.summary}",
            "",
            "| | |", "|---|---|",
            f"| What it is | {self.hardware} |",
            f"| All-in price now | ¥{self.price:,} |",
            f"| Est. market value | {'¥%s' % format(self.market, ',') if self.market else '?'} |",
            f"| Good-deal ceiling | ¥{self.ceiling:,} |",
            f"| Time left | {self.hours_left:.1f} h |",
            f"| Analyzed by | {', '.join(self.models)} |",
        ]
        if self.red_flags:
            lines += ["", "**Red flags**", *[f"- {f}" for f in self.red_flags]]
        if self.positives:
            lines += ["", "**Positive signals**", *[f"- {p}" for p in self.positives]]
        if self.reasoning:
            lines += ["", "<details><summary>Reasoning</summary>", "", self.reasoning, "", "</details>"]
        if self.image:
            lines += ["", f"![photo]({self.image})"]
        return "\n".join(lines)

    def body_text(self) -> str:
        flags = ("\nRed flags: " + "; ".join(self.red_flags)) if self.red_flags else ""
        return (f"{KIND_LABEL.get(self.kind, self.kind)} · {self.verdict}\n{self.summary}\n"
                f"Now ¥{self.price:,} · market ~{self.market or '?'} · ceiling ¥{self.ceiling:,} · "
                f"{self.hours_left:.1f}h left{flags}\n{self.url}")


def send(alert: Alert) -> list[str]:
    sent = []
    for name, fn in (("github", _github), ("ntfy", _ntfy), ("discord", _discord), ("email", _email)):
        try:
            if fn(alert):
                sent.append(name)
        except Exception as e:  # one broken channel must not block the others
            log.error("notify via %s failed: %s", name, e)
    if not sent:
        log.warning("no notification channel configured; alert only logged:\n%s", alert.body_text())
    return sent


def _github(a: Alert) -> bool:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo or os.environ.get("DISABLE_GITHUB_ISSUES"):
        return False
    labels = ["deal", a.kind.replace("_", "-"), a.verdict.replace("_", "-")]
    r = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": a.headline(), "body": a.body_markdown(), "labels": labels},
        timeout=30,
    )
    r.raise_for_status()
    return True


def _ntfy(a: Alert) -> bool:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    r = requests.post(server, headers=headers, timeout=30, json={
        "topic": topic, "title": a.headline(), "message": a.body_text(), "click": a.url,
        "priority": 5 if a.verdict == "strong_deal" else 4, "tags": ["moneybag"],
        **({"attach": a.image} if a.image else {}),
    })
    r.raise_for_status()
    return True


def _discord(a: Alert) -> bool:
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        return False
    embed = {"title": a.headline()[:256], "url": a.url, "description": a.body_text()[:4000]}
    if a.image:
        embed["thumbnail"] = {"url": a.image}
    requests.post(hook, json={"embeds": [embed]}, timeout=30).raise_for_status()
    return True


def _email(a: Alert) -> bool:
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_EMAIL_TO")
    if not host or not to:
        return False
    msg = EmailMessage()
    msg["Subject"] = a.headline()
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or to
    msg["To"] = to
    msg.set_content(a.body_text())
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
        s.starttls()
        if os.environ.get("SMTP_USER"):
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        s.send_message(msg)
    return True
