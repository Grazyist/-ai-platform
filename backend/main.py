import uuid
import shutil
import tarfile
import io
import os
import re
import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from database import init_db, get_db, async_session
from models import User, Project, ProjectFile, ApiUsage, Payment, ShareLink, SystemSetting, GeneratedFile, UserFile, FileShareLink, PublicFile, gen_id
from schemas import *
from auth import hash_password, verify_password, create_token, get_current_user, get_admin
from ai_proxy import ai_proxy
from config import PLANS, MODELS, FILE_TYPES, SSH_BASE_DIR, PROJECTS_DIR, UPLOAD_DIR, MAX_STORAGE_BYTES

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

    # First registered user becomes admin with full privileges
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
        is_admin=is_first,
        plan="enterprise" if is_first else "free",
        credits=999999 if is_first else 100.0,
        model_tier="paid" if is_first else "free"
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


@app.put("/api/auth/password")
async def change_password(old_pw: str = Query(...), new_pw: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if not verify_password(old_pw, user.hashed_password):
        raise HTTPException(400, "Current password is incorrect")
    user.hashed_password = hash_password(new_pw)
    await db.commit()
    return {"ok": True}


@app.put("/api/auth/apikey")
async def set_own_apikey(api_key: str = Query(...), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    user.api_key = api_key.strip()
    await db.commit()
    return {"ok": True, "has_api_key": bool(user.api_key)}


@app.get("/api/auth/usage")
async def my_usage(days: int = 30, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    since = datetime.utcnow() - timedelta(days=days)
    result = await db.execute(
        select(ApiUsage).where(ApiUsage.user_id == user.id, ApiUsage.created_at >= since)
        .order_by(ApiUsage.created_at.desc()).limit(100)
    )
    usages = result.scalars().all()
    total_input = sum(u.input_tokens for u in usages)
    total_output = sum(u.output_tokens for u in usages)
    total_cost = sum(u.cost_credits for u in usages)
    return {
        "total_calls": len(usages),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_credits": round(total_cost, 4),
        "records": [
            {"model": u.model, "input": u.input_tokens, "output": u.output_tokens,
             "cost": u.cost_credits, "time": str(u.created_at)}
            for u in usages[:50]
        ]
    }


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
    project = await _get_user_project(data.project_id, user, db)
    msgs = [{"role": m.role, "content": m.content} for m in data.messages]
    try:
        result = await ai_proxy.chat(db, user, data.project_id, msgs, data.model, data.file_type)
    except ValueError as e:
        raise HTTPException(402, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))

    # Parse and save files from AI response, auto-build document types
    built_files = await _parse_and_save_files(project, user, result["reply"], data.file_type, db)
    if built_files:
        result["reply"] += "\n\n" + built_files

    return ChatResponse(**result)


# ── Models & File Types ───────────────────────────────

@app.get("/api/models", response_model=list[ModelInfo])
async def list_models(user: User = Depends(get_current_user)):
    tier = user.model_tier if user.model_tier in MODELS else "free"
    return [ModelInfo(**m) for m in MODELS.get(tier, MODELS["free"])]


@app.get("/api/file-types")
async def list_file_types():
    return FILE_TYPES


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
    return ShareLinkOut(token=token, url=f"{_base_url()}/dl/{token}", download_count=0, expires_at=link.expires_at)


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
    host = os.environ.get("GIT_HOST", "gristai.top")
    return {
        "host": host,
        "username": user.ssh_username,
        "port": user.ssh_port or 22,
        "command": f"ssh {user.ssh_username}@{host} -p {user.ssh_port or 22}",
        "note": "Use your platform password to login. Your projects are in ~/projects/"
    }


# ── File Upload & Share ────────────────────────────────

@app.get("/api/files", response_model=dict)
async def list_user_files(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(UserFile).where(UserFile.user_id == user.id).order_by(UserFile.created_at.desc())
    )
    files = result.scalars().all()
    total_bytes = sum(f.size_bytes for f in files)
    user_limit = user.storage_limit or MAX_STORAGE_BYTES
    return {
        "files": [
            {
                "id": f.id, "filename": f.filename, "original_name": f.original_name,
                "size_bytes": f.size_bytes, "download_count": f.download_count,
                "created_at": f.created_at
            }
            for f in files
        ],
        "storage_used": total_bytes,
        "storage_limit": user_limit,
        "storage_pct": round(total_bytes / user_limit * 100, 1) if user_limit > 0 else 0
    }


@app.post("/api/files/upload")
async def upload_file(file: UploadFile, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Check storage quota (per-user limit)
    result = await db.execute(
        select(func.sum(UserFile.size_bytes)).where(UserFile.user_id == user.id)
    )
    used = result.scalar() or 0
    user_limit = user.storage_limit or MAX_STORAGE_BYTES
    content = await file.read()
    file_size = len(content)
    if used + file_size > user_limit:
        raise HTTPException(400, f"Storage limit reached: {used//1024}KB used of {user_limit//1024//1024}MB")

    upload_dir = Path(UPLOAD_DIR.format(username=user.ssh_username))
    upload_dir.mkdir(parents=True, exist_ok=True)

    file_id = gen_id()
    stored_name = f"{file_id}_{file.filename}"
    disk_path = upload_dir / stored_name
    disk_path.write_bytes(content)

    uf = UserFile(
        id=file_id, user_id=user.id, filename=stored_name,
        original_name=file.filename, size_bytes=file_size
    )
    db.add(uf)
    await db.commit()
    await db.refresh(uf)
    return {"ok": True, "id": file_id, "size_bytes": file_size}


@app.delete("/api/files/{file_id}")
async def delete_user_file(file_id: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(UserFile).where(UserFile.id == file_id, UserFile.user_id == user.id)
    )
    uf = result.scalar_one_or_none()
    if not uf:
        raise HTTPException(404, "File not found")

    # Delete share links for this file
    links = await db.execute(select(FileShareLink).where(FileShareLink.file_id == file_id))
    for link in links.scalars().all():
        await db.delete(link)

    await db.delete(uf)
    upload_dir = Path(UPLOAD_DIR.format(username=user.ssh_username))
    (upload_dir / uf.filename).unlink(missing_ok=True)
    await db.commit()
    return {"ok": True}


@app.post("/api/files/{file_id}/share", response_model=FileShareOut)
async def share_user_file(file_id: str, expires_hours: int = 48, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(UserFile).where(UserFile.id == file_id, UserFile.user_id == user.id)
    )
    uf = result.scalar_one_or_none()
    if not uf:
        raise HTTPException(404, "File not found")

    token = uuid.uuid4().hex[:16]
    link = FileShareLink(
        file_id=file_id, token=token,
        expires_at=datetime.utcnow() + timedelta(hours=expires_hours)
    )
    db.add(link)
    await db.commit()
    return FileShareOut(token=token, url=f"{_base_url()}/dl/file/{token}", download_count=0, expires_at=link.expires_at)


@app.get("/api/files/shares", response_model=list[FileShareOut])
async def list_file_shares(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Get all share links for this user's files
    result = await db.execute(
        select(FileShareLink).join(UserFile).where(UserFile.user_id == user.id)
        .order_by(FileShareLink.created_at.desc())
    )
    links = result.scalars().all()
    out = []
    for link in links:
        file_result = await db.execute(select(UserFile).where(UserFile.id == link.file_id))
        uf = file_result.scalar_one_or_none()
        out.append(FileShareOut(
            token=link.token, url=f"{_base_url()}/dl/file/{link.token}",
            download_count=link.download_count, expires_at=link.expires_at
        ))
    return out


@app.get("/dl/file/{token}")
async def download_shared_file(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(FileShareLink).where(FileShareLink.token == token))
    link = result.scalar_one_or_none()
    if not link or (link.expires_at and link.expires_at < datetime.utcnow()):
        raise HTTPException(404, "Link expired or not found")

    file_result = await db.execute(select(UserFile).where(UserFile.id == link.file_id))
    uf = file_result.scalar_one_or_none()
    if not uf:
        raise HTTPException(404, "File not found")

    link.download_count += 1
    uf.download_count += 1
    await db.commit()

    user_result = await db.execute(select(User).where(User.id == uf.user_id))
    file_user = user_result.scalar_one()
    upload_dir = Path(UPLOAD_DIR.format(username=file_user.ssh_username))
    file_path = upload_dir / uf.filename

    if not file_path.exists():
        raise HTTPException(404, "File not found on server")

    return FileResponse(
        file_path, media_type="application/octet-stream",
        filename=uf.original_name
    )


# ── Public Upload ──────────────────────────────────────

PUBLIC_UPLOAD_DIR = Path("/home/public_uploads")
PUBLIC_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/upload")
async def serve_upload_page():
    upload_html = os.path.join(FRONTEND_DIR, "upload.html")
    with open(upload_html) as f:
        return HTMLResponse(f.read())


@app.post("/api/public/upload")
async def public_upload(file: UploadFile, db: AsyncSession = Depends(get_db)):
    content = await file.read()
    if len(content) > 100 * 1024 * 1024:  # 100MB max
        raise HTTPException(400, "File too large (max 100MB)")

    token = uuid.uuid4().hex[:12]
    stored_name = f"{token}_{file.filename}"
    (PUBLIC_UPLOAD_DIR / stored_name).write_bytes(content)

    pf = PublicFile(
        token=token, filename=stored_name,
        original_name=file.filename, size_bytes=len(content),
        expires_at=datetime.utcnow() + timedelta(days=7)
    )
    db.add(pf)
    await db.commit()

    download_url = f"{_base_url()}/dl/p/{token}"
    return {
        "ok": True,
        "token": token,
        "url": download_url,
        "expires_in": "7 days",
        "size_bytes": len(content)
    }


@app.get("/dl/p/{token}")
async def download_public_file(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PublicFile).where(PublicFile.token == token))
    pf = result.scalar_one_or_none()
    if not pf or (pf.expires_at and pf.expires_at < datetime.utcnow()):
        raise HTTPException(404, "File not found or expired")

    pf.download_count += 1
    await db.commit()

    file_path = PUBLIC_UPLOAD_DIR / pf.filename
    if not file_path.exists():
        raise HTTPException(404, "File not found on disk")

    return FileResponse(file_path, media_type="application/octet-stream", filename=pf.original_name)


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


# ── Admin User Management ─────────────────────────────

@app.put("/api/admin/users/{user_id}")
async def admin_update_user(user_id: str, data: AdminUserUpdate, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    if data.api_key is not None:
        user.api_key = data.api_key
    if data.plan is not None and data.plan in PLANS:
        user.plan = data.plan
        user.model_tier = PLANS[data.plan].get("model_tier", "free")
    if data.model_tier is not None:
        user.model_tier = data.model_tier
    if data.credits is not None:
        user.credits = data.credits
    if data.storage_limit is not None:
        user.storage_limit = data.storage_limit
    if data.is_active is not None:
        user.is_active = data.is_active
    if data.is_admin is not None:
        user.is_admin = data.is_admin
    await db.commit()
    return {"ok": True, "user": _user_out(user)}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    if user_id == admin.id:
        raise HTTPException(400, "Cannot delete yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    # Clean up user data
    await db.execute(select(Project).where(Project.user_id == user_id))
    for p in (await db.execute(select(Project).where(Project.user_id == user_id))).scalars().all():
        shutil.rmtree(_project_path(user, p), ignore_errors=True)
    shutil.rmtree(Path(UPLOAD_DIR.format(username=user.ssh_username)), ignore_errors=True)
    await db.delete(user)
    await db.commit()
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: str, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    new_pw = uuid.uuid4().hex[:10]
    user.hashed_password = hash_password(new_pw)
    await db.commit()
    return {"ok": True, "new_password": new_pw}


# ── Admin: Enhanced Stats & Health ─────────────────────

@app.get("/api/admin/dashboard")
async def admin_dashboard(admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    today = datetime.utcnow().date()
    # Basic counts
    users_count = (await db.execute(select(func.count(User.id)))).scalar()
    active_today = (await db.execute(
        select(func.count(ApiUsage.id)).where(ApiUsage.created_at >= today)
    )).scalar()
    projects_count = (await db.execute(select(func.count(Project.id)))).scalar()
    files_count = (await db.execute(select(func.count(ProjectFile.id)))).scalar()
    gen_files = (await db.execute(select(func.count(GeneratedFile.id)))).scalar()

    # Revenue
    revenue = (await db.execute(
        select(func.sum(Payment.amount_cny)).where(Payment.status == "paid")
    )).scalar() or 0
    pending_payments = (await db.execute(
        select(func.count(Payment.id)).where(Payment.status == "pending")
    )).scalar()

    # Credits usage
    today_credits = (await db.execute(
        select(func.sum(ApiUsage.cost_credits)).where(ApiUsage.created_at >= today)
    )).scalar() or 0
    total_credits = (await db.execute(
        select(func.sum(ApiUsage.cost_credits))
    )).scalar() or 0

    # API calls
    today_calls = (await db.execute(
        select(func.count(ApiUsage.id)).where(ApiUsage.created_at >= today)
    )).scalar()

    # Last 7 days usage
    week_ago = today - timedelta(days=7)
    daily_usage = []
    for i in range(7):
        d = today - timedelta(days=6-i)
        dt_start = datetime(d.year, d.month, d.day)
        dt_end = datetime(d.year, d.month, d.day, 23, 59, 59)
        cred = (await db.execute(
            select(func.sum(ApiUsage.cost_credits)).where(
                ApiUsage.created_at >= dt_start, ApiUsage.created_at <= dt_end
            )
        )).scalar() or 0
        daily_usage.append({"date": str(d), "credits": round(cred, 2)})

    # Top users
    top_result = await db.execute(
        select(User.username, User.credits, User.total_credits_used)
        .order_by(User.total_credits_used.desc()).limit(5)
    )
    top_users = [{"username": r[0], "credits": r[1], "total_used": r[2]} for r in top_result.all()]

    return {
        "users": users_count, "active_today": active_today,
        "projects": projects_count, "files": files_count, "generated_files": gen_files,
        "revenue_cny": round(revenue, 2), "pending_payments": pending_payments,
        "today_credits": round(today_credits, 2), "total_credits": round(total_credits, 2),
        "today_calls": today_calls,
        "daily_usage": daily_usage, "top_users": top_users
    }


@app.get("/api/admin/health")
async def admin_health(admin: User = Depends(get_admin)):
    import psutil
    return {
        "cpu_pct": psutil.cpu_percent(interval=0.5),
        "mem_used_pct": psutil.virtual_memory().percent,
        "mem_total_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        "disk_used_pct": psutil.disk_usage("/").percent,
        "disk_free_gb": round(psutil.disk_usage("/").free / 1024**3, 1),
        "uptime_hours": round((datetime.utcnow() - datetime.fromtimestamp(psutil.boot_time())).total_seconds() / 3600, 1)
    }


@app.get("/api/admin/generated-files")
async def admin_generated_files(skip: int = 0, limit: int = 50, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(GeneratedFile).order_by(GeneratedFile.created_at.desc()).offset(skip).limit(limit)
    )
    files = result.scalars().all()
    out = []
    for f in files:
        user_r = await db.execute(select(User.username).where(User.id == f.user_id))
        uname = user_r.scalar_one_or_none() or "?"
        proj_r = await db.execute(select(Project.name).where(Project.id == f.project_id))
        pname = proj_r.scalar_one_or_none() or "?"
        out.append({
            "id": f.id, "username": uname, "project": pname,
            "file_type": f.file_type, "model": f.model_used,
            "credits": f.credits_cost, "created_at": str(f.created_at)
        })
    return out


@app.get("/api/admin/user-files")
async def admin_user_files(admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(UserFile, User.username).join(User, UserFile.user_id == User.id)
        .order_by(UserFile.created_at.desc()).limit(100)
    )
    rows = result.all()
    return [
        {
            "id": f.id, "username": username, "original_name": f.original_name,
            "size_bytes": f.size_bytes, "download_count": f.download_count,
            "created_at": str(f.created_at)
        }
        for f, username in rows
    ]


@app.delete("/api/admin/user-files/{file_id}")
async def admin_delete_user_file(file_id: str, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(UserFile).where(UserFile.id == file_id))
    uf = result.scalar_one_or_none()
    if not uf:
        raise HTTPException(404, "File not found")
    user_result = await db.execute(select(User).where(User.id == uf.user_id))
    file_user = user_result.scalar_one()
    upload_dir = Path(UPLOAD_DIR.format(username=file_user.ssh_username))
    (upload_dir / uf.filename).unlink(missing_ok=True)
    # Delete associated share links
    links = await db.execute(select(FileShareLink).where(FileShareLink.file_id == file_id))
    for link in links.scalars().all():
        await db.delete(link)
    await db.delete(uf)
    await db.commit()
    return {"ok": True}


@app.get("/api/admin/users/{user_id}/detail")
async def admin_user_detail(user_id: str, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    proj_result = await db.execute(select(Project).where(Project.user_id == user_id))
    projects = proj_result.scalars().all()

    file_count = 0
    for p in projects:
        file_count += await _count_files(p.id, db)

    usage_result = await db.execute(
        select(ApiUsage).where(ApiUsage.user_id == user_id)
        .order_by(ApiUsage.created_at.desc()).limit(50)
    )
    usages = usage_result.scalars().all()

    payments_result = await db.execute(
        select(Payment).where(Payment.user_id == user_id).order_by(Payment.created_at.desc())
    )
    payments = payments_result.scalars().all()

    gen_result = await db.execute(
        select(GeneratedFile).where(GeneratedFile.user_id == user_id)
        .order_by(GeneratedFile.created_at.desc()).limit(50)
    )
    gen_files = gen_result.scalars().all()

    return {
        "user": _user_out(user),
        "projects": [_project_out(p) for p in projects],
        "file_count": file_count,
        "usage": [
            {"model": u.model, "input": u.input_tokens, "output": u.output_tokens,
             "cost": u.cost_credits, "endpoint": u.endpoint, "time": str(u.created_at)}
            for u in usages
        ],
        "payments": [
            {"id": p.id, "amount": p.amount_cny, "method": p.payment_method,
             "status": p.status, "plan": p.plan_purchased, "time": str(p.created_at)}
            for p in payments
        ],
        "generated_files": [
            {"id": f.id, "type": f.file_type, "model": f.model_used,
             "credits": f.credits_cost, "time": str(f.created_at)}
            for f in gen_files
        ]
    }


@app.get("/api/admin/users/{user_id}/usage")
async def admin_user_usage_detail(user_id: str, days: int = 30, admin: User = Depends(get_admin), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    since = datetime.utcnow() - timedelta(days=days)
    usage_result = await db.execute(
        select(ApiUsage).where(ApiUsage.user_id == user_id, ApiUsage.created_at >= since)
        .order_by(ApiUsage.created_at.desc()).limit(200)
    )
    usages = usage_result.scalars().all()
    gen_result = await db.execute(
        select(func.count(GeneratedFile.id), func.sum(GeneratedFile.credits_cost))
        .where(GeneratedFile.user_id == user_id)
    )
    gen_stats = gen_result.one()
    return {
        "user": _user_out(user),
        "total_api_calls": len(usages),
        "total_input_tokens": sum(u.input_tokens for u in usages),
        "total_output_tokens": sum(u.output_tokens for u in usages),
        "total_credits_used": round(sum(u.cost_credits for u in usages), 4),
        "generated_files": gen_stats[0] or 0,
        "gen_credits": round(gen_stats[1] or 0, 4),
        "records": [
            {"model": u.model, "input": u.input_tokens, "output": u.output_tokens,
             "cost": u.cost_credits, "endpoint": u.endpoint, "time": str(u.created_at)}
            for u in usages[:100]
        ]
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
                   credits=u.credits, model_tier=u.model_tier or "free",
                   has_api_key=bool(u.api_key and u.api_key.strip()),
                   ssh_username=u.ssh_username, ssh_port=u.ssh_port,
                   is_admin=u.is_admin, is_active=u.is_active,
                   storage_limit=u.storage_limit or 20971520,
                   created_at=u.created_at)


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


async def _parse_and_save_files(project: Project, user: User, reply: str, file_type: str, db: AsyncSession) -> str:
    """Parse file:path blocks from AI reply, save to DB+disk, auto-build doc files."""
    pattern = r'```file:([^\n]+)\n([\s\S]*?)```'
    matches = re.findall(pattern, reply)
    if not matches:
        return ""

    messages = []
    for file_path, content in matches:
        file_path = file_path.strip()
        content = content.strip()
        if not file_path or not content:
            continue

        # Save file to DB + disk
        existing = await db.execute(
            select(ProjectFile).where(ProjectFile.project_id == project.id, ProjectFile.path == file_path)
        )
        pf = existing.scalar_one_or_none()
        if pf:
            pf.content = content
            pf.size_bytes = len(content.encode())
            pf.updated_at = datetime.utcnow()
        else:
            pf = ProjectFile(project_id=project.id, path=file_path, content=content, size_bytes=len(content.encode()))
            db.add(pf)

        # Write to disk
        disk_path = _project_path(user, project) / file_path
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_text(content)

        messages.append(f"[saved] {file_path}")

        # Auto-build: execute .py generator scripts for ppt/doc/pdf
        if file_type in ("ppt", "doc", "pdf") and file_path.endswith(".py"):
            build_msg = await _exec_build_script(project, user, file_path, db)
            if build_msg:
                messages.append(build_msg)

    project.file_count = await _count_files(project.id, db)
    project.updated_at = datetime.utcnow()
    await db.commit()

    return "\n".join(messages) if messages else ""


async def _exec_build_script(project: Project, user: User, script_path: str, db: AsyncSession) -> str:
    """Execute a Python generator script to produce the final document file."""
    proj_dir = _project_path(user, project)
    script_file = proj_dir / script_path

    if not script_file.exists():
        return ""

    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", str(script_file),
            cwd=str(proj_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=30
        )
    except asyncio.TimeoutError:
        return f"[build] {script_path}: timed out"
    except Exception as e:
        return f"[build] {script_path}: {e}"

    if proc.returncode != 0:
        return f"[build] {script_path}: {stderr.decode()[:200]}"

    # Discover new files created by the script
    new_files = []
    for f in proj_dir.iterdir():
        if f.is_file() and f.name != script_path and f.suffix in (".pptx", ".docx", ".pdf"):
            rel = f.relative_to(proj_dir)
            content = f.read_bytes()
            # Save binary-generated file to DB as base64 reference
            existing = await db.execute(
                select(ProjectFile).where(ProjectFile.project_id == project.id, ProjectFile.path == str(rel))
            )
            if not existing.scalar_one_or_none():
                pf = ProjectFile(
                    project_id=project.id,
                    path=str(rel),
                    content=f"[binary: {f.suffix} file, {len(content)} bytes]",
                    size_bytes=len(content)
                )
                db.add(pf)
                new_files.append(str(rel))

    if new_files:
        return f"[built] {', '.join(new_files)}"
    return f"[build] {script_path}: ok"


def _base_url() -> str:
    host = os.environ.get("GIT_HOST", "gristai.top")
    return f"https://{host}"


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
