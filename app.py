import os
import time
import threading
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import numpy as np
import pyotp

from flask import Flask, jsonify
from flask_cors import CORS

from SmartApi.smartConnect import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2


# ============================================================
# APP
# ============================================================

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
# SETTINGS
# ============================================================

MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

# Original scanner direction
# BUY / SELL / BOTH
DIRECTION = "SELL"

# Original Early settings
EARLY_RVOL_MIN = 1.20
EARLY_VOL_ACCEL_MIN = 1.20
EARLY_NEAR_PCT = 0.30
EARLY_NEED = 7

# Original Final settings
FINAL_RVOL_MIN = 1.10
FINAL_MOVE_MIN = 0.10
FINAL_BODY_MIN = 0.40

# Historical API protection
API_SLEEP = 0.40

INSTRUMENT_URL = (
    "https://margincalculator.angelone.in/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# SECTORS
# ============================================================

SECTORS = {
    "DEFENCE": [
        "HAL", "BEL", "BDL", "COCHINSHIP",
        "MAZDOCK", "SOLARINDS", "BHEL", "BHARATFORG"
    ],

    "IT": [
        "TCS", "INFY", "HCLTECH", "WIPRO",
        "TECHM", "LTIM", "MPHASIS", "COFORGE"
    ],

    "PHARMA": [
        "SUNPHARMA", "DRREDDY", "CIPLA",
        "DIVISLAB", "TORNTPHARM", "AUROPHARMA", "LUPIN"
    ],

    "BANKING": [
        "HDFCBANK", "ICICIBANK", "SBIN",
        "AXISBANK", "KOTAKBANK", "INDUSINDBK",
        "BANKBARODA", "PNB", "YESBANK"
    ],

    "FINANCIAL 1": [
        "BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN",
        "CHOLAFIN", "MUTHOOTFIN", "LICHSGFIN"
    ],

    "FINANCIAL 2": [
        "PFC", "RECLTD", "IRFC",
        "HUDCO", "IEX", "JIOFIN"
    ],

    "AUTO": [
        "MARUTI", "M&M", "TATAMOTORS",
        "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO",
        "TVSMOTOR", "ASHOKLEY"
    ],

    "REALTY": [
        "DLF", "LODHA", "GODREJPROP",
        "OBEROIRLTY", "PRESTIGE", "PHOENIXLTD"
    ],

    "CEMENT": [
        "ULTRACEMCO", "GRASIM", "SHREECEM",
        "DALBHARAT", "AMBUJACEM", "ACC"
    ],

    "ENERGY 1": [
        "RELIANCE", "ONGC", "IOC",
        "BPCL", "GAIL", "OIL"
    ],

    "ENERGY 2": [
        "NTPC", "POWERGRID", "TATAPOWER",
        "ADANIGREEN", "ADANIPOWER", "JSWENERGY"
    ],

    "FMCG": [
        "HINDUNILVR", "ITC", "NESTLEIND",
        "BRITANNIA", "TATACONSUM", "MARICO", "DABUR"
    ],

    "CHEMICAL": [
        "PIDILITIND", "SRF", "UPL",
        "DEEPAKNTR", "NAVINFLUOR", "PIIND"
    ],

    "CAPITAL GOODS": [
        "LT", "SIEMENS", "ABB",
        "CUMMINSIND", "BEL", "BHEL", "CGPOWER"
    ],

    "TELECOM": [
        "BHARTIARTL", "IDEA", "INDUSTOWER"
    ],

    "CONSUMER": [
        "TITAN", "TRENT", "KALYANKJIL",
        "DMART", "VOLTAS", "HAVELLS"
    ],
}


# ============================================================
# GLOBAL DATA
# ============================================================

LIVE = {}
LOCK = threading.RLock()

FNO_TOKENS = {}
TOKEN_TO_SYMBOL = {}

RVOL_CACHE = {}
RVOL_LOCK = threading.RLock()

smart_api = None
websocket = None

LAST_LOGIN = None
LAST_HISTORY_UPDATE = None
LAST_ERROR = ""

STATE_DATE = None


# ============================================================
# TIME HELPERS
# ============================================================

def now_ist():
    return datetime.now(IST)


def today_key():
    return now_ist().strftime("%Y-%m-%d")


def market_status():
    now = now_ist()

    if now.weekday() >= 5:
        return "CLOSED"

    current = now.time()

    if current < MARKET_OPEN:
        return "PRE-MARKET"

    if current <= MARKET_CLOSE:
        return "OPEN"

    return "CLOSED"


def market_open_now():
    return market_status() == "OPEN"


# ============================================================
# DAILY RESET
# ============================================================

def reset_daily_state_if_needed():
    global STATE_DATE

    current_date = today_key()

    with LOCK:
        if STATE_DATE == current_date:
            return

        STATE_DATE = current_date

        for symbol in list(LIVE.keys()):
            LIVE[symbol]["pass_time"] = None
            LIVE[symbol]["signal"] = "WAIT"
            LIVE[symbol]["signal_time"] = None

        print("DAILY STATE RESET:", current_date, flush=True)


# ============================================================
# SECTOR HELPERS
# ============================================================

def symbol_sector(symbol):
    for sector_name, stocks in SECTORS.items():
        if symbol in stocks:
            return sector_name
    return None


def all_symbols():
    result = []

    for stocks in SECTORS.values():
        for symbol in stocks:
            if symbol not in result:
                result.append(symbol)

    return result


# ============================================================
# ANGEL LOGIN
# ============================================================

def angel_login():
    global smart_api
    global LAST_LOGIN
    global LAST_ERROR

    if not all([
        API_KEY,
        CLIENT_CODE,
        PASSWORD,
        TOTP_SECRET
    ]):
        LAST_ERROR = "Angel One environment variables missing."
        print(LAST_ERROR, flush=True)
        return False

    try:
        print("ANGEL LOGIN START...", flush=True)

        api = SmartConnect(api_key=API_KEY)

        totp = pyotp.TOTP(TOTP_SECRET).now()

        session = api.generateSession(
            CLIENT_CODE,
            PASSWORD,
            totp
        )

        if not session or not session.get("status"):
            LAST_ERROR = f"Angel login failed: {session}"
            print(LAST_ERROR, flush=True)
            return False

        smart_api = api

        LAST_LOGIN = now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        LAST_ERROR = ""

        print(
            "ANGEL ONE LOGIN SUCCESS:",
            LAST_LOGIN,
            flush=True
        )

        return True

    except Exception as e:
        LAST_ERROR = f"Angel login error: {e}"
        print(LAST_ERROR, flush=True)
        return False


# ============================================================
# LOAD F&O FUTURE TOKENS
# ============================================================

def load_fno_tokens():
    global FNO_TOKENS
    global TOKEN_TO_SYMBOL
    global LAST_ERROR

    try:
        print(
            "Loading Angel One F&O instrument master...",
            flush=True
        )

        response = requests.get(
            INSTRUMENT_URL,
            timeout=60
        )

        response.raise_for_status()

        data = response.json()

        wanted = {
            symbol.upper()
            for symbol in all_symbols()
        }

        today = now_ist().date()

        candidates = {}

        for item in data:

            if str(
                item.get("exch_seg", "")
            ).upper() != "NFO":
                continue

            if str(
                item.get("instrumenttype", "")
            ).upper() != "FUTSTK":
                continue

            name = str(
                item.get("name", "")
            ).upper().strip()

            if name not in wanted:
                continue

            expiry_text = str(
                item.get("expiry", "")
            ).strip()

            if not expiry_text:
                continue

            try:
                expiry = datetime.strptime(
                    expiry_text,
                    "%d%b%Y"
                ).date()

            except Exception:
                continue

            if expiry < today:
                continue

            token = str(
                item.get("token", "")
            ).strip()

            if not token:
                continue

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

        reverse = {
            token: name
            for name, token in found.items()
        }

        with LOCK:
            FNO_TOKENS = found
            TOKEN_TO_SYMBOL = reverse

            for symbol in found:
                LIVE.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "ltp": None,
                        "volume": None,
                        "change_pct": None,
                        "rvol": None,
                        "move": None,
                        "body": None,
                        "ema20": None,
                        "ema50": None,
                        "vwap": None,
                        "rsi": None,
                        "adx": None,
                        "di_plus": None,
                        "di_minus": None,
                        "pdh": None,
                        "pdl": None,
                        "early_score": None,
                        "early_signal": "WAIT",
                        "signal": "WAIT",
                        "pass_time": None,
                        "signal_time": None
                    }
                )

        print(
            "F&O FUTSTK TOKENS LOADED:",
            len(FNO_TOKENS),
            flush=True
        )

        for name, item in sorted(
            candidates.items()
        ):
            print(
                "TOKEN:",
                name,
                item["token"],
                "EXPIRY:",
                item["expiry"],
                flush=True
            )

        LAST_ERROR = ""

        return len(FNO_TOKENS) > 0

    except Exception as e:
        LAST_ERROR = f"F&O token loading error: {e}"
        print(LAST_ERROR, flush=True)
        return False


