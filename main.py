# main.py
import os, json, traceback, time
from typing import List, Dict, Optional, Iterable, Tuple
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from instagrapi import Client
import requests

app = FastAPI(title="AutoDMmer API")

# ---------- Optional Redis ----------
try:
    import redis  # optional
    REDIS_URL = os.getenv("REDIS_URL")
    r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    r = None

SEEN_KEY = os.getenv("SEEN_KEY", "ig_seen_followers")
SEEN_FILE = os.getenv("SEEN_FILE", "/tmp/seen_followers.json")
SETTINGS_KEY = os.getenv("SETTINGS_KEY", "ig_settings_json")
SETTINGS_FILE = os.getenv("SETTINGS_FILE", "/tmp/ig_settings.json")
SESSION_OVERRIDE_KEY = os.getenv("SESSION_OVERRIDE_KEY", "ig_sessionid_runtime")
SESSION_FILE = os.getenv("SESSION_FILE", "/tmp/ig_sessionid.txt")

# ---------- Global error wrapper: altijd JSON ----------
@app.middleware("http")
async def json_errors(request, call_next):
    try:
        return await call_next(request)
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"detail": he.detail})
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": f"unhandled_error: {e.__class__.__name__}"})

# ---------- Seen helpers ----------
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

# ---------- IG settings persist ----------
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

# ---------- Session override ----------
def _load_session_override() -> Optional[str]:
    try:
        if r:
            val = r.get(SESSION_OVERRIDE_KEY)
            if val:
                return val.strip()
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "r") as f:
                return f.read().strip()
    except Exception:
        return None
    return None

def _save_session_override(sessionid: str):
    try:
        if r:
            r.set(SESSION_OVERRIDE_KEY, sessionid.strip())
        else:
            with open(SESSION_FILE, "w") as f:
                f.write(sessionid.strip())
    except Exception:
        pass

# ---------- Utils ----------
def _probe_public_ip(proxy_url: Optional[str]) -> dict:
    try:
        kw = {}
        if proxy_url:
            kw["proxies"] = {"http": proxy_url, "https": proxy_url}
        resp = requests.get("https://api.ipify.org?format=json", timeout=8, **kw)
        return {"ok": True, "ip": resp.json().get("ip")}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def login_client() -> Client:
    IG_USERNAME = os.getenv("IG_USERNAME")
    IG_PASSWORD = os.getenv("IG_PASSWORD")
    IG_SESSION_ID = os.getenv("IG_SESSION_ID")
    IG_PROXY_URL = os.getenv("IG_PROXY_URL")

    cl = Client()
    if IG_PROXY_URL:
        cl.set_proxy(IG_PROXY_URL)

    settings = _load_ig_settings()
    if settings:
        try:
            cl.set_settings(settings)
        except Exception:
            pass

    runtime_session = _load_session_override()
    try:
        if runtime_session:
            cl.login_by_sessionid(runtime_session)
        elif IG_SESSION_ID:
            cl.login_by_sessionid(IG_SESSION_ID)
        elif IG_USERNAME and IG_PASSWORD:
            cl.login(IG_USERNAME, IG_PASSWORD)
        else:
            raise HTTPException(status_code=500, detail="Missing IG credentials: set IG_SESSION_ID or IG_USERNAME+IG_PASSWORD")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"login_failed: {e}")

    try:
        _save_ig_settings(cl.get_settings())
    except Exception:
        pass
    return cl

# ---------- Robust IG-call wrapper ----------
TRANSIENT_MARKERS = (
    "connection closed by server",
    "temporarily blocked",
    "please wait a few minutes",
    "timed out",
    "read timed out",
    "max retries exceeded",
    "connection reset",
)

def ig_call(cl: Client, func_name: str, *args, **kwargs):
    """
    Doe een instagrapi call met 3 pogingen, korte backoff en 1x sessie refresh.
    """
    last_err = None
    for attempt in range(3):
        try:
            fn = getattr(cl, func_name)
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            # Alleen retry bij duidelijke transient/network throttling fouten
            if any(m in msg for m in TRANSIENT_MARKERS):
                # 1e misser: probeer sessie te herladen (zonder password)
                if attempt == 0:
                    try:
                        sid = _load_session_override() or os.getenv("IG_SESSION_ID")
                        if sid:
                            cl.login_by_sessionid(sid.strip())
                    except Exception:
                        pass
                time.sleep(1 + 2 * attempt)
                continue
            break
    # Na retries nog steeds fout:
    raise last_err

