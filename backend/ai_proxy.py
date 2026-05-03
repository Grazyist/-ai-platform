import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from models import User, ApiUsage, ProjectFile, Project
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, PRICE_INPUT_1K, PRICE_OUTPUT_1K
from sqlalchemy import select


class AIProxy:
    def __init__(self):
        self.api_key = DEEPSEEK_API_KEY
        self.base_url = DEEPSEEK_BASE_URL

    async def chat(
        self, db: AsyncSession, user: User, project_id: str, messages: list, model: str = "deepseek-chat"
    ) -> dict:
        # Check credits
        if user.credits <= 0:
            raise ValueError("Insufficient credits. Please upgrade your plan.")

        # Build context from project files
        project_files = await self._get_project_context(db, project_id)

        system_msg = {
            "role": "system",
            "content": (
                "You are Claude Code, an expert AI software engineer. "
                "Help the user write, modify, and explain code for their project. "
                "When the user asks you to create or modify files, respond with the "
                "exact file path and complete content. Use this format:\n\n"
                "```file:path/to/file\n"
                "file content here\n"
                "```\n\n"
                "Always provide complete, working code. Be concise and direct.\n\n"
                f"Current project files:\n{project_files}"
            )
        }

        api_messages = [system_msg] + messages

        payload = {
            "model": model,
            "messages": api_messages,
            "max_tokens": 4096,
            "temperature": 0.7
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                json=payload
            )

        if resp.status_code != 200:
            raise RuntimeError(f"DeepSeek API error ({resp.status_code}): {resp.text[:500]}")

        data = resp.json()
        choice = data["choices"][0]["message"]
        usage = data.get("usage", {})

        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        cost = self._calculate_cost(input_tokens, output_tokens)

        # Deduct credits and record usage
        user.credits = max(0, user.credits - cost)
        user.total_credits_used += cost

        usage_record = ApiUsage(
            user_id=user.id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_credits=cost,
            endpoint="chat"
        )
        db.add(usage_record)
        await db.commit()
        await db.refresh(user)

        return {
            "reply": choice["content"],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "credits_used": round(cost, 6),
            "credits_remaining": round(user.credits, 6)
        }

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        input_cost = (input_tokens / 1000) * PRICE_INPUT_1K
        output_cost = (output_tokens / 1000) * PRICE_OUTPUT_1K
        return round(input_cost + output_cost, 6)

    async def _get_project_context(self, db: AsyncSession, project_id: str) -> str:
        result = await db.execute(
            select(ProjectFile).where(ProjectFile.project_id == project_id)
        )
        files = result.scalars().all()
        if not files:
            return "(empty project — create your first file)"
        lines = []
        for f in files[:50]:  # limit context
            lines.append(f"--- {f.path} ---")
            lines.append(f.content[:2000])
        return "\n".join(lines)


ai_proxy = AIProxy()