# ============================================================
# HISTORICAL CANDLE REQUEST
# ============================================================

def get_candles(
    token,
    from_date,
    to_date
):
    if smart_api is None:
        return []

    try:
        response = smart_api.getCandleData({
            "exchange": "NFO",
            "symboltoken": str(token),
            "interval": "FIVE_MINUTE",
            "fromdate": from_date,
            "todate": to_date
        })

        if not response:
            return []

        if not response.get("status"):
            return []

        return response.get("data") or []

    except Exception as e:
        print(
            "CANDLE ERROR:",
            token,
            e,
            flush=True
        )

        return []


# ============================================================
# CANDLE DATAFRAME
# ============================================================

def candles_to_df(candles):
    if not candles:
        return pd.DataFrame()

    rows = []

    for row in candles:
        if len(row) < 6:
            continue

        try:
            timestamp = datetime.fromisoformat(
                str(row[0]).replace(
                    "Z",
                    "+00:00"
                )
            )

            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(
                    tzinfo=IST
                )
            else:
                timestamp = timestamp.astimezone(
                    IST
                )

            rows.append({
                "timestamp": timestamp,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5])
            })

        except Exception:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    df = df.sort_values(
        "timestamp"
    ).drop_duplicates(
        "timestamp"
    )

    df = df.reset_index(drop=True)

    return df


