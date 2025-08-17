# main.py
import os
import json
from typing import List, Dict, Optional
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

# pip install instagrapi python-dotenv redis requests
from instagrapi import Client
import requests

app = FastAPI(title="AutoDMmer API")

# ---------- Optional Redis (persistent "seen" + settings) ----------
try:
    import redis  # optional
    REDIS_URL = os.getenv("REDIS_URL")
    r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    r = None

SEEN_KEY = os.getenv("SEEN_KEY", "ig_seen_followers")
SEEN_FILE = os.getenv("SEEN_FILE", "/tmp/seen_followers.json")  # fallback (ephemeral op Render)

SETTINGS_KEY = os.getenv("SETTINGS_KEY", "ig_settings_json")
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "/tmp/ig_settings.json")

# ---------- Helpers: persist "seen followers" ----------
def _load_seen_ids() -> set:
    if r:
        return set(map(int, r.smembers(SEEN_KEY) or []))
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def _save_seen_ids(ids: set):
    if r:
        pipe = r.pipeline()
        for i in ids:
            pipe.sadd(SEEN_KEY, int(i))
        pipe.execute()
    else:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(list(ids)), f)

# ---------- Helpers: persist instagrapi settings (device, cookies, etc.) ----------
def _load_ig_settings() -> Optional[dict]:
    try:
        if r:
            raw = r.get(SETTINGS_KEY)
            if raw:
                return json.loads(raw)
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
    except Exception:
        return None
    return None

def _save_ig_settings(settings: dict):
    try:
        if r:
            r.set(SETTINGS_KEY, json.dumps(settings))
        else:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(settings, f)
    except Exception:
        pass

# ---------- (Optional) IP-probe, handig voor debugging proxy ----------
def _probe_public_ip(proxy_url: Optional[str]) -> dict:
    try:
        kw = {}
        if proxy_url:
            kw["proxies"] = {"http": proxy_url, "https": proxy_url}
        resp = requests.get("https://api.ipify.org?format=json", timeout=8, **kw)
        return {"ok": True, "ip": resp.json().get("ip")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------- Auth / login met proxy + persistente settings ----------
def login_client() -> Client:
    """
    Login via session-id of username/password.
    - Gebruikt IG_PROXY_URL als proxy
    - Laadt en bewaart instagrapi settings (device, cookies)
    """
    IG_USERNAME = os.getenv("IG_USERNAME")
    IG_PASSWORD = os.getenv("IG_PASSWORD")
    IG_SESSION_ID = os.getenv("IG_SESSION_ID")
    IG_PROXY_URL = os.getenv("IG_PROXY_URL")  # bv. http://user:pass@host:port of socks5h://user:pass@host:port

    cl = Client()

    # 1) Proxy zetten (zo vroeg mogelijk)
    if IG_PROXY_URL:
        cl.set_proxy(IG_PROXY_URL)

    # 2) Bestaande settings laden (device ids e.d.)
    settings = _load_ig_settings()
    if settings:
        try:
            cl.set_settings(settings)
        except Exception:
            # als oude settings corrupt zijn, negeren en vers beginnen
            pass

    # 3) Probeer inloggen
    try:
        if IG_SESSION_ID:
            cl.login_by_sessionid(IG_SESSION_ID)
        elif IG_USERNAME and IG_PASSWORD:
            cl.login(IG_USERNAME, IG_PASSWORD)
        else:
            raise HTTPException(status_code=500, detail="Missing IG credentials: set IG_SESSION_ID or IG_USERNAME+IG_PASSWORD")
    except Exception as e:
        raise HTTPException(status_code=500, detail="login_failed: {}".format(e))

    # 4) Succesvolle login → settings bewaren
    try:
        _save_ig_settings(cl.get_settings())
    except Exception:
        pass

    return cl

# ---------- Followers polling core ----------
def _get_new_followers(cl: Client) -> List[Dict]:
    me_username = os.getenv("IG_USERNAME") or cl.account_info().username
    my_id = cl.user_id_from_username(me_username)
    followers = cl.user_followers_v1(my_id)  # {pk(str): UserShort}
    current_ids = set(map(int, followers.keys()))
    seen = _load_seen_ids()
    new_ids = current_ids - seen

    new_followers: List[Dict] = []
    for pk in new_ids:
        u = followers[str(pk)]
        new_followers.append({
            "pk": int(pk),
            "username": u.username,
            "full_name": getattr(u, "full_name", "") or ""
        })

    _save_seen_ids(current_ids)  # markeer snapshot als gezien
    return new_followers

def _within_time_window(tz_name: str, start_h: int, start_m: int, end_h: int, end_m: int) -> bool:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz).time()
    start = dtime(hour=start_h, minute=start_m)
    end = dtime(hour=end_h, minute=end_m)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end  # over-midnight

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

# ---------- Startup logging ----------
@app.on_event("startup")
def _startup_log():
    try:
        print("### STARTUP: main.py loaded")
        print("### STARTUP: session set? {} | username set? {}".format(bool(os.getenv("IG_SESSION_ID")), bool(os.getenv("IG_USERNAME"))))
        print("### STARTUP: proxy set? {}".format(bool(os.getenv("IG_PROXY_URL"))))
        ip_probe = _probe_public_ip(os.getenv("IG_PROXY_URL"))
        print("### STARTUP: egress IP probe = {}".format(ip_probe))
        route_paths = [r.path for r in app.routes]
        print("### STARTUP: routes = {}".format(route_paths))
    except Exception as e:
        print("### STARTUP: route log failed: {}".format(e))

