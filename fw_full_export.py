#!/usr/bin/env python3
"""
FriendWork full export script.
Tasks:
  1. Fetch accounts
  2. Fetch all candidate histories per-candidate (1089 candidates)
  3. Save bulk histories from the limited bulk endpoint (1000 records, no real pagination)
  4. Update status_map.json with any new statusIds found
  5. Update export_meta.json

Run: python3 fw_full_export.py
"""

import json
import os
import re
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
EXPORT_DIR = DATA_DIR / "fw_export"
EXPORT_DIR.mkdir(exist_ok=True)

ENV_FILE = SCRIPT_DIR / ".env"


def load_env():
    env = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


env = load_env()
BASE_URL = env.get("FRIENDWORK_API_URL", "https://api.friend.work").rstrip("/")
TOKEN = env.get("FRIENDWORK_API_TOKEN", "")

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "HR-Radar/1.0",
})


def api_get(path, timeout=45, retries=2):
    url = f"{BASE_URL}/{path.lstrip('/')}"
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json(), None
        except requests.exceptions.Timeout as e:
            if attempt < retries:
                print(f"    timeout attempt {attempt+1}/{retries+1}, retrying...")
                time.sleep(2)
                continue
            return None, f"Timeout after {retries+1} attempts: {e}"
        except Exception as e:
            return None, str(e)
    return None, "Max retries exceeded"


def api_post(path, body, timeout=45, retries=2):
    url = f"{BASE_URL}/{path.lstrip('/')}"
    for attempt in range(retries + 1):
        try:
            r = SESSION.post(url, json=body, timeout=timeout)
            r.raise_for_status()
            return r.json(), None
        except requests.exceptions.Timeout as e:
            if attempt < retries:
                print(f"    timeout attempt {attempt+1}/{retries+1}, retrying...")
                time.sleep(2)
                continue
            return None, f"Timeout after {retries+1} attempts: {e}"
        except Exception as e:
            return None, str(e)
    return None, "Max retries exceeded"


# ─── Task: Accounts ────────────────────────────────────────────────────────────
def fetch_accounts():
    print("\n=== Task: Fetching accounts ===")
    out_path = EXPORT_DIR / "accounts.json"
    if out_path.exists():
        try:
            existing = json.load(open(out_path, encoding="utf-8"))
            if existing:
                count = len(existing) if isinstance(existing, list) else 1
                print(f"  Already have {count} accounts, skipping")
                return count
        except Exception:
            pass
    data, err = api_get("/Accounts")
    if err:
        print(f"  ERROR: {err}")
        return 0
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    count = len(data) if isinstance(data, list) else 1
    print(f"  Saved {count} accounts to {out_path}")
    return count


# ─── Task: Bulk histories (1000-record snapshot) ───────────────────────────────
def fetch_bulk_histories():
    print("\n=== Task: Fetching bulk histories (POST /Candidate/CandidatesHistories) ===")
    # The API has broken pagination — always returns same 1000 records regardless of page.
    # We collect what we can and detect when we stop seeing new IDs.
    # If API is unavailable, we skip gracefully.
    out_path = EXPORT_DIR / "all_histories.json"

    # If already have data (even empty), note it and try once more
    if out_path.exists():
        try:
            existing = json.load(open(out_path, encoding="utf-8"))
            if existing:  # Non-empty list = already collected
                print(f"  Already have {len(existing)} bulk records, skipping")
                return existing
            else:
                print("  Previous bulk fetch got 0 records, will try once more")
        except Exception:
            pass

    seen_ids = set()
    all_records = []

    # Try once per page with short timeout — bulk endpoint is unreliable/slow
    # Stop after first error (timeout means endpoint is overloaded)
    for page in range(20):
        body = {"paging": {"page": page, "count": 100}}
        data, err = api_post("/Candidate/CandidatesHistories", body, timeout=20, retries=0)
        if err:
            print(f"  page {page}: ERROR {err} — bulk endpoint unavailable, stopping")
            break
        hist = data.get("CandidateHistories") or []
        msg = data.get("Message", "")
        if msg == "API Error" or not hist:
            print(f"  page {page}: no data (msg={msg}), stopping")
            break
        new_records = [h for h in hist if h.get("CandidateHistoryId") not in seen_ids]
        if not new_records:
            print(f"  page {page}: all {len(hist)} records already seen — pagination broken, stopping")
            break
        for h in new_records:
            seen_ids.add(h["CandidateHistoryId"])
        all_records.extend(new_records)
        print(f"  page {page}: +{len(new_records)} new records, total={len(all_records)}")
        time.sleep(0.5)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)
    print(f"  Saved {len(all_records)} bulk history records to {out_path}")
    return all_records