# ============================================================
# 20D SAME-TIME CUMULATIVE RVOL BASELINE
# ============================================================

def fetch_20d_rvol_baseline(
    symbol,
    token
):
    try:
        today = now_ist().date()

        start_date = today - timedelta(
            days=45
        )

        candles = get_candles(
            token,
            start_date.strftime(
                "%Y-%m-%d"
            ) + " 09:15",
            today.strftime(
                "%Y-%m-%d"
            ) + " 15:30"
        )

        if not candles:
            print(
                "RVOL HISTORY EMPTY:",
                symbol,
                flush=True
            )
            return False

        df = candles_to_df(candles)

        if df.empty:
            return False

        df["day"] = df["timestamp"].dt.date
        df["hhmm"] = df[
            "timestamp"
        ].dt.strftime("%H:%M")

        # आजचा data baseline मध्ये नाही
        df = df[
            df["day"] < today
        ]

        # Weekend नाही
        df = df[
            df["timestamp"].dt.weekday < 5
        ]

        if df.empty:
            return False

        trading_days = sorted(
            df["day"].unique()
        )[-20:]

        if len(trading_days) < 20:
            print(
                "RVOL NEEDS 20 DAYS:",
                symbol,
                "FOUND:",
                len(trading_days),
                flush=True
            )
            return False

        df = df[
            df["day"].isin(trading_days)
        ].copy()

        baseline = {}

        # प्रत्येक दिवसाचे 09:15 पासून cumulative volume
        for day in trading_days:

            day_df = df[
                df["day"] == day
            ].sort_values(
                "timestamp"
            ).copy()

            day_df["cum_volume"] = (
                day_df["volume"].cumsum()
            )

            for _, row in day_df.iterrows():

                hhmm = row["hhmm"]

                baseline.setdefault(
                    hhmm,
                    []
                )

                baseline[hhmm].append(
                    float(
                        row["cum_volume"]
                    )
                )

        final_baseline = {}

        for hhmm, values in baseline.items():

            if len(values) >= 20:
                final_baseline[hhmm] = (
                    sum(values) /
                    len(values)
                )

        with RVOL_LOCK:
            RVOL_CACHE[symbol] = final_baseline

        print(
            "20D SAME-TIME CUMULATIVE RVOL READY:",
            symbol,
            "days:",
            len(trading_days),
            "slots:",
            len(final_baseline),
            flush=True
        )

        return True

    except Exception as e:
        print(
            "RVOL BASELINE ERROR:",
            symbol,
            e,
            flush=True
        )
        return False


# ============================================================
# CURRENT DAY CANDLES
# ============================================================

def fetch_today_candles(
    symbol,
    token
):
    today = now_ist().date()

    candles = get_candles(
        token,
        today.strftime(
            "%Y-%m-%d"
        ) + " 09:15",
        today.strftime(
            "%Y-%m-%d"
        ) + " 15:30"
    )

    return candles_to_df(candles)


# ============================================================
# INDICATOR FUNCTIONS
# ============================================================

def ema(series, length):
    return series.ewm(
        span=length,
        adjust=False
    ).mean()


def rsi(series, length=14):
    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    result = 100 - (
        100 / (1 + rs)
    )

    return result


def dmi_adx(
    df,
    length=14
):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    up_move = high.diff()

    down_move = -low.diff()

    plus_dm = np.where(
        (up_move > down_move) &
        (up_move > 0),
        up_move,
        0
    )

    minus_dm = np.where(
        (down_move > up_move) &
        (down_move > 0),
        down_move,
        0
    )

    tr1 = high - low
    tr2 = (
        high -
        close.shift(1)
    ).abs()
    tr3 = (
        low -
        close.shift(1)
    ).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    atr = tr.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    plus_dm = pd.Series(
        plus_dm,
        index=df.index
    )

    minus_dm = pd.Series(
        minus_dm,
        index=df.index
    )

    plus_di = (
        100 *
        plus_dm.ewm(
            alpha=1 / length,
            adjust=False
        ).mean() /
        atr.replace(
            0,
            np.nan
        )
    )

    minus_di = (
        100 *
        minus_dm.ewm(
            alpha=1 / length,
            adjust=False
        ).mean() /
        atr.replace(
            0,
            np.nan
        )
    )

    dx = (
        100 *
        (
            plus_di -
            minus_di
        ).abs() /
        (
            plus_di +
            minus_di
        ).replace(
            0,
            np.nan
        )
    )

    adx = dx.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    return (
        plus_di,
        minus_di,
        adx
    )


