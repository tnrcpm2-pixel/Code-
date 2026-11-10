#!/usr/bin/env python3
"""
coins_bot.py — fully standalone Buy Coins bot.
Includes the drag-race injector + Unlock Slots with AUTO-INJECT fallback
when the account runs out of coins.

Requires:
  pip install python-telegram-bot aiohttp pycryptodome
  proxies-Spyder.txt in the same folder

Run:
  python coins_bot.py
"""

import os
import sys
import json
import base64
import random
import time
import asyncio
import logging
from typing import Dict, List, Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes,
)
from telegram import error as telegram_error
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


# ─────────────────────────────────────────────────────────
#  CONFIG  (edit BOT_TOKEN + ALLOWED_USERS)
# ─────────────────────────────────────────────────────────
BOT_TOKEN      = "8986993430:AAHvIgh8ijgRg-Zcsg4s6w1mptcKBEaoM1Y"
ALLOWED_USERS  = [7281768879]            # [] = everyone; [123456789] = only you

PROXY_FILE     = "proxies-Spyder.txt"
PROXY_TEST_CONCURRENT = 25      # phone-friendly; 100 on servers

API_KEY        = "AIzaSyCQDz9rgjgmvmFkvVfmvr2-7fT4tfrzRRQ"
LOGIN_URL      = f"https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={API_KEY}"
REFRESH_URL    = f"https://securetoken.googleapis.com/v1/token?key={API_KEY}"

VERSION        = "1.3.3.6"
UNITY_VER      = "2022.3.62f2"
USER_AGENT     = "UnityPlayer/2022.3.62f2 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)"
CLIENT_HASH    = "F05A72840B40DC4FAADF539C5E38062527AE6422"

CF_BASE = "https://europe-west1-cpm-2-7cea1.cloudfunctions.net"
OG_BASE = "https://cpm-2.ogames.kz/api"

CONFIG_URL         = "https://olzhas1.b-cdn.net/configs/cpm2/prod/android/1_3_2_2.json"
SESSION_START_URL  = f"{OG_BASE}/check-service/v1/session/start"
SESSION_EXTEND_URL = f"{OG_BASE}/check-service/v1/session/extend"
RATING_UPDATE_URL  = f"{OG_BASE}/progress-service/v1/rating/update"

MASTER_STARTUP_URL = f"{CF_BASE}/MasterMainStartup24_1"
GET_COINS_URL      = f"{CF_BASE}/GetCoins24_1"
GET_REWARDS_URL    = f"{CF_BASE}/GetRewards24_1"
GET_OFFERS_URL     = f"{CF_BASE}/GetOffersForPlayer24_1"
GET_EVENTS_URL     = f"{CF_BASE}/GetAllCurrentEvents24_1"
DRAG_URL           = f"{CF_BASE}/SetDragRacing23_1"
SLOT_PURCHASE_URL  = f"{CF_BASE}/PurchaseInAppItem24_1"

DRAG_WAIT_SEC = 120            # between drags (matches original)
DRAG_COUNT    = 36             # max drags per injection

# ── Unlock slots tuning ──
MAX_SLOTS               = 22     # CPM2 garage capacity
SLOT_LOW_BALANCE        = 5_000  # below this we consider "insufficient funds"
SLOT_START_BALANCE      = 30_000 # below this we inject once at the start
SLOT_MAX_EXTRA_INJECTS  = 2      # max times we re-inject mid-session

TIERS = [
    ("⚡ 1.6K · 12m",  6),
    ("🎯 2.8K · 20m", 10),
    ("💎 4.2K · 30m", 15),
    ("💠 5.6K · 40m", 20),
    ("👑 8.4K · 60m", 30),
    ("🏆 10K+ · 72m", 36),
]
EST_COINS_PER_DRAG = 280

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("coins_bot")


# ─────────────────────────────────────────────────────────
#  PROXY POOL  (with health check)
# ─────────────────────────────────────────────────────────
IP_CHECK_URLS = [
    "https://www.googleapis.com/generate_204",
    "https://checkip.amazonaws.com",
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://ipinfo.io/ip",
]


