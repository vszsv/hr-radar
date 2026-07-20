#!/usr/bin/env python3
"""
FriendWork — fetch missing candidates.

Strategy:
  1. Try to get candidate IDs from POST /Candidate/CandidatesHistories (needs 120s timeout)
  2. Exhaustive text query sweep:
     - All 256 two-character hex prefix queries (matching HH resume IDs)
     - Common Cyrillic letters А-Я (already done, but will re-run to catch any gaps)
     - Keywords: company names, positions, cities, source sites
  3. Fetch each new candidate profile via GET /api/candidates/{id}
  4. Fetch histories for each new candidate
  5. Merge and save

Run: python3 fw_export_missing.py
Intermediate results saved after each phase to avoid data loss.
"""

import json
import os
import time
import sys
import itertools
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
EXPORT_DIR = DATA_DIR / "fw_export"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

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


def api_get(path, timeout=30, retries=2):
    url = f"{BASE_URL}/{path.lstrip('/')}"
    for attempt in range(retries + 1):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 404:
                return None, "404 Not Found"
            if r.status_code == 400:
                return None, f"400: {r.text[:100]}"
            r.raise_for_status()
            return r.json(), None
        except requests.exceptions.Timeout:
            if attempt < retries:
                time.sleep(2)
                continue
            return None, f"Timeout after {retries+1} attempts"
        except requests.exceptions.HTTPError as e:
            return None, f"HTTP {e.response.status_code}: {e}"
        except Exception as e:
            return None, str(e)
    return None, "Max retries exceeded"


def api_post(path, body, timeout=30, retries=0):
    url = f"{BASE_URL}/{path.lstrip('/')}"
    for attempt in range(retries + 1):
        try:
            r = SESSION.post(url, json=body, timeout=timeout)
            r.raise_for_status()
            return r.json(), None
        except requests.exceptions.Timeout:
            if attempt < retries:
                time.sleep(2)
                continue
            return None, f"Timeout after {retries+1} attempts"
        except requests.exceptions.HTTPError as e:
            return None, f"HTTP {e.response.status_code}: {e}"
        except Exception as e:
            return None, str(e)
    return None, "Max retries exceeded"


def load_existing():
    """Load existing candidates and return (candidates_list, ids_set)."""
    path = EXPORT_DIR / "candidates.json"
    candidates = json.load(open(path, encoding="utf-8"))
    return candidates, {c["candidateId"] for c in candidates}


def save_checkpoint(new_candidates, label="checkpoint"):
    """Save discovered new candidates to interim file."""
    interim_path = EXPORT_DIR / "new_candidates_interim.json"
    if interim_path.exists():
        try:
            existing_interim = json.load(open(interim_path, encoding="utf-8"))
            existing_interim_ids = {c["candidateId"] for c in existing_interim}
            for c in new_candidates:
                if c["candidateId"] not in existing_interim_ids:
                    existing_interim.append(c)
                    existing_interim_ids.add(c["candidateId"])
            new_candidates = existing_interim
        except Exception:
            pass
    with open(interim_path, "w", encoding="utf-8") as f:
        json.dump(new_candidates, f, ensure_ascii=False, indent=2)
    print(f"    [checkpoint] {label}: {len(new_candidates)} new candidates saved to interim file")
    return new_candidates