# ============================================================
# VWAP
# ============================================================

def calculate_vwap(df):
    typical_price = (
        df["high"] +
        df["low"] +
        df["close"]
    ) / 3

    cumulative_volume = (
        df["volume"].cumsum()
    )

    cumulative_pv = (
        typical_price *
        df["volume"]
    ).cumsum()

    return (
        cumulative_pv /
        cumulative_volume.replace(
            0,
            np.nan
        )
    )


# ============================================================
# RVOL CURRENT VALUE
# ============================================================

def calculate_current_rvol(
    symbol,
    df
):
    if df.empty:
        return None

    current = df.iloc[-1]

    hhmm = current[
        "timestamp"
    ].strftime("%H:%M")

    current_cumulative = float(
        df["volume"].sum()
    )

    with RVOL_LOCK:
        baseline = RVOL_CACHE.get(
            symbol,
            {}
        )

    base = baseline.get(
        hhmm
    )

    if base is None or base <= 0:
        return None

    return (
        current_cumulative /
        base
    )


# ============================================================
# PREVIOUS DAY HIGH / LOW
# ============================================================

def fetch_previous_day_levels(
    symbol,
    token
):
    try:
        today = now_ist().date()

        start_date = today - timedelta(
            days=7
        )

        candles = get_candles(
            token,
            start_date.strftime(
                "%Y-%m-%d"
            ) + " 09:15",
            today.strftime(
                "%Y-%m-%d"
            ) + " 09:15"
        )

        if not candles:
            return (
                None,
                None
            )

        df = candles_to_df(candles)

        if df.empty:
            return (
                None,
                None
            )

        df["day"] = (
            df["timestamp"].dt.date
        )

        previous_days = sorted(
            [
                d
                for d in df["day"].unique()
                if d < today
            ]
        )

        if not previous_days:
            return (
                None,
                None
            )

        previous_day = previous_days[-1]

        day_df = df[
            df["day"] == previous_day
        ]

        if day_df.empty:
            return (
                None,
                None
            )

        pdh = float(
            day_df["high"].max()
        )

        pdl = float(
            day_df["low"].min()
        )

        return (
            pdh,
            pdl
        )

    except Exception as e:
        print(
            "PDH PDL ERROR:",
            symbol,
            e,
            flush=True
        )

        return (
            None,
            None
        )


# ============================================================
# CALCULATE STOCK
# ============================================================

