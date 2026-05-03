#!/usr/bin/env python3
"""Share any file on the server via a public URL.

  share /path/to/file [days] [-p password]   Share a file
  share -l | --list                            List active shares
  share -d <token> | --delete <token>          Delete a share
"""
import sys, os, uuid, asyncio, aiosqlite, bcrypt
from datetime import datetime, timedelta
from pathlib import Path

DB = Path(__file__).parent / "ai_platform.db"
PUBLIC_DIR = Path("/home/public_uploads")
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)


async def share(file_path: str, days: int = 7, password: str = ""):
    src = Path(file_path).resolve()
    if not src.is_file():
        print(f"Error: file not found: {src}")
        print(f"  (you are in: {Path.cwd()})")
        sys.exit(1)

    token = uuid.uuid4().hex[:12]
    stored_name = f"{token}_{src.name}"
    dest = PUBLIC_DIR / stored_name

    import shutil
    shutil.copy2(src, dest)

    size = src.stat().st_size
    expires = datetime.utcnow() + timedelta(days=days)
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode() if password else ""

    async with aiosqlite.connect(str(DB)) as db:
        await db.execute(
            "INSERT INTO public_files (id, token, filename, original_name, size_bytes, password_hash, expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex[:12], token, stored_name, src.name, size, pw_hash, expires, datetime.utcnow())
        )
        await db.commit()

    print(f"https://gristai.top/dl/p/{token}")
    print(f"(expires in {days} days, {size} bytes)" + (" [password protected]" if password else ""))


async def list_shares():
    async with aiosqlite.connect(str(DB)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT token, original_name, size_bytes, download_count, password_hash, expires_at, created_at "
            "FROM public_files ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        if not rows:
            print("No active shares.")
            return
        print(f"{'token':<14} {'file':<30} {'size':>8} {'dls':>5} {'pw':>4} {'expires':<20}")
        print("-" * 90)
        for r in rows:
            size_kb = r["size_bytes"] / 1024
            size_str = f"{size_kb:.1f}KB" if size_kb < 1024 else f"{size_kb/1024:.1f}MB"
            exp = r["expires_at"][:16] if r["expires_at"] else "never"
            pw = "yes" if r["password_hash"] else "no"
            print(f"{r['token']:<14} {r['original_name']:<30} {size_str:>8} {r['download_count']:>5} {pw:>4} {exp:<20}")


async def delete_share(token: str):
    async with aiosqlite.connect(str(DB)) as db:
        cursor = await db.execute("SELECT filename, original_name FROM public_files WHERE token = ?", (token,))
        row = await cursor.fetchone()
        if not row:
            print(f"No share found with token: {token}")
            sys.exit(1)
        filename, original_name = row
        (PUBLIC_DIR / filename).unlink(missing_ok=True)
        await db.execute("DELETE FROM public_files WHERE token = ?", (token,))
        await db.commit()
        print(f"Deleted: {original_name} ({token})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  share <file> [days] [-p password]     Share a file")
        print("  share -l | --list                      List active shares")
        print("  share -d <token> | --delete <token>    Delete a share")
        sys.exit(1)

    arg1 = sys.argv[1]

    if arg1 in ("-l", "--list"):
        asyncio.run(list_shares())
    elif arg1 in ("-d", "--delete") and len(sys.argv) > 2:
        asyncio.run(delete_share(sys.argv[2]))
    else:
        # Parse file path, optional days, optional -p password
        file_path = arg1
        days = 7
        password = ""
        i = 2
        while i < len(sys.argv):
            if sys.argv[i] == "-p" and i + 1 < len(sys.argv):
                password = sys.argv[i + 1]
                i += 2
            else:
                try:
                    days = int(sys.argv[i])
                except ValueError:
                    print(f"Unknown option: {sys.argv[i]}")
                    sys.exit(1)
                i += 1
        asyncio.run(share(file_path, days, password))