class ProxyPool:
    def __init__(self, path: str):
        self.all_proxies: List[str] = []
        self.proxies: List[str] = []
        self.index = 0
        self._ip_idx = 0
        self._lock = asyncio.Lock()
        self._load(path)

    def _parse(self, line: str) -> Optional[str]:
        s = line.strip()
        if not s or s.startswith("#"):
            return None
        if "@" in s:
            try:
                creds, hostport = s.rsplit("@", 1)
                u, pw = creds.split(":", 1)
                h, pt = hostport.split(":", 1)
                return f"http://{u}:{pw}@{h}:{pt}"
            except Exception:
                return None
        p = s.split(":")
        if len(p) >= 4:
            return f"http://{p[2]}:{p[3]}@{p[0]}:{p[1]}"
        if len(p) == 2:
            return f"http://{p[0]}:{p[1]}"
        return None

    def _load(self, path: str):
        try:
            with open(path) as f:
                for line in f:
                    url = self._parse(line)
                    if url:
                        self.all_proxies.append(url)
            self.proxies = list(self.all_proxies)
            logger.info(f"[PROXY] Loaded {len(self.all_proxies)} from {path}")
        except Exception as e:
            logger.warning(f"[PROXY] File not loaded: {e}")

    async def _test_one(self, session, proxy_url):
        url = IP_CHECK_URLS[self._ip_idx % len(IP_CHECK_URLS)]
        self._ip_idx += 1
        try:
            async with session.get(
                url, proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=5),
                ssl=False,
            ) as r:
                if r.status == 200:
                    txt = (await r.text()).strip()
                    return bool(txt) and len(txt) < 64
        except Exception:
            return False
        return False

    async def health_check(self):
        if not self.all_proxies:
            return 0
        total = len(self.all_proxies)
        logger.info(f"[PROXY] Checking {total} proxies ({PROXY_TEST_CONCURRENT} at a time)...")
        t0 = time.time()
        live: List[str] = []
        sem = asyncio.Semaphore(PROXY_TEST_CONCURRENT)
        connector = aiohttp.TCPConnector(
            ssl=False, force_close=True, limit=PROXY_TEST_CONCURRENT,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            async def _probe(px):
                async with sem:
                    if await self._test_one(session, px):
                        live.append(px)
            await asyncio.gather(*[_probe(px) for px in self.all_proxies])
        if live:
            self.proxies = live
            self.index = 0
        logger.info(f"[PROXY] Done: {len(live)}/{total} live ({time.time()-t0:.1f}s)")
        return len(live)

    async def next(self) -> Optional[str]:
        if not self.proxies:
            return None
        async with self._lock:
            url = self.proxies[self.index % len(self.proxies)]
            self.index += 1
        return url


pool = ProxyPool(PROXY_FILE)


# ─────────────────────────────────────────────────────────
#  CRYPTO / UTILS
# ─────────────────────────────────────────────────────────
def _ki(uid: str):
    first8 = uid[:8] if len(uid) >= 8 else uid.ljust(8, "0")
    return (first8 + "12345678").encode()[:16], (first8 + "01234567").encode()[:16]


def encrypt_data(data, user_id: str) -> str:
    key, iv = _ki(user_id)
    s = json.dumps(data, separators=(",", ":")) if isinstance(data, (dict, list)) else str(data)
    return base64.b64encode(AES.new(key, AES.MODE_CBC, iv).encrypt(pad(s.encode(), 16))).decode()


def decrypt_data(b64: str, user_id: str) -> str:
    try:
        key, iv = _ki(user_id)
        raw = AES.new(key, AES.MODE_CBC, iv).decrypt(base64.b64decode(b64))
        try:
            return unpad(raw, 16).decode()
        except Exception:
            p = raw[-1]
            return raw[:-p].decode() if p < 16 else raw.decode()
    except Exception:
        return ""


def encode_drag(uid: str, c: int, cup: int, m: int) -> str:
    key, iv = _ki(uid)
    plain = f"{c},{cup},{m}".encode()
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain, 16))
    return base64.b64encode(enc).decode()


def gen_device_id() -> str:
    return "".join(random.choices("0123456789abcdef", k=32))


def _game_session(proxy_url: Optional[str], timeout: int = 90):
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(ssl=False, force_close=True, limit=0),
        proxy=proxy_url,
        timeout=aiohttp.ClientTimeout(total=timeout, connect=20, sock_read=timeout - 10),
    )


# ─────────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────────
async def login(email: str, password: str, proxy: Optional[str] = None, max_attempts: int = 4) -> Dict:
    last_err = "no response"
    for attempt in range(max_attempts):
        try:
            async with _game_session(proxy, timeout=25) as s:
                async with s.post(
                    LOGIN_URL,
                    headers={"Content-Type": "application/json", "X-Unity-Version": UNITY_VER},
                    json={
                        "email": email, "password": password,
                        "returnSecureToken": True,
                        "clientType": "CLIENT_TYPE_ANDROID",
                    },
                ) as r:
                    txt = await r.text()
                    if r.status == 200:
                        j = json.loads(txt)
                        if "idToken" in j:
                            return j
                        last_err = j.get("error", "login failed")
                        if isinstance(last_err, dict):
                            last_err = last_err.get("message", str(last_err))
                    else:
                        last_err = f"HTTP {r.status}"
            upper = str(last_err).upper()
            if "TOO_MANY" in upper or "BLOCK" in upper or "QUOTA" in upper or "429" in upper:
                proxy = await pool.next()
                await asyncio.sleep(3 * (attempt + 1))
                continue
            if any(k in upper for k in ("INVALID_PASSWORD", "EMAIL_NOT_FOUND", "INVALID_LOGIN")):
                return {"error": last_err}
            await asyncio.sleep(1)
        except Exception as e:
            last_err = str(e)[:80]
            await asyncio.sleep(2 * (attempt + 1))
    return {"error": last_err}


async def refresh_id_token(refresh_token: str, proxy: Optional[str] = None):
    if not refresh_token:
        return None
    try:
        async with _game_session(proxy, timeout=20) as s:
            async with s.post(
                REFRESH_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded", "X-Unity-Version": UNITY_VER},
                data=f"grant_type=refresh_token&refresh_token={refresh_token}",
            ) as r:
                if r.status != 200:
                    return None
                j = await r.json(content_type=None)
                idt = j.get("id_token")
                rft = j.get("refresh_token")
                if idt and rft:
                    return idt, rft
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────
#  WARMUP
# ─────────────────────────────────────────────────────────
async def get_config(proxy: Optional[str] = None):
    try:
        async with _game_session(proxy, timeout=15) as s:
            async with s.get(CONFIG_URL, headers={"X-Unity-Version": UNITY_VER}) as r:
                await r.read()
    except Exception:
        pass


