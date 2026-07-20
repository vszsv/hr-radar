#!/usr/bin/env python3
"""Merge phase3 profiles into candidates.json, fetch histories for new candidates, update all export files."""

import json, os, time, requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv('/root/projects/hr-radar/.env')
API_URL = os.getenv('FRIENDWORK_API_URL', 'https://api.friend.work')
API_TOKEN = os.getenv('FRIENDWORK_API_TOKEN', '')
HEADERS = {'Authorization': f'Bearer {API_TOKEN}', 'Content-Type': 'application/json'}
EXPORT_DIR = '/root/projects/hr-radar/data/fw_export'

def main():
    # Load existing
    with open(f'{EXPORT_DIR}/candidates.json') as f:
        existing = json.load(f)
    existing_ids = {c['candidateId'] for c in existing}
    print(f"Existing candidates: {len(existing)}")

    # Load phase3
    with open(f'{EXPORT_DIR}/phase3_profiles.json') as f:
        new_profiles = json.load(f)
    
    # Deduplicate and merge
    added = 0
    for p in new_profiles:
        cid = p.get('candidateId')
        if cid and cid not in existing_ids:
            existing.append(p)
            existing_ids.add(cid)
            added += 1
    
    print(f"New profiles added: {added}")
    print(f"Total candidates now: {len(existing)}")
    
    # Save merged candidates
    with open(f'{EXPORT_DIR}/candidates.json', 'w') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print("Saved candidates.json")
    
    # Load existing histories
    with open(f'{EXPORT_DIR}/candidate_histories.json') as f:
        all_histories = json.load(f)
    existing_hist_ids = set(all_histories.keys())
    print(f"Existing histories for {len(existing_hist_ids)} candidates")
    
    # Find candidates without histories
    need_histories = []
    for c in existing:
        cid_str = str(c['candidateId'])
        if cid_str not in existing_hist_ids:
            need_histories.append(c['candidateId'])
    
    print(f"Need to fetch histories for {len(need_histories)} candidates")
    
    # Fetch histories
    fetched = 0
    errors = 0
    new_status_ids = set()
    
    for i, cid in enumerate(need_histories):
        try:
            resp = requests.get(
                f'{API_URL}/api/candidates/{cid}/candidateHistories',
                headers=HEADERS,
                timeout=15
            )
            if resp.status_code == 200:
                events = resp.json()
                all_histories[str(cid)] = events
                fetched += 1
                for ev in events:
                    sid = ev.get('statusId')
                    if sid:
                        new_status_ids.add(sid)
            else:
                # Try alternate URL
                resp2 = requests.get(
                    f'{API_URL}/Candidate/{cid}/CandidateHistories',
                    headers=HEADERS,
                    timeout=15
                )
                if resp2.status_code == 200:
                    events = resp2.json()
                    all_histories[str(cid)] = events
                    fetched += 1
                    for ev in events:
                        sid = ev.get('statusId')
                        if sid:
                            new_status_ids.add(sid)
                else:
                    all_histories[str(cid)] = []
                    errors += 1
        except Exception as e:
            all_histories[str(cid)] = []
            errors += 1
        
        if (i + 1) % 100 == 0:
            print(f"  Progress: {i+1}/{len(need_histories)} (fetched: {fetched}, errors: {errors})")
        
        time.sleep(0.3)
    
    print(f"Fetched histories: {fetched}, errors: {errors}")
    
    # Save histories
    with open(f'{EXPORT_DIR}/candidate_histories.json', 'w') as f:
        json.dump(all_histories, f, ensure_ascii=False)
    
    total_events = sum(len(v) for v in all_histories.values())
    print(f"Total candidates with histories: {len(all_histories)}")
    print(f"Total events: {total_events}")
    
    # Update status map
    with open(f'{EXPORT_DIR}/status_map.json') as f:
        status_map = json.load(f)
    
    existing_sids = set(status_map.keys())
    new_sids = {str(s) for s in new_status_ids} - existing_sids
    if new_sids:
        print(f"New status IDs found: {len(new_sids)}")
        for sid in new_sids:
            # Try to find name from events
            name = None
            for cid, events in all_histories.items():
                for ev in events:
                    if str(ev.get('statusId')) == sid and ev.get('Name'):
                        name = ev['Name']
                        break
                if name:
                    break
            status_map[sid] = {
                "status_id": int(sid),
                "inferred_name": name or "Unknown",
                "is_name_confirmed": bool(name)
            }
        with open(f'{EXPORT_DIR}/status_map.json', 'w') as f:
            json.dump(status_map, f, ensure_ascii=False, indent=2)
    
    # Update meta
    meta = {
        "export_timestamp": datetime.now(timezone.utc).isoformat(),
        "candidates_count": len(existing),
        "jobs_count": 67,
        "status_ids_count": len(status_map),
        "candidate_histories_fetched": len(all_histories),
        "total_history_events": total_events,
        "phase3_new_profiles": added,
        "phase4_new_histories": fetched,
        "phase4_errors": errors,
        "coverage_note": f"Exported {len(existing)} candidates from FW via search sweep + individual ID fetch. History events: {total_events}."
    }
    with open(f'{EXPORT_DIR}/export_meta.json', 'w') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    
    print(f"\n=== FINAL SUMMARY ===")
    print(f"Total candidates: {len(existing)}")
    print(f"Total history events: {total_events}")
    print(f"Status IDs mapped: {len(status_map)}")
    print(f"Done!")

if __name__ == '__main__':
    main()
