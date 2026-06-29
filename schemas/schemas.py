from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import datetime
import uuid


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserDocument(BaseModel):
    user_id: str = Field(default_factory=lambda: generate_id("usr"))
    name: str
    email: str
    phone: str
    created_at: datetime = Field(default_factory=datetime.now())
    updated_at: datetime = Field(default_factory=datetime.now())


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

class PlanDocument(BaseModel):
    plan_id: str = Field(default_factory=lambda: generate_id("plan"))
    name: Literal["starter", "professional"]
    description: str
    price_paise: int                              # e.g. ₹299 → 29900
    currency: str = "INR"
    period: Literal["monthly", "yearly"]
    is_active: bool = True


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

class SubscriptionDocument(BaseModel):
    subscription_id: str = Field(default_factory=lambda: generate_id("sub"))
    user_id: str
    plan_id: str
    price_paise_at_purchase: int                  # snapshot at time of purchase
    status: Literal["pending", "active", "halted", "cancelled"] = "pending"
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    next_billing_date: Optional[datetime] = None
    last_payment_status: Optional[Literal["success", "failed", "pending"]] = None
    razorpay_subscription_id: Optional[str] = None   # unused in v1, for recurring later
    created_at: datetime = Field(default_factory=datetime.now())
    updated_at: datetime = Field(default_factory=datetime.now())


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------

class PaymentDocument(BaseModel):
    payment_id: str = Field(default_factory=lambda: generate_id("pay"))
    user_id: str
    subscription_id: Optional[str] = None         # nullable: one-time has no subscription
    razorpay_order_id: str                        # from Razorpay, starts with order_
    razorpay_payment_id: Optional[str] = None     # filled after user pays
    amount_paise: int                             # NEVER float, NEVER rupees
    currency: str = "INR"
    status: Literal["created", "authorized", "captured", "failed"] = "created"
    payment_method: Optional[str] = None
    error_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now())
    updated_at: datetime = Field(default_factory=datetime.now())


# ---------------------------------------------------------------------------
# WebhookEvent
# ---------------------------------------------------------------------------

class WebhookEventDocument(BaseModel):
    webhook_id: str = Field(default_factory=lambda: generate_id("wh"))
    razorpay_event_id: str                        # unique — dedupe key, starts with evt_
    event_type: str                               # e.g. payment.captured, payment.failed
    raw_payload: str                              # raw request body string, stored for audit
    signature_verified: bool
    processed: bool = False
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.now())
    processed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# IdempotencyKey  (backed by Redis TTL — this is just the shape of the value stored)
# ---------------------------------------------------------------------------

class IdempotencyKeyDocument(BaseModel):
    key: str                                      # Redis key: idempotency:{user_id}:{plan_id}
    user_id: str
    request_hash: str                             # hash of user_id + plan_id + intent
    response_snapshot: Optional[dict] = None      # stored Razorpay order response
    status: Literal["in_progress", "completed", "failed"] = "in_progress"
    created_at: datetime = Field(default_factory=datetime.now())
    expires_at: datetime                          # TTL managed by Redis EX param


# ---------------------------------------------------------------------------
# Request / Response schemas (used by routes, not stored in DB)
# ---------------------------------------------------------------------------

class CreateOrderRequest(BaseModel):
    user_id: str
    plan_id: str

class CreateOrderResponse(BaseModel):
    order_id: str                                 # razorpay order_id, pass to Checkout
    amount_paise: int
    currency: str
    key_id: str                                   # RAZORPAY_KEY_ID, needed by frontend Checkout

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

class VerifyPaymentResponse(BaseModel):
    success: bool
    payment_id: str                               # your internal payment_id
    message: str