# ─── Task: Per-candidate histories ─────────────────────────────────────────────
def fetch_candidate_histories():
    print("\n=== Task: Fetching per-candidate histories ===")

    candidates_path = EXPORT_DIR / "candidates.json"
    candidates = json.load(open(candidates_path, encoding="utf-8"))
    candidate_ids = [c["candidateId"] for c in candidates]
    print(f"  Total candidates to process: {len(candidate_ids)}")

    out_path = EXPORT_DIR / "candidate_histories.json"
    # Resume from existing if partial
    if out_path.exists():
        existing = json.load(open(out_path, encoding="utf-8"))
        print(f"  Resuming: {len(existing)} already fetched")
    else:
        existing = {}

    results = dict(existing)
    errors = {}
    skipped = 0

    for i, cid in enumerate(candidate_ids):
        cid_str = str(cid)
        if cid_str in results:
            skipped += 1
            continue

        path = f"/Candidate/{cid}/CandidateHistories"
        data, err = api_get(path, timeout=60)

        if err:
            errors[cid_str] = str(err)
            results[cid_str] = []
            print(f"  [{i+1}/{len(candidate_ids)}] cid={cid} ERROR: {err}")
        else:
            hist = data.get("CandidateHistories", []) if isinstance(data, dict) else (data or [])
            results[cid_str] = hist

        # Progress every 100
        processed = (i + 1) - skipped
        if processed % 100 == 0:
            total_events = sum(len(v) for v in results.values())
            print(f"  [{i+1}/{len(candidate_ids)}] processed={processed}, skipped={skipped}, "
                  f"total_events={total_events}, errors={len(errors)}")
            # Save checkpoint
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

        time.sleep(0.3)

    # Final save
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    total_events = sum(len(v) for v in results.values())
    print(f"\n  Done: {len(results)} candidates, {total_events} total events, {len(errors)} errors")
    if errors:
        print(f"  Errors (sample): {list(errors.items())[:5]}")
    return results


# ─── Task: Update status map ───────────────────────────────────────────────────
def update_status_map(bulk_histories, candidate_histories):
    print("\n=== Task: Updating status map ===")

    status_map_path = EXPORT_DIR / "status_map.json"
    status_map = json.load(open(status_map_path, encoding="utf-8"))

    # Collect all (statusId, statusName, description, jobName) from bulk
    new_entries = {}

    def collect_record(sid, name, desc, job_name, job_id):
        if sid is None:
            return
        sid_str = str(sid)
        if sid_str not in new_entries:
            new_entries[sid_str] = {
                "status_id": sid,
                "names_seen": set(),
                "sample_descriptions": [],
                "sample_jobs": [],
            }
        entry = new_entries[sid_str]
        if name:
            entry["names_seen"].add(name)
        if desc and len(entry["sample_descriptions"]) < 5:
            entry["sample_descriptions"].append({"jobName": job_name, "description": str(desc)[:200]})
        if job_id and job_id not in {j.get("jobId") for j in entry["sample_jobs"]}:
            entry["sample_jobs"].append({"jobId": job_id, "jobName": job_name})

    for record in bulk_histories:
        collect_record(
            record.get("StatusId"),
            record.get("Name", ""),
            record.get("Description", ""),
            record.get("JobName", ""),
            record.get("JobId", 0),
        )

    for cid_str, hist_list in candidate_histories.items():
        for record in hist_list:
            collect_record(
                record.get("StatusId") or record.get("statusId"),
                record.get("Name") or record.get("name", ""),
                record.get("Description") or record.get("description", ""),
                record.get("JobName") or record.get("jobName", ""),
                record.get("JobId") or record.get("jobId", 0),
            )

    added = 0
    updated = 0
    for sid_str, entry in new_entries.items():
        names = entry["names_seen"]
        # Pick shortest non-empty name as inferred (usually the canonical status name)
        inferred = sorted(names, key=len)[0] if names else None

        if sid_str not in status_map:
            status_map[sid_str] = {
                "status_id": entry["status_id"],
                "inferred_name": inferred,
                "is_name_confirmed": False,
                "jobs_count": len(entry["sample_jobs"]),
                "event_types": [],
                "sample_jobs": entry["sample_jobs"][:3],
                "sample_descriptions": entry["sample_descriptions"][:5],
            }
            added += 1
        else:
            existing = status_map[sid_str]
            if not existing.get("inferred_name") and inferred:
                existing["inferred_name"] = inferred
                existing["is_name_confirmed"] = False
                updated += 1
            # Merge new sample jobs
            existing_job_ids = {j.get("jobId") for j in existing.get("sample_jobs", [])}
            for j in entry["sample_jobs"][:3]:
                if j.get("jobId") not in existing_job_ids:
                    existing.setdefault("sample_jobs", []).append(j)
                    existing_job_ids.add(j.get("jobId"))

    with open(status_map_path, "w", encoding="utf-8") as f:
        json.dump(status_map, f, ensure_ascii=False, indent=2)

    total_in_map = len(status_map)
    confirmed = sum(1 for v in status_map.values() if v.get("is_name_confirmed"))
    print(f"  Status map: {total_in_map} total IDs, {confirmed} confirmed names")
    print(f"  Added {added} new, updated {updated} existing entries")
    return total_in_map, confirmed


