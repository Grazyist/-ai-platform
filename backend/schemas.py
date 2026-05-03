from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime


class UserRegister(BaseModel):
    username: str
    email: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    username: str
    email: str
    plan: str
    credits: float
    model_tier: str = "free"
    has_api_key: bool = False
    ssh_username: Optional[str] = None
    ssh_port: Optional[int] = None
    is_admin: bool = False
    is_active: bool = True
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ProjectCreate(BaseModel):
    name: str
    description: str = ""


class ProjectOut(BaseModel):
    id: str
    name: str
    description: str
    share_token: Optional[str] = None
    is_public: bool
    file_count: int
    size_bytes: int
    created_at: datetime
    updated_at: datetime


class ProjectFileOut(BaseModel):
    id: str
    path: str
    content: str
    size_bytes: int


class FileWrite(BaseModel):
    path: str
    content: str


class FileDelete(BaseModel):
    path: str


class ChatMessage(BaseModel):
    role: str           # user / assistant / system
    content: str


class ChatRequest(BaseModel):
    project_id: str
    messages: List[ChatMessage]
    model: str = "deepseek-chat"
    file_type: str = "code"  # code, ppt, doc, html, pdf, other


class ChatResponse(BaseModel):
    reply: str
    input_tokens: int
    output_tokens: int
    credits_used: float
    credits_remaining: float
    model_used: str = ""


class ModelInfo(BaseModel):
    id: str
    name: str
    desc: str
    multiplier: float = 1.0


class AdminUserUpdate(BaseModel):
    api_key: Optional[str] = None
    plan: Optional[str] = None
    model_tier: Optional[str] = None
    credits: Optional[float] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None


class BillingPlan(BaseModel):
    key: str
    name: str
    price_cny: float
    credits: int
    max_projects: int


class PaymentRequest(BaseModel):
    plan_key: str
    method: str  # wechat, alipay


class PaymentOut(BaseModel):
    id: str
    amount_cny: float
    payment_method: str
    status: str
    trade_no: str
    qr_url: Optional[str] = None  # QR code URL for WeChat/Alipay


class ShareLinkOut(BaseModel):
    token: str
    url: str
    download_count: int
    expires_at: Optional[datetime]
