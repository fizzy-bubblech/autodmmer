# main.py
import os, json
from typing import List, Dict, Optional
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

# pip install instagrapi python-dotenv redis
from instagrapi import Client

app = FastAPI(title="AutoDMmer API")

# ---------- Optional Redis (persistent "seen" storage) ----------
try:
    import redis  # optional
    REDIS_URL = os.getenv("REDIS_URL")
    r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    r = None

SEEN_KEY = os.getenv("SEEN_KEY", "ig_seen_followers")
SEEN_FILE = os.getenv("SEEN_FILE", "/tmp/seen_followers.json")  # fallback (ephemeral op Render)

# ---------- Auth / login ----------
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")
IG_SESSION_ID = os.getenv("IG_SESSION_ID")  # aanbevolen boven password

def login_client() -> Client:
    """Login via session-id of username/password; gooi nette 500 als creds ontbreken."""
    cl = Client()
    try:
        if IG_SESSION_ID:
            cl.login_by_sessionid(IG_SESSION_ID)
            return cl
        if IG_USERNAME and IG_PASSWORD:
            cl.login(IG_USERNAME, IG_PASSWORD)
            return cl
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"login_failed: {e}")
    raise HTTPException(status_code=500, detail="Missing IG credentials: set IG_SESSION_ID or IG_USERNAME+IG_PASSWORD")

# ---------- Helpers: persist "seen followers" ----------
def _load_seen_ids() -> set:
    if r:
        return set(map(int, r.smembers(SEEN_KEY) or []))
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
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

# ---------- Followers polling core ----------
def _get_new_followers(cl: Client) -> List[Dict]:
    me_username = IG_USERNAME or cl.account_info().username
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
    print("### STARTUP: main.py loaded")
    print(f"### STARTUP: Auth via session? {bool(IG_SESSION_ID)} | username set? {bool(IG_USERNAME)}")
    try:
        # log beschikbare routes
        route_paths = [r.path for r in app.routes]
        print(f"### STARTUP: routes = {route_paths}")
    except Exception as e:
        print(f"###