async def session_start(id_token: str, device_id: str, proxy: Optional[str] = None):
    h = {
        "X-Firebase-Token": f"Bearer {id_token}",
        "X-Client-Platform": "ANDROID",
        "X-Client-Version": VERSION,
        "X-Client-DeviceId": device_id,
        "X-Unity-Version": UNITY_VER,
    }
    try:
        async with _game_session(proxy, timeout=20) as s:
            async with s.post(SESSION_START_URL, headers=h, json={}) as r:
                await r.read()
    except Exception:
        pass


async def session_extend(id_token: str, device_id: str, proxy: Optional[str] = None):
    h = {
        "X-Firebase-Token": f"Bearer {id_token}",
        "X-Client-Platform": "ANDROID",
        "X-Client-Version": VERSION,
        "X-Client-DeviceId": device_id,
        "X-Unity-Version": UNITY_VER,
    }
    try:
        async with _game_session(proxy, timeout=20) as s:
            async with s.post(SESSION_EXTEND_URL, headers=h, json={}) as r:
                await r.read()
    except Exception:
        pass


async def master_startup(id_token: str, proxy: Optional[str] = None):
    h = {
        "Authorization": f"Bearer {id_token}",
        "Content-Type": "application/json",
        "X-Unity-Version": UNITY_VER,
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    }
    try:
        async with _game_session(proxy, timeout=30) as s:
            async with s.post(MASTER_STARTUP_URL, headers=h, json={"data": "1"}) as r:
                await r.read()
    except Exception:
        pass


async def send_rating_update(id_token: str, uid: str, proxy: Optional[str] = None):
    rating = {
        "cars": 19, "run": 99999, "time": 86400, "car_fix": 99999,
        "car_collided": 99999, "car_exchange": 99999, "car_trade": 99999,
        "car_wash": 99999, "slicer_cut": 99999, "drift_max": 99999,
        "drift": 99999, "race_win": 3627, "taxi": 99999, "levels": 79,
        "gifts": 99999, "fuel": 99999, "offroad": 99999,
        "speed_banner": 99999, "reactions": 99999, "police": 99999,
        "real_estate": 99999, "t_distance": 96350, "treasure": 99999,
        "block_post": 99999, "push_ups": 99999, "burnt_tire": 99999,
        "passanger_distance": 99999, "cargo": 0, "delivery": 0,
        "battlepass": {"daily": {"finished": [0, 1, 2]}},
    }
    enc = encrypt_data(rating, uid)
    h = {
        "X-Firebase-Token": id_token,
        "Content-Type": "application/json",
        "X-Unity-Version": UNITY_VER,
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
        "X-Client-Hash": CLIENT_HASH,
        "X-Client-Platform": "ANDROID",
        "X-Client-Version": VERSION,
        "X-Client-DeviceId": gen_device_id(),
    }
    try:
        async with _game_session(proxy, timeout=30) as s:
            async with s.post(RATING_UPDATE_URL, headers=h, json={"data": enc}) as r:
                await r.read()
                return r.status == 200
    except Exception:
        return False


async def parallel_warmup(id_token: str, proxy: Optional[str] = None):
    h = {
        "Authorization": f"Bearer {id_token}",
        "X-Unity-Version": UNITY_VER,
        "User-Agent": USER_AGENT,
    }

    async def _call(url, payload):
        try:
            async with _game_session(proxy, timeout=15) as s:
                async with s.post(url, headers=h, json={"data": payload}) as r:
                    await r.read()
        except Exception:
            pass

    await asyncio.gather(
        _call(GET_REWARDS_URL, ""),
        _call(GET_OFFERS_URL, '{"version":"1.3.3.6","platform":"android"}'),
        _call(GET_EVENTS_URL, '{"Dev":false,"Version":"1.3.3.6","Platform":"Android"}'),
        return_exceptions=True,
    )


# ─────────────────────────────────────────────────────────
#  COINS + DRAG
# ─────────────────────────────────────────────────────────
async def get_coins(id_token: str, uid: str, proxy: Optional[str] = None) -> Optional[int]:
    h = {
        "Authorization": f"Bearer {id_token}",
        "Content-Type": "application/json; charset=utf-8",
        "X-Unity-Version": UNITY_VER,
        "User-Agent": USER_AGENT,
    }
    try:
        async with _game_session(proxy, timeout=20) as s:
            async with s.post(GET_COINS_URL, headers=h, json={"data": None}) as r:
                if r.status != 200:
                    return None
                result = await r.json(content_type=None)
                if not isinstance(result, dict):
                    return None
                res = result.get("result", result)
                if isinstance(res, dict) and "data" in res:
                    try:
                        return int(decrypt_data(res["data"], uid))
                    except Exception:
                        return None
                if isinstance(res, str):
                    try:
                        arr = json.loads(res)
                        if isinstance(arr, list) and len(arr) >= 2:
                            return int(decrypt_data(arr[1], uid))
                    except Exception:
                        return None
                if isinstance(res, int):
                    return res
    except Exception:
        pass
    return None


