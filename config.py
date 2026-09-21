import os
from dotenv import load_dotenv

load_dotenv()

# ── Capital.com ──────────────────────────────────────────────────────────────
TRADING_MODE = os.getenv("TRADING_MODE", "DEMO").upper()

CAPITAL_API_KEY    = os.getenv("CAPITAL_API_KEY")
CAPITAL_PASSWORD   = os.getenv("CAPITAL_PASSWORD")
CAPITAL_IDENTIFIER = os.getenv("CAPITAL_IDENTIFIER")

CAPITAL_BASE_URL = (
    "https://demo-api-capital.backend-capital.com"
    if TRADING_MODE == "DEMO"
    else "https://api-capital.backend-capital.com"
)

# ── EIA (oil inventory — free API key required) ───────────────────────────────
EIA_API_KEY = os.getenv("EIA_API_KEY", "")

# ── Risk ─────────────────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE   = float(os.getenv("MAX_RISK_PER_TRADE", 0.01))   # 1% of account
MIN_MARGIN_PERCENT   = float(os.getenv("MIN_MARGIN_PERCENT", 350))    # 350%
MAX_SLIPPAGE         = 0.0005   # 0.05%
ATR_SL_MULTIPLIER    = 1.0      # SL = 1x ATR
PARTIAL_CLOSE_RR     = 3.0      # TP = 3x SL distance (3:1 R:R)
TRAILING_STOP_PCT    = 0.003    # 0.3%
BREAKEVEN_BUFFER        = 0.001   # 0.1% above/below entry after TP1
BREAKEVEN_PROFIT_PCT    = 0.003   # move SL to breakeven when unrealized profit hits 0.3%
ATR_REFRESH_INTERVAL    = 3600    # refresh ATR every hour (seconds)

# ── Kelly Criterion ───────────────────────────────────────────────────────────
# Max fraction of capital to risk per trade (Kelly is capped at this)
KELLY_MAX_FRACTION   = float(os.getenv("KELLY_MAX_FRACTION", 0.03))   # 3% hard cap
# Win probability priors per confidence tier (tuned conservatively)
KELLY_WIN_PROB = {"HIGH": 0.62, "MEDIUM": 0.55, "LOW": 0.50}

# ── Circuit Breaker ───────────────────────────────────────────────────────────
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT", 0.05))   # 5% of balance

# ── Polygon (Massive.com) ─────────────────────────────────────────────────────
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
POLYGON_BASE_URL = "https://api.polygon.io"

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
FOMO_PRICE_THRESHOLD = 0.01     # 1% price move = signal expired


# ── Technical Analysis ────────────────────────────────────────────────────────
EMA_PERIOD         = 21
ATR_PERIOD         = 14
OHLCV_INTERVAL     = "MINUTE_15"   # Capital.com resolution
OHLCV_BARS         = 100           # bars to fetch for TA

# Fibonacci Value Zone (price must be within this % of Fib 0.618 or S/R)
VALUE_ZONE_TOLERANCE = 0.005   # 0.5%
