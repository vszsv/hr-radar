#!/usr/bin/env python3
import os, re, json, sqlite3, imaplib, email, datetime
from pathlib import Path
from typing import Dict, List
import requests

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("HR_RADAR_DB", BASE / "data" / "radar.db"))
PROMPT_PATH = Path(os.getenv("HR_RADAR_PROMPT", BASE / "prompt_account_director.txt"))

IMAP_HOST = os.getenv("IMAP_HOST", "imap.yandex.ru")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
IMAP_USER = os.environ["IMAP_USER"]
IMAP_PASS = os.environ["IMAP_PASS"]

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2")


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_links (
      normalized_link TEXT PRIMARY KEY,
      first_seen_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started_at TEXT NOT NULL,
      total_candidates INTEGER NOT NULL,
      relevant_candidates INTEGER NOT NULL,
      notes TEXT
    )""")
    conn.commit()
    return conn


def normalize_link(link: str) -> str:
    return (link or "").split("?")[0].strip()


def parse_candidates_from_html(html: str, subject: str) -> List[Dict]:
    out = []
    text = html or ""
    # Более устойчивый парсинг: ищем anchor с resume-ссылкой и вытаскиваем текст ссылки.
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']*hh\.ru/resume/[^"\']*)["\'][^>]*>([\s\S]*?)</a>', text, flags=re.I):
        link = m.group(1)
        anchor_text = m.group(2)
        title = re.sub(r"<[^>]+>", "", anchor_text).replace("&nbsp;", " ")
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue

        # Берём небольшой контекст вокруг ссылки — там обычно есть зарплата/последняя работа.
        s, e = m.span()
        block = text[max(0, s - 1200): min(len(text), e + 1200)]

        last_job = ""
        jm = re.search(r"Последнее место работы[:\s]*([^<]+)", block, flags=re.I)
        if jm:
            last_job = re.sub(r"\s+", " ", jm.group(1).replace("&nbsp;", " ")).strip()

        salary = ""
        sm = re.search(r"Уровень дохода[:\s]*([^<]+)", block, flags=re.I)
        if sm:
            salary = re.sub(r"\s+", " ", sm.group(1).replace("&nbsp;", " ")).strip()

        description = f"Автопоиск: {subject}"
        if last_job:
            description += f" | Последнее место: {last_job}"
        if salary:
            description += f" | Доход: {salary}"

        out.append({
            "title": title,
            "link": link,
            "normalized_link": normalize_link(link),
            "lastJob": last_job,
            "salary": salary,
            "description": description,
        })
    return out


def fetch_today_candidates() -> List[Dict]:
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(IMAP_USER, IMAP_PASS)
    mail.select("INBOX")
    today = datetime.datetime.now(datetime.UTC).strftime("%d-%b-%Y")
    status, data = mail.search(None, f"SINCE {today}")
    ids = data[0].split() if data and data[0] else []

    out = []
    for mid in ids:
        st, msg_data = mail.fetch(mid, "(RFC822)")
        if st != "OK" or not msg_data or not msg_data[0]:
            continue
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", "Без темы"))))

        def _part_to_text(part):
            payload = part.get_payload(decode=True)
            if payload is None:
                raw = part.get_payload()
                return raw if isinstance(raw, str) else ""
            return payload.decode(part.get_content_charset() or "utf-8", errors="ignore")

        html = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    html = _part_to_text(part)
                    if html:
                        break
            if not html:
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        html = _part_to_text(part)
                        if html:
                            break
        else:
            html = _part_to_text(msg)

        out.extend(parse_candidates_from_html(html, subject))

    mail.logout()
    return out


def fallback_score(c: Dict) -> Dict:
    t = (c.get("title", "") + " " + c.get("lastJob", "")).lower()
    yes_keywords = ["account director", "client service director", "head of client service", "руководитель клиентского сервиса", "group account"]
    near_keywords = ["senior account", "key account", "account manager", "team lead", "project director"]
    bad_keywords = ["pr", "influencer", "seo", "smm", "дизайнер", "копирайтер", "бухгалтер"]
    if any(k in t for k in yes_keywords):
        return {"relevant": True, "fit_type": "target", "confidence": 0.88, "reason": "target keyword"}
    if any(k in t for k in bad_keywords):
        return {"relevant": False, "fit_type": "not_fit", "confidence": 0.75, "reason": "excluded domain"}
    if any(k in t for k in near_keywords):
        return {"relevant": True, "fit_type": "near_target", "confidence": 0.62, "reason": "near target keyword"}
    return {"relevant": False, "fit_type": "not_fit", "confidence": 0.55, "reason": "insufficient signal"}


def llm_score(candidates: List[Dict]) -> List[Dict]:
    if not OPENAI_API_KEY:
        return [fallback_score(c) for c in candidates]

    system_prompt = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else ""
    schema_hint = (
        "Верни только JSON-массив объектов в порядке входа: "
        "[{\"index\":1,\"relevant\":true/false,\"fit_type\":\"target|near_target|not_fit\",\"confidence\":0..1,\"suggested_action\":\"...\"}]"
    )

    def score_batch(batch: List[Dict]) -> List[Dict]:
        listing = []
        for i, c in enumerate(batch, start=1):
            listing.append(f"[{i}] {c['title']} | {c['description']}")
        user_text = "КАНДИДАТЫ НА ВХОД:\n\n" + "\n".join(listing)
        payload = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt + "\n\n" + schema_hint},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.1,
        }
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=60,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        m = re.search(r"\[[\s\S]*\]", content)
        arr = json.loads(m.group(0) if m else content)
        out = []
        for i, c in enumerate(batch, start=1):
            a = next((x for x in arr if x.get("index") == i), arr[i-1] if i-1 < len(arr) else {})
            out.append({
                "relevant": bool(a.get("relevant")),
                "fit_type": a.get("fit_type", "not_fit"),
                "confidence": float(a.get("confidence", 0)),
                "reason": a.get("suggested_action", ""),
            })
        return out

    BATCH_SIZE = 15
    all_scores: List[Dict] = []
    total_batches = (len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(candidates), BATCH_SIZE):
        batch_num = i // BATCH_SIZE + 1
        batch = candidates[i:i + BATCH_SIZE]
        print(f"[LLM] Batch {batch_num}/{total_batches} ({len(batch)} candidates)...", flush=True)
        try:
            all_scores.extend(score_batch(batch))
            print(f"[LLM] Batch {batch_num} done.", flush=True)
        except Exception as e:
            print(f"[LLM] Batch {batch_num} FAILED: {e}", flush=True)
            # Не валим весь ран, если модель/сеть упала на одном батче.
            all_scores.extend([fallback_score(c) for c in batch])
    return all_scores


def send_tg(text: str):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    ).raise_for_status()


def main():
    conn = db()
    started = datetime.datetime.now(datetime.UTC).isoformat()
    seen = {row[0] for row in conn.execute("SELECT normalized_link FROM seen_links")}
    raw = fetch_today_candidates()

    uniq = []
    in_run = set()
    for c in raw:
        nl = c["normalized_link"]
        if not nl or nl in seen or nl in in_run:
            continue
        uniq.append(c)
        in_run.add(nl)

    if not uniq:
        send_tg("📭 <b>HR Radar</b>\nНовых кандидатов за сегодня не найдено.")
        conn.execute("INSERT INTO runs(started_at,total_candidates,relevant_candidates,notes) VALUES(?,?,?,?)", (started, 0, 0, "no candidates"))
        conn.commit()
        return

    scores = llm_score(uniq)
    relevant = []
    for c, s in zip(uniq, scores):
        c.update(s)
        if c["relevant"]:
            relevant.append(c)

    for c in uniq:
        conn.execute("INSERT OR IGNORE INTO seen_links(normalized_link,first_seen_at) VALUES(?,?)", (c["normalized_link"], started))
    conn.execute(
        "INSERT INTO runs(started_at,total_candidates,relevant_candidates,notes) VALUES(?,?,?,?)",
        (started, len(uniq), len(relevant), "ok"),
    )
    conn.commit()

    if not relevant:
        send_tg(
            "📭 <b>HR Radar</b>\n"
            f"Проанализировано: {len(uniq)}\n"
            "Подходящих на Account Director не найдено."
        )
        return

    relevant.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    fit_type_label = {
        "target": "🎯 Целевой",
        "near_target": "🟡 Близкий к целевому",
        "not_fit": "⚪ Нецелевой",
    }

    targets = [c for c in relevant if c.get("fit_type") == "target" and c.get("confidence", 0) >= 0.75]
    doubtful = [c for c in relevant if c not in targets]

    def send_list(title: str, rows: List[Dict], footer: str):
        if not rows:
            return
        chunks, cur = [], f"<b>{title}</b>\n\n"
        for i, c in enumerate(rows, start=1):
            conf = round(c.get("confidence", 0) * 100)
            label = fit_type_label.get(c.get("fit_type", "target"), "⚪ Нецелевой")
            block = (
                f"<b>{i}. {c['title']}</b>\n"
                f"{label} | {conf}%\n"
                f"🏢 {c.get('lastJob','-')}\n"
                f"💰 {c.get('salary','-')}\n"
                f"🔗 <a href=\"{c['link']}\">Резюме на HH</a>\n"
                "─────────────────────\n"
            )
            if len(cur) + len(block) > 3800:
                chunks.append(cur)
                cur = block
            else:
                cur += block
        chunks.append(cur + f"\n<i>{footer}</i>")
        for t in chunks:
            send_tg(t)

    send_list("🔥 Подборка кандидатов (целевые)", targets, f"Целевых: {len(targets)} из {len(uniq)}")
    send_list("⚠️ Сомнительные кандидаты (на проверку)", doubtful, f"Сомнительных: {len(doubtful)} из {len(uniq)}")


if __name__ == "__main__":
    main()