# ---------- Basic & debug ----------
@app.get("/")
def root():
    return {"ok": True, "service": "AutoDMmer API", "ts": datetime.utcnow().isoformat()}

@app.get("/ping")
def ping():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

@app.get("/routes")
def list_routes():
    return {"routes": [{"path": r.path, "methods": list(getattr(r, "methods", []))} for r in app.routes]}

@app.get("/debug_login")
def debug_login():
    cl = login_client()
    me = cl.account_info()
    return {"ok": True, "username": me.username, "pk": me.pk, "full_name": getattr(me, "full_name", "")}

@app.get("/whoami_ip")
def whoami_ip():
    """Check je publieke IP, met of zonder proxy (voor debugging)."""
    direct = _probe_public_ip(None)
    via_proxy = _probe_public_ip(os.getenv("IG_PROXY_URL"))
    return {"direct": direct, "via_proxy": via_proxy}

# ---------- Serializers ----------
def _serialize_thread(t) -> Dict:
    return {
        "id": t.id,
        "pk": t.pk,
        "users": [{"pk": u.pk, "username": u.username, "full_name": u.full_name} for u in (t.users or [])],
        "last_message": (
            {
                "id": str(t.messages[0].id),
                "user_id": t.messages[0].user_id,
                "text": t.messages[0].text,
                "timestamp": t.messages[0].timestamp.isoformat() if getattr(t.messages[0], "timestamp", None) else None,
                "item_type": t.messages[0].item_type,
            } if getattr(t, "messages", None) and len(t.messages) > 0 else None
        ),
    }

def _serialize_message(m) -> Dict:
    return {
        "id": str(m.id),
        "user_id": m.user_id,
        "thread_id": m.thread_id,
        "text": m.text,
        "item_type": m.item_type,
        "timestamp": m.timestamp.isoformat() if getattr(m, "timestamp", None) else None,
        "reactions": getattr(m, "reactions", None),
    }

# ---------- Threads & messages ----------
@app.get("/threads")
def list_threads(
    amount: int = 20,
    selected_filter: str = Query(default="", description="Allowed: '', 'flagged', 'unread'"),
    thread_message_limit: Optional[int] = None
):
    if selected_filter not in ("", "flagged", "unread"):
        raise HTTPException(status_code=400, detail="selected_filter must be '', 'flagged', or 'unread'")
    try:
        cl = login_client()
        kwargs = {"amount": amount}
        if selected_filter:
            kwargs["selected_filter"] = selected_filter
        if thread_message_limit is not None:
            kwargs["thread_message_limit"] = thread_message_limit
        threads = cl.direct_threads(**kwargs)
        return {"threads": [_serialize_thread(t) for t in threads], "count": len(threads)}
    except Exception as e:
        raise HTTPException(status_code=500, detail="threads_failed: {}".format(e))

@app.get("/thread/{thread_id}")
def get_thread(thread_id: str, amount: int = 20):
    try:
        cl = login_client()
        t = cl.direct_thread(thread_id, amount=amount)
        return {"thread": _serialize_thread(t), "messages": [_serialize_message(m) for m in (t.messages or [])]}
    except Exception as e:
        raise HTTPException(status_code=404, detail="thread_failed: {}".format(e))

@app.get("/messages/{thread_id}")
def list_messages(thread_id: str, amount: int = 20):
    try:
        cl = login_client()
        msgs = cl.direct_messages(thread_id, amount=amount)
        return {"messages": [_serialize_message(m) for m in msgs], "count": len(msgs)}
    except Exception as e:
        raise HTTPException(status_code=500, detail="messages_failed: {}".format(e))

@app.get("/thread_by_participants")
def thread_by_participants(usernames: str):
    try:
        cl = login_client()
        names = [u.strip() for u in usernames.split(",") if u.strip()]
        user_ids = [cl.user_id_from_username(u) for u in names]
        t = cl.direct_thread_by_participants(user_ids)
        return {"thread": _serialize_thread(t)}
    except Exception as e:
        raise HTTPException(status_code=500, detail="thread_by_participants_failed: {}".format(e))

# ---------- Endpoints die n8n aanroept ----------
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

@app.post("/process_new_followers")
def process_new_followers(payload: ProcessFollowersBody):
    if payload.time_check:
        ok = _within_time_window(
            payload.timezone,
            payload.start_hour, payload.start_minute,
            payload.end_hour, payload.end_minute
        )
        if not ok:
            return {"new_followers": [], "new_count": 0, "reason": "outside_time_window"}
    cl = login_client()
    new_followers = _get_new_followers(cl)
    return {"new_followers": new_followers, "new_count": len(new_followers)}

@app.post("/send_dm_if_no_history")
def send_dm_if_no_history(payload: SendIfNoHistoryBody):
    cl = login_client()
    try:
        user_id = cl.user_id_from_username(payload.username)
    except Exception as e:
        return {"sent": False, "reason": "username_lookup_failed: {}".format(e)}
    try:
        threads = cl.direct_threads(amount=50)
        existing = None
        for t in threads:
            if any(getattr(u, "pk", None) == user_id for u in (t.users or [])):
                existing = t
                break
        has_history = False
        if existing:
            try:
                t_full = cl.direct_thread(existing.id, amount=1)
                has_history = bool(getattr(t_full, "messages", None)) and len(t_full.messages) > 0
            except Exception:
                has_history = True
        if has_history:
            return {"sent": False, "reason": "history_exists"}
        cl.direct_send(payload.message, [int(user_id)])
        return {"sent": True, "to": payload.username}
    except Exception as e:
        return {"sent": False, "reason": "send_failed: {}".format(e)}
