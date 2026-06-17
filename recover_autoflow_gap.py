#!/usr/bin/env python3
"""One-off recovery: run autoflow over relevant candidates found 2026-06-14..06-17
that were missed while radar_controls.json was empty (autoflow silently off).

Deep-scores (GPT-5.2) + imports to FriendWork for every autoflow-enabled vacancy
across event_agencies, btl_agencies, btl_spb. FW import has its own dupe-check.
Does NOT write radar_controls.json back (avoids the non-atomic truncation bug).
"""
import json
import os
import datetime
import sqlite3
import sys
from pathlib import Path

BASE = Path("/opt/hr-radar")
sys.path.insert(0, str(BASE))

# Load .env (the module relies on systemd EnvironmentFile; we replicate it here)
env_path = BASE / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import requests  # noqa: E402
from run_multi_radar import (  # noqa: E402
    run_autoflow_deep_scoring, run_autoflow_fw_import,
    load_config, parse_profile_config,
)

DATE_FROM = "2026-06-14"
DATE_TO = "2026-06-17"
PROFILES = ["event_agencies", "btl_agencies", "btl_spb"]

config = load_config()
panel_config = json.loads((BASE / "data" / "panel_config.json").read_text())
controls = json.loads((BASE / "data" / "radar_controls.json").read_text())
routes = panel_config.get("routes", {})

grand = {"scored": 0, "approved": 0, "reviewed": 0, "rejected": 0,
         "imported": 0, "dupes": 0, "errors": 0}

for profile_name in PROFILES:
    pc = controls.get("profiles", {}).get(profile_name, {})
    autoflow_cfg = pc.get("autoflow", {})
    profile = parse_profile_config(profile_name, config["profiles"][profile_name], config["common"])
    openai_config = config["common"]["openai"]

    conn = sqlite3.connect(str(BASE / "data" / f"{profile_name}.db"))
    conn.row_factory = sqlite3.Row

    for job in profile.jobs:
        af = autoflow_cfg.get(job.slug, {})
        if not af.get("enabled", False):
            continue

        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT normalized_link FROM scored_candidates "
            "WHERE date(run_date) BETWEEN ? AND ? AND job_slug=? AND relevant=1",
            (DATE_FROM, DATE_TO, job.slug),
        )
        links = [r["normalized_link"] for r in cur.fetchall() if r["normalized_link"]]
        if not links:
            continue

        print(f"\n⚡ [{profile_name}/{job.name}]: {len(links)} кандидатов", flush=True)
        thresholds = af.get("thresholds", {})
        t_approve = thresholds.get("approve", panel_config.get("score_threshold_approve", 7))
        t_reject = thresholds.get("reject", panel_config.get("score_threshold_reject", 5))

        deep_results = []
        if af.get("deep_scoring", True):
            deep_results = run_autoflow_deep_scoring(
                links, job, openai_config, t_approve, t_reject, panel_config, profile_name
            )
            for r in deep_results:
                print(f"    {r.get('candidateName','?')}: {r.get('score',0)} → {r.get('fw_status','?')}", flush=True)

        approved = len([r for r in deep_results if r.get("score", 0) >= t_approve])
        reviewed = len([r for r in deep_results if t_reject <= r.get("score", 0) < t_approve])
        rejected = len([r for r in deep_results if 0 < r.get("score", 0) < t_reject])
        grand["scored"] += len(deep_results)
        grand["approved"] += approved
        grand["reviewed"] += reviewed
        grand["rejected"] += rejected

        route_key = f"{profile_name}.{job.slug}"
        fw_vacancy_id = routes.get(route_key)
        if fw_vacancy_id and deep_results:
            imported, dupes, errors = run_autoflow_fw_import(
                deep_results, int(fw_vacancy_id), af, t_approve, t_reject,
                panel_config.get("default_model", "gpt-5.2"),
            )
            grand["imported"] += imported
            grand["dupes"] += dupes
            grand["errors"] += errors
            print(f"  📤 FW {fw_vacancy_id}: {imported} new, {dupes} dupes, {errors} err", flush=True)

            summary = (
                f"♻️ <b>Восстановление 14-17.06 [{profile.name}]: {job.emoji} {job.name}</b>\n\n"
                f"📋 Первичный отбор: {len(links)} релевантных\n"
                f"🤖 Глубокий скоринг: ✅{approved} 👁{reviewed} ❌{rejected}\n"
                f"📤 Импорт в FW: {imported} новых, {dupes} дубликатов"
            )
            try:
                requests.post(
                    f"https://api.telegram.org/bot{profile.telegram_token}/sendMessage",
                    json={"chat_id": profile.telegram_chat, "text": summary, "parse_mode": "HTML"},
                    timeout=30,
                )
            except Exception as e:
                print(f"  ⚠️ telegram: {e}", flush=True)
        elif deep_results:
            print(f"  ⚠️ нет FW-маршрута для {route_key}", flush=True)

    conn.close()

print(f"\n=== ИТОГО ===\n{json.dumps(grand, ensure_ascii=False)}", flush=True)
