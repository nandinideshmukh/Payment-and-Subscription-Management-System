import hashlib
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any

# Redis key prefix
IDEMPOTENCY_PREFIX = "idempotency:"


def make_request_hash(user_id: str, plan_id: str) -> str:
    """
    Create a unique hash for idempotency based on the request.
    This ensures the same user+plan combination gets the same order.
    """
    data = {
        "user_id": user_id,
        "plan_id": plan_id,
        "intent": "create_order"
    }
    json_str = json.dumps(data, sort_keys=True)
    return hashlib.sha256(json_str.encode()).hexdigest()


def get_idempotency_key(redis_client, user_id: str, plan_id: str) -> Optional[Dict[str, Any]]:
    """Check if we have a cached response for this idempotency key."""
    request_hash = make_request_hash(user_id, plan_id)
    key = f"{IDEMPOTENCY_PREFIX}{request_hash}"
    
    cached = redis_client.get(key)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            return None
    return None


def set_idempotency_in_progress(redis_client, user_id: str, plan_id: str, ttl_seconds: int = 40):
    """Set idempotency key status to 'in_progress' before calling Razorpay."""
    request_hash = make_request_hash(user_id, plan_id)
    key = f"{IDEMPOTENCY_PREFIX}{request_hash}"
    
    data = {
        "status": "in_progress",
        "user_id": user_id,
        "plan_id": plan_id,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    redis_client.setex(key, ttl_seconds, json.dumps(data))


def set_idempotency_completed(redis_client, user_id: str, plan_id: str, response: Dict[str, Any], ttl_seconds: int = 40):
    """Set idempotency key status to 'completed' with the response."""
    request_hash = make_request_hash(user_id, plan_id)
    key = f"{IDEMPOTENCY_PREFIX}{request_hash}"
    
    data = {
        "status": "completed",
        "user_id": user_id,
        "plan_id": plan_id,
        "response": response,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    redis_client.setex(key, ttl_seconds, json.dumps(data))


def set_idempotency_failed(redis_client, user_id: str, plan_id: str, error: str = None, ttl_seconds: int = 40):
    """Set idempotency key status to 'failed' if order creation fails."""
    request_hash = make_request_hash(user_id, plan_id)
    key = f"{IDEMPOTENCY_PREFIX}{request_hash}"
    
    data = {
        "status": "failed",
        "user_id": user_id,
        "plan_id": plan_id,
        "error": error,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    redis_client.setex(key, ttl_seconds, json.dumps(data))


def rupees_to_paise(rupees: float) -> int:
    """Convert rupees (float) to paise (int)."""
    return int(round(rupees * 100))


def paise_to_rupees(paise: int) -> float:
    """Convert paise to rupees for display purposes only."""
    return paise / 100.0