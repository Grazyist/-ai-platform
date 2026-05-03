import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from models import User, ApiUsage, ProjectFile, Project, GeneratedFile
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, PRICE_INPUT_1K, PRICE_OUTPUT_1K, MODELS, FILE_TYPES
from sqlalchemy import select
from datetime import datetime


class AIProxy:
    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY
        self.base_url = DEEPSEEK_BASE_URL

    def _get_effective_key(self, user: User) -> str:
        """Use user's personal API key if set, otherwise system key."""
        if user.api_key and user.api_key.strip():
            return user.api_key.strip()
        return self.api_key

    def _get_models_for_user(self, user: User) -> list:
        tier = user.model_tier if user.model_tier in MODELS else "free"
        return MODELS.get(tier, MODELS["free"])

    def _get_model_multiplier(self, user: User, model_id: str) -> float:
        models = self._get_models_for_user(user)
        for m in models:
            if m["id"] == model_id:
                return m.get("multiplier", 1.0)
        return 1.0

    async def chat(
        self, db: AsyncSession, user: User, project_id: str, messages: list,
        model: str = "deepseek-chat", file_type: str = "code"
    ) -> dict:
        if user.credits <= 0:
            raise ValueError("Insufficient credits. Please upgrade your plan.")

        # Validate model access
        available_models = [m["id"] for m in self._get_models_for_user(user)]
        if model not in available_models:
            raise ValueError(f"Model '{model}' not available on your tier. Upgrade to access it.")

        api_key = self._get_effective_key(user)
        if not api_key:
            raise ValueError("No API key configured. Admin must set a DeepSeek API key.")

        project_files = await self._get_project_context(db, project_id)
        system_msg = self._build_system_prompt(file_type, project_files)

        api_messages = [{"role": "system", "content": system_msg}] + messages

        payload = {
            "model": model,
            "messages": api_messages,
            "max_tokens": 8192,
            "temperature": 0.7
        }

        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json=payload
            )

        if resp.status_code != 200:
            err = resp.text[:300]
            if resp.status_code == 401:
                err = "API Key invalid or expired. Ask admin to update it."
            raise RuntimeError(f"API error ({resp.status_code}): {err}")

        data = resp.json()
        choice = data["choices"][0]["message"]
        usage = data.get("usage", {})

        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        multiplier = self._get_model_multiplier(user, model)
        cost = self._calculate_cost(input_tokens, output_tokens, multiplier)

        user.credits = max(0, user.credits - cost)
        user.total_credits_used += cost

        usage_record = ApiUsage(
            user_id=user.id, model=model, input_tokens=input_tokens,
            output_tokens=output_tokens, cost_credits=cost, endpoint="chat"
        )
        db.add(usage_record)

        # Track generated file
        gen_file = GeneratedFile(
            user_id=user.id, project_id=project_id, file_type=file_type,
            model_used=model, credits_cost=cost
        )
        db.add(gen_file)

        await db.commit()
        await db.refresh(user)

        return {
            "reply": choice["content"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "credits_used": round(cost, 6),
            "credits_remaining": round(user.credits, 6),
            "model_used": model
        }

    def _calculate_cost(self, input_tokens: int, output_tokens: int, multiplier: float = 1.0) -> float:
        input_cost = (input_tokens / 1000) * PRICE_INPUT_1K
        output_cost = (output_tokens / 1000) * PRICE_OUTPUT_1K
        return round((input_cost + output_cost) * multiplier, 6)

    def _build_system_prompt(self, file_type: str, project_files: str) -> str:
        prompts = {
            "code": (
                "You are Claude Code, an expert AI software engineer. "
                "Help the user write, modify, and explain code. "
                "When creating files, use this format:\n"
                "```file:path/to/file\ncontent\n```\n"
                "Be concise, provide complete working code."
            ),
            "ppt": (
                "You generate PowerPoint presentations. Create structured slide content with titles and bullet points. "
                "Format your output as a Python script using python-pptx library. Use this format:\n"
                "```file:presentation.py\n"
                "from pptx import Presentation\n"
                "from pptx.util import Inches, Pt\n"
                "prs = Presentation()\n"
                "# Add slides with proper layouts...\n"
                "```\n"
                "Include clear slide titles, concise bullet points, and professional formatting."
            ),
            "doc": (
                "You generate Word documents. Create structured document content. "
                "Format output as Python using python-docx. Use format:\n"
                "```file:document.py\n"
                "from docx import Document\n"
                "from docx.shared import Inches, Pt\n"
                "doc = Document()\n"
                "# Add headings, paragraphs, formatting...\n"
                "```"
            ),
            "html": (
                "You generate complete HTML pages with embedded CSS and JS. "
                "Create beautiful, responsive single-page applications. "
                "Use format: ```file:index.html\n<!DOCTYPE html>...```"
            ),
            "pdf": (
                "You generate PDF documents. Create structured reports with professional formatting. "
                "Format output as Python using fpdf2 library. Use format:\n"
                "```file:document.py\n"
                "from fpdf import FPDF\n"
                "pdf = FPDF()\n"
                "pdf.add_page()\n"
                "# Add content with pdf.cell(), pdf.multi_cell()...\n"
                "```\n"
                "Include headers, sections, tables, and proper UTF-8 font support."
            ),
        }
        base = prompts.get(file_type, prompts["code"])
        if project_files and project_files != "(empty project — create your first file)":
            base += f"\n\nCurrent project files:\n{project_files}"
        return base

    async def _get_project_context(self, db: AsyncSession, project_id: str) -> str:
        result = await db.execute(
            select(ProjectFile).where(ProjectFile.project_id == project_id)
        )
        files = result.scalars().all()
        if not files:
            return "(empty project — create your first file)"
        lines = []
        for f in files[:50]:
            lines.append(f"--- {f.path} ---")
            lines.append(f.content[:2000])
        return "\n".join(lines)


ai_proxy = AIProxy()
