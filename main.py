from typing import Optional
from fastapi import Query, HTTPException

# ... bestaande imports/vars/login_client() blijven ongewijzigd ...

def _serialize_thread(t):
    return {
        "id": t.id,           # string id (instagrapi DirectThread.id)
        "pk": t.pk,           # numeric pk
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

def _serialize_message(m):
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
    selected_filter: str = Query(default="", regex="^$|^flagged$|^unread$"),
    thread_message_limit: Optional[int] = None
):
    """
    Haal inbox threads op.
    selected_filter: "", "flagged", "unread"
    """
    cl = login_client()
    threads = cl.direct_threads(
        amount=amount,
        selected_filter=selected_filter,
        thread_message_limit=thread_message_limit
    )  # gedocumenteerd in usage guide
    return {"threads": [_serialize_thread(t) for t in threads], "count": len(threads)}

@app.get("/thread/{thread_id}")
def get_thread(thread_id: int, amount: int = 20):
    """
    Haal één thread met tot 'amount' berichten op.
    """
    cl = login_client()
    try:
        t = cl.direct_thread(thread_id, amount=amount)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Thread not found: {e}")
    return {
        "thread": _serialize_thread(t),
        "messages": [_serialize_message(m) for m in (t.messages or [])]
    }

@app.get("/messages/{thread_id}")
def list_messages(thread_id: int, amount: int = 20):
    """
    Alleen de berichten van een thread (handig voor paginering).
    """
    cl = login_client()
    msgs = cl.direct_messages(thread_id, amount=amount)
    return {"messages": [_serialize_message(m) for m in msgs], "count": len(msgs)}

@app.get("/thread_by_participants")
def thread_by_participants(usernames: str):
    """
    Zoek of maak de 1:1 / groeps-thread o.b.v. komma-gescheiden usernames.
    """
    cl = login_client()
    names = [u.strip() for u in usernames.split(",") if u.strip()]
    user_ids = [cl.user_id_from_username(u) for u in names]
    t = cl.direct_thread_by_participants(user_ids)  # documented
    return {"thread": _serialize_thread(t)}