async def fire_drag(session, id_token: str, uid: str, c: int, cup: int, m: int):
    h = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {id_token}",
        "X-Unity-Version": UNITY_VER,
    }
    body = {"data": encode_drag(uid, c, cup, m)}
    t0 = time.time()
    try:
        async with session.post(DRAG_URL, headers=h, json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
            text = await r.text()
    except Exception as e:
        return None, time.time() - t0, str(e)[:80]
    code = None
    try:
        j = json.loads(text)
        res = j.get("result", j)
        if isinstance(res, dict):
            code = res.get("code")
        elif isinstance(res, int):
            code = res
    except Exception:
        pass
    return code, time.time() - t0, text[:100]


# ─────────────────────────────────────────────────────────
#  PRECHECK
# ─────────────────────────────────────────────────────────
async def precheck(email: str, password: str) -> str:
    proxy = await pool.next()
    lg = await login(email, password, proxy=proxy)
    if not lg or "idToken" not in lg:
        return "error"
    id_token = lg["idToken"]
    uid = lg["localId"]
    device_id = gen_device_id()
    try:
        await get_config(proxy=proxy)
        await session_start(id_token, device_id, proxy=proxy)
        await asyncio.sleep(1)
        await master_startup(id_token, proxy=proxy)
        await asyncio.sleep(2)
        await session_extend(id_token, device_id, proxy=proxy)
        async with _game_session(proxy) as s:
            code, _, _ = await fire_drag(s, id_token, uid, 0, 1, 0)
        if code == 1:
            return "fresh"
        if code == 232:
            return "done"
        return "unknown"
    except Exception:
        return "error"


# ─────────────────────────────────────────────────────────
#  CORE INJECTION
# ─────────────────────────────────────────────────────────
async def inject_one(
    email: str,
    password: str,
    drag_count: int = DRAG_COUNT,
    progress_cb=None,
) -> Dict:
    """Full injection flow. Returns dict matching the API response shape."""
    logger.info(f"[inject] start {email}")
    proxy = await pool.next()

    try:
        lg = await login(email, password, proxy=proxy)
        if not lg or "idToken" not in lg:
            err = (lg.get("error", "login failed") if lg else "no response")
            if isinstance(err, dict):
                err = err.get("message", str(err))
            return {"success": False, "error": f"login: {err}"}

        id_token = lg["idToken"]
        refresh_token = lg.get("refreshToken")
        local_id = lg["localId"]
        device_id = gen_device_id()

        await get_config(proxy=proxy)
        await session_start(id_token, device_id, proxy=proxy)
        await asyncio.sleep(2)
        await master_startup(id_token, proxy=proxy)
        await asyncio.sleep(6)
        await session_extend(id_token, device_id, proxy=proxy)
        await send_rating_update(id_token, local_id, proxy=proxy)
        await asyncio.sleep(3)
        await parallel_warmup(id_token, proxy=proxy)
        await asyncio.sleep(3)

        coins_before = await get_coins(id_token, local_id, proxy=proxy)
        logger.info(f"[{email[:20]}] coins before: {coins_before}")

        async with _game_session(proxy) as s:
            combos = [(c, cup, m) for cup in (1, 2, 3) for c in range(6) for m in range(2)]
            random.shuffle(combos)

            hit = already = fail = 0
            for i, (c, cup, m) in enumerate(combos[:drag_count], 1):
                code, _, raw = await fire_drag(s, id_token, local_id, c, cup, m)

                if code is None and refresh_token:
                    refreshed = await refresh_id_token(refresh_token, proxy=proxy)
                    if refreshed:
                        id_token, refresh_token = refreshed
                        code, _, raw = await fire_drag(s, id_token, local_id, c, cup, m)

                if i % 20 == 0 and refresh_token and random.random() < 0.7:
                    refreshed = await refresh_id_token(refresh_token, proxy=proxy)
                    if refreshed:
                        id_token, refresh_token = refreshed

                if code == 1:
                    hit += 1
                elif code == 232:
                    already += 1
                else:
                    fail += 1

                if progress_cb:
                    try:
                        await progress_cb(i, drag_count, hit, already, fail)
                    except Exception:
                        pass

                if i < drag_count:
                    await asyncio.sleep(DRAG_WAIT_SEC)

        await asyncio.sleep(2)
        coins_after = await get_coins(id_token, local_id, proxy=proxy)
        if coins_before is None or coins_after is None:
            cb = coins_before or 0
            ca = coins_after or 0
            gained = 0
        else:
            cb = coins_before
            ca = coins_after
            gained = ca - cb

        logger.info(
            f"[{email[:20]}] hit={hit} already={already} fail={fail} "
            f"coins {cb:,} -> {ca:,} ({gained:+,})"
        )

        return {
            "success": True,
            "coins_before": cb,
            "coins_after": ca,
            "gained": gained,
            "error": "",
            "hit": hit,
            "already": already,
            "fail": fail,
        }

    except Exception as e:
        logger.error(f"[inject] {email}: {e}")
        return {"success": False, "error": str(e)[:120]}


# ─────────────────────────────────────────────────────────
#  UNLOCK SLOTS  (with auto-inject)
# ─────────────────────────────────────────────────────────
async def _buy_one_slot(id_token: str, uid: str) -> int:
    """Returns the numeric `code` from PurchaseInAppItem24_1. -1 on network failure."""
    payload = {"ProductId": "garageSlot", "PayLoad": {"GarageId": 0}}
    encrypted = encrypt_data(payload, uid)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {id_token}",
        "X-Unity-Version": UNITY_VER,
        "User-Agent": USER_AGENT,
    }
    for _ in range(3):
        proxy = await pool.next()
        try:
            async with _game_session(proxy, timeout=30) as s:
                async with s.post(
                    SLOT_PURCHASE_URL,
                    headers=headers,
                    json={"data": encrypted},
                ) as r:
                    if r.status == 200:
                        try:
                            data = await r.json(content_type=None)
                        except Exception:
                            return -1
                        res = data.get("result", {}) if isinstance(data, dict) else {}
                        if isinstance(res, dict):
                            return res.get("code", -1)
                        if isinstance(res, int):
                            return res
                        if isinstance(data, dict):
                            return data.get("code", -1)
                        return -1
                    await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(1)
    return -1


