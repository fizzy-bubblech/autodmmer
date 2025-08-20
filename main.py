from fastapi import FastAPI, HTTPException, Body, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import os, json, time, uuid, requests

# ---- Optional Redis ----
try:
    import redis  # type: ignore
    REDIS_URL = os.getenv("REDIS_URL")
    r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    r = None

from instagrapi import Client

app = FastAPI()

# ---------- Config / Storage ----------
SEEN_KEY          = os.getenv("SEEN_KEY", "ig_seen_followers")
SEEN_FILE         = os.getenv("SEEN_FILE", "/tmp/seen_followers.json")

DM_SENT_PREFIX    = os.getenv("DM_SENT_PREFIX", "ig_dm_sent")   # ttl-keys per username
DM_SENT_FILE      = os.getenv("DM_SENT_FILE", "/tmp/dm_sent.json")

IG_USERNAME       = os.getenv("IG_USERNAME", "").strip()        # VERPLICHT voor follower-polling
IG_SESSION_ID     = (os.getenv("IG_SESSION_ID") or "").strip()
IG_PROXY_URL      = (os.getenv("IG_PROXY_URL") or "").strip()
IG_SETTINGS_FILE  = os.getenv("IG_SETTINGS_FILE", "/tmp/ig_settings.json")

CLIENT: Optional[Client] = None
LAST_LOGIN_TS: float = 0.0
LOGIN_TTL_SEC = 60 * 20  # reinit client na ~20 minuten