def calculate_stock(
    symbol,
    token,
    df
):
    if df.empty:
        return None

    if len(df) < 20:
        return None

    current = df.iloc[-1]

    close = float(
        current["close"]
    )

    open_price = float(
        current["open"]
    )

    high = float(
        current["high"]
    )

    low = float(
        current["low"]
    )

    # --------------------------------------------------------
    # INDICATORS
    # --------------------------------------------------------

    df = df.copy()

    df["ema20"] = ema(
        df["close"],
        20
    )

    df["ema50"] = ema(
        df["close"],
        50
    )

    df["rsi"] = rsi(
        df["close"],
        14
    )

    df["vwap"] = calculate_vwap(
        df
    )

    (
        df["di_plus"],
        df["di_minus"],
        df["adx"]
    ) = dmi_adx(
        df,
        14
    )

    last = df.iloc[-1]

    ema20 = float(
        last["ema20"]
    )

    ema50 = float(
        last["ema50"]
    )

    vwap_value = float(
        last["vwap"]
    )

    rsi_value = float(
        last["rsi"]
    )

    adx_value = float(
        last["adx"]
    )

    di_plus = float(
        last["di_plus"]
    )

    di_minus = float(
        last["di_minus"]
    )

    # --------------------------------------------------------
    # RVOL
    # --------------------------------------------------------

    rvol_value = calculate_current_rvol(
        symbol,
        df
    )

    # --------------------------------------------------------
    # MOVE
    # Day open -> current close
    # --------------------------------------------------------

    day_open = float(
        df.iloc[0]["open"]
    )

    if day_open:
        move = (
            (close - day_open) /
            day_open
        ) * 100
    else:
        move = 0.0

    # --------------------------------------------------------
    # CANDLE BODY
    # --------------------------------------------------------

    if open_price:
        body = (
            abs(close - open_price) /
            open_price
        ) * 100
    else:
        body = 0.0

    # --------------------------------------------------------
    # CHANGE %
    # First candle open -> current close
    # This is today's intraday change.
    # --------------------------------------------------------

    change_pct = move

    # --------------------------------------------------------
    # PDH / PDL
    # --------------------------------------------------------

    with LOCK:
        old = LIVE.get(
            symbol,
            {}
        )

    pdh = old.get(
        "pdh"
    )

    pdl = old.get(
        "pdl"
    )

    # --------------------------------------------------------
    # EARLY CONDITIONS
    # --------------------------------------------------------

    near_pdh = False
    near_pdl = False

    if pdh is not None and pdh > 0:
        near_pdh = (
            abs(
                (close - pdh) /
                pdh
            ) * 100
            <= EARLY_NEAR_PCT
        )

    if pdl is not None and pdl > 0:
        near_pdl = (
            abs(
                (close - pdl) /
                pdl
            ) * 100
            <= EARLY_NEAR_PCT
        )

    early_rvol_ok = (
        rvol_value is not None
        and rvol_value >= EARLY_RVOL_MIN
    )

    # Volume acceleration:
    # latest 5m volume versus average
    # of previous 5m candles.
    if len(df) >= 4:
        latest_volume = float(
            df.iloc[-1]["volume"]
        )

        previous_avg = float(
            df.iloc[-4:-1]["volume"].mean()
        )

        if previous_avg > 0:
            vol_accel = (
                latest_volume /
                previous_avg
            )
        else:
            vol_accel = 0.0

    else:
        vol_accel = 0.0

    early_vol_accel_ok = (
        vol_accel >=
        EARLY_VOL_ACCEL_MIN
    )

    early_buy_count = 0
    early_sell_count = 0

    # 8-condition early structure
    early_buy_conditions = [
        close > ema20,
        ema20 > ema50,
        close > vwap_value,
        rsi_value > 55,
        adx_value > 18,
        di_plus > di_minus,
        early_rvol_ok,
        near_pdl
    ]

    early_sell_conditions = [
        close < ema20,
        ema20 < ema50,
        close < vwap_value,
        rsi_value < 45,
        adx_value > 18,
        di_minus > di_plus,
        early_rvol_ok,
        near_pdh
    ]

    early_buy_count = sum(
        bool(x)
        for x in early_buy_conditions
    )

    early_sell_count = sum(
        bool(x)
        for x in early_sell_conditions
    )

    early_signal = "WAIT"

    if (
        early_buy_count >= EARLY_NEED
        and early_vol_accel_ok
    ):
        early_signal = "BUY"

    if (
        early_sell_count >= EARLY_NEED
        and early_vol_accel_ok
    ):
        early_signal = "SELL"

    # --------------------------------------------------------
    # FINAL CONDITIONS
    # --------------------------------------------------------

    final_buy = (
        close > ema20
        and ema20 > ema50
        and close > vwap_value
        and rsi_value > 55
        and adx_value > 18
        and di_plus > di_minus
        and rvol_value is not None
        and rvol_value >= FINAL_RVOL_MIN
        and move >= FINAL_MOVE_MIN
        and body >= FINAL_BODY_MIN
    )

    final_sell = (
        close < ema20
        and ema20 < ema50
        and close < vwap_value
        and rsi_value < 45
        and adx_value > 18
        and di_minus > di_plus
        and rvol_value is not None
        and rvol_value >= FINAL_RVOL_MIN
        and move <= -FINAL_MOVE_MIN
        and body >= FINAL_BODY_MIN
    )

    # --------------------------------------------------------
    # FINAL PRIORITY OVER EARLY
    # --------------------------------------------------------

    signal = "WAIT"

    if DIRECTION in (
        "BUY",
        "BOTH"
    ):
        if final_buy:
            signal = "BUY"

    if DIRECTION in (
        "SELL",
        "BOTH"
    ):
        if final_sell:
            signal = "SELL"

    # If BOTH is selected, final signal wins.
    if DIRECTION == "BOTH":

        if final_buy:
            signal = "BUY"

        elif final_sell:
            signal = "SELL"

        elif early_signal in (
            "BUY",
            "SELL"
        ):
            signal = early_signal

    # For original SELL direction,
    # use final SELL first, then early SELL.
    if DIRECTION == "SELL":

        if final_sell:
            signal = "SELL"

        elif early_signal == "SELL":
            signal = "SELL"

        else:
            signal = "WAIT"

    # For BUY direction
    if DIRECTION == "BUY":

        if final_buy:
            signal = "BUY"

        elif early_signal == "BUY":
            signal = "BUY"

        else:
            signal = "WAIT"

    return {
        "symbol": symbol,
        "ltp": close,
        "volume": float(
            df["volume"].sum()
        ),
        "change_pct": change_pct,
        "rvol": rvol_value,
        "move": move,
        "body": body,
        "ema20": ema20,
        "ema50": ema50,
        "vwap": vwap_value,
        "rsi": rsi_value,
        "adx": adx_value,
        "di_plus": di_plus,
        "di_minus": di_minus,
        "pdh": pdh,
        "pdl": pdl,
        "vol_accel": vol_accel,
        "early_score": max(
            early_buy_count,
            early_sell_count
        ),
        "early_buy_score": early_buy_count,
        "early_sell_score": early_sell_count,
        "early_signal": early_signal,
        "final_buy": final_buy,
        "final_sell": final_sell,
        "signal": signal
    }


