"""Telegram notifications."""
import requests
from config import TG_BOT_TOKEN, TG_CHAT_ID

API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


def send_message(text: str, parse_mode: str = "HTML"):
    """Send message to configured Telegram chat."""
    try:
        resp = requests.post(
            f"{API}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[notifier] TG send failed: {e}")


def notify_qualified_lead(lead_id: int, title: str, result: dict, source_type: str, extras: dict | None = None):
    """Send notification about a qualified lead."""
    cat = result.get("category", "?")
    confidence = result.get("confidence", 0)
    summary = result.get("summary", "")
    reason = result.get("reason", "")
    qualified = result.get("qualified")

    icon = {"call": "📞", "form": "📝", "email": "📧"}.get(source_type, "📨")

    if cat == "needs_review":
        header = f"🔍 <b>Требует проверки — лид #{lead_id}</b>"
    else:
        header = f"{icon} <b>Новый лид #{lead_id}</b>"

    lines = [
        header,
        f"<b>Категория:</b> {cat} (уверенность: {confidence:.0%})",
        "",
        f"<b>Тема:</b> {title}",
    ]

    if extras:
        if extras.get("name"):
            lines.append(f"<b>Контакт:</b> {extras['name']}")
        if extras.get("company"):
            lines.append(f"<b>Компания:</b> {extras['company']}")
        if extras.get("phone"):
            lines.append(f"<b>Телефон:</b> {extras['phone']}")
        if extras.get("email"):
            lines.append(f"<b>Email:</b> {extras['email']}")

    lines.append("")
    if summary:
        lines.append(f"📋 {summary}")
    if reason:
        lines.append(f"\n💡 {reason}")

    lines.append(f"\n🔗 https://btl-piter.bitrix24.ru/crm/lead/details/{lead_id}/")

    send_message("\n".join(lines))


def notify_call(lead_id: int, phone: str, transcript: str | None, duration: int = 0, missed: bool = False):
    """Send notification about an incoming call."""
    if missed:
        text = (
            f"📞 <b>Пропущенный звонок</b>\n"
            f"Номер: {phone}\n"
            f"🔗 https://btl-piter.bitrix24.ru/crm/lead/details/{lead_id}/"
        )
    else:
        dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""
        text = f"📞 <b>Входящий звонок</b> ({dur_str})\nНомер: {phone}\n"
        if transcript:
            t = transcript[:1500] + "..." if len(transcript) > 1500 else transcript
            text += f"\n<b>Транскрипт:</b>\n<i>{t}</i>\n"
        text += f"\n🔗 https://btl-piter.bitrix24.ru/crm/lead/details/{lead_id}/"

    send_message(text)


def notify_form(lead_id: int, title: str, name: str = "", company: str = "", phone: str = ""):
    """Send notification about a CRM form submission."""
    lines = [
        f"📝 <b>Заполнена форма на сайте</b>",
        f"<b>Лид #{lead_id}</b>: {title}",
    ]
    if name:
        lines.append(f"<b>Имя:</b> {name}")
    if company:
        lines.append(f"<b>Компания:</b> {company}")
    if phone:
        lines.append(f"<b>Телефон:</b> {phone}")
    lines.append(f"\n🔗 https://btl-piter.bitrix24.ru/crm/lead/details/{lead_id}/")
    send_message("\n".join(lines))
