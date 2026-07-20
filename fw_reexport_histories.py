#!/usr/bin/env python3
"""Re-export ALL candidate histories properly, using both API patterns, with dedup."""

import json, os, time, requests
from dotenv import load_dotenv

load_dotenv('/root/projects/hr-radar/.env')
API_URL = os.getenv('FRIENDWORK_API_URL', 'https://api.friend.work')
TOKEN = os.getenv('FRIENDWORK_API_TOKEN', '')
HEADERS = {'Authorization': f'Bearer {TOKEN}', 'Content-Type': 'application/json'}
EXPORT_DIR = '/root/projects/hr-radar/data/fw_export'

def fetch_histories(cid):
    """Fetch from both patterns, merge and dedup."""
    events = []
    seen_ids = set()
    
    # Pattern 1: /api/candidates/{id}/candidateHistories — detailed fields
    try:
        r = requests.get(f'{API_URL}/api/candidates/{cid}/candidateHistories', headers=HEADERS, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                for e in data:
                    hid = e.get('candidateHistoryId')
                    if hid and hid not in seen_ids:
                        seen_ids.add(hid)
                        events.append(e)
    except:
        pass
    
    # Pattern 2: /Candidate/{id}/CandidateHistories — has status Name field, sometimes more events
    try:
        r = requests.get(f'{API_URL}/Candidate/{cid}/CandidateHistories', headers=HEADERS, timeout=15)
        if r.status_code == 200:
            data = r.json()
            ch = data.get('CandidateHistories', []) if isinstance(data, dict) else []
            for e in ch:
                hid = e.get('CandidateHistoryId')
                if hid and hid not in seen_ids:
                    seen_ids.add(hid)
                    # Normalize field names to match pattern 1
                    events.append({
                        'candidateHistoryId': hid,
                        'statusId': e.get('CandidateStatusId'),
                        'statusName': e.get('Name', ''),
                        'jobId': e.get('JobId'),
                        'jobName': e.get('JobName', ''),
                        'candidateId': cid,
                        'dateCreated': e.get('DateCreated', ''),
                        'eventType': e.get('EventType'),
                        'description': e.get('Description', ''),
                        'isClosed': e.get('IsClosed', 0),
                        'accountId': e.get('AccountId'),
                        'responsibleId': e.get('ResponsibleId'),
                        '_source': 'pattern2'
                    })
                elif hid in seen_ids:
                    # Enrich pattern1 event with Name if available
                    name = e.get('Name', '')
                    if name:
                        for ev in events:
                            if ev.get('candidateHistoryId') == hid and not ev.get('statusName'):
                                ev['statusName'] = name
                                break
    except:
        pass
    
    return events

def main():
    with open(f'{EXPORT_DIR}/candidates.json') as f:
        candidates = json.load(f)
    
    total = len(candidates)
    print(f"Total candidates: {total}")
    
    all_histories = {}
    total_events = 0
    status_names = {}  # statusId -> name
    
    for i, c in enumerate(candidates):
        cid = c['candidateId']
        events = fetch_histories(cid)
        all_histories[str(cid)] = events
        total_events += len(events)
        
        # Collect status names
        for e in events:
            sid = e.get('statusId')
            name = e.get('statusName', '')
            if sid and name and str(sid) not in status_names:
                status_names[str(sid)] = name
        
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{total} — events so far: {total_events}")
        
        time.sleep(0.4)  # rate limit (2 requests per candidate)
    
    # Save histories
    with open(f'{EXPORT_DIR}/candidate_histories.json', 'w') as f:
        json.dump(all_histories, f, ensure_ascii=False)
    
    # Update status map with discovered names
    with open(f'{EXPORT_DIR}/status_map.json') as f:
        status_map = json.load(f)
    
    updated = 0
    for sid, name in status_names.items():
        if sid not in status_map:
            status_map[sid] = {"status_id": int(sid), "inferred_name": name, "is_name_confirmed": True}
            updated += 1
        elif not status_map[sid].get('is_name_confirmed'):
            status_map[sid]['inferred_name'] = name
            status_map[sid]['is_name_confirmed'] = True
            updated += 1
    
    with open(f'{EXPORT_DIR}/status_map.json', 'w') as f:
        json.dump(status_map, f, ensure_ascii=False, indent=2)
    
    # Update meta
    from datetime import datetime, timezone
    meta = json.load(open(f'{EXPORT_DIR}/export_meta.json'))
    meta['export_timestamp'] = datetime.now(timezone.utc).isoformat()
    meta['candidates_count'] = total
    meta['total_history_events'] = total_events
    meta['candidate_histories_fetched'] = len(all_histories)
    meta['status_ids_count'] = len(status_map)
    meta['status_names_confirmed'] = sum(1 for v in status_map.values() if v.get('is_name_confirmed'))
    meta['history_note'] = f'Fetched via both /api/candidates/id/candidateHistories AND /Candidate/id/CandidateHistories, merged and deduped.'
    with open(f'{EXPORT_DIR}/export_meta.json', 'w') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    
    print(f"\n=== FINAL ===")
    print(f"Candidates: {total}")
    print(f"Total events: {total_events}")
    print(f"Status names discovered: {len(status_names)}")
    print(f"Status map updated: {updated} new/confirmed")
    print(f"Done!")

if __name__ == '__main__':
    main()
