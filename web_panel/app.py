"""HR Radar Web Panel — FastAPI backend."""
import os
import sys
import json
import asyncio
import time
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from contextlib import asynccontextmanager

# Load env
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

ACCESS_KEY = os.environ.get("HR_PANEL_KEY", "hrpanel2026")
HH_RESUMES_DIR = Path(__file__).parent.parent / "data" / "hh_resumes"
HH_RESUMES_DIR.mkdir(parents=True, exist_ok=True)
JOURNEY_DB = Path(__file__).parent.parent / "data" / "candidate_journey.db"

def _init_journey_db():
    conn = sqlite3.connect(str(JOURNEY_DB))
    conn.execute("""CREATE TABLE IF NOT EXISTS journey (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hh_link TEXT NOT NULL,
        resume_id TEXT,
        profile_id TEXT,
        job_slug TEXT,
        -- primary screening
        primary_date TEXT,
        primary_score REAL,
        primary_fit_type TEXT,
        primary_reason TEXT,
        -- deep scoring
        deep_date TEXT,
        deep_model TEXT,
        deep_prompt TEXT,
        deep_score INTEGER,
        deep_status TEXT,
        deep_reason TEXT,
        -- FW import
        fw_candidate_id INTEGER,
        fw_vacancy_id INTEGER,
        fw_import_date TEXT,
        fw_status TEXT,
        UNIQUE(hh_link, profile_id, job_slug)
    )""")
    conn.commit()
    conn.close()

_init_journey_db()
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
DATA_DIR = Path(__file__).parent.parent / "data"
PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPTS_DIR.mkdir(exist_ok=True)
DEFAULT_PROMPT_PATH = PROMPTS_DIR / "default_am.txt"
CONFIG_PATH = DATA_DIR / "panel_config.json"
PROFILES_PATH = Path(__file__).parent.parent / "config" / "profiles.yaml"
CONTROLS_PATH = DATA_DIR / "radar_controls.json"

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"default_model": "gpt-5.2", "score_threshold_approve": 7, "score_threshold_reject": 4, "routes": {}}

def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))

def load_controls():
    if CONTROLS_PATH.exists():
        return json.loads(CONTROLS_PATH.read_text())
    return {"profiles": {}}

def save_controls(ctrl):
    CONTROLS_PATH.write_text(json.dumps(ctrl, ensure_ascii=False, indent=2))

def load_profiles():
    import yaml
    if PROFILES_PATH.exists():
        return yaml.safe_load(PROFILES_PATH.read_text())
    return {"profiles": {}}

def _save_journey(data: dict):
    """Upsert a candidate journey record."""
    conn = sqlite3.connect(str(JOURNEY_DB))
    keys = list(data.keys())
    vals = list(data.values())
    placeholders = ','.join(['?'] * len(keys))
    cols = ','.join(keys)
    update_parts = ','.join(f"{k}=excluded.{k}" for k in keys if k not in ('hh_link','profile_id','job_slug'))
    conn.execute(
        f"INSERT INTO journey ({cols}) VALUES ({placeholders}) ON CONFLICT(hh_link,profile_id,job_slug) DO UPDATE SET {update_parts}",
        vals
    )
    conn.commit()
    conn.close()

def _cache_hh_resume(resume_id: str, resume_data: dict):
    path = HH_RESUMES_DIR / f"{resume_id}.json"
    if not path.exists():
        path.write_text(json.dumps(resume_data, ensure_ascii=False, indent=2))
    # Cache photo
    photo_url = (resume_data.get('photo') or {}).get('500') or (resume_data.get('photo') or {}).get('medium')
    if photo_url:
        photo_path = HH_RESUMES_DIR / f"{resume_id}.jpg"
        if not photo_path.exists():
            try:
                import requests as req
                r = req.get(photo_url, timeout=15)
                if r.status_code == 200:
                    photo_path.write_bytes(r.content)
            except: pass

def _load_cached_resume(resume_id: str):
    path = HH_RESUMES_DIR / f"{resume_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None

scoring_state = {}
deep_scoring_state = {}  # keyed by "profileId.jobSlug"

@asynccontextmanager
async def lifespan(app):
    yield

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

def check_key(key):
    if key != ACCESS_KEY:
        raise HTTPException(status_code=403, detail="Invalid key")

# ─── Pages ───
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, key: str = Query("")):
    check_key(key)
    return templates.TemplateResponse("index.html", {"request": request, "key": key})

# ─── API: Dashboard stats ───
@app.get("/api/dashboard")
async def api_dashboard(key: str = Query("")):
    check_key(key)
    labels = {"event_agencies": "🎪 Event агентства", "btl_agencies": "📢 BTL агентства", "outsource_agencies": "🏭 Аутсорсинг"}
    stats = []
    for db_name in ["event_agencies", "btl_agencies", "outsource_agencies"]:
        db_path = DATA_DIR / f"{db_name}.db"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            c = conn.cursor()
            total = c.execute("SELECT count(*) FROM scored_candidates").fetchone()[0]
            unique_total = c.execute("SELECT count(DISTINCT normalized_link) FROM scored_candidates").fetchone()[0]
            today = c.execute("SELECT count(*) FROM scored_candidates WHERE date(run_date)=date('now')").fetchone()[0]
            relevant = c.execute("SELECT count(*) FROM scored_candidates WHERE relevant=1").fetchone()[0]
            relevant_today = c.execute("SELECT count(*) FROM scored_candidates WHERE relevant=1 AND date(run_date)=date('now')").fetchone()[0]
            last = c.execute("SELECT run_date FROM scored_candidates ORDER BY id DESC LIMIT 1").fetchone()
            by_job = c.execute(
                "SELECT job_slug, count(*), sum(CASE WHEN relevant=1 THEN 1 ELSE 0 END) FROM scored_candidates WHERE date(run_date)=date('now') GROUP BY job_slug"
            ).fetchall()
            conn.close()
            stats.append({
                "id": db_name, "label": labels.get(db_name, db_name),
                "total": total, "unique_total": unique_total, "today": today, "relevant": relevant, "relevant_today": relevant_today,
                "last_run": last[0] if last else None,
                "by_job": [{"job": r[0], "total": r[1], "relevant": r[2]} for r in by_job]
            })
        except Exception as e:
            stats.append({"id": db_name, "label": labels.get(db_name, db_name), "error": str(e)})
    return {"stats": stats}

# ─── API: Controls (profiles on/off) ───
@app.get("/api/controls")
async def api_get_controls(key: str = Query("")):
    check_key(key)
    profiles = load_profiles()
    controls = load_controls()
    result = []
    for p_name, p_data in profiles.get("profiles", {}).items():
        p_ctrl = controls.get("profiles", {}).get(p_name, {})
        jobs = []
        for job in p_data.get("jobs", []):
            slug = job["slug"]
            jobs.append({
                "slug": slug, "name": job.get("name", slug), "emoji": job.get("emoji", ""),
                "enabled": p_ctrl.get("jobs", {}).get(slug, True)
            })
        # Autoflow per job
        autoflow_data = p_ctrl.get("autoflow", {})
        for job_item in jobs:
            af = autoflow_data.get(job_item["slug"], {})
            job_item["autoflow"] = {
                "enabled": af.get("enabled", False),
                "deep_scoring": af.get("deep_scoring", True),
                "fw_import_approved": af.get("fw_import_approved", True),
                "fw_import_reviewed": af.get("fw_import_reviewed", False),
                "fw_import_rejected": af.get("fw_import_rejected", False),
                "thresholds": af.get("thresholds", {
                    "approve": load_config().get("score_threshold_approve", 7),
                    "reject": load_config().get("score_threshold_reject", 4),
                })
            }

        result.append({
            "id": p_name, "name": p_data.get("name", p_name),
            "email": p_data.get("imap", {}).get("user", "") if p_data.get("source", "imap") == "imap" else "HH API",
            "source": p_data.get("source", "imap"),
            "enabled": p_ctrl.get("enabled", True),
            "report_enabled": p_ctrl.get("report_enabled", True),
            "jobs": jobs
        })
    return {"profiles": result}