# ---------- Small utils ----------
def _load_json_file(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _save_json_file(path: str, data) -> None:
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def _seen_load() -> set:
    if r:
        try:
            return set(map(int, r.smembers(SEEN_KEY) or []))
        except Exception:
            pass
    data = _load_json_file(SEEN_FILE, [])
    return set(map(int, data or []))

def _seen_save(all_ids: set) -> None:
    if r:
        pipe = r.pipeline()
        for i in all_ids:
            pipe.sadd(SEEN_KEY, int(i))
        pipe.execute()
    else:
        _save_json_file(SEEN_FILE, sorted(list(map(int, all_ids))))

def _dm_key(username: str) -> str:
    return f"{DM_SENT_PREFIX}:{username.lower()}"

def _dm_was_sent(username: str) -> bool:
    if r:
        try:
            return r.get(_dm_key(username)) is not None
        except Exception:
            pass
    data = _load_json_file(DM_SENT_FILE, {})
    ts = data.get(username.lower())
    return bool(ts)

def _dm_mark_sent(username: str, ttl_hours: int = 72) -> None:
    if r:
        try:
            r.setex(_dm_key(username), ttl_hours * 3600, "1")
            return
        except Exception:
            pass
    data = _load_json_file(DM_SENT_FILE, {})
    data[username.lower()] = int(time.time())
    _save_json_file(DM_SENT_FILE, data)

def _within_time_window(tz_name: str, start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz).time()
    start = dtime(hour=start_h, minute=start_m)
    end   = dtime(hour=end_h, minute=end_m)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end

# ---------- Login handling (use provided session + settings; no account_info) ----------
def login_client(force: bool = False) -> Client:
    """
    Reuse/create a logged-in Client using sessionid + settings + proxy.
    Avoids account_info/current_user to skip parser bugs.
    """
    global CLIENT, LAST_LOGIN_TS, IG_SESSION_ID
    if CLIENT is not None and not force and (time.time() - LAST_LOGIN_TS) < LOGIN_TTL_SEC:
        return CLIENT

    if not IG_SESSION_ID:
        raise HTTPException(status_code=401, detail="missing_session: IG_SESSION_ID")

    cl = Client()
    if IG_PROXY_URL:
        cl.set_proxy(IG_PROXY_URL)

    settings = _load_json_file(IG_SETTINGS_FILE, None)
    if settings:
        try:
            cl.set_settings(settings)
        except Exception:
            pass

    try:
        cl.login_by_sessionid(IG_SESSION_ID)
    except Exception as e:
        # Sommige builds gooien KeyError('pinned_channels_info') of login_required zonder verdere info.
        msg = str(e)
        if "login_required" in msg.lower():
            raise HTTPException(status_code=401, detail=f"login_required")
        if "pinned_channels_info" not in msg:
            raise HTTPException(status_code=500, detail=f"login_failed: {msg}")

    try:
        _save_json_file(IG_SETTINGS_FILE, cl.get_settings())
    except Exception:
        pass

    CLIENT = cl
    LAST_LOGIN_TS = time.time()
    return cl

# ---------- Followers ----------
def fetch_followers(cl: Client) -> Dict[str, Any]:
    if not IG_USERNAME:
        raise HTTPException(status_code=400, detail="missing_env: IG_USERNAME")
    user_id = cl.user_id_from_username(IG_USERNAME)
    followers = cl.user_followers_v1(user_id)  # dict[str(pk)] -> UserShort
    return followers

def compute_new_followers_snapshot(cl: Client) -> List[Dict]:
    followers = fetch_followers(cl)
    current_ids = set(map(int, followers.keys()))
    seen = _seen_load()
    new_ids = current_ids - seen

    new_followers = []
    for pk in new_ids:
        u = followers[str(pk)]
        new_followers.append({
            "pk": int(pk),
            "username": getattr(u, "username", ""),
            "full_name": getattr(u, "full_name", "") or ""
        })

    _seen_save(current_ids)
    return new_followers

# ---------- Models ----------
class ProcessFollowersBody(BaseModel):
    time_check: bool = False
    start_hour: int = 0
    start_minute: int = 0
    end_hour: int = 23
    end_minute: int = 59
    timezone: str = "UTC"

class SendIfNoHistoryBody(BaseModel):
    username: str
    message: str
    dedupe_hours: Optional[int] = 72

class SendSimpleBody(BaseModel):
    username: str
    message: str
    dedupe_hours: Optional[int] = 72

class SessionSettingsBody(BaseModel):
    sessionid: str
    settings: dict

class SessionOnlyBody(BaseModel):
    sessionid: str

# ---------- Utility routes ----------
@app.get("/")
def root():
    return {"ok": True, "service": "autodmmer", "ts": datetime.utcnow().isoformat()}

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/routes")
def list_routes():
    return {"routes": [{"path": r.path, "methods": list(r.methods)} for r in app.routes]}

@app.get("/proxy_info")
def proxy_info():
    masked = ""
    if IG_PROXY_URL:
        try:
            masked = IG_PROXY_URL
            if "@" in masked:
                creds, rest = masked.split("@", 1)
                if ":" in creds:
                    user = creds.split(":")[0]
                    masked = f"{user}:***@{rest}"
        except Exception:
            masked = "***"
    return {"proxy_set": bool(IG_PROXY_URL), "proxy_masked": masked}

@app.get("/whoami_ip")
def whoami_ip():
    out = {"direct": {}, "via_proxy": {}}
    try:
        out["direct"] = {"ok": True, "ip": requests.get("https://api.ipify.org?format=json", timeout=8).json().get("ip")}
    except Exception as e:
        out["direct"] = {"ok": False, "error": str(e)}
    try:
        proxies = {"http": IG_PROXY_URL, "https": IG_PROXY_URL} if IG_PROXY_URL else None
        out["via_proxy"] = {"ok": True, "ip": requests.get("https://api.ipify.org?format=json", timeout=10, proxies=proxies).json().get("ip")}
    except Exception as e:
        out["via_proxy"] = {"ok": False, "error": str(e)}
    return out

@app.get("/debug_login")
def debug_login():
    try:
        cl = login_client(force=True)
        info = {}
        if IG_USERNAME:
            try:
                uid = cl.user_id_from_username(IG_USERNAME)
                info = {"username": IG_USERNAME, "user_id": str(uid)}
            except Exception as ie:
                info = {"username": IG_USERNAME, "error": f"user_id_lookup_failed: {ie!s}"}
        return {"ok": True, **info}
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"login_failed: {e!s}")

@app.get("/session_info")
def session_info():
    settings = _load_json_file(IG_SETTINGS_FILE, {})
    dev = settings.get("device_settings", {})
    return {
        "has_session_env": bool(IG_SESSION_ID),
        "has_settings_file": bool(settings),
        "device_id": dev.get("device_id"),
        "phone_id": dev.get("phone_id"),
        "advertising_id": dev.get("advertising_id"),
        "proxy_set": bool(IG_PROXY_URL),
        "last_login_age_sec": int(time.time() - LAST_LOGIN_TS) if LAST_LOGIN_TS else None
    }

