import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_URL = f"sqlite+aiosqlite:///{BASE_DIR}/ai_platform.db"
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production-8f3a1b2c")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"

# Billing: price per 1K tokens (input, output)
PRICE_INPUT_1K = 0.00014   # $0.14 / 1M tokens
PRICE_OUTPUT_1K = 0.00028  # $0.28 / 1M tokens

# Subscription tiers (CNY)
PLANS = {
    "free": {"name": "Free", "price_cny": 0, "credits": 100, "max_projects": 3},
    "pro": {"name": "Pro", "price_cny": 49, "credits": 5000, "max_projects": 20},
    "enterprise": {"name": "Enterprise", "price_cny": 199, "credits": 50000, "max_projects": 999},
}

SSH_BASE_DIR = "/home"
PROJECTS_DIR = "/home/{username}/projects"
