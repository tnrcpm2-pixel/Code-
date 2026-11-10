import argparse
import base64
import json
import sys
import time
import uuid

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

FIREBASE_KEY = "AIzaSyCQDz9rgjgmvmFkvVfmvr2-7fT4tfrzRRQ"
FB_VERIFY = ("https://www.googleapis.com/identitytoolkit/v3/relyingparty/"
             "verifyPassword?key=" + FIREBASE_KEY)
FB_INFO = ("https://www.googleapis.com/identitytoolkit/v3/relyingparty/"
           "getAccountInfo?key=" + FIREBASE_KEY)
CF_BASE = "https://europe-west1-cpm-2-7cea1.cloudfunctions.net"
OG_BASE = "https://cpm-2.ogames.kz/api"
OG_KEY = "320b93f3e7f4410aa52ce24da363ad04"
UA = "UnityPlayer/2022.3.62f2 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)"
UNITY = "2022.3.62f2"
CLIENT_VERSION = "1.3.3.6"
CLIENT_HASH = "F05A72840B40DC4FAADF539C5E38062527AE6422"
BUNDLE_ID = "com.olzhas.carparking.multyplayer2"
KEY_ADD = "12345678"
IV_ADD = "01234567"
TARGET = 1000
COMBO = "3,5,0"
DRAG_COMBOS = ((0, 1, 0), (0, 2, 0), (1, 1, 0), (2, 1, 0))
RATING = {
    "rating": 15.0, "cars": 8.0, "run": 200000.147,
    "t_distance": 200020.068, "time": 200008.0,
    "reactions": 200001.0, "car_collided": 200002.0,
    "slicer_cut": 200000.0, "block_post": 200000.0,
    "burnt_tire": 200000.0, "car_exchange": 200000.0,
    "car_fix": 200000.0, "car_trade": 200000.0, "car_wash": 200000.0,
    "cargo": 200000.0, "delivery": 200000.0, "drift": 200000.0,
    "drift_max": 100000.0, "fuel": 200000.0, "gifts": 88.0,
    "levels": 100000.0, "offroad": 200000.0,
    "passanger_distance": 200000.0, "police": 200000.0,
    "push_ups": 200000.0, "race_win": 10000.0,
    "real_estate": 100000.0, "speed_banner": 200000.0,
    "taxi": 200000.0, "treasure": 200000.0,
}