async def unlock_slots(
    email: str,
    password: str,
    count: int = MAX_SLOTS,
    progress_cb=None,
) -> Dict:
    """
    Unlock garage slots on `email`. Auto-injects coins if the account
    doesn't have enough to buy the next slot.

    Returns dict:
      {bought, requested, final_coins}  on success
      {error}                           on fatal failure
    """
    async def say(msg: str):
        if progress_cb:
            try:
                await progress_cb(msg)
            except Exception:
                pass

    # ── 1. Login ──
    proxy = await pool.next()
    lg = await login(email, password, proxy=proxy)
    if not lg or "idToken" not in lg:
        err = (lg.get("error", "login failed") if lg else "no response")
        if isinstance(err, dict):
            err = err.get("message", str(err))
        return {"error": f"login: {err}"}

    id_token = lg["idToken"]
    uid      = lg["localId"]
    refresh_token = lg.get("refreshToken")

    # ── 2. Proactive balance check ──
    coins = await get_coins(id_token, uid, proxy=proxy)
    await say(f"💰 Balance: {coins or 0:,} coins")

    if (coins or 0) < SLOT_START_BALANCE:
        await say(f"⚠️ Low balance ({coins or 0:,} < {SLOT_START_BALANCE:,}) — injecting coins first (~70 min)...")

        async def _inject_progress(current, total, hit, already, fail):
            await say(
                f"🪙 Injecting... {current}/{total} drags\n"
                f"✅ {hit}  ⏭ {already}  ❌ {fail}"
            )

        inj = await inject_one(email, password, drag_count=DRAG_COUNT, progress_cb=_inject_progress)
        if not inj.get("success"):
            return {"error": f"injection failed: {inj.get('error', 'unknown')}"}

        await say(f"✅ Injected: +{inj.get('gained', 0):,} coins — starting unlocks...")

        # Re-login so our token sees the new coins
        lg = await login(email, password, proxy=await pool.next())
        if not lg or "idToken" not in lg:
            return {"error": "relogin failed after injection"}
        id_token = lg["idToken"]
        uid      = lg["localId"]
        refresh_token = lg.get("refreshToken")

    # ── 3. Buy slots ──
    bought = 0
    consecutive_fail = 0
    extra_injects_done = 0

    for attempt in range(count * 3):
        if bought >= count:
            break

        code = await _buy_one_slot(id_token, uid)

        if code == 1:
            bought += 1
            consecutive_fail = 0
            await say(f"✅ Unlocked slot {bought}/{count}")
            await asyncio.sleep(5)
            continue

        # Failure — decide what to do
        consecutive_fail += 1
        if consecutive_fail >= 3:
            break

        current_coins = await get_coins(id_token, uid, proxy=await pool.next())
        if current_coins is None:
            current_coins = 0

        # Case A: not enough coins — inject more
        if current_coins < SLOT_LOW_BALANCE and extra_injects_done < SLOT_MAX_EXTRA_INJECTS:
            await say(
                f"💰 Low funds ({current_coins:,}) after slot {bought} — "
                f"injecting again ({extra_injects_done + 1}/{SLOT_MAX_EXTRA_INJECTS})..."
            )

            async def _inject_progress2(current, total, hit, already, fail):
                await say(
                    f"🪙 Injecting... {current}/{total} drags\n"
                    f"✅ {hit}  ⏭ {already}  ❌ {fail}"
                )

            inj = await inject_one(email, password, drag_count=DRAG_COUNT, progress_cb=_inject_progress2)
            if not inj.get("success"):
                # Injection failed — stop trying to buy
                await say(f"❌ Injection failed: {inj.get('error', 'unknown')}")
                break

            extra_injects_done += 1
            await say(f"✅ Injected: +{inj.get('gained', 0):,} coins — resuming unlocks...")

            lg = await login(email, password, proxy=await pool.next())
            if lg and "idToken" in lg:
                id_token = lg["idToken"]
                uid      = lg["localId"]
                refresh_token = lg.get("refreshToken")

            consecutive_fail = 0
            await asyncio.sleep(2)
            continue

        # Case B: rate-limited / daily cap — refresh the token and try once more
        if code in (296, 297) and refresh_token:
            refreshed = await refresh_id_token(refresh_token, proxy=await pool.next())
            if refreshed:
                id_token, refresh_token = refreshed
                await asyncio.sleep(3)
                continue

        # Case C: unknown response — just back off and try again
        if refresh_token:
            refreshed = await refresh_id_token(refresh_token, proxy=await pool.next())
            if refreshed:
                id_token, refresh_token = refreshed
        await asyncio.sleep(2)

    final_coins = await get_coins(id_token, uid, proxy=await pool.next())
    return {
        "bought": bought,
        "requested": count,
        "final_coins": final_coins or 0,
    }


# ─────────────────────────────────────────────────────────
#  BOT STATE
# ─────────────────────────────────────────────────────────
sessions: Dict[int, Dict] = {}
locks: Dict[int, asyncio.Lock] = {}
pending_login: Dict[int, bool] = {}
pending_injections: set = set()


def get_lock(uid: int) -> asyncio.Lock:
    if uid not in locks:
        locks[uid] = asyncio.Lock()
    return locks[uid]


def is_busy(uid: int) -> bool:
    return bool(locks.get(uid) and locks[uid].locked())


def allowed(uid: int) -> bool:
    return not ALLOWED_USERS or uid in ALLOWED_USERS


