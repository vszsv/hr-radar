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

scoring_state = {}

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
    labels = {"event_agencies": "🎪 Event агентства", "btl_agencies": "📢 BTL агентства"}
    stats = []
    for db_name in ["event_agencies", "btl_agencies"]:
        db_path = DATA_DIR / f"{db_name}.db"
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            c = conn.cursor()
            total = c.execute("SELECT count(*) FROM scored_candidates").fetchone()[0]
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
                "total": total, "today": today, "relevant": relevant, "relevant_today": relevant_today,
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
        result.append({
            "id": p_name, "name": p_data.get("name", p_name),
            "email": p_data.get("imap", {}).get("user", ""),
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
        json_fmt = 'Ответь СТРОГО JSON без markdown: {"verdict": "одобрен" или "отказ", "score": 1-10, "reason": "причина до 80 символов"}'
        
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
                        model=model, temperature=0.1, max_completion_tokens=200,
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

# ─── Run ───
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8093)