@app.post("/api/controls/toggle")
async def api_toggle_control(request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    controls = load_controls()
    profile = body.get("profile")
    field = body.get("field", "enabled")  # enabled, report_enabled, or job slug
    
    if profile not in controls.get("profiles", {}):
        controls.setdefault("profiles", {})[profile] = {"enabled": True, "report_enabled": True, "jobs": {}}
    
    p = controls["profiles"][profile]
    if field == "enabled":
        p["enabled"] = not p.get("enabled", True)
    elif field == "report_enabled":
        p["report_enabled"] = not p.get("report_enabled", True)
    else:
        # Toggle job
        p.setdefault("jobs", {})[field] = not p.get("jobs", {}).get(field, True)
    
    save_controls(controls)
    return {"ok": True, "controls": controls}

# ─── API: Autoflow settings per job ───
@app.get("/api/autoflow/{profile_id}/{job_slug}")
async def api_get_autoflow(profile_id: str, job_slug: str, key: str = Query("")):
    check_key(key)
    controls = load_controls()
    p = controls.get("profiles", {}).get(profile_id, {})
    af = p.get("autoflow", {}).get(job_slug, {})
    defaults = {
        "enabled": False,
        "deep_scoring": True,
        "fw_import_approved": True,
        "fw_import_reviewed": False,
        "fw_import_rejected": False,
        "thresholds": {
            "approve": load_config().get("score_threshold_approve", 7),
            "reject": load_config().get("score_threshold_reject", 4),
        }
    }
    defaults.update(af)
    if "thresholds" in af:
        defaults["thresholds"].update(af["thresholds"])
    return defaults

@app.post("/api/autoflow/{profile_id}/{job_slug}")
async def api_set_autoflow(profile_id: str, job_slug: str, request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    controls = load_controls()
    p = controls.setdefault("profiles", {}).setdefault(profile_id, {"enabled": True, "report_enabled": True, "jobs": {}})
    af = p.setdefault("autoflow", {}).setdefault(job_slug, {})
    # Update only provided fields
    for field in ("enabled", "deep_scoring", "fw_import_approved", "fw_import_reviewed", "fw_import_rejected"):
        if field in body:
            af[field] = bool(body[field])
    if "thresholds" in body:
        af.setdefault("thresholds", {}).update(body["thresholds"])
    save_controls(controls)
    return {"ok": True, "autoflow": af}

@app.post("/api/autoflow/{profile_id}/{job_slug}/toggle")
async def api_toggle_autoflow(profile_id: str, job_slug: str, request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    field = body.get("field", "enabled")
    controls = load_controls()
    p = controls.setdefault("profiles", {}).setdefault(profile_id, {"enabled": True, "report_enabled": True, "jobs": {}})
    af = p.setdefault("autoflow", {}).setdefault(job_slug, {"enabled": False})
    if field in ("enabled", "deep_scoring", "fw_import_approved", "fw_import_reviewed", "fw_import_rejected"):
        af[field] = not af.get(field, False)
    save_controls(controls)
    return {"ok": True, "autoflow": af}

# ─── API: Routes (email profile → FW vacancy) ───
@app.get("/api/routes")
async def api_get_routes(key: str = Query("")):
    check_key(key)
    cfg = load_config()
    return {"routes": cfg.get("routes", {})}

@app.post("/api/routes")
async def api_set_routes(request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    cfg = load_config()
    cfg["routes"] = body.get("routes", {})
    save_config(cfg)
    return {"ok": True}

# ─── API: Vacancies (for route picker and scoring) ───
@app.get("/api/vacancies")
async def api_vacancies(key: str = Query("")):
    check_key(key)
    try:
        from fw_import import get_fw_open_vacancies
        vacancies = get_fw_open_vacancies()
        result = []
        for j in vacancies:
            v = {"id": j["id"], "name": j["name"], "status": j.get("status", "Open"), "new_count": None}
            cache = DATA_DIR / f"vacancy_cache_{j['id']}.json"
            if cache.exists():
                try:
                    cached = json.loads(cache.read_text())
                    v["new_count"] = len(cached)
                except: pass
            result.append(v)
        return {"vacancies": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ─── API: Today's results from email screening ───
@app.get("/api/today/{profile_id}/{job_slug}")
async def api_today_results(profile_id: str, job_slug: str, key: str = Query(""), date: str = Query("")):
    check_key(key)
    db_path = DATA_DIR / f"{profile_id}.db"
    if not db_path.exists():
        return {"candidates": [], "total": 0, "relevant": 0, "date": ""}
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    target_date = date if date else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cur.execute(
        "SELECT candidate_title, last_job, salary, normalized_link, reason, confidence, relevant, fit_type "
        "FROM scored_candidates WHERE date(run_date) = ? AND job_slug = ? ORDER BY relevant DESC, confidence DESC",
        (target_date, job_slug)
    )
    rows = cur.fetchall()
    # Also return available dates
    dates = [r[0] for r in cur.execute(
        "SELECT DISTINCT date(run_date) FROM scored_candidates WHERE job_slug = ? ORDER BY 1 DESC LIMIT 7", (job_slug,)
    ).fetchall()]
    conn.close()
    candidates = []
    for r in rows:
        candidates.append({
            "title": r[0], "last_job": r[1], "salary": r[2], "link": r[3],
            "reason": r[4], "confidence": r[5], "relevant": bool(r[6]), "fit_type": r[7]
        })
    relevant = sum(1 for c in candidates if c["relevant"])
    target = sum(1 for c in candidates if c["relevant"] and c["fit_type"] == "target")
    near = sum(1 for c in candidates if c["relevant"] and c["fit_type"] != "target")
    return {"candidates": candidates, "total": len(candidates), "relevant": relevant, "target": target, "near": near, "date": target_date, "dates": dates}

# ─── API: Import candidates to FriendWork ───
@app.post("/api/import")
async def api_import_to_fw(request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    fw_job_id = body.get("vacancy_id")
    links = body.get("links", [])  # list of HH resume URLs
    if not fw_job_id or not links:
        return JSONResponse({"error": "vacancy_id and links required"}, status_code=400)
    
    from fw_import import import_hh_to_fw, extract_resume_id_from_url
    
    # Load vacancy names for resolving job_ids
    vacancy_names = {}
    try:
        from fw_import import get_fw_open_vacancies
        for v in get_fw_open_vacancies():
            vacancy_names[v['id']] = v['name']
    except: pass
    
    results = []
    for link in links:
        resume_id = extract_resume_id_from_url(link)
        if not resume_id:
            results.append({"link": link, "ok": False, "candidate_id": None, "message": "Не удалось извлечь resume_id"})
            continue
        try:
            r = import_hh_to_fw(resume_id, fw_job_id)
            # Resolve job_ids to names
            if r.get("job_ids"):
                job_names = [vacancy_names.get(jid, f"#{jid}") for jid in r["job_ids"][:3]]
                r["vacancies"] = job_names
            results.append({"link": link, **r})
        except Exception as e:
            results.append({"link": link, "ok": False, "candidate_id": None, "message": str(e)[:100]})
        await asyncio.sleep(1)  # rate limit
    
    ok_count = sum(1 for r in results if r.get("ok"))
    dup_count = sum(1 for r in results if "Дубликат" in r.get("message", ""))
    return {"results": results, "imported": ok_count, "duplicates": dup_count, "total": len(results)}

# ─── API: Candidates for vacancy ───
@app.get("/api/vacancy/{vacancy_id}/candidates")
async def api_candidates(vacancy_id: int, key: str = Query(""), refresh: str = Query("")):
    check_key(key)
    cache_path = DATA_DIR / f"vacancy_cache_{vacancy_id}.json"
    if cache_path.exists() and refresh != "1":
        try:
            candidates = json.loads(cache_path.read_text())
            return {"candidates": candidates, "total": len(candidates)}
        except: pass
    try:
        import requests as req_lib
        from fw_import import get_fw_headers
        headers = get_fw_headers()
        candidates = await _fetch_vacancy_candidates(vacancy_id, headers, req_lib)
        cache_path.write_text(json.dumps(candidates, ensure_ascii=False))
        return {"candidates": candidates, "total": len(candidates)}
    except Exception as e:
        return JSONResponse({"error": str(e), "candidates": [], "total": 0}, status_code=500)

async def _fetch_vacancy_candidates(vacancy_id, headers, req_lib):
    """Fetch candidates whose CURRENT status is 'Новый' on a vacancy."""
    potential = {}
    min_id = 200000000
    for _ in range(200):
        for attempt in range(3):
            try:
                r = req_lib.post("https://api.friend.work/Candidate/CandidatesHistories",
                    headers=headers, json={"Count": 1000, "MinCandidateHistoryId": min_id}, timeout=90)
                break
            except Exception:
                await asyncio.sleep(2 ** (attempt + 1))
        else:
            break
        if r.status_code != 200:
            break
        histories = r.json().get("CandidateHistories", [])
        if not histories:
            break
        for h in histories:
            if h.get("JobId") == vacancy_id:
                cid = h.get("CandidateId")
                if cid and cid not in potential:
                    potential[cid] = h
        min_id = max(h.get("CandidateHistoryId", 0) for h in histories) + 1
        if len(histories) < 1000:
            break
        await asyncio.sleep(0.5)
    
    confirmed = []
    for cid, h in potential.items():
        try:
            r = req_lib.get(f"https://api.friend.work/Candidate/{cid}/CandidateHistories", headers=headers, timeout=60)
            if r.status_code == 200:
                ch = r.json().get("CandidateHistories", [])
                vac = [x for x in ch if x.get("JobId") == vacancy_id]
                if vac:
                    latest = max(vac, key=lambda x: x.get("CandidateHistoryId", 0))
                    if latest.get("Name") == "Новый":
                        confirmed.append({
                            "candidateId": cid, "statusName": "Новый",
                            "comment": "", "date": latest.get("DateCreated", "")[:10],
                            "firstName": h.get("FirstName", h.get("firstName", "")),
                            "lastName": h.get("LastName", h.get("lastName", "")),
                        })
        except Exception:
            confirmed.append({"candidateId": cid, "statusName": "Новый", "comment": "", "date": "", "firstName": "", "lastName": ""})
        await asyncio.sleep(0.3)
    return confirmed

# ─── API: Scoring ───
@app.get("/api/scoring/{vacancy_id}/status")
async def scoring_status(vacancy_id: int, key: str = Query("")):
    check_key(key)
    return scoring_state.get(vacancy_id, {"status": "idle"})

@app.post("/api/scoring/{vacancy_id}/start")
async def scoring_start(vacancy_id: int, request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    model = body.get("model", load_config().get("default_model", "gpt-5.2"))
    prompt_name = body.get("prompt", "default_am")
    if vacancy_id in scoring_state and scoring_state[vacancy_id].get("status") == "running":
        return JSONResponse({"error": "Already running"}, status_code=409)
    scoring_state[vacancy_id] = {"status": "running", "progress": 0, "total": 0, "results": [], "model": model}
    asyncio.create_task(_run_scoring(vacancy_id, model, prompt_name))
    return {"status": "started", "model": model}

async def _run_scoring(vacancy_id: int, model: str, prompt_name: str):
    try:
        import requests as req_lib
        from openai import OpenAI
        from fw_import import get_fw_headers
        
        state = scoring_state[vacancy_id]
        prompt_path = PROMPTS_DIR / f"{prompt_name}.txt"
        prompt = prompt_path.read_text() if prompt_path.exists() else (DEFAULT_PROMPT_PATH.read_text() if DEFAULT_PROMPT_PATH.exists() else "Оцени кандидата.")
        headers = get_fw_headers()
        
        # Fetch candidates
        cand_list = await _fetch_vacancy_candidates(vacancy_id, headers, req_lib)
        cache_path = DATA_DIR / f"vacancy_cache_{vacancy_id}.json"
        cache_path.write_text(json.dumps(cand_list, ensure_ascii=False))
        
        state["total"] = len(cand_list)
        if not cand_list:
            state["status"] = "done"
            state["message"] = "Нет кандидатов в статусе 'Новый'"
            return
        
        # Load HH URL map
        import_log_path = DATA_DIR / "fw_imports.json"
        hh_url_map = {}
        if import_log_path.exists():
            try:
                hh_url_map = {v: k for k, v in json.loads(import_log_path.read_text()).items()}
            except: pass
        
        # HH API
        hh_get_resume = None
        try:
            from hh_api import get_resume as _hh_get
            hh_get_resume = _hh_get
        except: pass
        
        oai = OpenAI(api_key=OPENAI_KEY)
        json_fmt = 'Ответь СТРОГО JSON без markdown (формат — см. системный промпт).'
        
        for idx, cand in enumerate(cand_list):
            cid = cand["candidateId"]
            cand_name = cand.get("firstName", "")
            cand_last = cand.get("lastName", "")
            cand_text = ""
            
            # Try HH API for rich data
            hh_url = hh_url_map.get(cid)
            if hh_url and hh_get_resume:
                try:
                    resume_id = hh_url.rstrip("/").split("/")[-1]
                    resume = hh_get_resume(resume_id)
                    if resume and not resume.get("errors"):
                        title = resume.get("title", "")
                        area = resume.get("area", {}).get("name", "")
                        salary = resume.get("salary")
                        sal_text = f"{salary['amount']} {salary.get('currency','')}" if salary else "не указана"
                        total_exp = resume.get("total_experience") or {}
                        exp_months = total_exp.get("months", 0)
                        cand_text = f"Должность: {title}\nГород: {area}\nЗарплата: {sal_text}\nОпыт: {exp_months//12} лет {exp_months%12} мес\n"
                        for exp in resume.get("experience", [])[:5]:
                            cand_text += f"\nОпыт: {exp.get('company','')} — {exp.get('position','')} ({exp.get('start','')}-{exp.get('end','н.в.')})\n"
                            if exp.get("description"):
                                cand_text += f"  {exp['description'][:300]}\n"
                        skills = resume.get("skill_set", [])
                        if skills:
                            sk = [s if isinstance(s, str) else s.get("name","") for s in skills]
                            cand_text += f"\nНавыки: {', '.join(sk[:15])}\n"
                        cand_name = resume.get("first_name") or cand_name
                        cand_last = resume.get("last_name") or cand_last
                except Exception:
                    pass
            
            # Fallback: FW history
            if not cand_text:
                try:
                    r = req_lib.get(f"https://api.friend.work/Candidate/{cid}/CandidateHistories", headers=headers, timeout=30)
                    ch = r.json().get("CandidateHistories", []) if r.status_code == 200 else []
                    cand_text = f"Кандидат ID: {cid}\n"
                    for h in ch:
                        if h.get("Description"):
                            cand_text += f"Описание: {h['Description']}\n"
                except:
                    cand_text = f"Кандидат ID: {cid}\n"
            
            cand_text = f"Имя: {cand_name} {cand_last}\n" + cand_text
            
            # Score
            try:
                if model.startswith("gpt"):
                    resp = oai.chat.completions.create(
                        model=model, temperature=0.1, max_completion_tokens=500,
                        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": f"Оцени кандидата:\n\n{cand_text}\n\n{json_fmt}"}]
                    )
                    raw = resp.choices[0].message.content.strip()
                else:
                    raw = json.dumps({"verdict": "needs_review", "score": 0, "reason": "Claude API не настроен"})
                result = json.loads(raw.replace('```json','').replace('```','').strip())
            except Exception as e:
                result = {"verdict": "error", "score": 0, "reason": str(e)[:80]}
            
            result["candidateId"] = cid
            result["candidateName"] = f"{cand_name} {cand_last}".strip() or f"CID-{cid}"
            
            # Set FW status
            cfg = load_config()
            score = result.get("score", 0)
            if score >= cfg.get("score_threshold_approve", 7):
                fw_status = "Одобрен ИИ"
            elif 0 < score < cfg.get("score_threshold_reject", 4):
                fw_status = "Отказ ИИ"
            elif 0 < score:
                fw_status = "Просмотрен ИИ"
            else:
                fw_status = None
            result["fw_status"] = fw_status or "Новый"
            
            if fw_status and result.get("verdict") != "error":
                comment = f"[AI {model}] Оценка: {score}/10 — {result.get('reason', '')}"
                try:
                    fw_set = req_lib.post(
                        f"https://api.friend.work/Candidate/{cid}/CandidateHistories/set",
                        headers=headers, timeout=15,
                        json={"Name": fw_status, "JobId": vacancy_id, "Description": comment}
                    )
                    result["fw_status_set"] = fw_set.status_code == 200
                except:
                    result["fw_status_set"] = False
            
            state["results"].append(result)
            state["progress"] = idx + 1
            await asyncio.sleep(0.5)
        
        state["status"] = "done"
    except Exception as e:
        scoring_state[vacancy_id] = {"status": "error", "error": str(e)}

# ─── API: Deep Scoring (from HH links, no FW) ───
@app.post("/api/deep-scoring/start")
async def deep_scoring_start(request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    job_key = body.get("job_key", "")  # "profileId.jobSlug"
    links = body.get("links", [])
    model = body.get("model", load_config().get("default_model", "gpt-5.2"))
    prompt_name = body.get("prompt", "default_am")
    thresholds = body.get("thresholds")  # optional per-job thresholds
    if not links:
        return JSONResponse({"error": "No links provided"}, status_code=400)
    if job_key in deep_scoring_state and deep_scoring_state[job_key].get("status") == "running":
        return JSONResponse({"error": "Already running"}, status_code=409)
    deep_scoring_state[job_key] = {"status": "running", "progress": 0, "total": len(links), "results": [], "model": model}
    asyncio.create_task(_run_deep_scoring(job_key, links, model, prompt_name, thresholds))
    return {"status": "started", "total": len(links)}

async def _run_deep_scoring(job_key: str, links: list, model: str, prompt_name: str, thresholds: dict = None):
    try:
        from openai import OpenAI
        state = deep_scoring_state[job_key]
        prompt_path = PROMPTS_DIR / f"{prompt_name}.txt"
        prompt = prompt_path.read_text() if prompt_path.exists() else "Оцени кандидата."
        
        hh_get_resume = None
        try:
            from hh_api import get_resume as _hh_get
            hh_get_resume = _hh_get
        except: pass
        
        oai = OpenAI(api_key=OPENAI_KEY)
        json_fmt = 'Ответь СТРОГО JSON без markdown (формат — см. системный промпт).'
        cfg = load_config()
        # Use per-job thresholds if provided, else global
        t_approve = (thresholds or {}).get("approve") or cfg.get("score_threshold_approve", 7)
        t_reject = (thresholds or {}).get("reject") or cfg.get("score_threshold_reject", 4)
        
        for idx, link in enumerate(links):
            cand_text = ""
            cand_name = ""
            resume_id = link.rstrip("/").split("/")[-1].split("?")[0]
            
            # Fetch from HH API (or cache)
            resume = _load_cached_resume(resume_id)
            if not resume and hh_get_resume:
                try:
                    resume = hh_get_resume(resume_id)
                    if resume and not resume.get("errors"):
                        _cache_hh_resume(resume_id, resume)
                    else:
                        resume = None
                except:
                    resume = None
            if resume and not resume.get("errors"):
                try:
                    title = resume.get("title", "")
                    area = resume.get("area", {}).get("name", "")
                    salary = resume.get("salary")
                    sal_text = f"{salary['amount']} {salary.get('currency','')}" if salary else "не указана"
                    total_exp = resume.get("total_experience") or {}
                    exp_months = total_exp.get("months", 0)
                    fn = resume.get('first_name') or ''
                    ln = resume.get('last_name') or ''
                    cand_name = f"{ln} {fn}".strip()
                    if not cand_name or cand_name == 'None None' or cand_name == 'None':
                        cand_name = title or f"HH-{resume_id[:8]}"
                    cand_text = f"Кандидат: {cand_name}\nДолжность: {title}\nГород: {area}\nЗарплата: {sal_text}\nОпыт: {exp_months//12} лет {exp_months%12} мес\n"
                    for exp in resume.get("experience", [])[:5]:
                        cand_text += f"\nОпыт: {exp.get('company','')} — {exp.get('position','')} ({exp.get('start','')}-{exp.get('end','н.в.')})\n"
                        if exp.get("description"):
                            cand_text += f"  {exp['description'][:300]}\n"
                    skills = resume.get("skill_set", [])
                    if skills:
                        sk = [s if isinstance(s, str) else s.get("name","") for s in skills]
                        cand_text += f"\nНавыки: {', '.join(sk[:15])}\n"
                except Exception as e:
                    cand_text = f"Ошибка загрузки резюме: {e}"
            
            if not cand_text:
                cand_text = f"Резюме HH: {link}"
            
            # Score
            try:
                resp = oai.chat.completions.create(
                    model=model, temperature=0.1, max_completion_tokens=500,
                    messages=[{"role": "system", "content": prompt}, {"role": "user", "content": f"Оцени кандидата:\n\n{cand_text}\n\n{json_fmt}"}]
                )
                raw = resp.choices[0].message.content.strip()
                result = json.loads(raw.replace('```json','').replace('```','').strip())
            except Exception as e:
                result = {"verdict": "error", "score": 0, "reason": str(e)[:80]}
            
            score = result.get("score", 0)
            if score >= t_approve:
                fw_status = "Одобрен ИИ"
            elif 0 < score < t_reject:
                fw_status = "Отказ ИИ"
            elif 0 < score:
                fw_status = "Просмотрен ИИ"
            else:
                fw_status = "Новый"
            
            result["link"] = link
            result["candidateName"] = cand_name or f"HH-{resume_id[:8]}"
            result["fw_status"] = fw_status
            result["resume_id"] = resume_id
            
            # Save journey
            parts = job_key.split(".", 1)
            try:
                _save_journey({
                    "hh_link": link, "resume_id": resume_id,
                    "profile_id": parts[0] if len(parts) > 0 else "",
                    "job_slug": parts[1] if len(parts) > 1 else "",
                    "deep_date": datetime.now(timezone.utc).isoformat(),
                    "deep_model": model, "deep_prompt": prompt_name,
                    "deep_score": score, "deep_status": fw_status,
                    "deep_reason": result.get("reason", "")
                })
            except: pass
            
            state["results"].append(result)
            state["progress"] = idx + 1
            await asyncio.sleep(0.3)
        
        state["status"] = "done"
    except Exception as e:
        deep_scoring_state[job_key] = {"status": "error", "error": str(e)}

@app.get("/api/deep-scoring/{job_key}/stream")
async def deep_scoring_stream(job_key: str, key: str = Query("")):
    check_key(key)
    async def gen():
        last = -1
        while True:
            state = deep_scoring_state.get(job_key, {"status": "idle"})
            progress = state.get("progress", 0)
            if progress != last or state["status"] in ("done", "error"):
                yield {"event": "update", "data": json.dumps(state, ensure_ascii=False)}
                last = progress
            if state["status"] in ("done", "error", "idle"):
                break
            await asyncio.sleep(1)
    return EventSourceResponse(gen())

# ─── API: Import scored candidates to FW ───
@app.post("/api/import-scored")
async def api_import_scored(request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    vacancy_id = body.get("vacancy_id")
    candidates = body.get("candidates", [])  # [{link, fw_status, score, reason, candidateName}, ...]
    model = body.get("model", "gpt-5.2")
    if not vacancy_id or not candidates:
        return JSONResponse({"error": "vacancy_id and candidates required"}, status_code=400)
    
    from fw_import import import_hh_to_fw, extract_resume_id_from_url, get_fw_open_vacancies
    import requests as req_lib
    from fw_import import get_fw_headers
    
    vacancy_names = {}
    try:
        for v in get_fw_open_vacancies():
            vacancy_names[v['id']] = v['name']
    except: pass
    
    headers = get_fw_headers()
    results = []
    imported = 0
    duplicates = 0
    
    for cand in candidates:
        link = cand.get("link", "")
        resume_id = extract_resume_id_from_url(link)
        if not resume_id:
            results.append({"link": link, "ok": False, "message": "Bad link"})
            continue
        
        fw_status = cand.get("fw_status", "Новый")
        score = cand.get("score", 0)
        reason = cand.get("reason", "")
        comment = f"[AI {model}] Оценка: {score}/10 — {reason}"
        
        try:
            res = import_hh_to_fw(resume_id, vacancy_id)
            cid = res.get("candidate_id")
            is_new = res.get("ok", False)
            
            # Set status in FW
            if cid and fw_status != "Новый":
                try:
                    req_lib.post(
                        f"https://api.friend.work/Candidate/{cid}/CandidateHistories/set",
                        headers=headers, timeout=15,
                        json={"Name": fw_status, "JobId": vacancy_id, "Description": comment}
                    )
                except: pass
            
            job_names = []
            if res.get("job_ids"):
                job_names = [vacancy_names.get(jid, f"#{jid}") for jid in res["job_ids"]]
            
            if is_new:
                imported += 1
            else:
                duplicates += 1
            
            # Update journey with FW import info
            try:
                conn = sqlite3.connect(str(JOURNEY_DB))
                conn.execute(
                    "UPDATE journey SET fw_candidate_id=?, fw_vacancy_id=?, fw_import_date=?, fw_status=? WHERE hh_link=?",
                    (cid, vacancy_id, datetime.now(timezone.utc).isoformat(), fw_status, link)
                )
                conn.commit()
                conn.close()
            except: pass
            
            results.append({
                "link": link, "ok": is_new, "candidate_id": cid,
                "fw_status": fw_status, "vacancies": job_names,
                "message": res.get("message", "")
            })
        except Exception as e:
            results.append({"link": link, "ok": False, "candidate_id": None, "fw_status": fw_status, "vacancies": [], "message": str(e)[:100]})
    
    return {"results": results, "imported": imported, "duplicates": duplicates, "total": len(results)}

# ─── API: Config ───
@app.get("/api/config")
async def api_get_config(key: str = Query("")):
    check_key(key)
    return load_config()

@app.post("/api/config")
async def api_set_config(request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    cfg = load_config()
    cfg.update(body)
    save_config(cfg)
    return {"status": "ok"}

# ─── API: Schedule ───
@app.get("/api/schedule")
async def api_get_schedule(key: str = Query("")):
    check_key(key)
    cfg = load_config()
    return {"time_msk": cfg.get("schedule_time_msk", "10:00")}

@app.post("/api/schedule")
async def api_set_schedule(request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    time_msk = body.get("time_msk", "10:00")
    cfg = load_config()
    cfg["schedule_time_msk"] = time_msk
    save_config(cfg)
    # Convert MSK to UTC (MSK = UTC+3)
    try:
        h, m = map(int, time_msk.split(":"))
        utc_h = (h - 3) % 24
        utc_time = f"{utc_h:02d}:{m:02d}"
        # Update systemd timer
        import subprocess
        timer_content = f"""[Unit]
Description=HR Radar Multi-Profile Timer
Requires=hr-radar-multi.service

[Timer]
OnCalendar=*-*-* {utc_time}:00
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
"""
        Path("/etc/systemd/system/hr-radar-multi.timer").write_text(timer_content)
        subprocess.run(["systemctl", "daemon-reload"], timeout=10)
        subprocess.run(["systemctl", "restart", "hr-radar-multi.timer"], timeout=10)
        return {"ok": True, "time_msk": time_msk, "time_utc": utc_time}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ─── API: Models ───
@app.get("/api/models")
async def api_models(key: str = Query("")):
    check_key(key)
    return {"models": [
        {"id": "gpt-5.2", "name": "GPT-5.2", "price": "$0.01/кандидат", "quality": "⭐⭐⭐⭐⭐"},
        {"id": "gpt-4o", "name": "GPT-4o", "price": "$0.005/кандидат", "quality": "⭐⭐⭐"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "price": "$0.005/кандидат", "quality": "⭐⭐⭐⭐"},
        {"id": "claude-opus-4-6", "name": "Claude Opus 4.6", "price": "$0.02/кандидат", "quality": "⭐⭐⭐⭐⭐"},
    ]}

# ─── API: Prompts ───
@app.get("/api/prompts")
async def api_prompts(key: str = Query("")):
    check_key(key)
    prompts = []
    for f in sorted(PROMPTS_DIR.glob("*.txt")):
        prompts.append({"name": f.stem, "size": f.stat().st_size})
    return {"prompts": prompts}

@app.get("/api/prompt/{name}")
async def api_get_prompt(name: str, key: str = Query("")):
    check_key(key)
    p = PROMPTS_DIR / f"{name}.txt"
    if not p.exists():
        raise HTTPException(404)
    return {"name": name, "content": p.read_text()}

@app.post("/api/prompt/{name}")
async def api_set_prompt(name: str, request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    (PROMPTS_DIR / f"{name}.txt").write_text(body["content"])
    return {"status": "ok", "name": name}

# ─── SSE: Scoring stream ───
@app.get("/api/scoring/{vacancy_id}/stream")
async def scoring_stream(vacancy_id: int, key: str = Query("")):
    check_key(key)
    async def gen():
        last = -1
        while True:
            state = scoring_state.get(vacancy_id, {"status": "idle"})
            progress = state.get("progress", 0)
            if progress != last or state["status"] in ("done", "error"):
                yield {"event": "update", "data": json.dumps(state, ensure_ascii=False)}
                last = progress
            if state["status"] in ("done", "error", "idle"):
                break
            await asyncio.sleep(1)
    return EventSourceResponse(gen())

# ─── API: Retroscoring ───
retro_state = {}  # keyed by "profileId.jobSlug"

@app.post("/api/retroscore/{profile_id}/{job_slug}")
async def retroscore_start(profile_id: str, job_slug: str, request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    model = body.get("model", "gpt-4o")
    batch_size = body.get("batch_size", 15)
    state_key = f"{profile_id}.{job_slug}"
    if state_key in retro_state and retro_state[state_key].get("status") == "running":
        return JSONResponse({"error": "Already running"}, status_code=409)
    retro_state[state_key] = {"status": "running", "progress": 0, "total": 0, "relevant": 0, "results": [], "model": model}
    asyncio.create_task(_run_retroscore(profile_id, job_slug, model, batch_size))
    return {"status": "started", "model": model}

async def _run_retroscore(profile_id: str, job_slug: str, model: str, batch_size: int):
    state_key = f"{profile_id}.{job_slug}"
    state = retro_state[state_key]
    try:
        from openai import OpenAI
        import yaml

        # Load the primary scoring prompt for the new role
        profiles = load_profiles()
        profile_data = profiles.get("profiles", {}).get(profile_id, {})
        job_data = None
        for j in profile_data.get("jobs", []):
            if j["slug"] == job_slug:
                job_data = j
                break
        if not job_data:
            state["status"] = "error"
            state["error"] = f"Job {job_slug} not found in profile {profile_id}"
            return

        prompt_file = job_data.get("prompt_file", "")
        prompt_path = Path(__file__).parent.parent / "config" / "prompts" / prompt_file
        if not prompt_path.exists():
            state["status"] = "error"
            state["error"] = f"Prompt file {prompt_file} not found"
            return
        prompt = prompt_path.read_text()

        # Get all unique candidates from scored_candidates
        db_path = DATA_DIR / f"{profile_id}.db"
        if not db_path.exists():
            state["status"] = "error"
            state["error"] = f"Database {profile_id}.db not found"
            return

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        # Get unique candidates by normalized_link (excluding already scored for this job_slug)
        cur.execute("""
            SELECT DISTINCT normalized_link, candidate_title, last_job, salary
            FROM scored_candidates
            WHERE normalized_link IS NOT NULL AND normalized_link != ''
              AND normalized_link NOT IN (
                SELECT normalized_link FROM scored_candidates WHERE job_slug = ?
              )
        """, (job_slug,))
        candidates = cur.fetchall()
        conn.close()

        state["total"] = len(candidates)
        if not candidates:
            state["status"] = "done"
            state["message"] = "Нет новых кандидатов для ретроскоринга (все уже оценены или БД пуста)"
            return

        oai = OpenAI(api_key=OPENAI_KEY)
        now_str = datetime.now(timezone.utc).isoformat()

        # Process in batches
        for batch_start in range(0, len(candidates), batch_size):
            batch = candidates[batch_start:batch_start + batch_size]
            # Build batch input
            batch_text = ""
            for i, (link, title, last_job, salary) in enumerate(batch):
                batch_text += f"\n--- Кандидат {i+1} ---\n"
                batch_text += f"Резюме: {title or '—'}\n"
                batch_text += f"Последнее место: {last_job or '—'}\n"
                batch_text += f"Зарплата: {salary or '—'}\n"
                batch_text += f"HH: {link}\n"

            try:
                resp = oai.chat.completions.create(
                    model=model, temperature=0.1, max_completion_tokens=2000,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"Оцени {len(batch)} кандидатов:\n{batch_text}\n\nОтветь СТРОГО JSON-массивом без markdown."}
                    ]
                )
                raw = resp.choices[0].message.content.strip()
                results = json.loads(raw.replace('```json', '').replace('```', '').strip())
                if not isinstance(results, list):
                    results = [results]
            except Exception as e:
                results = [{"index": i+1, "relevant": False, "fit_type": "not_fit", "confidence": 0, "signals": [], "red_flags": [str(e)[:80]], "suggested_action": "error"} for i in range(len(batch))]

            # Save results to DB
            conn = sqlite3.connect(str(db_path))
            for i, (link, title, last_job, salary) in enumerate(batch):
                if i < len(results):
                    r = results[i]
                else:
                    r = {"relevant": False, "fit_type": "not_fit", "confidence": 0}
                relevant = 1 if r.get("relevant", False) else 0
                fit_type = r.get("fit_type", "not_fit")
                confidence = r.get("confidence", 0)
                reason = ", ".join(r.get("signals", [])) if r.get("signals") else r.get("suggested_action", "")
                if relevant:
                    state["relevant"] += 1

                conn.execute("""
                    INSERT INTO scored_candidates (candidate_title, last_job, salary, normalized_link, reason, confidence, relevant, fit_type, job_slug, run_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (title, last_job, salary, link, reason, confidence, relevant, fit_type, job_slug, now_str))

                state["results"].append({
                    "link": link, "title": title, "relevant": bool(relevant),
                    "fit_type": fit_type, "confidence": confidence
                })

            conn.commit()
            conn.close()
            state["progress"] = min(batch_start + len(batch), len(candidates))
            await asyncio.sleep(1)

        state["status"] = "done"
        state["message"] = f"Готово! Оценено {len(candidates)} кандидатов, релевантных: {state['relevant']}"
    except Exception as e:
        retro_state[state_key] = {"status": "error", "error": str(e)}

@app.get("/api/retroscore/{profile_id}/{job_slug}/stream")
async def retroscore_stream(profile_id: str, job_slug: str, key: str = Query("")):
    check_key(key)
    state_key = f"{profile_id}.{job_slug}"
    async def gen():
        last = -1
        while True:
            state = retro_state.get(state_key, {"status": "idle"})
            progress = state.get("progress", 0)
            # Send compact state (without full results list to reduce SSE payload)
            compact = {k: v for k, v in state.items() if k != "results"}
            compact["results_count"] = len(state.get("results", []))
            if progress != last or state["status"] in ("done", "error"):
                yield {"event": "update", "data": json.dumps(compact, ensure_ascii=False)}
                last = progress
            if state["status"] in ("done", "error", "idle"):
                break
            await asyncio.sleep(1)
    return EventSourceResponse(gen())

# ─── Zoom Webhook for Voice Recognition ───
@app.get("/zoom-webhook")
async def zoom_webhook_get():
    """Zoom webhook validation endpoint"""
    return "E7aXudtqS8iiIt4rlvClfw"

@app.post("/zoom-webhook")
async def zoom_webhook_post(request: Request):
    """Zoom webhook for recording notifications"""
    try:
        data = await request.json()
        event_type = data.get("event", "unknown")
        
        # Handle webhook validation challenge
        if event_type == "endpoint.url_validation":
            plain_token = data.get("payload", {}).get("plainToken")
            if plain_token:
                import hashlib
                import hmac
                secret_token = "E7aXudtqS8iiIt4rlvClfw"
                hash_for_verify = hmac.new(
                    secret_token.encode('utf-8'),
                    plain_token.encode('utf-8'),
                    hashlib.sha256
                ).hexdigest()
                print(f"✅ Zoom webhook validation: plainToken={plain_token}")
                return {
                    "plainToken": plain_token,
                    "encryptedToken": hash_for_verify
                }
        
        # Process recording webhooks
        print(f"📨 Zoom webhook received: {event_type}")
        
        if event_type == "recording.completed":
            import threading
            def _process_recording():
                try:
                    import importlib.util
                    spec = importlib.util.spec_from_file_location(
                        "zoom_processor",
                        "/root/.openclaw/workspace/voice-recognition-system/zoom_processor.py"
                    )
                    zp = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(zp)
                    result_path = zp.process_zoom_webhook(data)
                    if result_path:
                        print(f"✅ Zoom recording processed: {result_path}")
                except Exception as e:
                    print(f"❌ Zoom processing error: {e}")
            
            threading.Thread(target=_process_recording, daemon=True).start()
        
        return {"status": "success"}
        
    except Exception as e:
        print(f"❌ Zoom webhook error: {e}")
        return {"error": str(e)}, 500

# ─── Interview Analysis ───
import uuid
import threading
import subprocess
import requests as http_requests
import anthropic

INTERVIEWS_DIR = Path(__file__).parent.parent / "data" / "interviews"
INTERVIEWS_DIR.mkdir(parents=True, exist_ok=True)
INTERVIEW_UPLOADS_DIR = Path(__file__).parent.parent / "data" / "interview_uploads"
INTERVIEW_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
(INTERVIEW_UPLOADS_DIR / "vacancies").mkdir(exist_ok=True)
(INTERVIEW_UPLOADS_DIR / "resumes").mkdir(exist_ok=True)
(INTERVIEW_UPLOADS_DIR / "videos").mkdir(exist_ok=True)

# In-memory task state for SSE streaming
_interview_tasks = {}  # task_id -> {"stage": ..., "progress": ..., "result": ..., "error": ...}

def _get_prompts_list():
    """Return list of available prompt files."""
    prompts = []
    for f in sorted(PROMPTS_DIR.glob("*.txt")):
        prompts.append({"slug": f.stem, "name": f.stem.replace("_", " ").title(), "path": str(f)})
    return prompts

def _download_from_cloud_mail(public_url: str, dest_path: str) -> str:
    """Download file from cloud.mail.ru public link."""
    import re as _re
    session = http_requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })

    # Extract weblink from URL: https://cloud.mail.ru/public/HASH/filename
    parts = public_url.rstrip('/').split('/')
    pub_idx = parts.index('public')
    weblink = '/'.join(parts[pub_idx + 1:])

    # Step 1: Get dispatcher to find the download host
    disp_resp = session.get("https://cloud.mail.ru/api/v2/dispatcher", timeout=15)
    disp_data = disp_resp.json()
    
    # Get weblink_get URL from dispatcher (e.g. https://cloclo62.cloud.mail.ru/public/...)
    weblink_get = disp_data.get("body", {}).get("weblink_get", [])
    if weblink_get and isinstance(weblink_get, list):
        dl_base = weblink_get[0].get("url", "")
    else:
        dl_base = ""
    
    # Also get weblink_view URL for fallback
    weblink_view = disp_data.get("body", {}).get("weblink_view", [])
    if weblink_view and isinstance(weblink_view, list):
        view_base = weblink_view[0].get("url", "")
    else:
        view_base = ""

    # Step 2: Try to get download token
    token = ""
    try:
        # Visit the page first to get cookies
        session.get(public_url, allow_redirects=True, timeout=15)
        token_resp = session.post("https://cloud.mail.ru/api/v2/tokens/download", timeout=15)
        token = token_resp.json().get("body", token_resp.json().get("token", ""))
    except:
        pass

    # Step 3: Try downloading with weblink_get URL
    errors = []
    for base_url in [dl_base, view_base]:
        if not base_url:
            continue
        # Build download URL: base already has path, we need to append weblink
        # weblink_get URL format: https://clocloXX.cloud.mail.ru/public/...
        # We need: https://clocloXX.cloud.mail.ru/weblink/view/WEBLINK?key=TOKEN
        host = '/'.join(base_url.split('/')[:3])  # https://clocloXX.cloud.mail.ru
        dl_url = f"{host}/weblink/view/{weblink}"
        if token:
            dl_url += f"?key={token}"
        
        try:
            dl_resp = session.get(dl_url, stream=True, allow_redirects=True, timeout=300,
                                  headers={"Referer": public_url})
            content_length = int(dl_resp.headers.get('content-length', 0))
            content_type = dl_resp.headers.get('content-type', '')
            
            if dl_resp.status_code == 200 and (content_length > 1000 or 'video' in content_type or 'octet' in content_type):
                with open(dest_path, 'wb') as f:
                    for chunk in dl_resp.iter_content(chunk_size=65536):
                        f.write(chunk)
                return dest_path
            else:
                errors.append(f"{host}: status={dl_resp.status_code}, len={content_length}, type={content_type}")
        except Exception as e:
            errors.append(f"{host}: {str(e)[:100]}")

    # Step 4: Fallback — parse page HTML for direct links
    try:
        page_resp = session.get(public_url, allow_redirects=True, timeout=15)
        # Look for direct download URLs in page source
        matches = _re.findall(r'"(https?://cloclo\d+\.cloud\.mail\.ru/[^"]*)"', page_resp.text)
        for url in matches:
            try:
                dl_resp = session.get(url, stream=True, allow_redirects=True, timeout=300)
                if dl_resp.status_code == 200 and int(dl_resp.headers.get('content-length', 0)) > 1000:
                    with open(dest_path, 'wb') as f:
                        for chunk in dl_resp.iter_content(chunk_size=65536):
                            f.write(chunk)
                    return dest_path
            except:
                continue
    except:
        pass

    raise Exception(f"Failed to download from cloud.mail.ru. Errors: {'; '.join(errors)}")


def _download_direct(url: str, dest_path: str) -> str:
    """Download file from direct URL."""
    resp = http_requests.get(url, stream=True, allow_redirects=True, timeout=300)
    resp.raise_for_status()
    with open(dest_path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest_path


def _extract_audio(video_path: str, audio_path: str):
    """Extract audio from video using ffmpeg."""
    subprocess.run([
        "ffmpeg", "-i", video_path, "-vn", "-acodec", "libmp3lame",
        "-q:a", "4", "-y", audio_path
    ], check=True, capture_output=True)


def _split_audio_if_needed(audio_path: str, max_size_mb: int = 24) -> list:
    """Split audio file into chunks if it exceeds max_size_mb. Returns list of file paths."""
    file_size = os.path.getsize(audio_path)
    if file_size <= max_size_mb * 1024 * 1024:
        return [audio_path]

    # Get duration
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True
    )
    duration = float(result.stdout.strip())

    # Calculate chunk duration (aim for ~20MB chunks)
    num_chunks = int(file_size / (max_size_mb * 1024 * 1024)) + 1
    chunk_duration = duration / num_chunks

    chunks = []
    base = audio_path.rsplit('.', 1)[0]
    ext = audio_path.rsplit('.', 1)[1]

    for i in range(num_chunks):
        start = i * chunk_duration
        chunk_path = f"{base}_chunk{i}.{ext}"
        subprocess.run([
            "ffmpeg", "-i", audio_path, "-ss", str(start), "-t", str(chunk_duration),
            "-acodec", "libmp3lame", "-q:a", "4", "-y", chunk_path
        ], check=True, capture_output=True)
        chunks.append(chunk_path)

    return chunks


def _transcribe_audio(audio_path: str) -> str:
    """Transcribe audio with speaker diarization using AssemblyAI, fallback to Whisper."""
    # Try AssemblyAI first (has diarization)
    aai_key = os.environ.get("ASSEMBLY_AI_KEY", "")
    if aai_key:
        try:
            return _transcribe_with_assemblyai(audio_path, aai_key)
        except Exception as e:
            print(f"AssemblyAI failed, falling back to Whisper: {e}")

    # Fallback: Whisper (no diarization)
    from openai import OpenAI
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise Exception("No transcription API key configured (ASSEMBLY_AI_KEY or OPENAI_API_KEY)")

    client = OpenAI(api_key=api_key)
    chunks = _split_audio_if_needed(audio_path)

    transcripts = []
    for chunk_path in chunks:
        with open(chunk_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ru"
            )
        transcripts.append(transcript.text)

    if len(chunks) > 1:
        for chunk_path in chunks:
            try:
                os.remove(chunk_path)
            except:
                pass

    return " ".join(transcripts)


def _transcribe_with_assemblyai(audio_path: str, api_key: str) -> str:
    """Transcribe with AssemblyAI including speaker diarization via REST API."""
    import time as _time
    headers = {"authorization": api_key}

    # Step 1: Upload audio file
    with open(audio_path, "rb") as f:
        upload_resp = http_requests.post(
            "https://api.assemblyai.com/v2/upload",
            headers=headers, data=f, timeout=300
        )
    upload_resp.raise_for_status()
    upload_url = upload_resp.json()["upload_url"]

    # Step 2: Create transcript with diarization
    create_resp = http_requests.post(
        "https://api.assemblyai.com/v2/transcript",
        headers=headers,
        json={
            "audio_url": upload_url,
            "speaker_labels": True,
            "language_code": "ru",
            "speech_models": ["universal-3-pro", "universal-2"]
        },
        timeout=30
    )
    create_resp.raise_for_status()
    transcript_id = create_resp.json()["id"]

    # Step 3: Poll until complete (max 15 minutes)
    for _ in range(300):
        _time.sleep(3)
        poll_resp = http_requests.get(
            f"https://api.assemblyai.com/v2/transcript/{transcript_id}",
            headers=headers, timeout=30
        )
        data = poll_resp.json()
        status = data["status"]

        if status == "completed":
            utterances = data.get("utterances", [])
            if utterances:
                lines = []
                for u in utterances:
                    speaker = f"Спикер {u['speaker']}"
                    lines.append(f"**{speaker}:** {u['text']}")
                return "\n\n".join(lines)
            # No utterances — return plain text
            return data.get("text", "")

        elif status == "error":
            raise Exception(f"AssemblyAI error: {data.get('error', 'unknown')}")

    raise Exception("AssemblyAI timeout: transcription took too long")


def _extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from PDF file."""
    try:
        import subprocess
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    # Fallback: try with python
    try:
        import PyPDF2
        text = ""
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                text += page.extract_text() or ""
        return text.strip()
    except Exception:
        pass
    return ""


def _identify_speakers(transcript: str, client) -> str:
    """Identify who is the interviewer and who is the candidate, relabel speakers."""
    if "Спикер" not in transcript and "Speaker" not in transcript:
        return transcript  # No diarization, skip

    response = client.messages.create(
        model="claude-sonnet-4-20250514",  # Use fast model for this step
        max_tokens=100,
        system="Определи, кто из спикеров интервьюер (рекрутер), а кто кандидат. Обычно интервьюер задаёт вопросы, а кандидат отвечает и рассказывает о себе. Ответь СТРОГО в формате JSON: {\"interviewer\": \"A\", \"candidate\": \"B\"} — укажи буквы спикеров.",
        messages=[{"role": "user", "content": f"Начало транскрипции:\n\n{transcript[:3000]}"}]
    )
    
    try:
        import re
        raw = response.content[0].text.strip()
        match = re.search(r'\{[^}]+\}', raw)
        if match:
            roles = json.loads(match.group(0))
            interviewer = roles.get("interviewer", "A")
            candidate = roles.get("candidate", "B")
            # Replace speaker labels
            result = transcript
            result = result.replace(f"**Спикер {interviewer}:**", "**🎤 Интервьюер:**")
            result = result.replace(f"**Спикер {candidate}:**", "**👤 Кандидат:**")
            # Handle remaining speakers
            for letter in "CDEFGH":
                result = result.replace(f"**Спикер {letter}:**", f"**Спикер {letter}:**")
            return result
    except:
        pass
    return transcript


def _analyze_interview(transcript: str, vacancy_prompt: str, model: str, vacancy_pdf_text: str = "", resume_pdf_text: str = "") -> dict:
    """Analyze interview transcript using Claude."""
    client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env

    vacancy_section = f"Промпт вакансии:\n{vacancy_prompt}"
    if vacancy_pdf_text:
        vacancy_section += f"\n\nПодробное описание вакансии (из PDF):\n{vacancy_pdf_text}"
    
    resume_section = ""
    if resume_pdf_text:
        resume_section = f"\n\nРезюме кандидата:\n{resume_pdf_text}\n\nСопоставь информацию из резюме с тем, что кандидат рассказал на интервью. Отметь расхождения и подтверждения."

    system_prompt = f"""Ты — экспертный HR-аналитик. Проанализируй транскрипцию интервью кандидата.
В транскрипции реплики подписаны: 🎤 Интервьюер (рекрутер) и 👤 Кандидат. Анализируй только ответы кандидата, вопросы интервьюера используй для контекста.

{vacancy_section}{resume_section}

ВАЖНО: Для ключевых утверждений приводи 1-2 конкретные цитаты кандидата из интервью (в кавычках). Не нужно цитировать всё — только самые показательные моменты.

Дай структурированный анализ в формате JSON (только JSON, без markdown):
{{
    "summary": "Краткое резюме интервью (2-3 предложения)",
    "vacancy_fit": {{
        "score": <число от 1 до 10>,
        "relevant_experience": ["что подтвердилось — кратко, с ключевой цитатой"],
        "hard_skills": ["навыки"],
        "gaps": ["чего не хватает"]
    }},
    "psychological_profile": {{
        "thinking_type": "3-4 предложения: тип мышления + ключевая цитата-подтверждение",
        "communication_style": "3-4 предложения: стиль коммуникации + пример из речи",
        "leadership": "3-4 предложения: лидерские качества + цитата",
        "stress_resistance": "2-3 предложения: стрессоустойчивость + пример",
        "motivation": "3-4 предложения: мотивация, red flags + цитата",
        "emotional_intelligence": "2-3 предложения: эмпатия, работа с людьми + пример",
        "values_and_culture": "2-3 предложения: ценности, культурный fit + цитата"
    }},
    "speech_analysis": {{
        "confidence_level": "2-3 предложения + пример",
        "specificity": "2-3 предложения + пример",
        "self_presentation": "2-3 предложения + пример",
        "red_flags": ["red flags с краткой цитатой"]
    }},
    "psychotype_analysis": {{
        "accentuation": "Акцентуация по Личко/Леонгарду (2-3 предложения): какой тип (гипертимный/истероидный/шизоидный/эпилептоидный/лабильный и т.д.), почему, как проявляется в речи",
        "enneagram": "Эннеаграмма (2-3 предложения): тип 1-9, как проявляется",
        "disc": "DISC-профиль (2-3 предложения): D/I/S/C доминанта, как проявляется в коммуникации",
        "conflict_style": "Стиль поведения в конфликте (2-3 предложения): избегание/компромисс/конкуренция/сотрудничество/приспособление"
    }},
    "overall": {{
        "strengths": ["топ-3 сильные стороны — кратко с обоснованием"],
        "risks": ["топ-3 зоны риска — кратко с обоснованием"],
        "recommendation": "рекомендация (2-3 предложения)",
        "next_interview_questions": ["5-7 вопросов для следующего интервью с заказчиком"]
    }}
}}"""

    response = client.messages.create(
        model=model,
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"Транскрипция интервью:\n\n{transcript}"
        }],
        system=system_prompt
    )

    text = response.content[0].text.strip()
    # Try to extract JSON from response
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _generate_interview_pdf(task_id: str, analysis: dict, vacancy: str, model: str) -> str:
    """Generate a beautiful PDF report from interview analysis."""
    from weasyprint import HTML

    score = analysis.get("vacancy_fit", {}).get("score", 0)
    score_color = "#22c55e" if score >= 7 else "#eab308" if score >= 5 else "#ef4444"

    def _render_list(items):
        if not items:
            return "<li>—</li>"
        return "".join(f"<li>{item}</li>" for item in items)

    def _render_field(val):
        if isinstance(val, list):
            return "<ul>" + _render_list(val) + "</ul>"
        return f"<p>{val}</p>"

    psych = analysis.get("psychological_profile", {})
    speech = analysis.get("speech_analysis", {})
    psychotype = analysis.get("psychotype_analysis", {})
    overall = analysis.get("overall", {})
    fit = analysis.get("vacancy_fit", {})

    html_content = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<style>
@page {{ margin: 2cm; size: A4; }}
body {{ font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 11pt; line-height: 1.6; color: #1f2937; }}
h1 {{ font-size: 20pt; color: #1e3a5f; border-bottom: 2px solid #3b82f6; padding-bottom: 8px; }}
h2 {{ font-size: 14pt; color: #1e3a5f; margin-top: 20px; border-left: 4px solid #3b82f6; padding-left: 10px; }}
h3 {{ font-size: 12pt; color: #374151; margin-top: 12px; }}
.score-box {{ display: inline-block; font-size: 36pt; font-weight: bold; color: {score_color}; border: 3px solid {score_color}; border-radius: 12px; padding: 8px 20px; margin: 10px 0; }}
.summary {{ background: #f0f9ff; padding: 15px; border-radius: 8px; margin: 15px 0; border-left: 4px solid #3b82f6; }}
.strengths {{ background: #f0fdf4; padding: 12px; border-radius: 8px; border-left: 4px solid #22c55e; }}
.risks {{ background: #fef2f2; padding: 12px; border-radius: 8px; border-left: 4px solid #ef4444; }}
.questions {{ background: #faf5ff; padding: 12px; border-radius: 8px; border-left: 4px solid #8b5cf6; }}
ul {{ padding-left: 20px; }}
li {{ margin-bottom: 6px; }}
.section {{ margin-bottom: 15px; }}
.meta {{ color: #6b7280; font-size: 9pt; }}
p {{ margin: 4px 0; }}
</style></head><body>

<h1>🎥 Анализ интервью</h1>
<p class="meta">Вакансия: {vacancy} | Модель: {model} | Дата: {datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M")} UTC</p>

<div class="summary">
<strong>Резюме:</strong> {analysis.get("summary", "—")}
</div>

<h2>🎯 Соответствие вакансии</h2>
<div class="score-box">{score}/10</div>

<div class="section">
<h3>Подтверждённый опыт</h3>
<ul>{_render_list(fit.get("relevant_experience", []))}</ul>
<h3>Hard Skills</h3>
<ul>{_render_list(fit.get("hard_skills", []))}</ul>
<h3>Пробелы</h3>
<ul>{_render_list(fit.get("gaps", []))}</ul>
</div>

<h2>🧠 Психологический профиль</h2>
<div class="section">
<h3>Тип мышления</h3>{_render_field(psych.get("thinking_type", "—"))}
<h3>Коммуникативный стиль</h3>{_render_field(psych.get("communication_style", "—"))}
<h3>Лидерские качества</h3>{_render_field(psych.get("leadership", "—"))}
<h3>Стрессоустойчивость</h3>{_render_field(psych.get("stress_resistance", "—"))}
<h3>Мотивация</h3>{_render_field(psych.get("motivation", "—"))}
<h3>Эмоциональный интеллект</h3>{_render_field(psych.get("emotional_intelligence", "—"))}
<h3>Ценности и культура</h3>{_render_field(psych.get("values_and_culture", "—"))}
</div>

<h2>🔍 Анализ речи</h2>
<div class="section">
<h3>Уверенность</h3>{_render_field(speech.get("confidence_level", "—"))}
<h3>Конкретика vs абстракция</h3>{_render_field(speech.get("specificity", "—"))}
<h3>Самопрезентация</h3>{_render_field(speech.get("self_presentation", "—"))}
<h3>Red Flags</h3>
<ul>{_render_list(speech.get("red_flags", []))}</ul>
</div>

<h2>🔮 Психотипирование</h2>
<div class="section">
<h3>🎭 Акцентуация (Личко/Леонгард)</h3>{_render_field(psychotype.get("accentuation", "—"))}
<h3>🔢 Эннеаграмма</h3>{_render_field(psychotype.get("enneagram", "—"))}
<h3>📊 DISC-профиль</h3>{_render_field(psychotype.get("disc", "—"))}
<h3>⚔️ Стиль в конфликте</h3>{_render_field(psychotype.get("conflict_style", "—"))}
</div>

<h2>📊 Итог</h2>
<div class="strengths">
<h3>✅ Сильные стороны</h3>
<ul>{_render_list(overall.get("strengths", []))}</ul>
</div>
<br>
<div class="risks">
<h3>⚠️ Зоны риска</h3>
<ul>{_render_list(overall.get("risks", []))}</ul>
</div>
<br>
<p><strong>Рекомендация:</strong> {overall.get("recommendation", "—")}</p>

<div class="questions">
<h3>❓ Вопросы для следующего интервью</h3>
<ul>{_render_list(overall.get("next_interview_questions", []))}</ul>
</div>

</body></html>"""

    pdf_path = str(INTERVIEWS_DIR / f"{task_id}.pdf")
    HTML(string=html_content).write_pdf(pdf_path)
    return pdf_path


def _run_interview_pipeline(task_id: str, video_path: str, vacancy_slug: str, model: str, video_url: str = "", vacancy_pdf_path: str = "", resume_pdf_path: str = ""):
    """Run the full interview analysis pipeline in a background thread."""
    task = _interview_tasks[task_id]
    work_dir = INTERVIEWS_DIR / task_id
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: Download if needed
        if not video_path:
            task.update({"stage": "downloading", "progress": 10})
            # Generate a readable filename from URL
            import hashlib as _hl
            url_hash = _hl.md5(video_url.encode()).hexdigest()[:8]
            url_name = video_url.rstrip('/').split('/')[-1].split('?')[0]
            if not url_name or len(url_name) > 60:
                url_name = f"video_{url_hash}"
            if not any(url_name.lower().endswith(ext) for ext in ('.mp4', '.mov', '.avi', '.mkv', '.webm')):
                url_name += '.mp4'
            # Save to persistent uploads
            persist_path = INTERVIEW_UPLOADS_DIR / "videos" / url_name
            dest = str(work_dir / "video.mp4")
            if "cloud.mail.ru" in video_url:
                task.update({"stage": "downloading", "progress": 30})
                _download_from_cloud_mail(video_url, dest)
            else:
                task.update({"stage": "downloading", "progress": 30})
                _download_direct(video_url, dest)
            # Copy to persistent storage for reuse
            try:
                import shutil
                shutil.copy2(dest, str(persist_path))
            except:
                pass
            video_path = dest
            task.update({"stage": "downloading", "progress": 100})

        # Step 2: Extract audio
        task.update({"stage": "extracting_audio", "progress": 10})
        audio_path = str(work_dir / "audio.mp3")
        _extract_audio(video_path, audio_path)
        task.update({"stage": "extracting_audio", "progress": 100})

        # Step 3: Transcribe
        task.update({"stage": "transcribing", "progress": 10})
        transcript = _transcribe_audio(audio_path)
        task.update({"stage": "transcribing", "progress": 80})

        # Step 3.5: Identify speakers (before analysis to avoid bias)
        if "Спикер" in transcript:
            import anthropic as _anth
            _client = _anth.Anthropic()
            transcript = _identify_speakers(transcript, _client)
        task.update({"stage": "transcribing", "progress": 100})

        # Step 4: Analyze
        task.update({"stage": "analyzing", "progress": 10})
        prompt_path = PROMPTS_DIR / f"{vacancy_slug}.txt"
        if prompt_path.exists():
            vacancy_prompt = prompt_path.read_text()
        else:
            vacancy_prompt = f"Вакансия: {vacancy_slug}"

        vacancy_pdf_text = ""
        if vacancy_pdf_path and os.path.exists(vacancy_pdf_path):
            vacancy_pdf_text = _extract_text_from_pdf(vacancy_pdf_path)
        resume_pdf_text = ""
        if resume_pdf_path and os.path.exists(resume_pdf_path):
            resume_pdf_text = _extract_text_from_pdf(resume_pdf_path)
        analysis = _analyze_interview(transcript, vacancy_prompt, model, vacancy_pdf_text, resume_pdf_text)
        task.update({"stage": "analyzing", "progress": 100})

        # Save result
        result_data = {
            "task_id": task_id,
            "video_url": video_url,
            "vacancy": vacancy_slug,
            "model": model,
            "transcript": transcript,
            "analysis": analysis,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        (INTERVIEWS_DIR / f"{task_id}.json").write_text(
            json.dumps(result_data, ensure_ascii=False, indent=2)
        )

        # Generate PDF report
        try:
            _generate_interview_pdf(task_id, analysis, vacancy_slug, model)
        except Exception as e:
            print(f"PDF generation failed: {e}")

        task.update({"stage": "done", "result": analysis, "transcript": transcript, "task_id": task_id})

    except Exception as e:
        task.update({"stage": "error", "message": str(e)})
    finally:
        # Cleanup work dir (keep result JSON)
        try:
            import shutil
            if work_dir.exists():
                shutil.rmtree(work_dir)
        except:
            pass


@app.get("/interview", response_class=HTMLResponse)
async def interview_page(request: Request, key: str = Query("")):
    check_key(key)
    return templates.TemplateResponse("interview.html", {"request": request, "key": key})


@app.get("/api/interview/prompts")
async def api_interview_prompts(key: str = Query("")):
    check_key(key)
    return {"prompts": _get_prompts_list()}


@app.get("/api/interview/uploads")
async def api_interview_uploads(key: str = Query("")):
    """List previously uploaded files by category."""
    check_key(key)
    result = {}
    for category in ("vacancies", "resumes", "videos"):
        cat_dir = INTERVIEW_UPLOADS_DIR / category
        files = []
        for f in sorted(cat_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.startswith('.'):
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "date": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "path": f"{category}/{f.name}"
                })
        result[category] = files[:20]  # last 20
    return result


from fastapi import UploadFile, File, Form

@app.post("/api/interview/analyze")
async def api_interview_analyze(
    key: str = Query(""),
    video_url: str = Form(""),
    vacancy: str = Form(""),
    model: str = Form("claude-sonnet-4-20250514"),
    video_file: UploadFile = File(None),
    vacancy_pdf: UploadFile = File(None),
    resume_pdf: UploadFile = File(None),
    existing_vacancy: str = Form(""),   # path like "vacancies/filename.pdf"
    existing_resume: str = Form(""),    # path like "resumes/filename.pdf"
    existing_video: str = Form("")      # path like "videos/filename.mp4"
):
    check_key(key)

    if not video_url and not video_file and not existing_video:
        return JSONResponse({"error": "Нужна ссылка на видео или файл"}, status_code=400)
    if not vacancy:
        return JSONResponse({"error": "Выберите вакансию"}, status_code=400)

    # Map model names
    model_map = {
        "claude-sonnet-4-6": "claude-sonnet-4-20250514",
        "claude-opus-4-6": "claude-opus-4-20250514",
    }
    model = model_map.get(model, model)

    task_id = str(uuid.uuid4())
    _interview_tasks[task_id] = {"stage": "queued", "progress": 0}

    video_path = ""
    # Use existing video if selected
    if existing_video and not video_file:
        ep = INTERVIEW_UPLOADS_DIR / existing_video
        if ep.exists():
            video_path = str(ep)
    elif video_file and video_file.filename:
        # Save uploaded file to persistent uploads
        safe_name = video_file.filename.replace("/", "_").replace("..", "_")
        persist_path = INTERVIEW_UPLOADS_DIR / "videos" / safe_name
        content = await video_file.read()
        with open(persist_path, "wb") as f:
            f.write(content)
        video_path = str(persist_path)

    # Vacancy PDF — existing or new upload
    vacancy_pdf_path = ""
    if existing_vacancy and not (vacancy_pdf and vacancy_pdf.filename):
        ep = INTERVIEW_UPLOADS_DIR / existing_vacancy
        if ep.exists():
            vacancy_pdf_path = str(ep)
    elif vacancy_pdf and vacancy_pdf.filename:
        safe_name = vacancy_pdf.filename.replace("/", "_").replace("..", "_")
        persist_path = INTERVIEW_UPLOADS_DIR / "vacancies" / safe_name
        pdf_content = await vacancy_pdf.read()
        with open(persist_path, "wb") as f:
            f.write(pdf_content)
        vacancy_pdf_path = str(persist_path)

    # Resume PDF — existing or new upload
    resume_pdf_path = ""
    if existing_resume and not (resume_pdf and resume_pdf.filename):
        ep = INTERVIEW_UPLOADS_DIR / existing_resume
        if ep.exists():
            resume_pdf_path = str(ep)
    elif resume_pdf and resume_pdf.filename:
        safe_name = resume_pdf.filename.replace("/", "_").replace("..", "_")
        persist_path = INTERVIEW_UPLOADS_DIR / "resumes" / safe_name
        resume_content = await resume_pdf.read()
        with open(persist_path, "wb") as f:
            f.write(resume_content)
        resume_pdf_path = str(persist_path)

    # Run pipeline in background
    threading.Thread(
        target=_run_interview_pipeline,
        args=(task_id, video_path, vacancy, model, video_url, vacancy_pdf_path, resume_pdf_path),
        daemon=True
    ).start()

    return {"task_id": task_id}


@app.get("/api/interview/{task_id}/pdf")
async def api_interview_pdf(task_id: str, key: str = Query("")):
    check_key(key)
    from fastapi.responses import FileResponse
    pdf_path = INTERVIEWS_DIR / f"{task_id}.pdf"
    if not pdf_path.exists():
        raise HTTPException(404, "PDF not found")
    return FileResponse(str(pdf_path), media_type="application/pdf", filename=f"interview_report_{task_id[:8]}.pdf")


@app.get("/api/interview/{task_id}/stream")
async def api_interview_stream(task_id: str, key: str = Query("")):
    check_key(key)

    if task_id not in _interview_tasks:
        return JSONResponse({"error": "Task not found"}, status_code=404)

    async def event_generator():
        last_stage = ""
        last_progress = -1
        while True:
            task = _interview_tasks.get(task_id, {})
            stage = task.get("stage", "queued")
            progress = task.get("progress", 0)

            if stage != last_stage or progress != last_progress:
                if stage == "done":
                    yield {"data": json.dumps({
                        "stage": "done",
                        "result": task.get("result", {}),
                        "transcript": task.get("transcript", "")
                    }, ensure_ascii=False)}
                    break
                elif stage == "error":
                    yield {"data": json.dumps({
                        "stage": "error",
                        "message": task.get("message", "Unknown error")
                    }, ensure_ascii=False)}
                    break
                else:
                    yield {"data": json.dumps({
                        "stage": stage,
                        "progress": progress
                    }, ensure_ascii=False)}

                last_stage = stage
                last_progress = progress

            await asyncio.sleep(0.5)

        # Cleanup task from memory after some time
        await asyncio.sleep(60)
        _interview_tasks.pop(task_id, None)

    return EventSourceResponse(event_generator())


# ─── Event Companies (Autosearches) ───

EVENT_COMPANIES_PATH = DATA_DIR / "event_companies.json"

def _load_event_companies():
    if EVENT_COMPANIES_PATH.exists():
        return json.loads(EVENT_COMPANIES_PATH.read_text())
    return []

def _save_event_companies(companies):
    EVENT_COMPANIES_PATH.write_text(json.dumps(companies, ensure_ascii=False, indent=2))

@app.get("/autosearch")
async def autosearch_page(request: Request, key: str = Query("")):
    check_key(key)
    return templates.TemplateResponse("autosearch.html", {"request": request, "key": key})

@app.get("/autosearch/btl")
async def btl_autosearch_page(request: Request, key: str = Query("")):
    check_key(key)
    return templates.TemplateResponse("btl_autosearch.html", {"request": request, "key": key})

@app.get("/api/autosearch/companies")
async def api_autosearch_companies(key: str = Query("")):
    check_key(key)
    return _load_event_companies()

@app.post("/api/autosearch/companies")
async def api_autosearch_save(request: Request, key: str = Query("")):
    check_key(key)
    data = await request.json()
    _save_event_companies(data)
    return {"ok": True}

@app.post("/api/autosearch/toggle")
async def api_autosearch_toggle(request: Request, key: str = Query("")):
    check_key(key)
    body = await request.json()
    idx = body.get("idx")
    companies = _load_event_companies()
    if idx is None or idx < 0 or idx >= len(companies):
        raise HTTPException(400, "Invalid index")
    companies[idx]["enabled"] = not companies[idx].get("enabled", True)
    _save_event_companies(companies)
    return {"ok": True, "enabled": companies[idx]["enabled"]}

@app.get("/api/autosearch/check")
async def api_autosearch_check(idx: int = Query(0), key: str = Query("")):
    check_key(key)
    companies = _load_event_companies()
    if idx < 0 or idx >= len(companies):
        raise HTTPException(400, "Invalid index")
    comp = companies[idx]
    hh_key = comp.get("hh_key", "")
    if not hh_key:
        return {"count": 0}

    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from hh_api import hh_request

    try:
        r = hh_request("GET", "/resumes", params={"text": hh_key, "per_page": "1", "area": "113"})
        count = r.json().get("found", 0)
    except Exception as e:
        logger.error(f"HH autosearch check error: {e}")
        count = -1

    # Check 24h count too
    count_24h = 0
    try:
        from datetime import datetime, timedelta, timezone
        date_from = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        r2 = hh_request("GET", "/resumes", params={"text": hh_key, "per_page": "1", "area": "113", "date_from": date_from, "order_by": "publication_time"})
        count_24h = r2.json().get("found", 0)
    except:
        pass

    # Save count back
    companies[idx]["last_count"] = count
    companies[idx]["count_24h"] = count_24h
    _save_event_companies(companies)
    return {"count": count, "count_24h": count_24h}


@app.get("/api/autosearch/preview")
async def api_autosearch_preview(idx: int = Query(0), key: str = Query("")):
    """Get preview of top 5 resumes for a company."""
    check_key(key)
    companies = _load_event_companies()
    if idx < 0 or idx >= len(companies):
        raise HTTPException(400, "Invalid index")
    comp = companies[idx]
    hh_key = comp.get("hh_key", "")
    if not hh_key:
        return {"items": []}

    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from hh_api import hh_request

    try:
        from datetime import datetime, timedelta, timezone
        date_from = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        r = hh_request("GET", "/resumes", params={
            "text": hh_key, "per_page": "5", "area": "113",
            "date_from": date_from, "order_by": "publication_time"
        })
        data = r.json()
        items = []
        for item in data.get("items", []):
            exp = item.get("experience", [])
            last_job = f"{exp[0].get('company', '')} — {exp[0].get('position', '')}" if exp else "—"
            salary = item.get("salary")
            sal_str = f"{salary['amount']} {salary.get('currency', '')}" if salary else "—"
            items.append({
                "title": item.get("title", ""),
                "url": item.get("alternate_url", ""),
                "last_job": last_job,
                "salary": sal_str,
                "updated": item.get("updated_at", ""),
                "area": (item.get("area") or {}).get("name", ""),
            })
        return {"items": items, "total_24h": data.get("found", 0)}
    except Exception as e:
        return {"items": [], "error": str(e)}


@app.get("/api/autosearch/stats")
async def api_autosearch_stats(key: str = Query("")):
    check_key(key)
    db_path = DATA_DIR / "event_agencies.db"
    scored = 0
    relevant = 0
    if db_path.exists():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            scored = conn.execute("SELECT COUNT(*) FROM scored_candidates").fetchone()[0]
            relevant = conn.execute("SELECT COUNT(*) FROM scored_candidates WHERE relevant=1").fetchone()[0]
        except:
            pass
        conn.close()
    return {"scored": scored, "relevant": relevant}


# ─── Event Autosearch Run ───

_autosearch_tasks = {}

@app.post("/api/autosearch/run")
async def api_autosearch_run(key: str = Query("")):
    check_key(key)
    import uuid, threading
    task_id = str(uuid.uuid4())[:8]
    _autosearch_tasks[task_id] = {"status": "running", "messages": [], "progress": 0}

    def _run_sync():
        import subprocess
        try:
            _autosearch_tasks[task_id]["messages"].append({"stage": "progress", "message": "Запуск сканера event_agencies...", "progress": 5})
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent.parent / "run_multi_radar.py"), "event_agencies"],
                capture_output=True, text=True, timeout=600,
                cwd=str(Path(__file__).parent.parent),
                env={**os.environ}
            )
            output = result.stdout + result.stderr
            lines = [l for l in output.split('\n') if l.strip()]
            for i, line in enumerate(lines):
                _autosearch_tasks[task_id]["messages"].append({
                    "stage": "progress",
                    "message": line[:200],
                    "progress": min(95, 10 + int(85 * (i + 1) / max(len(lines), 1)))
                })
            if result.returncode == 0:
                _autosearch_tasks[task_id]["messages"].append({"stage": "done", "message": "Готово!", "progress": 100})
            else:
                _autosearch_tasks[task_id]["messages"].append({"stage": "error", "message": f"Код выхода: {result.returncode}", "progress": 0})
            _autosearch_tasks[task_id]["status"] = "done"
        except Exception as e:
            _autosearch_tasks[task_id]["messages"].append({"stage": "error", "message": str(e)[:200], "progress": 0})
            _autosearch_tasks[task_id]["status"] = "error"

    threading.Thread(target=_run_sync, daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/autosearch/run/{task_id}/stream")
async def api_autosearch_stream(task_id: str, key: str = Query("")):
    check_key(key)
    async def generate():
        sent = 0
        while True:
            task = _autosearch_tasks.get(task_id)
            if not task:
                yield {"data": json.dumps({"stage": "error", "message": "Task not found"})}
                return
            messages = task["messages"]
            while sent < len(messages):
                yield {"data": json.dumps(messages[sent])}
                sent += 1
            if task["status"] in ("done", "error"):
                return
            await asyncio.sleep(0.5)
    return EventSourceResponse(generate())


# ─── Outsource ───

OUTSOURCE_COMPANIES_PATH = DATA_DIR / "outsource_companies.json"

def _load_outsource_companies():
    if OUTSOURCE_COMPANIES_PATH.exists():
        return json.loads(OUTSOURCE_COMPANIES_PATH.read_text())
    return []

def _save_outsource_companies(companies):
    OUTSOURCE_COMPANIES_PATH.write_text(json.dumps(companies, ensure_ascii=False, indent=2))

@app.get("/outsource")
async def outsource_page(request: Request, key: str = Query("")):
    check_key(key)
    return templates.TemplateResponse("outsource.html", {"request": request, "key": key})

@app.get("/api/outsource/companies")
async def api_outsource_companies(key: str = Query("")):
    check_key(key)
    return _load_outsource_companies()

@app.post("/api/outsource/companies")
async def api_outsource_save(request: Request, key: str = Query("")):
    check_key(key)
    data = await request.json()
    _save_outsource_companies(data)
    return {"ok": True}

@app.get("/api/outsource/check")
async def api_outsource_check(idx: int = Query(0), key: str = Query("")):
    check_key(key)
    companies = _load_outsource_companies()
    if idx < 0 or idx >= len(companies):
        raise HTTPException(400, "Invalid index")
    comp = companies[idx]
    hh_key = comp.get("hh_key", "")
    if not hh_key:
        return {"count": 0}

    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from hh_api import hh_request

    try:
        r = hh_request("GET", "/resumes", params={"text": hh_key, "per_page": "1"})
        count = r.json().get("found", 0)
    except Exception as e:
        logger.error(f"HH check error: {e}")
        count = -1

    # Save count back
    companies[idx]["last_count"] = count
    _save_outsource_companies(companies)
    return {"count": count}

@app.get("/api/outsource/stats")
async def api_outsource_stats(key: str = Query("")):
    check_key(key)
    db_path = DATA_DIR / "outsource_agencies.db"
    scored = 0
    relevant = 0
    if db_path.exists():
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            scored = conn.execute("SELECT COUNT(*) FROM scored_candidates").fetchone()[0]
            relevant = conn.execute("SELECT COUNT(*) FROM scored_candidates WHERE relevant=1").fetchone()[0]
        except:
            pass
        conn.close()
    return {"scored": scored, "relevant": relevant}

outsource_tasks: dict = {}

@app.post("/api/outsource/run")
async def api_outsource_run(key: str = Query("")):
    check_key(key)
    task_id = str(uuid.uuid4())
    outsource_tasks[task_id] = {"stage": "starting", "progress": 0, "message": "Запуск..."}

    def _run_sync():
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from hh_api import hh_request, format_resume
        import time as _time

        task = outsource_tasks[task_id]
        companies = _load_outsource_companies()
        enabled = [c for c in companies if c.get("enabled")]

        # Create DB with standard schema (same as event/btl)
        db_path = DATA_DIR / "outsource_agencies.db"
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE IF NOT EXISTS scored_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            normalized_link TEXT NOT NULL,
            candidate_title TEXT,
            last_job TEXT,
            salary TEXT,
            job_slug TEXT NOT NULL,
            relevant INTEGER NOT NULL DEFAULT 0,
            fit_type TEXT,
            confidence REAL,
            reason TEXT,
            source_subject TEXT
        )""")
        conn.execute("CREATE TABLE IF NOT EXISTS seen_links (normalized_link TEXT PRIMARY KEY)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scored_date ON scored_candidates(run_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scored_job ON scored_candidates(job_slug, relevant)")
        conn.commit()

        # Get already scored links
        existing = set(r[0] for r in conn.execute("SELECT normalized_link FROM seen_links").fetchall())

        total_new = 0
        total_scored = 0

        for ci, comp in enumerate(enabled):
            hh_key = comp.get("hh_key", "")
            if not hh_key:
                continue

            pct = int((ci / len(enabled)) * 100)
            task.update({"stage": "progress", "progress": pct, "message": f"🔍 {comp['name']}..."})

            try:
                # Fetch resumes
                is_direct = comp.get("brand", "").startswith("direct_search")
                max_pages = 3 if is_direct else 5
                all_items = []
                for page in range(max_pages):
                    params = {
                        "text": hh_key,
                        "per_page": "20",
                        "page": str(page),
                        "order_by": "publication_time",
                    }
                    if is_direct:
                        params["experience"] = "between3And6"
                        params["area"] = "1"
                    r = hh_request("GET", "/resumes", params=params)
                    data = r.json()
                    items = data.get("items", [])
                    all_items.extend(items)
                    if page >= data.get("pages", 1) - 1:
                        break
                    _time.sleep(0.5)

                new_items = []
                for item in all_items:
                    link = item.get("alternate_url", "")
                    norm = link.split("?")[0].strip() if link else ""
                    if norm and norm not in existing:
                        new_items.append(item)
                        existing.add(norm)

                total_new += len(new_items)
                task.update({"message": f"🔍 {comp['name']}: {len(new_items)} новых из {len(all_items)}"})

                # Score new items with GPT-4o (brief card)
                if new_items:
                    from openai import OpenAI
                    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

                    for item in new_items:
                        title = item.get("title", "")
                        exp_list = item.get("experience", [])
                        exp_text = ""
                        for e in exp_list[:3]:
                            exp_text += f"- {e.get('position','')} в {e.get('company','')} ({e.get('start','')}-{e.get('end','н.в.')})\n"

                        salary = item.get("salary")
                        sal_str = f"{salary['amount']} {salary['currency']}" if salary else "не указана"
                        area = (item.get("area") or {}).get("name", "")
                        total_exp = item.get("total_experience", {})
                        months = total_exp.get("months", 0) if total_exp else 0

                        card = f"""Должность: {title}
Город: {area}
Опыт: {months // 12} лет {months % 12} мес
Зарплата: {sal_str}
Опыт работы:
{exp_text}
Источник (компания из автопоиска): {comp['name']}"""

                        prompt = f"""Оцени кандидата для позиции "Руководитель отдела аутсорсинга персонала".

Целевой профиль: опыт в аутсорсинге/аутстаффинге персонала на руководящей позиции 2+ лет,
понимание тендерных процедур, B2B продажи, управление командой рекрутеров/аккаунтов.

Кандидат:
{card}

Ответь JSON:
{{"fit_type": "target|near_target|not_fit", "confidence": 0-100, "reason": "краткое обоснование"}}"""

                        try:
                            resp = client.chat.completions.create(
                                model="gpt-4o",
                                messages=[{"role": "user", "content": prompt}],
                                temperature=0.1,
                                max_tokens=200,
                                response_format={"type": "json_object"},
                            )
                            score = json.loads(resp.choices[0].message.content)
                        except Exception as e:
                            score = {"fit_type": "not_fit", "confidence": 0, "reason": f"Ошибка: {e}"}

                        # Save to DB (standard schema)
                        last_comp = exp_list[0].get("company", "") if exp_list else ""
                        last_pos = exp_list[0].get("position", "") if exp_list else ""
                        fit = score.get("fit_type", "not_fit")
                        relevant = 1 if fit in ("target", "near_target") else 0
                        link = item.get("alternate_url", "")
                        norm_link = link.split("?")[0].strip()
                        
                        conn.execute("""INSERT INTO scored_candidates 
                            (run_date, normalized_link, candidate_title, last_job, salary,
                             job_slug, relevant, fit_type, confidence, reason, source_subject)
                            VALUES (date('now'),?,?,?,?,?,?,?,?,?,?)""",
                            (norm_link, title, f"{last_pos} @ {last_comp}", sal_str,
                             "outsource_manager", relevant, fit,
                             score.get("confidence", 0), score.get("reason", ""),
                             comp["name"]))
                        conn.execute("INSERT OR IGNORE INTO seen_links VALUES (?)", (norm_link,))
                        conn.commit()
                        total_scored += 1

                        _time.sleep(0.3)

            except Exception as e:
                task.update({"message": f"❌ {comp['name']}: {e}"})
                logger.error(f"Outsource search error for {comp['name']}: {e}")

            _time.sleep(0.5)

        conn.close()
        task.update({
            "stage": "done",
            "progress": 100,
            "message": f"✅ Готово! Новых: {total_new}, оценено: {total_scored}"
        })

    import asyncio, concurrent.futures
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_sync)
    return {"task_id": task_id}

@app.get("/api/outsource/run/{task_id}/stream")
async def api_outsource_stream(task_id: str, key: str = Query("")):
    check_key(key)
    import asyncio

    async def generate():
        while True:
            task = outsource_tasks.get(task_id, {"stage": "error", "message": "Task not found"})
            yield f"data: {json.dumps(task, ensure_ascii=False)}\n\n"
            if task.get("stage") in ("done", "error"):
                break
            await asyncio.sleep(1)

    from starlette.responses import StreamingResponse
    return StreamingResponse(generate(), media_type="text/event-stream")

# ─── Run ───
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8093)