# ─────────────────────────────────────────────────────────
#  KEYBOARDS
# ─────────────────────────────────────────────────────────
def tiers_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(TIERS[0][0], callback_data="tier_0"),
            InlineKeyboardButton(TIERS[1][0], callback_data="tier_1"),
            InlineKeyboardButton(TIERS[2][0], callback_data="tier_2"),
        ],
        [
            InlineKeyboardButton(TIERS[3][0], callback_data="tier_3"),
            InlineKeyboardButton(TIERS[4][0], callback_data="tier_4"),
            InlineKeyboardButton(TIERS[5][0], callback_data="tier_5"),
        ],
        [InlineKeyboardButton("🔓 Unlock Slots", callback_data="unlock_slots")],
    ])


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🪙 Buy Coins", callback_data="open_tiers"),
            InlineKeyboardButton("💳 Balance",   callback_data="refresh_balance"),
        ],
        [InlineKeyboardButton("🔓 Unlock Slots", callback_data="unlock_slots")],
    ])


async def safe_edit(query, text: str, **kw):
    try:
        if query.message.photo:
            return await query.edit_message_caption(caption=text, **kw)
        return await query.edit_message_text(text=text, **kw)
    except telegram_error.BadRequest as e:
        if "not modified" in str(e).lower():
            return query.message
        try:
            return await query.message.reply_text(text, **kw)
        except Exception:
            return None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
#  HANDLERS
# ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not allowed(uid):
        await update.message.reply_text("⛔ Not authorized.")
        return

    sess = sessions.get(uid)
    if sess:
        await update.message.reply_text(
            f"👋 Welcome back\n\n📧 <code>{sess['email']}</code>\n\nTap below:",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            "🪙 <b>Buy Coins Bot</b>\n\n"
            "Send your account credentials:\n\n"
            "<code>email password</code>\n\n"
            "Tap to copy 👆\n\n"
            "Or use /unlock to unlock all garage slots.",
            parse_mode="HTML",
        )
        pending_login[uid] = True


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    sess = sessions.get(uid)
    if not sess:
        await update.message.reply_text("❌ Please /start first.")
        return
    m = await update.message.reply_text("💳 Fetching balance...")
    coins = await get_coins(sess["idToken"], sess["uid"])
    if coins is None:
        lg = await login(sess["email"], sess["password"], proxy=await pool.next())
        if "error" in lg:
            sessions.pop(uid, None)
            await m.edit_text(f"⚠️ Session expired: {lg['error']}\n\nUse /start")
            return
        sess["idToken"] = lg["idToken"]
        sess["uid"] = lg["localId"]
        coins = await get_coins(sess["idToken"], sess["uid"])
    await m.edit_text(f"💳 Balance: <b>{coins if coins is not None else '?'}</b> coins", parse_mode="HTML")


