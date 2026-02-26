#!/usr/bin/env python3
import os
import json
import time
import requests
import threading
from pathlib import Path
import yaml

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config" / "profiles.yaml"
CONTROLS_PATH = BASE / "data" / "radar_controls.json"

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def load_config():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def ensure_controls(config):
    default = {"profiles": {}}
    for p_name, p_data in config.get("profiles", {}).items():
        default["profiles"][p_name] = {
            "enabled": True,
            "report_enabled": True,
            "jobs": {j["slug"]: True for j in p_data.get("jobs", [])}
        }

    if not CONTROLS_PATH.exists():
        CONTROLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTROLS_PATH.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return default

    try:
        current = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    except Exception:
        current = {"profiles": {}}

    for p_name, p_cfg in default["profiles"].items():
        current.setdefault("profiles", {}).setdefault(p_name, p_cfg)
        current["profiles"][p_name].setdefault("enabled", True)
        current["profiles"][p_name].setdefault("report_enabled", True)
        current["profiles"][p_name].setdefault("jobs", {})
        for slug, v in p_cfg["jobs"].items():
            current["profiles"][p_name]["jobs"].setdefault(slug, v)

    CONTROLS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return current


def save_controls(controls):
    CONTROLS_PATH.write_text(json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8")


def keyboard(config, controls):
    rows = []
    for p_name, p_data in config.get("profiles", {}).items():
        p_state = controls["profiles"].get(p_name, {})
        pe = "✅" if p_state.get("enabled", True) else "❌"
        re = "📣" if p_state.get("report_enabled", True) else "🔕"
        rows.append([
            {"text": f"{pe} {p_data.get('name', p_name)}", "callback_data": f"profile|{p_name}"},
            {"text": f"{re} Отчёт", "callback_data": f"report|{p_name}"}
        ])
        for job in p_data.get("jobs", []):
            on = p_state.get("jobs", {}).get(job["slug"], True)
            icon = "✅" if on else "❌"
            rows.append([
                {"text": f"{icon} {job.get('name', job['slug'])}", "callback_data": f"job|{p_name}|{job['slug']}"}
            ])
    rows.append([
        {"text": "🔄 Обновить", "callback_data": "refresh"}
    ])
    return rows


def render_text(config, controls):
    lines = ["<b>HR Radar — настройки</b>"]
    for p_name, p_data in config.get("profiles", {}).items():
        st = controls["profiles"].get(p_name, {})
        lines.append(f"\n<b>{p_data.get('name', p_name)}</b> {'✅' if st.get('enabled', True) else '❌'} | Отчёт {'ON' if st.get('report_enabled', True) else 'OFF'}")
        for job in p_data.get("jobs", []):
            on = st.get("jobs", {}).get(job["slug"], True)
            lines.append(f"• {'✅' if on else '❌'} {job.get('name', job['slug'])}")
    lines.append("\nНажми кнопки ниже, чтобы включать/выключать.")
    return "\n".join(lines)


def tg(method, payload):
    return requests.post(f"{API}/{method}", json=payload, timeout=30).json()


def setup_telegram_menu_button():
    # Синяя кнопка Menu в левом нижнем углу + список команд
    tg("setMyCommands", {
        "commands": [
            {"command": "radar", "description": "Меню HR Radar"},
            {"command": "menu", "description": "Открыть меню"},
            {"command": "start", "description": "Старт"}
        ]
    })
    tg("setChatMenuButton", {
        "chat_id": int(ADMIN_CHAT_ID),
        "menu_button": {"type": "commands"}
    })


def is_admin(update):
    msg = update.get("message") or update.get("callback_query", {}).get("message", {})
    chat = str((msg.get("chat") or {}).get("id", ""))
    return chat == ADMIN_CHAT_ID


def send_or_edit_menu(config, controls, chat_id, message_id=None):
    payload = {
        "chat_id": chat_id,
        "text": render_text(config, controls),
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": keyboard(config, controls)}
    }
    if message_id:
        payload["message_id"] = message_id
        tg("editMessageText", payload)
    else:
        tg("sendMessage", payload)


def handle_update(update, config, controls):
    if not is_admin(update):
        return controls

    msg = update.get("message")
    if msg:
        text = (msg.get("text") or "").strip().lower()
        if text in {"/radar", "/menu", "/start"}:
            send_or_edit_menu(config, controls, msg["chat"]["id"])
        return controls

    cq = update.get("callback_query")
    if not cq:
        return controls

    data = cq.get("data", "")
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]

    parts = data.split("|")
    if parts[0] == "profile" and len(parts) == 2:
        p = parts[1]
        cur = controls["profiles"][p].get("enabled", True)
        controls["profiles"][p]["enabled"] = not cur
    elif parts[0] == "report" and len(parts) == 2:
        p = parts[1]
        cur = controls["profiles"][p].get("report_enabled", True)
        controls["profiles"][p]["report_enabled"] = not cur
    elif parts[0] == "job" and len(parts) == 3:
        p, slug = parts[1], parts[2]
        cur = controls["profiles"][p]["jobs"].get(slug, True)
        controls["profiles"][p]["jobs"][slug] = not cur

    # === FriendWork import: pick vacancy ===
    if parts[0] == "fw_pick" and len(parts) >= 2:
        batch_id = parts[1]
        batch_file = BASE / "data" / f"batch_{batch_id}.json"
        if not batch_file.exists():
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "❌ Пакет кандидатов не найден (устарел?)", "show_alert": True})
            return controls
        
        # Load open vacancies from FriendWork
        from fw_import import get_fw_open_vacancies
        vacancies = get_fw_open_vacancies()
        if not vacancies:
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "❌ Нет открытых вакансий в FriendWork", "show_alert": True})
            return controls
        
        batch_data = json.loads(batch_file.read_text())
        count = len(batch_data.get("resume_ids", []))
        
        # Show vacancy picker — split into pages of 15 if needed
        page = 0
        if len(parts) == 3:
            try:
                page = int(parts[2])
            except:
                page = 0
        
        PAGE_SIZE = 15
        total_pages = (len(vacancies) + PAGE_SIZE - 1) // PAGE_SIZE
        start = page * PAGE_SIZE
        page_vacancies = vacancies[start:start + PAGE_SIZE]
        
        buttons = []
        for v in page_vacancies:
            name = v['name'][:35]
            buttons.append([{
                "text": f"📋 {name}",
                "callback_data": f"fw_go|{batch_id}|{v['id']}"
            }])
        
        # Navigation buttons
        nav_row = []
        if page > 0:
            nav_row.append({"text": "⬅️ Назад", "callback_data": f"fw_pick|{batch_id}|{page-1}"})
        if page < total_pages - 1:
            nav_row.append({"text": "Ещё ➡️", "callback_data": f"fw_pick|{batch_id}|{page+1}"})
        if nav_row:
            buttons.append(nav_row)
        buttons.append([{"text": "❌ Отмена", "callback_data": "fw_cancel"}])
        
        page_text = f" (стр. {page+1}/{total_pages})" if total_pages > 1 else ""
        
        # Edit existing message or send new
        if cq.get("message", {}).get("text", "").startswith("📥"):
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f"📥 <b>Импорт {count} кандидатов в FriendWork</b>{page_text}\n\nВыбери вакансию:",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": buttons}
            })
        else:
            tg("sendMessage", {
                "chat_id": chat_id,
                "text": f"📥 <b>Импорт {count} кандидатов в FriendWork</b>{page_text}\n\nВыбери вакансию:",
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": buttons}
            })
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return controls
    
    # === FriendWork import: execute ===
    if parts[0] == "fw_go" and len(parts) == 3:
        batch_id = parts[1]
        job_id = int(parts[2])
        batch_file = BASE / "data" / f"batch_{batch_id}.json"
        
        if not batch_file.exists():
            tg("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "❌ Пакет не найден", "show_alert": True})
            return controls
        
        batch_data = json.loads(batch_file.read_text())
        resume_ids = batch_data.get("resume_ids", [])
        
        # Update button to show progress
        tg("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": f"⏳ Импортирую {len(resume_ids)} кандидатов... Это займёт ~{len(resume_ids) * 3} сек.",
            "parse_mode": "HTML"
        })
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        
        # Run import in background thread
        def do_import():
            from fw_import import import_hh_to_fw
            results = []
            for rid in resume_ids:
                try:
                    res = import_hh_to_fw(rid, job_id)
                    results.append(res)
                except Exception as e:
                    results.append({"ok": False, "candidate_id": None, "message": str(e)})
                time.sleep(1.5)  # rate limit
            
            ok_count = sum(1 for r in results if r["ok"])
            dupe_count = sum(1 for r in results if "Дубликат" in r.get("message", ""))
            fail_count = len(results) - ok_count - dupe_count
            
            lines = [f"✅ <b>Импорт завершён</b>"]
            lines.append(f"Успешно: {ok_count} | Дубли: {dupe_count} | Ошибки: {fail_count}")
            
            for i, (rid, res) in enumerate(zip(resume_ids, results), 1):
                if res["ok"]:
                    cid = res.get("candidate_id", "")
                    fw_url = f"https://app.friend.work/Candidate/Profile/{cid}" if cid else ""
                    lines.append(f'✅ {i}. <a href="{fw_url}">Открыть в FW</a> — Импортирован')
                elif "Дубликат" in res.get("message", ""):
                    # Extract duplicate ID and link
                    import re
                    dupe_match = re.search(r'\[(\d+)\]', res["message"])
                    if dupe_match:
                        dupe_id = dupe_match.group(1)
                        fw_url = f"https://app.friend.work/Candidate/Profile/{dupe_id}"
                        lines.append(f'🔄 {i}. <a href="{fw_url}">Уже в FW (ID:{dupe_id})</a>')
                    else:
                        lines.append(f"🔄 {i}. {rid[:12]}... — {res['message']}")
                else:
                    lines.append(f"❌ {i}. {rid[:12]}... — {res['message']}")
            
            tg("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML"
            })
            
            # Cleanup batch file
            try:
                batch_file.unlink()
            except:
                pass
        
        threading.Thread(target=do_import, daemon=True).start()
        return controls
    
    # === FriendWork import: cancel ===
    if parts[0] == "fw_cancel":
        tg("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": "❌ Импорт отменён",
        })
        tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return controls

    save_controls(controls)
    send_or_edit_menu(config, controls, chat_id, message_id)
    tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
    return controls


def main():
    config = load_config()
    controls = ensure_controls(config)
    setup_telegram_menu_button()
    offset = 0
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"timeout": 30, "offset": offset}, timeout=40)
            data = r.json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                controls = handle_update(upd, config, controls)
        except Exception:
            time.sleep(2)


if __name__ == "__main__":
    main()
