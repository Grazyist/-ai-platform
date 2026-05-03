#!/usr/bin/env python3
"""Share any file on the server via a public URL."""
import sys, os, uuid, asyncio, aiosqlite
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).parent / "ai_platform.db"
PUBLIC_DIR = Path("/home/public_uploads")
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)


async def share(file_path: str, days: int = 7):
    src = Path(file_path).resolve()
    if not src.is_file():
        print(f"Error: file not found: {src}")
        print(f"  (you are in: {Path.cwd()})")
        sys.exit(1)

    token = uuid.uuid4().hex[:12]
    stored_name = f"{token}_{src.name}"
    dest = PUBLIC_DIR / stored_name

    # Copy file
    import shutil
    shutil.copy2(src, dest)

    size = src.stat().st_size
    expires = datetime.utcnow() + timedelta(days=days)

    async with aiosqlite.connect(str(DB)) as db:
        await db.execute(
            "INSERT INTO public_files (id, token, filename, original_name, size_bytes, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], token, stored_name, src.name, size, expires, datetime.utcnow())
        )
        await db.commit()

    print(f"https://gristai.top/dl/p/{token}")
    print(f"(expires in {days} days, {size} bytes)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} <file_path> [days]")
        print(f"Example: python3 {sys.argv[0]} /tmp/report.pdf 30")
        sys.exit(1)
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    asyncio.run(share(sys.argv[1], days))