async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    sessions.pop(uid, None)
    pending_login.pop(uid, None)
    await update.message.reply_text("🚪 Logged out.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    pending_login.pop(uid, None)
    await update.message.reply_text("❌ Cancelled.")


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not allowed(uid):
        await update.message.reply_text("⛔ Not authorized.")
        return
    sess = sessions.get(uid)
    if not sess:
        await update.message.reply_text(
            "❌ Login first with /start, or send your credentials:\n\n"
            "<code>email password</code>",
            parse_mode="HTML",
        )
        return
    if is_busy(uid) or uid in pending_injections:
        await update.message.reply_text("⏳ Another injection is already running for you.")
        return

    pending_injections.add(uid)
    asyncio.create_task(_run_unlock(update, context, uid, sess))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if not allowed(uid):
        return
    if not pending_login.get(uid):
        if uid not in sessions:
            await update.message.reply_text("Use /start to begin.")
        return

    text = update.message.text.strip()
    try:
        email, password = text.split(maxsplit=1)
    except ValueError:
        await update.message.reply_text(
            "❌ Bad format. Send:\n\n<code>email password</code>",
            parse_mode="HTML",
        )
        return

    m = await update.message.reply_text("🔐 Logging in...")
    lg = await login(email.strip(), password.strip(), proxy=await pool.next())
    if "error" in lg:
        await m.edit_text(f"❌ Login failed\n\n<code>{lg['error']}</code>", parse_mode="HTML")
        return

    sessions[uid] = {
        "email": email.strip(),
        "password": password.strip(),
        "idToken": lg["idToken"],
        "uid": lg["localId"],
    }
    pending_login.pop(uid, None)
    coins = await get_coins(lg["idToken"], lg["localId"])

    await m.edit_text(
        f"✅ <b>Logged in!</b>\n\n"
        f"📧 <code>{sessions[uid]['email']}</code>\n"
        f"💳 Balance: <b>{(f'{coins:,}' if coins is not None else '?')}</b> coins\n\n"
        f"──────────────\n⚡ Select amount:",
        reply_markup=tiers_keyboard(),
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────────────────
#  UNLOCK RUNNER
# ─────────────────────────────────────────────────────────
async def _run_unlock(update, context, uid: int, sess: Dict):
    chat_id = update.effective_chat.id
    lock = get_lock(uid)
    try:
        async with lock:
            email = sess["email"]
            password = sess["password"]

            progress_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔓 <b>Unlock Slots</b>\n\n"
                    f"📧 <code>{email}</code>\n\n"
                    f"🔍 Checking balance..."
                ),
                parse_mode="HTML",
            )

            last_edit = [0.0]

            async def progress_cb(text: str):
                now = time.time()
                if now - last_edit[0] < 4:
                    return
                last_edit[0] = now
                try:
                    await progress_msg.edit_text(
                        f"🔓 <b>Unlock Slots</b>\n\n"
                        f"📧 <code>{email}</code>\n\n"
                        f"{text}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            result = await unlock_slots(
                email, password, count=MAX_SLOTS, progress_cb=progress_cb
            )

            if "error" in result:
                await progress_msg.edit_text(
                    f"❌ <b>Unlock failed</b>\n\n"
                    f"📧 <code>{email}</code>\n\n"
                    f"<code>{result['error']}</code>",
                    reply_markup=main_menu_keyboard(),
                    parse_mode="HTML",
                )
                return

            await progress_msg.edit_text(
                f"✅ <b>Slots Unlocked</b>\n\n"
                f"📧 <code>{email}</code>\n\n"
                f"🔓 Unlocked: <b>{result['bought']}/{result['requested']}</b>\n"
                f"💰 Remaining coins: <b>{result.get('final_coins', 0):,}</b>\n\n"
                f"<i>Reopen the garage in-game to see new slots.</i>",
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"[unlock] {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Unlock error: {str(e)[:100]}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    finally:
        pending_injections.discard(uid)


# ─────────────────────────────────────────────────────────
#  BUTTON HANDLER
# ─────────────────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    uid = query.from_user.id
    if not allowed(uid):
        await safe_edit(query, "⛔ Not authorized.")
        return

    data = query.data

    # ── Unlock slots ──
    if data == "unlock_slots":
        sess = sessions.get(uid)
        if not sess:
            await safe_edit(
                query,
                "❌ Login first with /start.\n\n"
                "Then tap this button again.",
            )
            return
        if is_busy(uid) or uid in pending_injections:
            await safe_edit(query,
                "⏳ Another injection is already running for you.\n\nWait for it to finish.")
            return
        pending_injections.add(uid)
        asyncio.create_task(_run_unlock_from_query(query, context, uid, sess))
        return

    # ── Buy coins: tier selection ──
    if data == "open_tiers":
        sess = sessions.get(uid)
        if not sess:
            await safe_edit(query, "❌ Login required. Use /start")
            return
        await safe_edit(
            query,
            f"💳 <b>{sess['email']}</b>\n\n⚡ Select amount:",
            reply_markup=tiers_keyboard(),
            parse_mode="HTML",
        )
        return

    # ── Balance refresh ──
    if data == "refresh_balance":
        sess = sessions.get(uid)
        if not sess:
            await safe_edit(query, "❌ Login required.")
            return
        await safe_edit(query, "💳 Fetching balance...")
        coins = await get_coins(sess["idToken"], sess["uid"])
        if coins is None:
            lg = await login(sess["email"], sess["password"], proxy=await pool.next())
            if "error" in lg:
                sessions.pop(uid, None)
                await safe_edit(query, f"⚠️ Session expired: {lg['error']}\n\nUse /start")
                return
            sess["idToken"] = lg["idToken"]
            sess["uid"] = lg["localId"]
            coins = await get_coins(sess["idToken"], sess["uid"])
        await safe_edit(
            query,
            f"💳 Balance: <b>{coins if coins is not None else '?'}</b> coins",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML",
        )
        return

    # ── Single tier injection ──
    if data.startswith("tier_"):
        try:
            idx = int(data.split("_")[1])
            tier_label, drag_count = TIERS[idx]
        except (IndexError, ValueError):
            await safe_edit(query, "❌ Invalid tier.")
            return

        sess = sessions.get(uid)
        if not sess:
            await safe_edit(query, "❌ Login required. Use /start")
            return
        if is_busy(uid) or uid in pending_injections:
            await safe_edit(query, "⏳ An injection is already running for you.\n\nWait for it to finish.")
            return

        est_coins = drag_count * EST_COINS_PER_DRAG
        est_min = drag_count * 2
        pending_injections.add(uid)
        asyncio.create_task(_run_injection_guarded(query, uid, sess, drag_count, est_coins, est_min))
        return


# ─────────────────────────────────────────────────────────
#  UNLOCK VIA BUTTON (uses callback query for the initial message)
# ─────────────────────────────────────────────────────────
async def _run_unlock_from_query(query, context, uid: int, sess: Dict):
    chat_id = query.message.chat.id
    lock = get_lock(uid)
    try:
        async with lock:
            email = sess["email"]
            password = sess["password"]

            progress_msg = await safe_edit(
                query,
                f"🔓 <b>Unlock Slots</b>\n\n"
                f"📧 <code>{email}</code>\n\n"
                f"🔍 Checking balance...",
                parse_mode="HTML",
            )
            if progress_msg is None:
                progress_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔓 <b>Unlock Slots</b>\n\n📧 <code>{email}</code>\n\n🔍 Checking balance...",
                    parse_mode="HTML",
                )

            last_edit = [0.0]

            async def progress_cb(text: str):
                now = time.time()
                if now - last_edit[0] < 4:
                    return
                last_edit[0] = now
                try:
                    await progress_msg.edit_text(
                        f"🔓 <b>Unlock Slots</b>\n\n"
                        f"📧 <code>{email}</code>\n\n"
                        f"{text}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            result = await unlock_slots(
                email, password, count=MAX_SLOTS, progress_cb=progress_cb
            )

            if "error" in result:
                await progress_msg.edit_text(
                    f"❌ <b>Unlock failed</b>\n\n"
                    f"📧 <code>{email}</code>\n\n"
                    f"<code>{result['error']}</code>",
                    reply_markup=main_menu_keyboard(),
                    parse_mode="HTML",
                )
                return

            await progress_msg.edit_text(
                f"✅ <b>Slots Unlocked</b>\n\n"
                f"📧 <code>{email}</code>\n\n"
                f"🔓 Unlocked: <b>{result['bought']}/{result['requested']}</b>\n"
                f"💰 Remaining coins: <b>{result.get('final_coins', 0):,}</b>\n\n"
                f"<i>Reopen the garage in-game to see new slots.</i>",
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"[unlock] {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Unlock error: {str(e)[:100]}",
                parse_mode="HTML",
            )
        except Exception:
            pass
    finally:
        pending_injections.discard(uid)


