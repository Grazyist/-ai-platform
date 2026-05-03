import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship
from database import Base


def gen_id():
    return uuid.uuid4().hex[:12]


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, default=gen_id)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(120), unique=True, nullable=False)
    hashed_password = Column(String(200), nullable=False)
    plan = Column(String(20), default="free")  # free, pro, enterprise
    credits = Column(Float, default=100.0)     # remaining credits
    total_credits_used = Column(Float, default=0.0)
    api_key = Column(String(100), default="")  # user's own DeepSeek API key (set by admin)
    model_tier = Column(String(20), default="free")  # free, paid
    ssh_username = Column(String(50), unique=True)
    ssh_password_hash = Column(String(200))
    ssh_port = Column(Integer)
    container_name = Column(String(100))
    storage_limit = Column(Integer, default=20971520)  # 20MB default
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    projects = relationship("Project", back_populates="owner")


class Project(Base):
    __tablename__ = "projects"
    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    share_token = Column(String(50), unique=True)
    is_public = Column(Boolean, default=False)
    file_count = Column(Integer, default=0)
    size_bytes = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)
    owner = relationship("User", back_populates="projects")
    files = relationship("ProjectFile", back_populates="project", cascade="all, delete-orphan")


class ProjectFile(Base):
    __tablename__ = "project_files"
    id = Column(String, primary_key=True, default=gen_id)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    path = Column(String(500), nullable=False)   # relative path in project
    content = Column(Text, default="")
    size_bytes = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)
    project = relationship("Project", back_populates="files")


class ApiUsage(Base):
    __tablename__ = "api_usage"
    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    model = Column(String(50))                    # deepseek-chat, deepseek-reasoner
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost_credits = Column(Float, default=0.0)
    endpoint = Column(String(50))                 # chat, code, etc
    created_at = Column(DateTime, default=datetime.utcnow)


class Payment(Base):
    __tablename__ = "payments"
    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    amount_cny = Column(Float, nullable=False)
    payment_method = Column(String(20))           # wechat, alipay
    status = Column(String(20), default="pending") # pending, paid, failed
    plan_purchased = Column(String(20))
    credits_added = Column(Float, default=0.0)
    trade_no = Column(String(100), unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ShareLink(Base):
    __tablename__ = "share_links"
    id = Column(String, primary_key=True, default=gen_id)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    token = Column(String(50), unique=True, nullable=False)
    expires_at = Column(DateTime)
    download_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow)


class GeneratedFile(Base):
    __tablename__ = "generated_files"
    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False)
    file_type = Column(String(20))   # ppt, doc, pdf, code, html, other
    file_name = Column(String(200))
    file_path = Column(String(500))
    size_bytes = Column(Integer, default=0)
    model_used = Column(String(50))
    credits_cost = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserFile(Base):
    __tablename__ = "user_files"
    id = Column(String, primary_key=True, default=gen_id)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    filename = Column(String(200), nullable=False)  # stored name
    original_name = Column(String(200), nullable=False)  # display name
    size_bytes = Column(Integer, default=0)
    download_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class FileShareLink(Base):
    __tablename__ = "file_share_links"
    id = Column(String, primary_key=True, default=gen_id)
    file_id = Column(String, ForeignKey("user_files.id"), nullable=False)
    token = Column(String(50), unique=True, nullable=False)
    expires_at = Column(DateTime)
    download_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class PublicFile(Base):
    __tablename__ = "public_files"
    id = Column(String, primary_key=True, default=gen_id)
    token = Column(String(30), unique=True, nullable=False, index=True)
    filename = Column(String(200), nullable=False)
    original_name = Column(String(200), nullable=False)
    size_bytes = Column(Integer, default=0)
    download_count = Column(Integer, default=0)
    password_hash = Column(String(200), default="")
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