# ─── Phase 1: CandidatesHistories bulk endpoint ────────────────────────────────
def phase1_bulk_histories(existing_ids):
    print("\n=== Phase 1: POST /Candidate/CandidatesHistories (120s timeout) ===")

    interim_path = EXPORT_DIR / "phase1_cids.json"
    if interim_path.exists():
        try:
            saved = json.load(open(interim_path))
            if saved.get("done"):
                cids = set(saved["candidate_ids"])
                print(f"  Loaded {len(cids)} CIDs from Phase 1 cache (complete)")
                return cids
        except Exception:
            pass

    all_cids = set()
    seen_event_ids = set()

    # Try with 120s timeout — this endpoint is slow
    print("  Attempting with 120s timeout...")
    data, err = api_post("/Candidate/CandidatesHistories",
                          {"paging": {"page": 0, "count": 20}},
                          timeout=120, retries=1)
    if err:
        print(f"  Failed: {err}")
        with open(interim_path, "w") as f:
            json.dump({"done": True, "candidate_ids": [], "error": err}, f)
        return set()

    hist = data.get("CandidateHistories") or []
    total_count = data.get("CandidateHistoriesCount", 0)
    for event in hist:
        eid = event.get("CandidateHistoryId")
        cid = event.get("CandidateId")
        if eid:
            seen_event_ids.add(eid)
        if cid:
            all_cids.add(cid)

    print(f"  Got {len(hist)} events, {len(all_cids)} unique CIDs, total_count={total_count}")

    # Pagination is broken — try a couple more pages to see if we get new data
    for page in range(1, 10):
        data, err = api_post("/Candidate/CandidatesHistories",
                              {"paging": {"page": page, "count": 20}},
                              timeout=120, retries=0)
        if err:
            print(f"  page {page}: {err} — stopping")
            break
        hist = data.get("CandidateHistories") or []
        new_events = sum(1 for h in hist if h.get("CandidateHistoryId") not in seen_event_ids)
        new_cids = 0
        for event in hist:
            eid = event.get("CandidateHistoryId")
            cid = event.get("CandidateId")
            if eid: seen_event_ids.add(eid)
            if cid and cid not in all_cids:
                all_cids.add(cid)
                new_cids += 1
        print(f"  page {page}: {len(hist)} events, {new_events} new events, {new_cids} new CIDs")
        if new_events == 0:
            print("  Pagination broken — same events repeating, stopping")
            break
        time.sleep(1)

    new_from_bulk = all_cids - existing_ids
    print(f"  Phase 1 result: {len(all_cids)} total CIDs, {len(new_from_bulk)} new (not in existing)")

    with open(interim_path, "w") as f:
        json.dump({"done": True, "candidate_ids": sorted(all_cids),
                   "new_count": len(new_from_bulk)}, f, indent=2)

    return all_cids


