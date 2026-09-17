import os
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

from flask import Flask, jsonify
from flask_cors import CORS

from SmartApi.smartConnect import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import pyotp

app = Flask(__name__)
CORS(app)

IST = ZoneInfo("Asia/Kolkata")

# ============================================================
# ANGEL ONE ENVIRONMENT VARIABLES
# ============================================================
API_KEY = os.getenv("ANGEL_API_KEY", "")
CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "")
PASSWORD = os.getenv("ANGEL_PASSWORD", "")
TOTP_SECRET = os.getenv("ANGEL_TOTP_KEY", "")

# ============================================================
# SECTORS
# ============================================================
SECTORS = {
    "DEFENCE": ["HAL", "BEL", "BDL", "COCHINSHIP", "MAZDOCK", "SOLARINDS", "BHEL", "BHARATFORG"],
    "IT": ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM", "MPHASIS", "COFORGE"],
    "PHARMA": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "TORNTPHARM", "AUROPHARMA", "LUPIN"],
    "BANKING": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK", "BANKBARODA", "PNB", "YESBANK"],
    "FINANCIAL 1": ["BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN", "CHOLAFIN", "MUTHOOTFIN", "LICHSGFIN"],
    "FINANCIAL 2": ["PFC", "RECLTD", "IRFC", "HUDCO", "IEX", "JIOFIN"],
    "AUTO": ["MARUTI", "M&M", "TATAMOTORS", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "TVSMOTOR", "ASHOKLEY"],
    "REALTY": ["DLF", "LODHA", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "PHOENIXLTD"],
    "CEMENT": ["ULTRACEMCO", "GRASIM", "SHREECEM", "DALBHARAT", "AMBUJACEM", "ACC"],
    "ENERGY 1": ["RELIANCE", "ONGC", "IOC", "BPCL", "GAIL", "OIL"],
    "ENERGY 2": ["NTPC", "POWERGRID", "TATAPOWER", "ADANIGREEN", "ADANIPOWER", "JSWENERGY"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "TATACONSUM", "MARICO", "DABUR"],
    "CHEMICAL": ["PIDILITIND", "SRF", "UPL", "DEEPAKNTR", "NAVINFLUOR", "PIIND"],
    "CAPITAL GOODS": ["LT", "SIEMENS", "ABB", "CUMMINSIND", "BEL", "BHEL", "CGPOWER"],
    "TELECOM": ["BHARTIARTL", "IDEA", "INDUSTOWER"],
    "CONSUMER": ["TITAN", "TRENT", "KALYANKJIL", "DMART", "VOLTAS", "HAVELLS"],
}

# ============================================================
# LIVE DATA CACHE
# ============================================================
LIVE = {}
LOCK = threading.Lock()

smart_api = None
websocket = None
INSTRUMENT_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
FNO_TOKENS = {}

def now_ist():
    return datetime.now(IST)


def market_status():
    now = now_ist()

    if now.weekday() >= 5:
        return "CLOSED"

    t = now.time()

    if t < datetime.strptime("09:15", "%H:%M").time():
        return "PRE-MARKET"

    if t <= datetime.strptime("15:30", "%H:%M").time():
        return "OPEN"

    return "CLOSED"

    def load_fno_tokens():
    global FNO_TOKENS

    try:
        print("Loading Angel One F&O instrument master...")

        response = requests.get(INSTRUMENT_URL, timeout=30)
        data = response.json()

        wanted = {s.upper() for stocks in SECTORS.values() for s in stocks}
        found = {}

        for item in data:
            if item.get("exch_seg") != "NFO":
                continue

            name = str(item.get("name", "")).upper()

            if name in wanted:
                found[name] = str(item.get("token"))

        with LOCK:
            FNO_TOKENS = found

                print("F&O tokens loaded:", len(FNO_TOKENS))

        except Exception as e:
                print("F&O token loading error:", e)
# ============================================================
# ANGEL ONE LOGIN
# ============================================================
def angel_login():
    global smart_api

    if not all([API_KEY, CLIENT_CODE, PASSWORD, TOTP_SECRET]):
        print("Angel One environment variables are missing.")
        return False

    try:
        smart_api = SmartConnect(api_key=API_KEY)

        totp = pyotp.TOTP(TOTP_SECRET).now()

        session = smart_api.generateSession(
            CLIENT_CODE,
            PASSWORD,
            totp
        )

        if not session or not session.get("status"):
            print("Angel One login failed:", session)
            return False

        print("Angel One login successful")
        return True

    except Exception as e:
        print("Angel One login error:", e)
        return False


# ============================================================
# SIMPLE LIVE ENDPOINTS
# ============================================================
@app.get("/")
def home():
    return jsonify({
        "app": "F&O SECTOR LEADER",
        "status": "running",
        "market": market_status(),
        "time": now_ist().strftime("%Y-%m-%d %H:%M:%S")
    })


@app.get("/api/sectors")
def sectors():
    return jsonify({
        "sectors": list(SECTORS.keys())
    })


@app.get("/api/sector/<sector>")
def sector(sector):
    if sector not in SECTORS:
        return jsonify({
            "error": "Unknown sector"
        }), 404

    stocks = []

    with LOCK:
        for symbol in SECTORS[sector]:
            item = LIVE.get(symbol, {})

            stocks.append({
                "symbol": symbol,
                "ltp": item.get("ltp"),
                "change_pct": item.get("change_pct"),
                "volume": item.get("volume"),
                "rvol": item.get("rvol"),
                "move": item.get("move"),
                "signal": item.get("signal"),
                "pass_time": item.get("pass_time")
            })

    return jsonify({
        "sector": sector,
        "market": market_status(),
        "stocks": stocks
    })


@app.get("/api/live")
def live():
    with LOCK:
        return jsonify(LIVE)


# ============================================================
# BACKGROUND ANGEL LOGIN
# ============================================================
def backend_worker():
    while True:
        try:
            if smart_api is None:
                angel_login()

        except Exception as e:
            print("Worker error:", e)

        time.sleep(30)


threading.Thread(
    target=backend_worker,
    daemon=True
).start()


# ============================================================
# RENDER PORT
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port
    )
