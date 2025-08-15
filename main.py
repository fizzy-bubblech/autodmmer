from fastapi import FastAPI
app = FastAPI()

# --- ADD: imports & basic store helpers ---
import os, json, time
from typing import List, Dict
from datetime import datetime, time as dtime
from fastapi import Body
from pydantic import BaseModel
from zoneinfo import ZoneInfo

try:
    import redis  # optional
    REDIS_URL = os.getenv("REDIS_URL")
    r = redis.from_url(REDIS_URL, decode_responses=True) if REDIS_URL else None
except Exception:
    r = None

SEEN_KEY = os.getenv("SEEN_KEY", "ig_seen_followers")
SEEN_FILE = os.getenv("SEEN_FILE", "/tmp/seen_followers.json")

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

# --- ADD: follower polling core ---
def _get_new_followers(cl) -> List[Dict]:
    me_username = os.getenv("IG_USERNAME") or cl.account_info().username
    my_id = cl.user_id_from_username(me_username)
    followers = cl.user_followers_v1(my_id)  # {pk(str): UserShort}
    current_ids = set(map(int, followers.keys()))
    seen = _load_seen_ids()
    new_ids = current_ids - seen

    new_followers = []
    for pk in new_ids:
        u = followers[str(pk)]
        new_followers.append({
            "pk": int(pk),
            "username": u.username,
            "full_name": getattr(u, "full_name", "") or ""
        })

    # Markeer huidige snapshot als gezien (robust tegen missed runs)
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
    # over-midnight window
    return now >= start or now <= end

# --- ADD: request models ---
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

# --- ADD: endpoints expected by n8n ---

@app.post("/process_new_followers")
def process_new_followers(payload: ProcessFollowersBody):
    """
    1) (Optioneel) check time window
    2) poll followers via user_followers_v1
    3) diff met seen set (Redis/JSON)
    4) return: {"new_followers": [...]}
    """
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
    """
    - Check of er al chat history is met deze username.
    - Alleen DM sturen als er geen history is.
    - Return {sent: bool, reason: "..."}
    """
    cl = login_client()
    try:
        user_id = cl.user_id_from_username(payload.username)
    except Exception as e:
        return {"sent": False, "reason": f"username_lookup_failed: {e}"}

    # Probeer bestaande thread te vinden via inbox
    try:
        threads = cl.direct_threads(amount=50)  # vergroot indien nodig
        existing = None
        for t in threads:
            # t.users is lijst UserShort; vergelijk pk
            if any(getattr(u, "pk", None) == user_id for u in (t.users or [])):
                existing = t
                break

        has_history = False
        if existing:
            # Vraag 1 bericht op om te zien of er history is
            try:
                t_full = cl.direct_thread(existing.id, amount=1)
                has_history = bool(getattr(t_full, "messages", None)) and len(t_full.messages) > 0
            except Exception:
                # als ophalen mislukt maar thread bestaat, ga uit van history
                has_history = True

        if has_history:
            return {"sent": False, "reason": "history_exists"}

        # Geen history gevonden: stuur DM
        cl.direct_send(payload.message, [int(user_id)])
        return {"sent": True, "to": payload.username}

    except Exception as e:
        return {"sent": False, "reason": f"send_failed: {e}"}