# ─── Phase 2: Exhaustive text query sweep ─────────────────────────────────────
def phase2_query_sweep(existing_ids):
    print("\n=== Phase 2: Exhaustive text query sweep ===")

    interim_path = EXPORT_DIR / "phase2_found_ids.json"
    found_ids = set()
    found_candidates = []

    # Load previous results
    if interim_path.exists():
        try:
            saved = json.load(open(interim_path))
            found_ids = set(saved.get("found_ids", []))
            found_candidates = saved.get("found_candidates", [])
            queries_done = set(saved.get("queries_done", []))
            print(f"  Resuming: {len(found_ids)} IDs found, {len(queries_done)} queries done")
        except Exception:
            queries_done = set()
    else:
        queries_done = set()

    def search(query):
        """Run a search query, return list of candidates."""
        data, err = api_post("/api/candidates",
                              {"paging": {"page": 0, "count": 20}, "query": query},
                              timeout=30, retries=1)
        if err:
            return [], err
        candidates = data.get("Candidates") or []
        return candidates, None

    def process_results(candidates, query_label):
        nonlocal found_ids, found_candidates
        new_from_query = []
        for c in candidates:
            cid = c.get("candidateId")
            if cid and cid not in existing_ids and cid not in found_ids:
                found_ids.add(cid)
                found_candidates.append(c)
                new_from_query.append(cid)
        return new_from_query

    total_new = len(found_ids)

    # --- Set 1: All 256 two-character hex pairs ---
    # HH resume IDs are hex hashes — searching prefix finds candidates with those resume IDs
    hex_chars = "0123456789abcdef"
    hex_pairs = ["".join(p) for p in itertools.product(hex_chars, hex_chars)]

    done_hex = sum(1 for q in hex_pairs if q in queries_done)
    print(f"\n  Set 1: {len(hex_pairs)} hex pair queries ({done_hex} already done)")

    for i, q in enumerate(hex_pairs):
        if q in queries_done:
            continue

        candidates, err = search(q)
        if err:
            print(f"    '{q}': ERROR {err}")
        else:
            new = process_results(candidates, q)
            if new:
                total_new += len(new)
                print(f"    '{q}': {len(candidates)} results, {len(new)} NEW (total new: {total_new})")

        queries_done.add(q)
        time.sleep(0.3)

        # Save checkpoint every 50 queries
        if (i + 1) % 50 == 0:
            with open(interim_path, "w") as f:
                json.dump({
                    "found_ids": sorted(found_ids),
                    "found_candidates": found_candidates,
                    "queries_done": sorted(queries_done),
                }, f, ensure_ascii=False, indent=2)
            print(f"    [checkpoint] {i+1}/{len(hex_pairs)} hex queries done, "
                  f"total new IDs: {len(found_ids - existing_ids)}")

    # --- Set 2: Cyrillic letter pairs (А-Я for first+second letter combos) ---
    # We already know А-Я single letter was done — try 2-letter Cyrillic pairs
    cyrillic = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    cyrillic_pairs = [a + b for a in cyrillic for b in cyrillic[:5]]  # First 5 = АА-АД, БА-БД, etc.
    # Also try full names / common patterns
    name_queries = (
        [c for c in cyrillic]  # Single letters (may be redundant but verify)
        + [c.lower() for c in cyrillic]  # Lowercase
        + cyrillic_pairs[:200]  # First 200 Cyrillic pairs
    )

    done_name = sum(1 for q in name_queries if q in queries_done)
    print(f"\n  Set 2: {len(name_queries)} Cyrillic name queries ({done_name} already done)")

    for i, q in enumerate(name_queries):
        if q in queries_done:
            continue

        candidates, err = search(q)
        if err:
            print(f"    '{q}': ERROR {err}")
        else:
            new = process_results(candidates, q)
            if new:
                print(f"    '{q}': {len(candidates)} results, {len(new)} NEW (total new: {total_new + len(new)})")
                total_new += len(new)

        queries_done.add(q)
        time.sleep(0.3)

        if (i + 1) % 100 == 0:
            with open(interim_path, "w") as f:
                json.dump({
                    "found_ids": sorted(found_ids),
                    "found_candidates": found_candidates,
                    "queries_done": sorted(queries_done),
                }, f, ensure_ascii=False, indent=2)

    # --- Set 3: Keyword queries ---
    keyword_queries = [
        # Sites/sources
        "headhunter", "hh.ru", "HeadHunter", "LinkedIn",
        # Common job titles
        "Event", "event", "BTL", "btl", "Account", "Project", "manager",
        "менеджер", "директор", "руководитель", "специалист", "координатор",
        "продюсер", "маркетолог", "рекрутер", "HR", "New Business",
        # Companies
        "Action", "DIVERSITY", "DPG", "CREON", "Leader", "СберМаркетинг",
        "Publicis", "OMD", "Media", "Promo", "Agency",
        # Cities
        "Москва", "Санкт-Петербург", "Краснодар", "Новосибирск", "Екатеринбург",
        # Other
        "Активный поиск", "Отклик", "фриланс", "удаленка", "Россия",
        "coordinator", "senior", "junior", "lead", "head",
        # Numbers that might appear in names/IDs
        "50", "51", "без имени", "Без имени",
        # Time periods
        "2024", "2025", "2026", "2023",
        # University/education
        "университет", "институт", "академия", "MBA",
    ]

    done_kw = sum(1 for q in keyword_queries if q in queries_done)
    print(f"\n  Set 3: {len(keyword_queries)} keyword queries ({done_kw} already done)")

    for i, q in enumerate(keyword_queries):
        if q in queries_done:
            continue

        candidates, err = search(q)
        if err:
            print(f"    '{q}': ERROR {err}")
        else:
            new = process_results(candidates, q)
            if new:
                print(f"    '{q}': {len(candidates)} results, {len(new)} NEW (total new: {len(found_ids - existing_ids)})")

        queries_done.add(q)
        time.sleep(0.3)

    # Final save
    with open(interim_path, "w") as f:
        json.dump({
            "found_ids": sorted(found_ids),
            "found_candidates": found_candidates,
            "queries_done": sorted(queries_done),
            "done": True,
        }, f, ensure_ascii=False, indent=2)

    new_ids = found_ids - existing_ids
    print(f"\n  Phase 2 complete: {len(queries_done)} queries run, {len(new_ids)} new candidate IDs found")
    return new_ids, {c["candidateId"]: c for c in found_candidates if c["candidateId"] not in existing_ids}