# ============================================================
# PASS TIME LOCK
# ============================================================

def apply_signal_state(
    symbol,
    result
):
    if result is None:
        return

    current_time = now_ist().strftime(
        "%H:%M"
    )

    with LOCK:

        old = LIVE.setdefault(
            symbol,
            {}
        )

        old_signal = old.get(
            "signal",
            "WAIT"
        )

        locked_pass_time = old.get(
            "pass_time"
        )

        # New qualifying signal
        if (
            result["signal"] in (
                "BUY",
                "SELL"
            )
            and locked_pass_time is None
        ):
            old["pass_time"] = (
                current_time
            )

            old["signal_time"] = (
                current_time
            )

        # Keep pass_time locked.
        # It does NOT move every recalculation.

        old.update(result)


# ============================================================
# HISTORY / INDICATOR WORKER
# ============================================================

def update_all_stocks():
    global LAST_HISTORY_UPDATE
    global LAST_ERROR

    if smart_api is None:
        return

    if not FNO_TOKENS:
        return

    reset_daily_state_if_needed()

    if not market_open_now():
        return

    print(
        "STARTING LIVE INDICATOR UPDATE...",
        flush=True
    )

    symbols = list(
        FNO_TOKENS.keys()
    )

    for symbol in symbols:

        token = FNO_TOKENS.get(
            symbol
        )

        if not token:
            continue

        try:
            df = fetch_today_candles(
                symbol,
                token
            )

            if df.empty:
                time.sleep(
                    API_SLEEP
                )
                continue

            # First time PDH / PDL
            with LOCK:
                current_pdh = LIVE.get(
                    symbol,
                    {}
                ).get("pdh")

                current_pdl = LIVE.get(
                    symbol,
                    {}
                ).get("pdl")

            if (
                current_pdh is None
                or current_pdl is None
            ):
                pdh, pdl = (
                    fetch_previous_day_levels(
                        symbol,
                        token
                    )
                )

                with LOCK:
                    LIVE.setdefault(
                        symbol,
                        {}
                    )

                    LIVE[symbol][
                        "pdh"
                    ] = pdh

                    LIVE[symbol][
                        "pdl"
                    ] = pdl

            result = calculate_stock(
                symbol,
                token,
                df
            )

            apply_signal_state(
                symbol,
                result
            )

        except Exception as e:
            print(
                "STOCK UPDATE ERROR:",
                symbol,
                e,
                flush=True
            )

        time.sleep(
            API_SLEEP
        )

    LAST_HISTORY_UPDATE = (
        now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    print(
        "LIVE INDICATOR UPDATE DONE:",
        LAST_HISTORY_UPDATE,
        flush=True
    )


# ============================================================
# RVOL BASELINE WORKER
# ============================================================

def rvol_baseline_worker():
    while True:

        try:

            if (
                smart_api is not None
                and FNO_TOKENS
            ):

                # Only rebuild if current day
                # baseline is not already loaded.

                missing = []

                with RVOL_LOCK:
                    for symbol in FNO_TOKENS:
                        if symbol not in RVOL_CACHE:
                            missing.append(symbol)

                if missing:

                    print(
                        "RVOL BASELINE BUILD:",
                        len(missing),
                        "stocks",
                        flush=True
                    )

                    for symbol in missing:

                        token = FNO_TOKENS.get(
                            symbol
                        )

                        if not token:
                            continue

                        fetch_20d_rvol_baseline(
                            symbol,
                            token
                        )

                        time.sleep(
                            API_SLEEP
                        )

            time.sleep(10)

        except Exception as e:

            print(
                "RVOL WORKER ERROR:",
                e,
                flush=True
            )

            time.sleep(10)


# ============================================================
# INDICATOR WORKER
# ============================================================

def indicator_worker():

    last_update = 0

    while True:

        try:

            reset_daily_state_if_needed()

            if (
                smart_api is not None
                and FNO_TOKENS
                and market_open_now()
            ):

                current = time.time()

                # Refresh every 60 seconds.
                if (
                    current - last_update
                    >= 60
                ):

                    update_all_stocks()

                    last_update = current

            time.sleep(5)

        except Exception as e:

            print(
                "INDICATOR WORKER ERROR:",
                e,
                flush=True
            )

            time.sleep(10)


# ============================================================
# WEBSOCKET LIVE DATA
# ============================================================

def on_live_data(
    wsapp,
    message
):
    global LAST_ERROR

    try:

        token = str(
            message.get(
                "token",
                ""
            )
        )

        symbol = TOKEN_TO_SYMBOL.get(
            token
        )

        if not symbol:
            return

        ltp_raw = message.get(
            "last_traded_price"
        )

        volume_raw = message.get(
            "volume_trade_for_the_day"
        )

        if ltp_raw is None:
            return

        ltp = (
            float(ltp_raw) /
            100.0
        )

        try:
            volume = int(
                volume_raw or 0
            )
        except Exception:
            volume = 0

        with LOCK:

            LIVE.setdefault(
                symbol,
                {}
            )

            LIVE[symbol][
                "symbol"
            ] = symbol

            LIVE[symbol][
                "ltp"
            ] = ltp

            LIVE[symbol][
                "volume"
            ] = volume

            # If historical indicator
            # hasn't refreshed yet, still
            # provide live price.

            if LIVE[symbol].get(
                "day_open"
            ):
                day_open = LIVE[
                    symbol
                ]["day_open"]

                if day_open:
                    LIVE[symbol][
                        "change_pct"
                    ] = (
                        (ltp - day_open) /
                        day_open
                    ) * 100

        LAST_ERROR = ""

    except Exception as e:

        print(
            "LIVE DATA ERROR:",
            e,
            flush=True
        )


def on_ws_open(
    wsapp
):
    print(
        "ANGEL ONE WEBSOCKET CONNECTED",
        flush=True
    )

    tokens = list(
        FNO_TOKENS.values()
    )

    if not tokens:
        print(
            "NO F&O TOKENS FOR WEBSOCKET",
            flush=True
        )
        return

    token_list = [
        {
            "exchangeType": 2,
            "tokens": [
                str(token)
                for token in tokens
            ]
        }
    ]

    try:

        websocket.subscribe(
            "fo-sector-leader",
            2,
            token_list
        )

        print(
            "F&O LIVE SUBSCRIPTION STARTED:",
            len(tokens),
            flush=True
        )

    except Exception as e:

        print(
            "WEBSOCKET SUBSCRIBE ERROR:",
            e,
            flush=True
        )


def on_ws_error(
    wsapp,
    error
):
    print(
        "ANGEL WEBSOCKET ERROR:",
        error,
        flush=True
    )


def on_ws_close(
    wsapp
):
    print(
        "ANGEL WEBSOCKET CLOSED",
        flush=True
    )


# ============================================================
# WEBSOCKET LOOP / RECONNECT
# ============================================================

def websocket_loop():

    global websocket

    while True:

        try:

            if smart_api is None:
                time.sleep(10)
                continue

            if not FNO_TOKENS:
                load_fno_tokens()
                time.sleep(10)
                continue

            auth_token = (
                smart_api.access_token
            )

            feed_token = (
                smart_api.getfeedToken()
            )

            websocket = SmartWebSocketV2(
                auth_token,
                API_KEY,
                CLIENT_CODE,
                feed_token
            )

            websocket.on_open = (
                on_ws_open
            )

            websocket.on_data = (
                on_live_data
            )

            websocket.on_error = (
                on_ws_error
            )

            websocket.on_close = (
                on_ws_close
            )

            print(
                "STARTING ANGEL ONE WEBSOCKET...",
                flush=True
            )

            websocket.connect()

        except Exception as e:

            print(
                "WEBSOCKET LOOP ERROR:",
                e,
                flush=True
            )

        websocket = None

        time.sleep(10)


# ============================================================
# LOGIN / STARTUP WORKER
# ============================================================

def backend_worker():

    global smart_api

    while True:

        try:

            if smart_api is None:

                if angel_login():

                    load_fno_tokens()

                    print(
                        "BACKEND READY:",
                        len(FNO_TOKENS),
                        "F&O STOCKS",
                        flush=True
                    )

            time.sleep(30)

        except Exception as e:

            print(
                "BACKEND WORKER ERROR:",
                e,
                flush=True
            )

            time.sleep(30)


# ============================================================
# API: HOME
# ============================================================

@app.get("/")
def home():

    return jsonify({
        "app": "F&O SECTOR LEADER",
        "status": "running",
        "market": market_status(),
        "time": now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "angel_logged_in": (
            smart_api is not None
        ),
        "websocket": (
            websocket is not None
        ),
        "fno_tokens": len(
            FNO_TOKENS
        ),
        "rvol_ready": len(
            RVOL_CACHE
        ),
        "last_login": LAST_LOGIN,
        "last_history_update": (
            LAST_HISTORY_UPDATE
        ),
        "error": LAST_ERROR
    })


# ============================================================
# API: SECTORS
# ============================================================

@app.get("/api/sectors")
def sectors():

    result = []

    with LOCK:

        for sector_name, stocks in SECTORS.items():

            signals = []

            for symbol in stocks:

                item = LIVE.get(
                    symbol,
                    {}
                )

                signal = item.get(
                    "signal",
                    "WAIT"
                )

                if signal in (
                    "BUY",
                    "SELL"
                ):
                    signals.append(
                        signal
                    )

            buy_count = signals.count(
                "BUY"
            )

            sell_count = signals.count(
                "SELL"
            )

            active = (
                buy_count +
                sell_count
            )

            if active == 0:
                sector_signal = "WAIT"

            elif buy_count > sell_count:
                sector_signal = "BUY"

            elif sell_count > buy_count:
                sector_signal = "SELL"

            else:
                sector_signal = "MIXED"

            moves = []

            for symbol in stocks:

                move = LIVE.get(
                    symbol,
                    {}
                ).get(
                    "move"
                )

                if move is not None:
                    moves.append(
                        float(move)
                    )

            sector_move = (
                sum(moves) /
                len(moves)
                if moves
                else 0.0
            )

            result.append({
                "sector": sector_name,
                "move": round(
                    sector_move,
                    2
                ),
                "buy_count": buy_count,
                "sell_count": sell_count,
                "active": active,
                "signal": sector_signal
            })

    # strongest sector movement first
    result.sort(
        key=lambda x: x["move"],
        reverse=True
    )

    return jsonify(result)


# ============================================================
# API: SECTOR
# ============================================================

@app.get("/api/sector/<sector>")
def sector(sector):

    if sector not in SECTORS:

        return jsonify({
            "error": "Unknown sector"
        }), 404

    reset_daily_state_if_needed()

    stocks = []

    with LOCK:

        for symbol in SECTORS[
            sector
        ]:

            item = LIVE.get(
                symbol,
                {}
            )

            stocks.append({
                "symbol": symbol,

                "ltp": item.get(
                    "ltp"
                ),

                "change_pct": item.get(
                    "change_pct"
                ),

                "volume": item.get(
                    "volume"
                ),

                "rvol": item.get(
                    "rvol"
                ),

                "move": item.get(
                    "move"
                ),

                "body": item.get(
                    "body"
                ),

                "ema20": item.get(
                    "ema20"
                ),

                "ema50": item.get(
                    "ema50"
                ),

                "vwap": item.get(
                    "vwap"
                ),

                "rsi": item.get(
                    "rsi"
                ),

                "adx": item.get(
                    "adx"
                ),

                "di_plus": item.get(
                    "di_plus"
                ),

                "di_minus": item.get(
                    "di_minus"
                ),

                "pdh": item.get(
                    "pdh"
                ),

                "pdl": item.get(
                    "pdl"
                ),

                "vol_accel": item.get(
                    "vol_accel"
                ),

                "early_score": item.get(
                    "early_score"
                ),

                "early_buy_score": item.get(
                    "early_buy_score"
                ),

                "early_sell_score": item.get(
                    "early_sell_score"
                ),

                "early_signal": item.get(
                    "early_signal",
                    "WAIT"
                ),

                "signal": item.get(
                    "signal",
                    "WAIT"
                ),

                "pass_time": item.get(
                    "pass_time"
                ),

                "signal_time": item.get(
                    "signal_time"
                )
            })

    # Sector movement
    moves = [
        x["move"]
        for x in stocks
        if x["move"] is not None
    ]

    sector_move = (
        sum(moves) /
        len(moves)
        if moves
        else 0.0
    )

    active_stocks = [
        x
        for x in stocks
        if x["signal"] in (
            "BUY",
            "SELL"
        )
    ]

    if active_stocks:

        # Highest absolute move as leader
        leader = max(
            active_stocks,
            key=lambda x:
                abs(
                    x["move"] or 0
                )
        )

    else:

        leader = None

    return jsonify({
        "sector": sector,
        "market": market_status(),
        "direction": DIRECTION,

        "sector_move": round(
            sector_move,
            2
        ),

        "leader": (
            leader["symbol"]
            if leader
            else None
        ),

        "leader_signal": (
            leader["signal"]
            if leader
            else "WAIT"
        ),

        "stocks": stocks
    })


# ============================================================
# API: ALL LIVE
# ============================================================

@app.get("/api/live")
def live():

    with LOCK:
        return jsonify(
            LIVE
        )


# ============================================================
# API: STATUS
# ============================================================

@app.get("/api/status")
def status():

    with LOCK:

        live_count = sum(
            1
            for item in LIVE.values()
            if item.get("ltp") is not None
        )

    return jsonify({
        "market": market_status(),
        "time": now_ist().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "angel_login": (
            smart_api is not None
        ),
        "websocket": (
            websocket is not None
        ),
        "tokens": len(
            FNO_TOKENS
        ),
        "live_stocks": live_count,
        "rvol_stocks": len(
            RVOL_CACHE
        ),
        "last_login": LAST_LOGIN,
        "last_history_update": (
            LAST_HISTORY_UPDATE
        ),
        "error": LAST_ERROR
    })


# ============================================================
# START BACKGROUND THREADS
# ============================================================

threading.Thread(
    target=backend_worker,
    daemon=True
).start()

threading.Thread(
    target=websocket_loop,
    daemon=True
).start()

threading.Thread(
    target=rvol_baseline_worker,
    daemon=True
).start()

threading.Thread(
    target=indicator_worker,
    daemon=True
).start()


# ============================================================
# RENDER
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    print(
        "F&O SECTOR LEADER BACKEND STARTING...",
        flush=True
    )

    print(
        "PORT:",
        port,
        flush=True
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