# ─── Task: Update export meta ──────────────────────────────────────────────────
def update_meta(accounts_count, bulk_count, candidate_histories, status_count, confirmed_status):
    print("\n=== Task: Updating export_meta.json ===")

    meta_path = EXPORT_DIR / "export_meta.json"
    meta = json.load(open(meta_path, encoding="utf-8"))

    total_hist_events = sum(len(v) for v in candidate_histories.values())
    candidates_with_hist = sum(1 for v in candidate_histories.values() if v)

    meta["export_timestamp"] = datetime.now(timezone.utc).isoformat()
    meta["accounts_count"] = accounts_count
    meta["bulk_histories_count"] = bulk_count
    meta["candidate_histories_fetched"] = len(candidate_histories)
    meta["candidate_histories_with_events"] = candidates_with_hist
    meta["total_history_events"] = total_hist_events
    meta["status_ids_count"] = status_count
    meta["status_ids_confirmed_names"] = confirmed_status
    meta["history_note"] = (
        "POST /Candidate/CandidatesHistories has broken pagination — always returns same ~1000 earliest records. "
        "Full history collected via GET /Candidate/{id}/CandidateHistories for all 1089 candidates."
    )

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"  Updated export_meta.json")
    print(f"  total_history_events={total_hist_events}, candidates_with_hist={candidates_with_hist}")


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"FriendWork Full Export — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Base URL: {BASE_URL}")
    print(f"Export dir: {EXPORT_DIR}")

    # Task 1: Accounts
    accounts_count = fetch_accounts()

    # Task 2: Bulk histories
    bulk_histories = fetch_bulk_histories()

    # Task 3: Per-candidate histories (longest task ~1089 * 0.3s = ~5.5 min)
    candidate_histories = fetch_candidate_histories()

    # Task 4: Update status map
    status_count, confirmed_status = update_status_map(bulk_histories, candidate_histories)

    # Task 5: Update meta
    update_meta(accounts_count, len(bulk_histories), candidate_histories, status_count, confirmed_status)

    print("\n=== FINAL SUMMARY ===")
    print(f"  accounts.json:            {accounts_count} accounts")
    print(f"  all_histories.json:       {len(bulk_histories)} bulk records (pagination broken at 1000)")
    total_events = sum(len(v) for v in candidate_histories.values())
    print(f"  candidate_histories.json: {len(candidate_histories)} candidates, {total_events} total events")
    print(f"  status_map.json:          {status_count} status IDs, {confirmed_status} confirmed names")


if __name__ == "__main__":
    main()