# ─── Phase 3: Fetch full profiles for missing candidates ──────────────────────
def phase3_fetch_profiles(missing_ids, profile_cache):
    """
    Fetch full candidate profiles.
    profile_cache: dict {candidateId: partial_data_from_search}
    Returns: list of full candidate dicts
    """
    print(f"\n=== Phase 3: Fetch full profiles for {len(missing_ids)} candidates ===")

    interim_path = EXPORT_DIR / "phase3_profiles.json"
    fetched = {}
    if interim_path.exists():
        try:
            fetched = {c["candidateId"]: c for c in json.load(open(interim_path, encoding="utf-8"))}
            print(f"  Resuming: {len(fetched)} already fetched")
        except Exception:
            pass

    errors = []
    not_found = []
    missing_sorted = sorted(missing_ids)

    for i, cid in enumerate(missing_sorted):
        if cid in fetched:
            continue

        data, err = api_get(f"/api/candidates/{cid}", timeout=30, retries=2)

        if err:
            if "404" in str(err) or "400" in str(err):
                # Use the partial data from search if available
                if cid in profile_cache:
                    fetched[cid] = profile_cache[cid]
                    print(f"  [{i+1}/{len(missing_sorted)}] {cid}: {err} — using partial from search")
                else:
                    not_found.append(cid)
            else:
                errors.append((cid, err))
                # Still use partial if available
                if cid in profile_cache:
                    fetched[cid] = profile_cache[cid]
        else:
            # Parse response
            if isinstance(data, list) and len(data) > 0:
                candidate = data[0]
            elif isinstance(data, dict) and "candidateId" in data:
                candidate = data
            elif isinstance(data, dict) and "message" in data:
                # Error response
                if cid in profile_cache:
                    fetched[cid] = profile_cache[cid]
                else:
                    not_found.append(cid)
                continue
            else:
                candidate = data

            if isinstance(candidate, dict):
                if "candidateId" not in candidate:
                    candidate["candidateId"] = cid
                fetched[cid] = candidate

        # Progress every 50
        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(missing_sorted)}] fetched={len(fetched)}, "
                  f"not_found={len(not_found)}, errors={len(errors)}")
            with open(interim_path, "w") as f:
                json.dump(list(fetched.values()), f, ensure_ascii=False, indent=2)

        time.sleep(0.3)

    # Final save
    with open(interim_path, "w") as f:
        json.dump(list(fetched.values()), f, ensure_ascii=False, indent=2)

    print(f"\n  Phase 3 complete: {len(fetched)} fetched, {len(not_found)} not found, {len(errors)} errors")
    if not_found[:5]:
        print(f"  Not found sample: {not_found[:5]}")
    return list(fetched.values())


# ─── Phase 4: Fetch histories for new candidates ──────────────────────────────
def phase4_fetch_histories(new_candidates, existing_histories):
    new_ids = [c["candidateId"] for c in new_candidates]
    print(f"\n=== Phase 4: Fetch histories for {len(new_ids)} new candidates ===")

    results = dict(existing_histories)
    errors = {}

    for i, cid in enumerate(new_ids):
        cid_str = str(cid)
        if cid_str in results:
            continue

        data, err = api_get(f"/Candidate/{cid}/CandidateHistories", timeout=30, retries=2)

        if err:
            errors[cid_str] = str(err)
            results[cid_str] = []
        else:
            if isinstance(data, dict):
                hist = data.get("CandidateHistories", [])
            elif isinstance(data, list):
                hist = data
            else:
                hist = []
            results[cid_str] = hist

        if (i + 1) % 100 == 0:
            total_events = sum(len(v) for v in results.values())
            print(f"  [{i+1}/{len(new_ids)}] errors={len(errors)}, total_events={total_events}")

        time.sleep(0.3)

    total_events = sum(len(v) for v in results.values())
    print(f"  Phase 4 complete: {len(results)} total, {total_events} events, {len(errors)} errors")
    return results


