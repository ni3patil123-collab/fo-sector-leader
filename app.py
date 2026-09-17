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
        response.raise_for_status()
        data = response.json()

        wanted = {
            s.upper()
            for stocks in SECTORS.values()
            for s in stocks
        }

        today = now_ist().date()
        candidates = {}

        for item in data:
            if str(item.get("exch_seg", "")).upper() != "NFO":
                continue

            if str(item.get("instrumenttype", "")).upper() != "FUTSTK":
                continue

            name = str(item.get("name", "")).upper().strip()

            if name not in wanted:
                continue

            expiry_text = str(item.get("expiry", "")).strip()

            if not expiry_text:
                continue

            try:
                expiry = datetime.strptime(
                    expiry_text, "%d%b%Y"
                ).date()
            except ValueError:
                continue

            if expiry < today:
                continue

            token = str(item.get("token", "")).strip()

            if not token:
                continue

            # Keep the nearest valid FUTSTK expiry
            if (
                name not in candidates
                or expiry < candidates[name]["expiry"]
            ):
                candidates[name] = {
                    "token": token,
                    "expiry": expiry
                }

        found = {
            name: item["token"]
            for name, item in candidates.items()
        }

        with LOCK:
            FNO_TOKENS = found

        print("F&O FUTSTK tokens loaded:", len(FNO_TOKENS))

        for name, item in sorted(candidates.items()):
            print(
                "TOKEN:",
                name,
                item["token"],
                "EXPIRY:",
                item["expiry"]
            )

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

        auth_token = session["data"]["jwtToken"]
        feed_token = smart_api.getfeedToken()

        print("Angel One auth token received")
        print("Angel One feed token received")

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
def on_live_data(wsapp, message):
    try:
        token = str(message.get("token", ""))

        ltp_raw = message.get("last_traded_price")
        volume_raw = message.get("volume_trade_for_the_day")

        if ltp_raw is None:
            return

        ltp = float(ltp_raw) / 100.0
        volume = int(volume_raw or 0)

        symbol = None

        with LOCK:
            for sym, tok in FNO_TOKENS.items():
                if str(tok) == token:
                    symbol = sym
                    break

            if symbol:
                LIVE.setdefault(symbol, {})
                LIVE[symbol]["ltp"] = ltp
                LIVE[symbol]["volume"] = volume

        if symbol:
            print("LIVE:", symbol, "LTP:", ltp, "VOLUME:", volume)

    except Exception as e:
        print("Live data error:", e)


def on_ws_open(wsapp):
    print("Angel One WebSocket connected")

    tokens = list(FNO_TOKENS.values())

    if not tokens:
        print("No F&O tokens available")
        return

    token_list = [{
        "exchangeType": 2,
        "tokens": [str(x) for x in tokens]
    }]

    websocket.subscribe(
        "foleader",
        2,
        token_list
    )

    print("F&O live subscription started:", len(tokens))


def on_ws_error(wsapp, error):
    print("WebSocket error:", error)


def on_ws_close(wsapp):
    print("Angel One WebSocket closed")


def start_live_websocket():
    global websocket

    load_fno_tokens()

    if not FNO_TOKENS:
        print("F&O tokens not loaded")
        return

    auth_token = smart_api.access_token
    feed_token = smart_api.getfeedToken()

    websocket = SmartWebSocketV2(
        auth_token,
        API_KEY,
        CLIENT_CODE,
        feed_token
    )

    websocket.on_open = on_ws_open
    websocket.on_data = on_live_data
    websocket.on_error = on_ws_error
    websocket.on_close = on_ws_close

    print("Starting Angel One WebSocket...")
    websocket.connect()


def backend_worker():
    while True:
        try:
            if smart_api is None:
                if angel_login():
                    start_live_websocket()
            elif websocket is None:
                start_live_websocket()

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