# ---------- Followers core ----------
def _followers_iter(followers_obj) -> Iterable:
    if isinstance(followers_obj, dict):
        return followers_obj.values()
    if isinstance(followers_obj, list):
        return iter(followers_obj)
    try:
        return iter(followers_obj)
    except TypeError:
        return iter([])

def _collect_current_ids_and_map(followers_obj) -> Tuple[set, Dict[int, object]]:
    current_ids: set = set()
    users_by_pk: Dict[int, object] = {}
    for u in _followers_iter(followers_obj):
        pk = None
        for attr in ("pk", "id"):
            v = getattr(u, attr, None)
            if v is not None:
                pk = v
                break
        try:
            if pk is None:
                continue
            pk = int(pk)
            current_ids.add(pk)
            users_by_pk[pk] = u
        except Exception:
            continue
    return current_ids, users_by_pk

def _get_new_followers(cl: Client) -> List[Dict]:
    me_username = os.getenv("IG_USERNAME") or ig_call(cl, "account_info").username
    my_id = ig_call(cl, "user_id_from_username", me_username)
    followers = ig_call(cl, "user_followers_v1", my_id)  # kan dict of list zijn

    try:
        typ = type(followers).__name__
        sample = []
        if isinstance(followers, dict):
            sample = list(followers.keys())[:3]
        elif isinstance(followers, list):
            sample = [getattr(x, "pk", None) for x in followers[:3]]
        print("### followers_type:", typ, "| sample:", sample)
    except Exception:
        pass

    current_ids, users_by_pk = _collect_current_ids_and_map(followers)

    seen = _load_seen_ids()
    new_ids = current_ids - seen

    new_followers: List[Dict] = []
    for pk in new_ids:
        u = users_by_pk.get(pk)
        if u is None:
            try:
                ui = ig_call(cl, "user_info", pk)
                new_followers.append({
                    "pk": int(pk),
                    "username": getattr(ui, "username", None),
                    "full_name": getattr(ui, "full_name", "") or ""
                })
            except Exception as e:
                print("### WARN enrich follower", pk, "->", repr(e))
                new_followers.append({"pk": int(pk), "username": None, "full_name": ""})
        else:
            new_followers.append({
                "pk": int(pk),
                "username": getattr(u, "username", None),
                "full_name": getattr(u, "full_name", "") or ""
            })

    _save_seen_ids(current_ids)
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
    return now >= start or now <= end

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

class SessionBody(BaseModel):
    sessionid: str

# ---------- Startup logs ----------
@app.on_event("startup")
def _startup_log():
    try:
        print("### STARTUP: main.py loaded")
        print(f"### STARTUP: session(env)? {bool(os.getenv('IG_SESSION_ID'))} | username set? {bool(os.getenv('IG_USERNAME'))}")
        print(f"### STARTUP: proxy set? {bool(os.getenv('IG_PROXY_URL'))}")
        ip_probe = _probe_public_ip(os.getenv("IG_PROXY_URL"))
        print(f"### STARTUP: egress IP probe = {ip_probe}")
        route_paths = [r.path for r in app.routes]
        print(f"### STARTUP: routes = {route_paths}")
    except Exception as e:
        print(f"### STARTUP: route log failed: {e}")

# ---------- Health & debug ----------
@app.get("/")
def root():
    return {"ok": True, "service": "AutoDMmer API", "ts": datetime.utcnow().isoformat()}

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.get("/routes")
def list_routes():
    return {"routes": [{"path": r.path, "methods": list(getattr(r, "methods", []))} for r in app.routes]}

@app.get("/proxy_info")
def proxy_info():
    IG_PROXY_URL = os.getenv("IG_PROXY_URL") or ""
    return {
        "env_proxy_set": bool(IG_PROXY_URL),
        "env_proxy_scheme": IG_PROXY_URL.split("://")[0] if "://" in IG_PROXY_URL else None,
        "whoami": _probe_public_ip(None),
        "whoami_via_proxy": _probe_public_ip(IG_PROXY_URL) if IG_PROXY_URL else None
    }

@app.get("/debug_login")
def debug_login():
    cl = login_client()
    me = ig_call(cl, "account_info")
    return {"ok": True, "username": me.username, "pk": str(me.pk), "full_name": getattr(me, "full_name", "")}