class Err(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def extract_int(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(float(text))
        except ValueError:
            pass
        try:
            return extract_int(json.loads(text))
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("coin", "coins", "Coin", "Coins", "money", "Money",
                    "reward", "value", "data"):
            if key in value:
                found = extract_int(value[key])
                if found is not None:
                    return found
        return None
    if isinstance(value, list):
        for item in value:
            found = extract_int(item)
            if found is not None:
                return found
    return None


class Account:
    def __init__(self, email, password, timeout=40):
        self.email = email
        self.password = password
        self.timeout = timeout
        self.token = None
        self.uid = None
        self.key = None
        self.iv = None
        self.device_id = uuid.uuid4().hex
        self.s = requests.Session()

    def login(self):
        r = self.s.post(FB_VERIFY, json={"email": self.email,
                                         "password": self.password,
                                         "returnSecureToken": True},
                        timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            raise Err("login: bad response")
        if "idToken" not in data or "localId" not in data:
            raise Err("login: " + str(data.get("error", {}).get("message")))
        self.token = data["idToken"]
        self.uid = data["localId"]
        self.key = (self.uid[:8] + KEY_ADD).encode()[:16]
        self.iv = (self.uid[:8] + IV_ADD).encode()[:16]
        try:
            self.s.post(FB_INFO, json={"idToken": self.token},
                        timeout=self.timeout)
        except requests.RequestException:
            pass
        return self

    def encrypt(self, text, key_source=None):
        src = key_source or self.uid
        key = (src[:8] + KEY_ADD).encode()[:16]
        iv = (src[:8] + IV_ADD).encode()[:16]
        return base64.b64encode(
            AES.new(key, AES.MODE_CBC, iv).encrypt(
                pad(str(text).encode(), 16))).decode()

    def decrypt(self, blob):
        if not isinstance(blob, str) or not blob:
            return None
        try:
            raw = AES.new(self.key, AES.MODE_CBC, self.iv).decrypt(
                base64.b64decode(blob))
            return unpad(raw, 16).decode()
        except Exception:
            return None

    def decode_int(self, blob):
        if isinstance(blob, dict):
            blob = blob.get("data")
        if not isinstance(blob, str):
            return extract_int(blob)
        plain = self.decrypt(blob)
        if plain is None:
            return None
        return extract_int(plain)

    def headers(self):
        return {"User-Agent": UA,
                "Content-Type": "application/json; charset=utf-8",
                "X-Unity-Version": UNITY,
                "Authorization": "Bearer " + self.token,
                "X-Client-Hash": CLIENT_HASH}

    def og_headers(self):
        return {"X-Firebase-Token": self.token,
                "X-Client-Platform": "ANDROID",
                "X-Client-Version": CLIENT_VERSION,
                "X-Client-DeviceId": self.device_id,
                "X-Api-Key": OG_KEY,
                "X-Client-Env": "prod",
                "X-Bundle-Id": BUNDLE_ID,
                "Content-Type": "application/json",
                "User-Agent": UA,
                "X-Client-Hash": CLIENT_HASH}

    def handshake(self):
        try:
            self.s.get(OG_BASE + "/check-service/v1/hash/check",
                       headers=self.og_headers(), timeout=15)
            self.s.post(OG_BASE + "/check-service/v1/session/start",
                        headers=self.og_headers(), json={}, timeout=15)
            return True
        except requests.RequestException:
            return False

    def call(self, name, payload):
        try:
            r = self.s.post(CF_BASE + "/" + name, headers=self.headers(),
                            data=json.dumps({"data": payload}),
                            timeout=self.timeout)
        except requests.RequestException as exc:
            raise Err("network: " + exc.__class__.__name__)
        if r.status_code == 404:
            raise Err("missing: " + name)
        if r.status_code != 200:
            raise Err("%s http %s" % (name, r.status_code))
        try:
            body = r.json()
        except ValueError:
            raise Err(name + ": bad json")
        result = body.get("result")
        if isinstance(result, dict) and "code" in result:
            return int(result["code"]), result.get("data")
        return 1, result

    def coins(self):
        for suffix in ("24_1", "25_1", "23_1"):
            try:
                code, data = self.call("GetCoins" + suffix, None)
            except Err:
                continue
            if code == 1:
                return self.decode_int(data)
        return None

    def tasks_pass(self):
        r = self.s.post(OG_BASE + "/progress-service/v1/rating/update",
                        headers=self.og_headers(),
                        data=json.dumps({"data": self.encrypt(
                            json.dumps(RATING))}),
                        timeout=self.timeout)
        try:
            body = r.json()
        except ValueError:
            return 0
        return int(body.get("code") or 0)

    def drag(self, car, place, gear):
        payload = "%d,%d,%d" % (car, place, gear)
        sealed = self.encrypt(payload)
        try:
            code, raw = self.call("SetDragRacing23_1", sealed)
        except Err as exc:
            if "missing" not in str(exc):
                raise
            code, raw = self.call("SetDragRacing22_1", sealed)
        coins = None
        if isinstance(raw, str):
            coins = self.decode_int(raw)
        elif isinstance(raw, list) and len(raw) >= 2:
            coins = self.decode_int(raw[1])
        return code, coins

    def circuit_token(self):
        r = self.s.post(OG_BASE + "/progress-service/fb/v1/racing/circuit/"
                        "token-client", headers=self.og_headers(),
                        data=json.dumps({"data": None}),
                        timeout=self.timeout)
        try:
            body = r.json()
        except ValueError:
            return 0, None
        return int(body.get("code") or 0), body.get("data")

    def circuit_submit(self, token, combo=COMBO):
        r = self.s.post(OG_BASE + "/progress-service/fb/v1/racing/circuit/"
                        "update-client", headers=self.og_headers(),
                        data=json.dumps({"data": self.encrypt(combo, token)}),
                        timeout=self.timeout)
        try:
            body = r.json()
        except ValueError:
            return 0, None
        return int(body.get("code") or 0), body.get("data")


def load_list(path):
    rows = []
    for line in open(path, encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line or "@" not in line:
            raise SystemExit("bad line (email:password expected): " + line)
        email, _, password = line.partition(":")
        rows.append((email.strip(), password.strip()))
    return rows


def run_tasks(accounts, gap, lock_wait, max_waits):
    out = {}
    waits = 0
    for index, (email, password) in enumerate(accounts):
        if index:
            time.sleep(gap)
        while True:
            try:
                acc = Account(email, password).login()
                acc.handshake()
                code = acc.tasks_pass()
            except Exception as exc:
                out[email] = str(exc)
                log("tasks %s: failed %s" % (email, exc))
                break
            if code == 297 and waits < max_waits:
                waits += 1
                log("tasks %s: locked, waiting %ss (%d/%d)"
                    % (email, lock_wait, waits, max_waits))
                time.sleep(lock_wait)
                continue
            out[email] = code
            log("tasks %s: code %s" % (email, code))
            break
    return out


def run_drag(accounts, gap, lock_wait, max_waits):
    out = {}
    waits = 0
    for index, (email, password) in enumerate(accounts):
        if index:
            time.sleep(gap)
        try:
            acc = Account(email, password).login()
            acc.handshake()
        except Exception as exc:
            out[email] = 0
            log("drag %s: failed %s" % (email, exc))
            continue
        got = 0
        for car, place, gear in DRAG_COMBOS:
            while True:
                try:
                    code, coins = acc.drag(car, place, gear)
                except Err as exc:
                    log("drag %s: %s" % (email, exc))
                    code, coins = 0, None
                    break
                if code == 297 and waits < max_waits:
                    waits += 1
                    log("drag %s: locked, waiting %ss (%d/%d)"
                        % (email, lock_wait, waits, max_waits))
                    time.sleep(lock_wait)
                    continue
                break
            if code == 1 and isinstance(coins, int) and coins > 0:
                got += coins
                log("drag %s: combo %d,%d,%d +%d"
                    % (email, car, place, gear, coins))
                time.sleep(2)
        out[email] = got
        log("drag %s: +%d" % (email, got))
    return out


def run_circuit(accounts, gap, lock_wait, max_waits):
    results = []
    total_coins = 0
    total_money = 0
    waits = 0
    for email, password in accounts:
        while True:
            try:
                acc = Account(email, password).login()
                acc.handshake()
                code, data = acc.circuit_token()
            except Exception as exc:
                results.append((email, 0, 0, str(exc)))
                log("circuit %s: failed %s" % (email, exc))
                code, data = 0, None
                break
            if code != 1 or not isinstance(data, dict) or not data.get("token"):
                results.append((email, 0, 0, "lane closed (code %s)" % code))
                log("circuit %s: lane closed (code %s)" % (email, code))
                break
            earned = int(data.get("circuitCoinsEarned") or 0)
            token = data["token"]
            coins_got = 0
            money_got = 0
            dry = 0
            stop = None
            while earned < TARGET and dry < 2:
                time.sleep(150)
                try:
                    code, data = acc.circuit_submit(token)
                except Exception as exc:
                    stop = str(exc)
                    break
                if code == 297:
                    if waits < max_waits:
                        waits += 1
                        time.sleep(lock_wait)
                        continue
                    stop = "locked (297)"
                    break
                if code != 1 or not isinstance(data, dict):
                    stop = "submit code %s" % code
                    break
                coins = int(data.get("coins") or 0)
                money = int(data.get("money") or 0)
                coins_got += coins
                money_got += money
                dry = 0 if coins > 0 else dry + 1
                code, fresh = acc.circuit_token()
                if code == 1 and isinstance(fresh, dict) and fresh.get("token"):
                    earned = int(fresh.get("circuitCoinsEarned") or earned)
                    token = fresh["token"]
                else:
                    token = data.get("token") or token
            total_coins += coins_got
            total_money += money_got
            why = stop or ("cap" if earned >= TARGET else "dry")
            results.append((email, coins_got, money_got, why))
            log("circuit %s: +%d coins +%d money (%s)"
                % (email, coins_got, money_got, why))
            break
        time.sleep(gap)
    return results, total_coins, total_money


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", required=True, metavar="FILE")
    parser.add_argument("--tasks", action="store_true")
    parser.add_argument("--drag", action="store_true")
    parser.add_argument("--circuit", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--gap", type=int, default=60)
    parser.add_argument("--lock-wait", type=int, default=900)
    parser.add_argument("--max-waits", type=int, default=3)
    args = parser.parse_args()
    accounts = load_list(args.list)
    if not accounts:
        raise SystemExit("no accounts in " + args.list)
    do_tasks = args.tasks or args.all
    do_drag = args.drag or args.all
    do_circuit = args.circuit or args.all
    if not (do_tasks or do_drag or do_circuit):
        do_tasks = do_drag = True
    log("%d account(s): tasks=%s drag=%s circuit=%s"
        % (len(accounts), do_tasks, do_drag, do_circuit))
    tasks = {}
    drag = {}
    if do_tasks:
        tasks = run_tasks(accounts, args.gap, args.lock_wait, args.max_waits)
    if do_drag:
        drag = run_drag(accounts, args.gap, args.lock_wait, args.max_waits)
    results, circuit_coins, circuit_money = [], 0, 0
    if do_circuit:
        results, circuit_coins, circuit_money = run_circuit(
            accounts, args.gap, args.lock_wait, args.max_waits)
    log("")
    for email, _password in accounts:
        acc = None
        try:
            acc = Account(email, _password).login()
            balance = acc.coins()
        except Exception:
            balance = None
        log("%s | tasks %s | drag +%d | balance %s"
            % (email, tasks.get(email, "-"), drag.get(email, 0), balance))
    log("drag +%d coins, circuit +%d coins +%d money"
        % (sum(drag.values()), circuit_coins, circuit_money))


if __name__ == "__main__":
    main()