# ─── Phase 5: Merge and save ──────────────────────────────────────────────────
def phase5_save(all_candidates, all_histories, prev_count):
    print("\n=== Phase 5: Merge and save ===")

    candidates_path = EXPORT_DIR / "candidates.json"
    histories_path = EXPORT_DIR / "candidate_histories.json"
    meta_path = EXPORT_DIR / "export_meta.json"
    status_map_path = EXPORT_DIR / "status_map.json"

    with open(candidates_path, "w", encoding="utf-8") as f:
        json.dump(all_candidates, f, ensure_ascii=False, indent=2)
    print(f"  candidates.json: {len(all_candidates)} candidates (was {prev_count}, +{len(all_candidates)-prev_count})")

    with open(histories_path, "w", encoding="utf-8") as f:
        json.dump(all_histories, f, ensure_ascii=False, indent=2)
    total_events = sum(len(v) for v in all_histories.values())
    print(f"  candidate_histories.json: {len(all_histories)} candidates, {total_events} events")

    # Update status map
    status_map = json.load(open(status_map_path, encoding="utf-8"))
    new_status_count = 0
    for cid_str, hist_list in all_histories.items():
        for record in hist_list:
            sid = record.get("StatusId") or record.get("statusId")
            name = record.get("Name") or record.get("name", "")
            if sid is not None:
                sid_str = str(sid)
                if sid_str not in status_map:
                    status_map[sid_str] = {
                        "status_id": sid,
                        "inferred_name": name or None,
                        "is_name_confirmed": False,
                        "jobs_count": 0,
                        "event_types": [],
                        "sample_jobs": [],
                        "sample_descriptions": [],
                    }
                    new_status_count += 1
    with open(status_map_path, "w", encoding="utf-8") as f:
        json.dump(status_map, f, ensure_ascii=False, indent=2)
    print(f"  status_map.json: {len(status_map)} total IDs, {new_status_count} new")

    # Update meta
    try:
        meta = json.load(open(meta_path, encoding="utf-8"))
    except Exception:
        meta = {}
    meta["export_timestamp"] = datetime.now(timezone.utc).isoformat()
    meta["candidates_count"] = len(all_candidates)
    meta["candidate_histories_fetched"] = len(all_histories)
    meta["total_history_events"] = total_events
    meta["status_ids_count"] = len(status_map)
    meta["api_limitation"] = (
        "POST /api/candidates uses Sphinx search, returns max 20 per call. "
        "Pagination params are ignored. Coverage achieved via: "
        "(1) global no-filter fetch, (2) 27 Cyrillic-letter queries, "
        "(3) per-job fetch for 67 jobs, (4) individual GET by known IDs, "
        "(5) POST /Candidate/CandidatesHistories bulk (274 unique CIDs, 1000 events), "
        "(6) Exhaustive hex-pair queries (256 combos matching HH resume IDs), "
        "(7) Cyrillic pair queries, (8) keyword queries."
    )
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"  export_meta.json updated")

    return total_events, len(status_map)


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"FriendWork Missing Candidates Export — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Base URL: {BASE_URL}")
    print(f"Export dir: {EXPORT_DIR}")

    start_time = time.time()

    # Load state
    existing_candidates, existing_ids = load_existing()
    existing_histories = json.load(open(EXPORT_DIR / "candidate_histories.json", encoding="utf-8"))
    prev_count = len(existing_candidates)

    print(f"\nStarting with: {len(existing_candidates)} candidates, "
          f"{sum(len(v) for v in existing_histories.values())} history events")

    # Phase 1: Bulk histories endpoint
    bulk_cids = phase1_bulk_histories(existing_ids)
    new_from_bulk = bulk_cids - existing_ids
    print(f"  New from bulk histories: {len(new_from_bulk)}")

    # Phase 2: Exhaustive query sweep
    new_query_ids, query_profile_cache = phase2_query_sweep(existing_ids)
    print(f"  New from query sweep: {len(new_query_ids)}")

    # Combine all new IDs
    all_new_ids = (new_from_bulk | new_query_ids) - existing_ids
    print(f"\nTotal new candidate IDs to fetch: {len(all_new_ids)}")

    if not all_new_ids:
        print("\nNo new candidates found. Export is already complete.")
        return

    # Phase 3: Fetch full profiles
    new_candidates = phase3_fetch_profiles(all_new_ids, query_profile_cache)

    # Phase 4: Fetch histories
    all_histories = phase4_fetch_histories(new_candidates, existing_histories)

    # Merge candidates
    all_candidates = list(existing_candidates)
    merged_ids = set(existing_ids)
    added = 0
    for c in new_candidates:
        cid = c.get("candidateId")
        if cid and cid not in merged_ids:
            all_candidates.append(c)
            merged_ids.add(cid)
            added += 1

    # Phase 5: Save
    total_events, status_count = phase5_save(all_candidates, all_histories, prev_count)

    elapsed = time.time() - start_time
    print(f"\n=== FINAL SUMMARY ===")
    print(f"  Time elapsed:        {elapsed:.0f}s ({elapsed/60:.1f} min)")
    print(f"  Started with:        {prev_count} candidates")
    print(f"  New IDs discovered:  {len(all_new_ids)}")
    print(f"  New candidates added:{added}")
    print(f"  Total candidates:    {len(all_candidates)}")
    print(f"  Total events:        {total_events}")
    print(f"  Status IDs:          {status_count}")


if __name__ == "__main__":
    main()