# ─────────────────────────────────────────────────────────
#  SINGLE INJECTION RUNNER
# ─────────────────────────────────────────────────────────
async def _run_injection_guarded(query, uid, *rest):
    try:
        await _run_injection(query, uid, *rest)
    finally:
        pending_injections.discard(uid)


async def _run_injection(query, uid, sess, drag_count, est_coins, est_min):
    lock = get_lock(uid)
    async with lock:
        email = sess["email"]
        password = sess["password"]

        wait = await safe_edit(
            query,
            f"🔍 <b>Pre-checking account...</b>\n\n"
            f"📧 <code>{email}</code>\n"
            f"<i>This takes ~15 seconds.</i>",
            parse_mode="HTML",
        )
        if wait is None:
            logger.error(f"safe_edit returned None for uid={uid}; aborting injection")
            return

        status = await precheck(email, password)
        note = {
            "fresh":   "✅ Status: fresh — full coins expected.",
            "done":    "⚠️ Status: already done today — earning will be low.",
            "unknown": "❓ Precheck inconclusive — will try anyway.",
            "error":   "❌ Precheck failed — will try anyway.",
        }.get(status, "❓ Unknown.")

        await wait.edit_text(
            f"🪙 <b>Starting injection</b>\n\n"
            f"📧 <code>{email}</code>\n"
            f"{note}\n\n"
            f"🎯 Target: ~{est_coins:,} coins\n"
            f"⚙️ Drags: {drag_count}\n"
            f"⏳ Est. time: ~{est_min} min",
            parse_mode="HTML",
        )

        t0 = time.time()
        last_edit = 0.0

        async def progress_cb(current, total, hit, already, fail):
            nonlocal last_edit
            now = time.time()
            if now - last_edit < 5 and current != total:
                return
            last_edit = now
            pct = current * 100 // max(total, 1)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            elapsed = int(now - t0)
            try:
                await wait.edit_text(
                    f"🪙 <b>Injecting Coins...</b>\n\n"
                    f"<code>[{bar}] {pct}%</code>\n\n"
                    f"🎯 Drag: {current}/{total}\n"
                    f"✅ Hit: {hit}   ⏭ Already: {already}   ❌ Fail: {fail}\n"
                    f"⏱ Elapsed: {elapsed // 60}m {elapsed % 60}s",
                    parse_mode="HTML",
                )
            except Exception:
                pass

        try:
            result = await inject_one(email, password, drag_count=drag_count, progress_cb=progress_cb)
        except Exception as e:
            logger.error(f"inject_one raised: {e}")
            result = {"success": False, "error": str(e)[:120]}

        if result.get("success"):
            try:
                await wait.edit_text(
                    f"✅ <b>Coins Added!</b>\n\n"
                    f"💰 Before: <b>{result.get('coins_before', 0):,}</b>\n"
                    f"🎁 Gained: <b>+{result.get('gained', 0):,}</b>\n"
                    f"🏆 Total:  <b>{result.get('coins_after', 0):,}</b>\n\n"
                    f"🎯 Drags: {drag_count}\n"
                    f"✅ Hit: {result.get('hit', 0)}   "
                    f"⏭ Already: {result.get('already', 0)}   "
                    f"❌ Fail: {result.get('fail', 0)}\n\n"
                    f"<i>(If 'Already' is high, the account hit its daily cap.)</i>",
                    reply_markup=main_menu_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            try:
                await wait.edit_text(
                    f"❌ <b>Injection failed</b>\n\n<code>{result.get('error', 'unknown')}</code>",
                    reply_markup=main_menu_keyboard(),
                    parse_mode="HTML",
                )
            except Exception:
                pass


# ─────────────────────────────────────────────────────────
#  BOOT
# ─────────────────────────────────────────────────────────
async def post_init(app):
    async def _bg_health():
        try:
            live = await pool.health_check()
            logger.info(f"[PROXY] Ready: {live} live proxies")
        except Exception as e:
            logger.error(f"[PROXY] Health check failed: {e}")
    asyncio.create_task(_bg_health())


def build_app() -> Application:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("logout",  cmd_logout))
    app.add_handler(CommandHandler("cancel",  cmd_cancel))
    app.add_handler(CommandHandler("unlock",  cmd_unlock))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_handler))
    return app


if __name__ == "__main__":
    if not BOT_TOKEN or "REPLACE" in BOT_TOKEN:
        print("❌ Set BOT_TOKEN in coins_bot.py first.")
        raise SystemExit(1)

    print("=" * 60)
    print("  coins_bot.py — Buy Coins + Unlock Slots (auto-inject)")
    print("=" * 60)
    print(f"  proxies     : {len(pool.all_proxies)} loaded from {PROXY_FILE}")
    print(f"  drag delay  : {DRAG_WAIT_SEC}s")
    print(f"  max drags   : {DRAG_COUNT}")
    print(f"  max slots   : {MAX_SLOTS}")
    print(f"  allowed     : {'EVERYONE' if not ALLOWED_USERS else ALLOWED_USERS}")
    print("=" * 60)
    print("  starting bot... (proxy health check runs in background)")
    print("=" * 60)
    build_app().run_polling(drop_pending_updates=True, close_loop=False)