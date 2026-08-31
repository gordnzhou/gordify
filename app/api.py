"""
Jobs website + API, backed by SQLite db for job queue

Routes for people (browser, session-based):
  GET  /login                 - login form
  POST /login                 - verify credentials, start session
  GET  /dashboard             - submit a job, see your jobs + status/logs/errors
  POST /dashboard/submit      - enqueue a job
  GET  /logout

Routes for scripts/automation (stateless, credentials per request):
  POST /api/jobs
  GET  /api/jobs/{id}
  GET  /api/jobs
"""
import os

import httpx
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi import Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette.middleware.sessions import SessionMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from pydantic import BaseModel

import job_db
from navidrome_client import change_user_password, fetch_user, verify_user

# IP-based rate limiting
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Song Downloader")
app.state.limiter = limiter

app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(SessionMiddleware, secret_key=os.environ["SESSION_SECRET"], same_site="lax")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def _startup():
    job_db.init_db()


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return templates.TemplateResponse("login.html",
        {
            "request": request,
            "error": "You have been rate limited. Please try again later.",
        },
        status_code=429
    )


# ---------- website (session-based) ----------
def _current_user(request: Request) -> str | None:
    return request.session.get("username")


def _current_user_is_admin(request: Request) -> bool:
    return bool(request.session.get("isAdmin"))


@app.get("/", include_in_schema=False)
def root(request: Request):
    if _current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
        
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", include_in_schema=False)
@limiter.limit("5/minute")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        if not verify_user(username, password, ignore_errors=False):
            return templates.TemplateResponse("login.html", { "request": request, "error": "Wrong username or password." }, status_code=401)

        user = fetch_user(username, ignore_errors=False)
    except httpx.HTTPError as exc:
        print(f"HTTP Exception for {exc.request.url} - {exc}")
        return templates.TemplateResponse("login.html", { "request": request, "error": "Could not connect to upstream server." }, status_code=401)

    request.session["username"] = username
    request.session["isAdmin"] = bool(user["isAdmin"])

    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/change-password", response_class=HTMLResponse, include_in_schema=False)
def change_password(request: Request):
    username = _current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=303)    

    return templates.TemplateResponse("change-password.html", { "request": request, "username": username, "error": None })


@app.post("/change-password", include_in_schema=False)
def change_password_submit(request: Request, oldpass: str = Form(...), newpass: str = Form(...), newpass2: str = Form(...)):
    username = _current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=303)
        
    error_message = None
    if newpass != newpass2:
        error_message = "New password does not match password in confirmation field"
    if len(newpass) < 8 or not any(c.isdigit() for c in newpass) or not any(c.isalnum() for c in newpass):
        error_message = "Password must be at least 8 letters long and contain at least one number and one special character"

    try:
        if not verify_user(username, oldpass, ignore_errors=False):
            error_message = "Incorrect current password"

        change_user_password(username, newpass, ignore_errors=False)
    except httpx.HTTPException as exc:
        print(f"HTTP Exception for {exc.request.url} - {exc}")
        error_message = "Unable to change password, something went wrong"

    if error_message:
        return templates.TemplateResponse(
            "change-password.html", 
            { "request": request, "error": error_message }
        )
    
    request.session["success"] = "Changed password!"
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request, sort: str = "date_desc", filter: str = "all", limit: str = "20"):
    username = _current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=303)
    is_admin = _current_user_is_admin(request)
    
    html_page = "dashboard.html" if not is_admin else "admin-dashboard.html"
    job_limit = limit if limit.isdigit() else 20
    if is_admin:
        jobs = job_db.fetch_jobs(filter=filter, sort=sort, limit=job_limit)
    else:
        jobs = job_db.fetch_jobs(username=username, filter=filter, sort=sort, limit=job_limit)

    for j in jobs:
        if not j["logs"]:
            continue

        if len(j["logs"]) > 16000:
            j["logs"] = f"{j["logs"][:8000]}\n...\n{j["logs"][-8000:]}"

    error = request.session.pop("error", None)
    success = request.session.pop("success", None)

    return templates.TemplateResponse(html_page, {
        "request": request, 
        "username": username,
        "jobs": jobs, 
        "error": error, 
        "success": success,
        "current_sort": sort,
        "current_filter": filter,
        "current_limit": limit
    })


@app.post("/dashboard/submit", include_in_schema=False)
@limiter.limit("60/hour")
def dashboard_submit(request: Request, query: str = Form(...)):
    username = _current_user(request)
    if not username:
        return RedirectResponse("/login", status_code=303)

    if query.strip():
        if job_db.job_already_exists(username, query.strip()):
            request.session["error"] = "A job for this query has already been queued or is running!"
            return RedirectResponse("/dashboard", status_code=303)
        
        job_id = job_db.create_job(username, query.strip())

    request.session["success"] = f"Created job!\n(id = {job_id})"
    return RedirectResponse("/dashboard", status_code=303)


# ---------- JSON API (for scripts, stateless) ----------
class JobRequest(BaseModel):
    query: str

security = HTTPBasic()

def _require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not verify_user(credentials.username, credentials.password):
        raise HTTPException(
            status_code=401,
            detail="invalid Navidrome credentials",
        )

    return credentials.username


@app.post("/api/jobs")
def api_create_job(req: JobRequest, username: str = Depends(_require_auth)):
    job_id = job_db.create_job(username, req.query.strip())
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str, username: str = Depends(_require_auth)):
    job = job_db.get_job_for_user(job_id, username)

    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/jobs")
def api_list_jobs(username: str = Depends(_require_auth)):
    return {"jobs": job_db.list_jobs_for_user(username)}


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}