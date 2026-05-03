import uuid
import shutil
import tarfile
import io
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import init_db, get_db, async_session
from models import User, Project, ProjectFile, ApiUsage, Payment, ShareLink, SystemSetting
from schemas import *
from auth import hash_password, verify_password, create_token, get_current_user, get_admin
from ai_proxy import ai_proxy
from config import PLANS, SSH_BASE_DIR, PROJECTS_DIR

app = FastAPI(title="AI Platform", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
async def startup():
    await init_db()
    # Load settings from DB into runtime config
    async with async_session() as db:
        result = await db.execute(select(SystemSetting))
        for s in result.scalars().all():
            if s.key == "deepseek_api_key" and s.value:
                import config
                config.DEEPSEEK_API_KEY = s.value
                ai_proxy.api_key = s.value
            elif s.key == "secret_key" and s.value:
                import config
                config.SECRET_KEY = s.value
            elif s.key == "git_host" and s.value:
                os.environ["GIT_HOST"] = s.value


# ── Auth ──────────────────────────────────────────────

@app.post("/api/auth/register", response_model=TokenResponse)
async def register(data: UserRegister, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(
        (User.username == data.username) | (User.email == data.email)
    ))
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Username or email already exists")

    # First registered user becomes admin
    user_count = (await db.execute(select(func.count(User.id)))).scalar()
    is_first = user_count == 0

    ssh_user = f"dev_{data.username}"
    ssh_port = 2200 + int(datetime.utcnow().timestamp()) % 1000

    user = User(
        username=data.username,
        email=data.email,
        hashed_password=hash_password(data.password),
        ssh_username=ssh_user,
        ssh_port=ssh_port,
        is_admin=is_first
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Create system user and projects directory
    script = os.path.join(os.path.dirname(__file__), "..", "deploy", "user_manager.sh")
    subprocess.run(["bash", script, "create", ssh_user, data.password], capture_output=True)

    token = create_token(user.id)
    return TokenResponse(access_token=token, user=_user_out(user))


@app.post("/api/auth/login", response_model=TokenResponse)
async def login(data: UserLogin, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == data.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(401, "Invalid username or password")
    token = create_token(user.id)
    return TokenResponse(access_token=token, user=_user_out(user))


@app.get("/api/auth/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return _user_out(user)


# ── Projects ──────────────────────────────────────────

@app.get("/api/projects", response_model=list[ProjectOut])
async def list_projects(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Project).where(Project.user_id == user.id).order_by(Project.updated_at.desc())
    )
    return [_project_out(p) for p in result.scalars().all()]


@app.post("/api/projects", response_model=ProjectOut)
async def create_project(data: ProjectCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    count = await db.execute(
        select(func.count(Project.id)).where(Project.user_id == user.id)
    )
    max_p = PLANS[user.plan]["max_projects"]
    if count.scalar() >= max_p:
        raise HTTPException(400, f"Project limit reached ({max_p}). Upgrade your plan.")

    project = Project(name=data.name, description=data.description, user_id=user.id)
    db.add(project)
    await db.commit()
    await db.refresh(project)

    # Create user's project directory
    _ensure_project_dir(user, project)
    return _project_out(project)


@app.get("/api/projects/{project_id}", response_model=ProjectOut)
async def get_project(project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_user_project(project_id, user, db)
    return _project_out(project)


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_user_project(project_id, user, db)
    shutil.rmtree(_project_path(user, project), ignore_errors=True)
    await db.delete(project)
    await db.commit()
    return {"ok": True}


# ── Files ─────────────────────────────────────────────

@app.get("/api/projects/{project_id}/files", response_model=list[ProjectFileOut])
async def list_files(project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_user_project(project_id, user, db)
    result = await db.execute(
        select(ProjectFile).where(ProjectFile.project_id == project_id).order_by(ProjectFile.path)
    )
    return [_file_out(f) for f in result.scalars().all()]


@app.post("/api/projects/{project_id}/files")
async def write_file(project_id: str, data: FileWrite, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_user_project(project_id, user, db)

    existing = await db.execute(
        select(ProjectFile).where(ProjectFile.project_id == project_id, ProjectFile.path == data.path)
    )
    pf = existing.scalar_one_or_none()

    if pf:
        pf.content = data.content
        pf.size_bytes = len(data.content.encode())
        pf.updated_at = datetime.utcnow()
    else:
        pf = ProjectFile(project_id=project_id, path=data.path, content=data.content, size_bytes=len(data.content.encode()))
        db.add(pf)

    # Write to disk
    disk_path = _project_path(user, project) / data.path
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_text(data.content)

    project.file_count = await _count_files(project_id, db)
    project.updated_at = datetime.utcnow()
    await db.commit()
    return {"ok": True, "path": data.path}


@app.delete("/api/projects/{project_id}/files")
async def delete_file(project_id: str, data: FileDelete, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_user_project(project_id, user, db)
    result = await db.execute(
        select(ProjectFile).where(ProjectFile.project_id == project_id, ProjectFile.path == data.path)
    )
    pf = result.scalar_one_or_none()
    if pf:
        await db.delete(pf)
        (_project_path(user, project) / data.path).unlink(missing_ok=True)
        project.file_count = await _count_files(project_id, db)
        await db.commit()
    return {"ok": True}


# ── AI Chat ───────────────────────────────────────────

@app.post("/api/chat", response_model=ChatResponse)
async def chat(data: ChatRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_user_project(data.project_id, user, db)
    msgs = [{"role": m.role, "content": m.content} for m in data.messages]
    try:
        result = await ai_proxy.chat(db, user, data.project_id, msgs, data.model)
    except ValueError as e:
        raise HTTPException(402, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return ChatResponse(**result)


# ── Billing ───────────────────────────────────────────

@app.get("/api/billing/plans", response_model=list[BillingPlan])
async def list_plans():
    return [BillingPlan(key=k, **v) for k, v in PLANS.items()]


@app.post("/api/billing/pay", response_model=PaymentOut)
async def create_payment(data: PaymentRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if data.plan_key not in PLANS:
        raise HTTPException(400, "Invalid plan")

    plan = PLANS[data.plan_key]
    if data.method not in ("wechat", "alipay"):
        raise HTTPException(400, "Payment method must be wechat or alipay")

    trade_no = f"PAY{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:6]}"
    payment = Payment(
        user_id=user.id,
        amount_cny=plan["price_cny"],
        payment_method=data.method,
        trade_no=trade_no,
        plan_purchased=data.plan_key,
        credits_added=plan["credits"]
    )
    db.add(payment)

    # In production: call WeChat/Alipay API to generate QR code
    qr_url = f"https://api.example.com/qr/{trade_no}?amount={plan['price_cny']}&method={data.method}"

    await db.commit()
    await db.refresh(payment)

    return PaymentOut(
        id=payment.id, amount_cny=payment.amount_cny,
        payment_method=payment.payment_method, status=payment.status,
        trade_no=payment.trade_no, qr_url=qr_url
    )


@app.post("/api/billing/confirm/{trade_no}")
async def confirm_payment(trade_no: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Payment).where(Payment.trade_no == trade_no, Payment.user_id == user.id))
    payment = result.scalar_one_or_none()
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status == "paid":
        raise HTTPException(400, "Already paid")

    # In production: verify with WeChat/Alipay callback
    payment.status = "paid"
    user.plan = payment.plan_purchased
    user.credits += payment.credits_added
    await db.commit()
    return {"ok": True, "plan": user.plan, "credits": user.credits}


# ── Share & Download ──────────────────────────────────

@app.post("/api/projects/{project_id}/share", response_model=ShareLinkOut)
async def create_share(project_id: str, expires_hours: int = 24, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    await _get_user_project(project_id, user, db)
    token = uuid.uuid4().hex[:16]
    link = ShareLink(
        project_id=project_id,
        token=token,
        expires_at=datetime.utcnow() + timedelta(hours=expires_hours)
    )
    db.add(link)
    await db.commit()
    return ShareLinkOut(token=token, url=f"/dl/{token}", download_count=0, expires_at=link.expires_at)


@app.get("/dl/{token}")
async def download_shared(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ShareLink).where(ShareLink.token == token))
    link = result.scalar_one_or_none()
    if not link or (link.expires_at and link.expires_at < datetime.utcnow()):
        raise HTTPException(404, "Link expired or not found")

    link.download_count += 1
    await db.commit()

    project_result = await db.execute(select(Project).where(Project.id == link.project_id))
    project = project_result.scalar_one()
    user_result = await db.execute(select(User).where(User.id == project.user_id))
    proj_user = user_result.scalar_one()

    return _stream_project_tar(proj_user, project)


@app.get("/api/projects/{project_id}/download")
async def download_project(project_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    project = await _get_user_project(project_id, user, db)
    return _stream_project_tar(user, project)


# ── SSH Info ──────────────────────────────────────────

@app.get("/api/ssh/info")
async def ssh_info(user: User = Depends(get_current_user)):
    return {
        "host": os.environ.get("GIT_HOST", "139.180.220.20"),
        "username": user.ssh_username,
        "port": user.ssh_port or 22,
        "command": f"ssh {user.ssh_username}@139.180.220.20 -p {user.ssh_port or 22}",
        "note": "Use your platform password to login. Your projects are in ~/projects/"
    }


# ── Admin ─────────────────────────────────────────────

@app.get("/api/admin/users", response_model=list[UserOut])
async def admin_users(admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [_user_out(u) for u in result.scalars().all()]


@app.get("/api/admin/stats")
async def admin_stats(admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    users_count = (await db.execute(select(func.count(User.id)))).scalar()
    projects_count = (await db.execute(select(func.count(Project.id)))).scalar()
    revenue = (await db.execute(
        select(func.sum(Payment.amount_cny)).where(Payment.status == "paid")
    )).scalar() or 0
    today_usage = (await db.execute(
        select(func.sum(ApiUsage.cost_credits)).where(ApiUsage.created_at >= datetime.utcnow().date())
    )).scalar() or 0
    return {"users": users_count, "projects": projects_count, "revenue_cny": revenue, "today_credits_used": today_usage}


@app.get("/api/admin/usage/{user_id}")
async def admin_user_usage(user_id: str, days: int = 30, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    since = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(ApiUsage).where(ApiUsage.user_id == user_id, ApiUsage.created_at >= since).order_by(ApiUsage.created_at.desc())
    )
    usages = result.scalars().all()
    total_input = sum(u.input_tokens for u in usages)
    total_output = sum(u.output_tokens for u in usages)
    total_cost = sum(u.cost_credits for u in usages)
    return {
        "user_id": user_id,
        "days": days,
        "total_calls": len(usages),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_credits": round(total_cost, 4),
        "records": [{"model": u.model, "input": u.input_tokens, "output": u.output_tokens, "cost": u.cost_credits, "time": str(u.created_at)} for u in usages[:100]]
    }


# ── Admin Settings ─────────────────────────────────────

@app.get("/api/admin/settings")
async def admin_get_settings(admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SystemSetting))
    settings = {s.key: s.value for s in result.scalars().all()}
    # Sensitive keys: mask the value
    masked = {}
    for k, v in settings.items():
        if "key" in k or "secret" in k or "password" in k:
            masked[k] = v[:6] + "****" + v[-4:] if len(v) > 10 else "****"
        else:
            masked[k] = v
    return masked


@app.put("/api/admin/settings")
async def admin_set_settings(data: dict, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    for key, value in data.items():
        result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = str(value)
            setting.updated_at = datetime.utcnow()
        else:
            db.add(SystemSetting(key=key, value=str(value)))
    await db.commit()

    # Reload AI proxy with new key if updated
    if "deepseek_api_key" in data:
        import config
        config.DEEPSEEK_API_KEY = data["deepseek_api_key"]
        ai_proxy.api_key = data["deepseek_api_key"]

    return {"ok": True}


@app.get("/api/admin/settings/reload")
async def admin_reload_settings(admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(SystemSetting))
    settings = {s.key: s.value for s in result.scalars().all()}
    import config
    for k, v in settings.items():
        if k == "deepseek_api_key": config.DEEPSEEK_API_KEY = v; ai_proxy.api_key = v
        elif k == "secret_key": config.SECRET_KEY = v
        elif k == "git_host": os.environ["GIT_HOST"] = v
    return {"ok": True, "loaded": list(settings.keys())}


# ── Helpers ───────────────────────────────────────────

def _user_out(u: User) -> UserOut:
    return UserOut(id=u.id, username=u.username, email=u.email, plan=u.plan,
                   credits=u.credits, ssh_username=u.ssh_username, ssh_port=u.ssh_port,
                   is_admin=u.is_admin, created_at=u.created_at)


def _project_out(p: Project) -> ProjectOut:
    return ProjectOut(id=p.id, name=p.name, description=p.description,
                      share_token=p.share_token, is_public=p.is_public,
                      file_count=p.file_count, size_bytes=p.size_bytes,
                      created_at=p.created_at, updated_at=p.updated_at)


def _file_out(f: ProjectFile) -> ProjectFileOut:
    return ProjectFileOut(id=f.id, path=f.path, content=f.content, size_bytes=f.size_bytes)


async def _get_user_project(project_id: str, user: User, db: AsyncSession) -> Project:
    result = await db.execute(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def _project_path(user: User, project: Project) -> Path:
    return Path(PROJECTS_DIR.format(username=user.ssh_username)) / project.name


def _ensure_project_dir(user: User, project: Project):
    path = _project_path(user, project)
    path.mkdir(parents=True, exist_ok=True)


async def _count_files(project_id: str, db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(ProjectFile.id)).where(ProjectFile.project_id == project_id)
    )
    return result.scalar() or 0


def _stream_project_tar(user: User, project: Project):
    project_dir = _project_path(user, project)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if project_dir.exists():
            tar.add(str(project_dir), arcname=project.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{project.name}.tar.gz"'}
    )


# ── Frontend ───────────────────────────────────────────
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/")
async def serve_frontend():
    with open(os.path.join(FRONTEND_DIR, "index.html")) as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