# ---------- Admin routes to push session+settings ----------
@app.post("/admin/set_session_and_settings")
def set_session_and_settings(payload: SessionSettingsBody):
    global IG_SESSION_ID, CLIENT, LAST_LOGIN_TS
    IG_SESSION_ID = payload.sessionid.strip()
    _save_json_file(IG_SETTINGS_FILE, payload.settings)
    CLIENT = None
    LAST_LOGIN_TS = 0
    return {"ok": True, "note": "session + settings stored; next call will re-login"}

@app.post("/admin/set_sessionid")
def set_sessionid(payload: SessionOnlyBody):
    global IG_SESSION_ID, CLIENT, LAST_LOGIN_TS
    IG_SESSION_ID = payload.sessionid.strip()
    CLIENT = None
    LAST_LOGIN_TS = 0
    return {"ok": True, "note": "session stored; next call will re-login"}

@app.post("/admin/clear_settings")
def clear_settings():
    try:
        if os.path.exists(IG_SETTINGS_FILE):
            os.remove(IG_SETTINGS_FILE)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---------- Followers ----------
@app.get("/debug_followers_keys")
def debug_followers_keys():
    cl = login_client()
    followers = fetch_followers(cl)
    sample = list(followers.items())[:3]
    sample_pks = [pk for pk, _ in sample]
    sample_usernames = [getattr(u, "username", "") for _, u in sample]
    return {
        "followers_type": type(followers).__name__,
        "count": len(followers),
        "sample_pks": sample_pks,
        "sample_usernames": sample_usernames
    }

@app.get("/seen_status")
def seen_status():
    backend = "redis" if r else "file"
    return {"backend": backend, "count": len(_seen_load())}

@app.post("/process_new_followers")
def process_new_followers(payload: ProcessFollowersBody, verbose: int = Query(default=0)):
    diag = {"steps": []}
    t0 = time.time()
    try:
        if payload.time_check:
            ok = _within_time_window(payload.timezone, payload.start_hour, payload.start_minute,
                                     payload.end_hour, payload.end_minute)
            if not ok:
                return {"new_followers": [], "new_count": 0, "reason": "outside_time_window", "diag": diag}

        cl = login_client()
        t1 = time.time()
        diag["steps"].append({"step": "login", "ms": int((t1 - t0) * 1000)})

        new_followers = compute_new_followers_snapshot(cl)
        t2 = time.time()
        diag["steps"].append({"step": "compute_new_followers", "ms": int((t2 - t1) * 1000)})

        out = {"new_followers": new_followers, "new_count": len(new_followers)}
        if verbose:
            out["diag"] = diag
        return out
    except HTTPException as he:
        if verbose:
            return {"detail": he.detail, "diag": diag}
        raise
    except Exception as e:
        detail = f"process_failed: {e!s}"
        if verbose:
            return {"detail": detail, "diag": diag}
        raise HTTPException(status_code=500, detail=detail)

# ---------- DM sending ----------
@app.post("/send_dm_if_no_history")
def send_dm_if_no_history(
    payload: SendIfNoHistoryBody,
    skip_history: int = Query(default=0)  # 1 = sla inbox-check over
):
    cl = login_client()
    username = payload.username.strip()
    message  = payload.message
    dedupe_h = int(payload.dedupe_hours or 72)

    if _dm_was_sent(username):
        return {"sent": False, "reason": "already_sent_local"}

    if not skip_history:
        try:
            threads = cl.direct_threads(amount=20)
            uid = cl.user_id_from_username(username)
            if any(any(getattr(u, "pk", None) == uid for u in (t.users or [])) for t in threads):
                return {"sent": False, "reason": "history_exists"}
        except Exception:
            # inbox is flaky; don't hard-fail
            pass

    try:
        uid = cl.user_id_from_username(username)
        cl.direct_send(message, [int(uid)])
        _dm_mark_sent(username, ttl_hours=dedupe_h)
        return {"sent": True, "to": username}
    except Exception as e:
        return {"sent": False, "reason": f"send_failed: {e!s}"}

@app.post("/send_dm_simple")
def send_dm_simple(payload: SendSimpleBody):
    cl = login_client()
    username = payload.username.strip()
    message  = payload.message
    dedupe_h = int(payload.dedupe_hours or 72)

    if _dm_was_sent(username):
        return {"sent": False, "reason": "already_sent_local"}

    try:
        uid = cl.user_id_from_username(username)
        cl.direct_send(message, [int(uid)])
        _dm_mark_sent(username, ttl_hours=dedupe_h)
        return {"sent": True, "to": username}
    except Exception as e:
        return {"sent": False, "reason": f"send_failed: {e!s}"}
