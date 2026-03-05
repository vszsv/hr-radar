#!/usr/bin/env python3
"""Manual autoflow run — uses today's primary screening results."""
import json, datetime, sqlite3, requests, sys, os
from pathlib import Path

BASE = Path('/opt/hr-radar')
sys.path.insert(0, str(BASE))

from run_multi_radar import (
    run_autoflow_deep_scoring, run_autoflow_fw_import,
    load_config, parse_profile_config
)

config = load_config()
panel_config = json.loads((BASE / "data" / "panel_config.json").read_text())
controls = json.loads((BASE / "data" / "radar_controls.json").read_text())

for profile_name in ['event_agencies', 'btl_agencies']:
    profile_controls = controls.get("profiles", {}).get(profile_name, {})
    autoflow_cfg = profile_controls.get("autoflow", {})
    
    profile_data = config['profiles'][profile_name]
    profile = parse_profile_config(profile_name, profile_data, config['common'])
    openai_config = config['common']['openai']
    
    db_path = BASE / "data" / f"{profile_name}.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    
    for job in profile.jobs:
        af = autoflow_cfg.get(job.slug, {})
        if not af.get("enabled", False):
            continue
        
        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        cur = conn.cursor()
        cur.execute(
            "SELECT normalized_link FROM scored_candidates WHERE date(run_date)=? AND job_slug=? AND relevant=1",
            (today, job.slug)
        )
        links = [row['normalized_link'] for row in cur.fetchall() if row['normalized_link']]
        
        if not links:
            print(f"⚡ {profile_name}/{job.slug}: no relevant candidates")
            continue
        
        print(f"⚡ Autoflow [{profile_name}/{job.name}]: {len(links)} кандидатов")
        
        thresholds = af.get("thresholds", {})
        t_approve = thresholds.get("approve", panel_config.get("score_threshold_approve", 7))
        t_reject = thresholds.get("reject", panel_config.get("score_threshold_reject", 5))
        print(f"  Thresholds: approve≥{t_approve}, reject<{t_reject}")
        
        deep_results = []
        if af.get("deep_scoring", True):
            print(f"  🔍 Deep scoring {len(links)} candidates...")
            deep_results = run_autoflow_deep_scoring(links, job, openai_config, t_approve, t_reject, panel_config, profile_name)
            
            for r in deep_results:
                print(f"    {r.get('candidateName','?')}: score={r.get('score',0)} → {r.get('fw_status','?')}")
            
            approved = [r for r in deep_results if r.get("score", 0) >= t_approve]
            reviewed = [r for r in deep_results if t_reject <= r.get("score", 0) < t_approve]
            rejected = [r for r in deep_results if 0 < r.get("score", 0) < t_reject]
            print(f"  📊 Results: ✅{len(approved)} 👁{len(reviewed)} ❌{len(rejected)}")
        
        routes = panel_config.get("routes", {})
        route_key = f"{profile_name}.{job.slug}"
        fw_vacancy_id = routes.get(route_key)
        
        if fw_vacancy_id and deep_results:
            print(f"  📤 Importing to FW vacancy {fw_vacancy_id}...")
            imported, dupes, errors = run_autoflow_fw_import(
                deep_results, int(fw_vacancy_id), af, t_approve, t_reject,
                openai_config.get("model", "gpt-4o")
            )
            print(f"  📤 FW: {imported} new, {dupes} dupes, {errors} errors")
            
            approved_count = len([r for r in deep_results if r.get("score", 0) >= t_approve])
            reviewed_count = len([r for r in deep_results if t_reject <= r.get("score", 0) < t_approve])
            rejected_count = len([r for r in deep_results if 0 < r.get("score", 0) < t_reject])
            
            summary = (
                f"⚡ <b>Autoflow: {job.emoji} {job.name}</b>\n\n"
                f"📋 Первичный отбор: {len(links)} релевантных\n"
                f"🤖 Глубокий скоринг: ✅{approved_count} 👁{reviewed_count} ❌{rejected_count}\n"
                f"📤 Импорт в FW: {imported} новых, {dupes} дубликатов"
            )
            requests.post(
                f"https://api.telegram.org/bot{profile.telegram_token}/sendMessage",
                json={"chat_id": profile.telegram_chat, "text": summary, "parse_mode": "HTML"},
                timeout=30
            )
            print(f"  📱 Report sent!")
            
            af["last_run"] = {
                "date": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M"),
                "primary": len(links), "approved": approved_count,
                "reviewed": reviewed_count, "rejected": rejected_count,
                "imported": imported, "dupes": dupes, "errors": errors,
            }
            controls["profiles"][profile_name].setdefault("autoflow", {})[job.slug] = af
            (BASE / "data" / "radar_controls.json").write_text(
                json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif not fw_vacancy_id:
            print(f"  ⚠️ No FW vacancy for {route_key}")
    conn.close()

print("\n🎉 Autoflow manual run completed!")
