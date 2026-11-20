"""
SmartAPI connection test  (step 1 of the NIFTY option charts project)

Checks, in order:
  1. Login with client code + MPIN + TOTP
  2. Download of Angel One's instrument master (symbol -> token list)
  3. NIFTY 50 daily candles (previous close)
  4. Nearest NIFTY weekly expiry and the CE/PE tokens around the closest strike
  5. 5-minute candles for one option contract (the data the charts will use)

Runs in GitHub Actions (workflow: smartapi_test.yml). Secrets used:
  ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET
Nothing secret is printed.
"""

import datetime as dt
import os
import sys
import time

import pyotp
import requests
from SmartApi import SmartConnect

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SCRIP_MASTER = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY_INDEX_TOKEN = "99926000"   # "Nifty 50" on NSE
STRIKE_STEP = 50
STRIKES_AFTER = 3                # closest strike + next 3


def fail(step, msg):
    print(f"FAILED at step {step}: {msg}")
    sys.exit(1)


def env(name):
    v = (os.environ.get(name) or "").strip()
    if not v:
        fail(0, f"GitHub secret {name} is missing or empty")
    return v


def candles(api, exchange, token, interval, start, end):
    res = api.getCandleData({
        "exchange": exchange,
        "symboltoken": token,
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    })
    if not res or not res.get("status"):
        msg = (res or {}).get("message", "no response")
        code = (res or {}).get("errorcode", "")
        raise RuntimeError(f"{msg} {code}".strip())
    return res.get("data") or []


def main():
    api_key = env("ANGEL_API_KEY")
    client = env("ANGEL_CLIENT_CODE")
    mpin = env("ANGEL_MPIN")
    totp_secret = env("ANGEL_TOTP_SECRET").replace(" ", "")

    # 1. Login
    try:
        totp = pyotp.TOTP(totp_secret).now()
    except Exception:
        fail(1, "ANGEL_TOTP_SECRET is not a valid TOTP secret (copy the text code, not the QR)")
    api = SmartConnect(api_key=api_key)
    res = api.generateSession(client, mpin, totp)
    if not res or not res.get("status"):
        fail(1, f"login rejected: {(res or {}).get('message')} {(res or {}).get('errorcode', '')}")
    print("1. Login OK")

    # 2. Instrument master
    try:
        r = requests.get(SCRIP_MASTER, timeout=90)
        r.raise_for_status()
        scrips = r.json()
    except Exception as e:
        fail(2, f"could not download instrument master: {e}")
    print(f"2. Instrument master OK ({len(scrips):,} instruments)")

    # 3. NIFTY 50 previous close
    now = dt.datetime.now(IST).replace(tzinfo=None)
    try:
        days = candles(api, "NSE", NIFTY_INDEX_TOKEN, "ONE_DAY", now - dt.timedelta(days=10), now)
    except RuntimeError as e:
        fail(3, f"index candles: {e}")
    if not days:
        fail(3, "no NIFTY daily candles returned")
    today = now.date().isoformat()
    done = [c for c in days if not str(c[0]).startswith(today)]   # completed days only
    prev = done[-1] if done else days[-1]
    prev_close = float(prev[4])
    print(f"3. NIFTY 50 previous close: {prev_close:.2f} on {str(prev[0])[:10]}")

    # 4. Nearest expiry and strike tokens
    opts = [s for s in scrips
            if s.get("name") == "NIFTY" and s.get("exch_seg") == "NFO" and s.get("instrumenttype") == "OPTIDX"]
    if not opts:
        fail(4, "no NIFTY options found in the instrument master")

    def exp_date(s):
        return dt.datetime.strptime(s["expiry"], "%d%b%Y").date()

    expiries = sorted({exp_date(s) for s in opts if exp_date(s) >= now.date()})
    if not expiries:
        fail(4, "no live NIFTY expiries found")
    expiry = expiries[0]
    atm = int(round(prev_close / STRIKE_STEP) * STRIKE_STEP)
    strikes = [atm + i * STRIKE_STEP for i in range(STRIKES_AFTER + 1)]
    table = {}
    for s in opts:
        if exp_date(s) != expiry:
            continue
        k = int(round(float(s["strike"]) / 100))
        side = s["symbol"][-2:]
        if k in strikes and side in ("CE", "PE"):
            table[(k, side)] = (s["token"], s["symbol"])
    print(f"4. Nearest expiry: {expiry:%d-%b-%Y}, closest strike: {atm}")
    for k in strikes:
        for side in ("CE", "PE"):
            tok = table.get((k, side))
            print(f"     {k} {side}: {tok[1] + ' (token ' + tok[0] + ')' if tok else 'NOT FOUND'}")
    if (atm, "CE") not in table:
        fail(4, f"no token for {atm} CE")

    # 5. 5-minute candles for the closest Call
    time.sleep(1)                                  # stay under the candle API rate limit
    token, symbol = table[(atm, "CE")]
    try:
        bars = candles(api, "NFO", token, "FIVE_MINUTE", now - dt.timedelta(days=5), now)
    except RuntimeError as e:
        fail(5, f"option candles: {e}")
    if not bars:
        fail(5, f"no 5-minute candles returned for {symbol}")
    last_day = str(bars[-1][0])[:10]
    day_bars = [b for b in bars if str(b[0]).startswith(last_day)]
    hi = max(float(b[2]) for b in day_bars)
    lo = min(float(b[3]) for b in day_bars)
    print(f"5. {symbol}: {len(day_bars)} five-minute candles on {last_day} "
          f"(high {hi}, low {lo}, last close {bars[-1][4]})")

    try:
        api.terminateSession(client)
    except Exception:
        pass
    print("\nAll checks passed - SmartAPI is ready for the chart script.")


if __name__ == "__main__":
    main()