@app.post("/set_sessionid")
def set_sessionid(body: SessionBody):
    sid = body.sessionid.strip()
    if not sid or len(sid) < 20:
        raise HTTPException(status_code=400, detail="sessionid lijkt ongeldig")
    _save_session_override(sid)
    return {"ok": True}

@app.get("/whoami_ip")
def whoami_ip():
    direct = _probe_public_ip(None)
    via_proxy = _probe_public_ip(os.getenv("IG_PROXY_URL"))
    return {"direct": direct, "via_proxy": via_proxy}

@app.get("/debug_followers_keys")
def debug_followers_keys(limit: int = 5):
    cl = login_client()
    me_username = os.getenv("IG_USERNAME") or ig_call(cl, "account_info").username
    my_id = ig_call(cl, "user_id_from_username", me_username)
    followers = ig_call(cl, "user_followers_v1", my_id)
    info = {"followers_type": type(followers).__name__}
    if isinstance(followers, dict):
        ks = list(followers.keys())[:limit]
        info["sample_keys"] = ks
        info["key_types"] = [type(k).__name__ for k in ks]
        info["count"] = len(followers)
    elif isinstance(followers, list):
        sample = followers[:limit]
        info["sample_pks"] = [getattr(u, "pk", None) for u in sample]
        info["sample_usernames"] = [getattr(u, "username", None) for u in sample]
        info["count"] = len(followers)
    else:
        info["repr"] = repr(followers)[:200]
    return info

# ---------- Threads & messages ----------
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
        threads = ig_call(cl, "direct_threads", **kwargs)
        return {"threads": [_serialize_thread(t) for t in threads], "count": len(threads)}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"threads_failed: {e}")

@app.get("/thread/{thread_id}")
def get_thread(thread_id: str, amount: int = 20):
    try:
        cl = login_client()
        t = ig_call(cl, "direct_thread", thread_id, amount=amount)
        return {"thread": _serialize_thread(t), "messages": [_serialize_message(m) for m in (t.messages or [])]}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=404, detail=f"thread_failed: {e}")

@app.get("/messages/{thread_id}")
def list_messages(thread_id: str, amount: int = 20):
    try:
        cl = login_client()
        msgs = ig_call(cl, "direct_messages", thread_id, amount=amount)
        return {"messages": [_serialize_message(m) for m in msgs], "count": len(msgs)}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"messages_failed: {e}")

@app.get("/thread_by_participants")
def thread_by_participants(usernames: str):
    try:
        cl = login_client()
        names = [u.strip() for u in usernames.split(",") if u.strip()]
        user_ids = [ig_call(cl, "user_id_from_username", u) for u in names]
        t = ig_call(cl, "direct_thread_by_participants", user_ids)
        return {"thread": _serialize_thread(t)}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"thread_by_participants_failed: {e}")

# ---------- n8n endpoints ----------
class ProcessFollowersBodyIn(BaseModel):
    time_check: bool = False
    start_hour: int = 0
    start_minute: int = 0
    end_hour: int = 23
    end_minute: int = 59
    timezone: str = "UTC"

@app.post("/process_new_followers")
def process_new_followers(payload: Optional[ProcessFollowersBodyIn] = None):
    try:
        if payload is None:
            payload = ProcessFollowersBodyIn()
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
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"process_failed: {e}")

class SendIfNoHistoryBody(BaseModel):
    username: str
    message: str

@app.post("/send_dm_if_no_history")
def send_dm_if_no_history(payload: SendIfNoHistoryBody):
    cl = login_client()
    try:
        user_id = ig_call(cl, "user_id_from_username", payload.username)
    except Exception as e:
        return {"sent": False, "reason": f"username_lookup_failed: {e}"}
    try:
        threads = ig_call(cl, "direct_threads", amount=50)
        existing = None
        for t in threads:
            if any(getattr(u, "pk", None) == user_id for u in (t.users or [])):
                existing = t
                break
        has_history = False
        if existing:
            try:
                t_full = ig_call(cl, "direct_thread", existing.id, amount=1)
                has_history = bool(getattr(t_full, "messages", None)) and len(t_full.messages) > 0
            except Exception:
                has_history = True
        if has_history:
            return {"sent": False, "reason": "history_exists"}
        ig_call(cl, "direct_send", payload.message, [int(user_id)])
        return {"sent": True, "to": payload.username}
    except Exception as e:
        traceback.print_exc()
        return {"sent": False, "reason": f"send_failed: {e}"}
