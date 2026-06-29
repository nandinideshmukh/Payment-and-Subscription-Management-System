import hmac
import hashlib
import json
import uuid
import logging
import sys
from datetime import datetime, timezone
from services.payment_notification_service import NotificationService

from utils.payment_util import (
    get_idempotency_key,
    set_idempotency_in_progress,
    set_idempotency_completed,
    set_idempotency_failed,
    make_request_hash,
    rupees_to_paise
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


def create_order(razorpay_client, redis_client, db, user_id: str, plan_id: str):
    """Create a Razorpay order with idempotency"""
    
    existing = get_idempotency_key(redis_client, user_id, plan_id)

    if existing:
        if existing["status"] == "completed":
            return {"ok": True, "data": existing["response"], "cached": True, "idempotent": True}
        if existing["status"] == "in_progress":
            return {"ok": False, "error": "duplicate_request", "message": "Request already in progress, please wait.", "idempotent": True}
        if existing["status"] == "failed":
            pass

    plan = db.plans.find_one({"plan_id": plan_id, "is_active": True})
    if not plan:
        return {"ok": False, "error": "plan_not_found", "message": "Plan not found or inactive."}

    user = db.users.find_one({"user_id": user_id})
    if not user:
        return {"ok": False, "error": "user_not_found", "message": "User not found."}

    set_idempotency_in_progress(redis_client, user_id, plan_id)
    
    existing_subscription = db.subscriptions.find_one({
        "user_id": user_id,
        "status": {"$in": ["active", "pending"]}
    })
    
    if existing_subscription:
        # Check if subscription is expired
        end_date = existing_subscription.get("end_date")
        if end_date:
            # Make sure end_date is timezone-aware
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            
            now = datetime.now(timezone.utc)
            
            if end_date >= now:
                logger.warning(f"[CREATE ORDER] User {user_id} has active subscription: {existing_subscription['subscription_id']}")
                return {
                    "ok": False, 
                    "error": "subscription_active", 
                    "message": "You already have an active subscription you may renew when you get a email."
                }

    try:
        receipt_id = f"receipt_{uuid.uuid4().hex[:16]}"
        razorpay_order = razorpay_client.order.create({
            "amount": plan["price_paise"],
            "currency": plan.get("currency", "INR"),
            "receipt": receipt_id,
            "payment_capture": 1
        })
    except Exception as e:
        set_idempotency_failed(redis_client, user_id, plan_id)
        return {"ok": False, "error": "razorpay_error", "message": str(e)}

    payment_doc = {
        "payment_id": f"pay_{uuid.uuid4().hex}",
        "user_id": user_id,
        "plan_id": plan_id,
        "subscription_id": None,
        "razorpay_order_id": razorpay_order["id"],
        "razorpay_payment_id": None,
        "amount_paise": plan["price_paise"],
        "currency": plan.get("currency", "INR"),
        "status": "created",
        "payment_method": None,
        "error_reason": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }

    db.payments.insert_one(payment_doc)

    order_response = {
        "order_id": razorpay_order["id"],
        "amount_paise": plan["price_paise"],
        "currency": plan.get("currency", "INR"),
        "payment_id": payment_doc["payment_id"],
        "receipt": receipt_id
    }

    set_idempotency_completed(redis_client, user_id, plan_id, order_response)

    return {"ok": True, "data": order_response, "cached": False, "idempotent": True}


def verify_checkout_signature(db, key_secret: str, razorpay_order_id: str, razorpay_payment_id: str, razorpay_signature: str):
    """Verify Razorpay checkout signature"""
    
    message = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected = hmac.new(
        key_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, razorpay_signature):
        return {"ok": False, "error": "invalid_signature", "message": "Signature verification failed."}

    # First check if payment already exists and its status
    existing_payment = db.payments.find_one({"razorpay_order_id": razorpay_order_id})
    
    if not existing_payment:
        return {"ok": False, "error": "payment_not_found", "message": "No payment found for this order."}
    
    # Only update to "authorized" if status is "created" (not already captured)
    if existing_payment.get('status') == 'created':
        result = db.payments.update_one(
            {"razorpay_order_id": razorpay_order_id},
            {"$set": {
                "razorpay_payment_id": razorpay_payment_id,
                "status": "authorized",
                "updated_at": datetime.now(timezone.utc)
            }}
        )
        logger.info(f"[VERIFY] Payment authorized for order: {razorpay_order_id}")
    else:
        # Payment already has a status (captured, failed, etc.)
        # Just update the payment_id without changing status
        result = db.payments.update_one(
            {"razorpay_order_id": razorpay_order_id},
            {"$set": {
                "razorpay_payment_id": razorpay_payment_id,
                "updated_at": datetime.now(timezone.utc)
            }}
        )
        logger.info(f"[VERIFY] Payment already has status: {existing_payment.get('status')}, keeping it")

    if result.matched_count == 0:
        return {"ok": False, "error": "payment_not_found", "message": "No payment found for this order."}

    return {"ok": True, "message": "Signature verified. Awaiting webhook confirmation."}

def verify_webhook_signature(raw_body: bytes, signature: str, webhook_secret: str) -> bool:
    """Verify Razorpay webhook signature on raw body"""
    expected = hmac.new(
        webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def handle_webhook_event(db, raw_body: bytes, payload: dict):
    """Handle webhook events with deduplication"""
    
    # FIX: Get event ID from 'id' field
    event_id = payload.get("id")
    event_type = payload.get("event")

    logger.info("=" * 60)
    logger.info(f"[WEBHOOK] Processing event: {event_type}")
    logger.info(f"[WEBHOOK] Event ID: {event_id}")

    if not event_id:
        logger.error("[WEBHOOK] ❌ Missing event ID - webhook rejected!")
        return {"ok": False, "error": "missing_event_id", "message": "Webhook must include 'id' field"}

    # --- Dedupe: check if event already processed ---
    existing = db.webhook_events.find_one({"razorpay_event_id": event_id})
    if existing:
        logger.info(f"[WEBHOOK] Duplicate event, skipping: {event_id}")
        return {"ok": True, "message": "already_processed"}

    # --- Store webhook event ---
    webhook_doc = {
        "webhook_id": f"wh_{uuid.uuid4().hex}",
        "razorpay_event_id": event_id,
        "event_type": event_type,
        "raw_payload": raw_body.decode("utf-8"),
        "signature_verified": True,
        "processed": False,
        "error": None,
        "created_at": datetime.now(timezone.utc),
        "processed_at": None
    }
    db.webhook_events.insert_one(webhook_doc)
    logger.info(f"[WEBHOOK] Webhook event stored: {webhook_doc['webhook_id']}")

    # --- Route to correct handler ---
    try:
        if event_type == "payment.authorized":
            logger.info("[WEBHOOK] Routing to payment.authorized handler")
            _handle_payment_authorized(db, payload)
        elif event_type == "payment.captured":
            logger.info("[WEBHOOK] Routing to payment.captured handler")
            _handle_payment_captured(db, payload)
        elif event_type == "order.paid":
            logger.info("[WEBHOOK] Routing to order.paid handler")
            _handle_order_paid(db, payload)
        elif event_type == "payment.failed":
            logger.info("[WEBHOOK] Routing to payment.failed handler")
            _handle_payment_failed(db, payload)
        # In handle_webhook_event function, add these cases:
        elif event_type == "subscription.charged":
            logger.info("[WEBHOOK] Routing to subscription.charged handler")
            _handle_subscription_charged(db, payload)

        elif event_type == "subscription.halted":
            logger.info("[WEBHOOK] Routing to subscription.halted handler")
            _handle_subscription_halted(db, payload)

        elif event_type == "subscription.cancelled":
            logger.info("[WEBHOOK] Routing to subscription.cancelled handler")
            _handle_subscription_cancelled(db, payload)

        elif event_type == "subscription.pending":
            logger.info("[WEBHOOK] Routing to subscription.pending handler")
            _handle_subscription_pending(db, payload)
        else:
            logger.warning(f"[WEBHOOK] Unknown event type: {event_type}")

        db.webhook_events.update_one(
            {"razorpay_event_id": event_id},
            {"$set": {
                "processed": True,
                "processed_at": datetime.now(timezone.utc)
            }}
        )
        logger.info(f"[WEBHOOK] Event processed successfully: {event_id}")
        return {"ok": True, "message": "processed"}

    except Exception as e:
        logger.error(f"[WEBHOOK] Error processing event: {e}")
        import traceback
        traceback.print_exc()
        db.webhook_events.update_one(
            {"razorpay_event_id": event_id},
            {"$set": {"error": str(e)}}
        )
        return {"ok": True, "message": "error_stored"}


def _handle_payment_authorized(db, payload: dict):
    """Handle payment.authorized event"""
    payment_entity = payload["payload"]["payment"]["entity"]
    razorpay_payment_id = payment_entity["id"]
    razorpay_order_id = payment_entity["order_id"]

    db.payments.update_one(
        {"razorpay_order_id": razorpay_order_id},
        {"$set": {
            "razorpay_payment_id": razorpay_payment_id,
            "status": "authorized",
            "payment_method": payment_entity.get("method"),
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    logger.info(f"[AUTHORIZED] Payment {razorpay_payment_id} authorized for order {razorpay_order_id}")


def _handle_payment_captured(db, payload: dict):
    """Handle payment.captured event - marks payment as captured"""
    logger.info("[CAPTURED] Handling payment.captured event")
    
    payment_entity = payload["payload"]["payment"]["entity"]
    razorpay_payment_id = payment_entity["id"]
    razorpay_order_id = payment_entity["order_id"]
    
    logger.info(f"[CAPTURED] Payment: {razorpay_payment_id}, Order: {razorpay_order_id}")

    # --- Check if payment exists ---
    existing_payment = db.payments.find_one({"razorpay_order_id": razorpay_order_id})
    if not existing_payment:
        logger.error(f"[CAPTURED] Payment NOT found for order: {razorpay_order_id}")
        existing_payment = db.payments.find_one({"razorpay_payment_id": razorpay_payment_id})
        if existing_payment:
            logger.info(f"[CAPTURED] Found payment by payment_id: {existing_payment.get('payment_id')}")
        else:
            logger.error(f"[CAPTURED] Payment not found by payment_id either")
            return
    
    logger.info(f"[CAPTURED] Current status before update: {existing_payment.get('status')}")
    updated_payment = None
    
    # Only update if not already captured
    if existing_payment.get('status') != 'captured':
        # --- Update Payment to captured ---
        result = db.payments.update_one(
            {"razorpay_order_id": razorpay_order_id},
            {"$set": {
                "razorpay_payment_id": razorpay_payment_id,
                "status": "captured",
                "payment_method": payment_entity.get("method"),
                "updated_at": datetime.now(timezone.utc)
            }}
        )
        
        logger.info(f"[CAPTURED] Update result - matched: {result.matched_count}, modified: {result.modified_count}")
        
        # --- Verify the update ---
        updated_payment = db.payments.find_one({"razorpay_order_id": razorpay_order_id})
        logger.info(f"[CAPTURED] Status after update: {updated_payment.get('status') if updated_payment else 'Not found'}")
        
        if updated_payment and updated_payment.get('status') == 'captured':
            logger.info("[CAPTURED] ✅ Payment successfully marked as captured")
            _create_or_update_subscription(db, updated_payment, razorpay_order_id)
        else:
            updated_payment = existing_payment
            logger.error("[CAPTURED] ❌ Payment status is NOT captured after update!")
    else:
        logger.info(f"[CAPTURED] Payment already captured, skipping update")
        updated_payment = existing_payment
        # Still create subscription if not exists
        _create_or_update_subscription(db, existing_payment, razorpay_order_id)
    if updated_payment:
        _send_payment_confirmation_email(db, updated_payment)
    else:
        logger.warning("[CAPTURED] No payment to send confirmation for")
        
def _handle_order_paid(db, payload: dict):
    """Handle order.paid event - backup for payment.captured"""
    logger.info("[ORDER PAID] Handling order.paid event")
    
    payment_entity = payload["payload"]["payment"]["entity"]
    razorpay_payment_id = payment_entity["id"]
    razorpay_order_id = payment_entity["order_id"]
    payment_status_from_webhook = payment_entity.get("status")
    
    logger.info(f"[ORDER PAID] Payment: {razorpay_payment_id}, Order: {razorpay_order_id}")
    logger.info(f"[ORDER PAID] Payment status from webhook: {payment_status_from_webhook}")
    
    # Find the payment
    existing_payment = db.payments.find_one({"razorpay_order_id": razorpay_order_id})
    
    if not existing_payment:
        logger.error(f"[ORDER PAID] Payment NOT found for order: {razorpay_order_id}")
        existing_payment = db.payments.find_one({"razorpay_payment_id": razorpay_payment_id})
        if existing_payment:
            logger.info(f"[ORDER PAID] Found payment by payment_id: {existing_payment.get('payment_id')}")
        else:
            logger.error(f"[ORDER PAID] Payment not found by payment_id either")
            return
    
    logger.info(f"[ORDER PAID] Current status in DB: {existing_payment.get('status')}")
    logger.info(f"[ORDER PAID] Payment ID: {existing_payment.get('payment_id')}")
    
    # ✅ Initialize updated_payment
    updated_payment = None
    
    # --- Update payment to captured (if not already) ---
    if existing_payment.get('status') != 'captured':
        # Use update_one with explicit filter
        result = db.payments.update_one(
            {"_id": existing_payment["_id"]},
            {"$set": {
                "razorpay_payment_id": razorpay_payment_id,
                "status": "captured",
                "payment_method": payment_entity.get("method"),
                "updated_at": datetime.now(timezone.utc)
            }}
        )
        logger.info(f"[ORDER PAID] Update result - matched: {result.matched_count}, modified: {result.modified_count}")
        
        # Force a verification by fetching fresh
        updated_payment = db.payments.find_one({"_id": existing_payment["_id"]})
        logger.info(f"[ORDER PAID] Status after update: {updated_payment.get('status') if updated_payment else 'Not found'}")
        
        if updated_payment and updated_payment.get('status') == 'captured':
            logger.info("[ORDER PAID] ✅ Payment successfully marked as captured")
            _create_or_update_subscription(db, updated_payment, razorpay_order_id)
        else:
            logger.error("[ORDER PAID] ❌ Payment status is NOT captured after update!")
            # Try one more time with direct update
            db.payments.update_one(
                {"_id": existing_payment["_id"]},
                {"$set": {"status": "captured", "updated_at": datetime.now(timezone.utc)}}
            )
            # Verify again
            final_check = db.payments.find_one({"_id": existing_payment["_id"]})
            logger.info(f"[ORDER PAID] Final status after retry: {final_check.get('status') if final_check else 'Not found'}")
            # ✅ Set updated_payment to the final check
            updated_payment = final_check or existing_payment
    else:
        logger.info(f"[ORDER PAID] Payment already captured, skipping update")
        # ✅ Use existing_payment
        updated_payment = existing_payment
    
    # ✅ Send email after both cases
    # if updated_payment and updated_payment.get('status') == 'captured':
    #     _send_payment_confirmation_email(db, updated_payment)
    # else:
    #     logger.warning("[ORDER PAID] No payment to send confirmation for")
            
def _create_or_update_subscription(db, payment, razorpay_order_id):
    """Create or update subscription"""
    logger.info("[SUBSCRIPTION] Creating/updating subscription")
    
    now = datetime.now(timezone.utc)
    
    plan = db.plans.find_one({"plan_id": payment["plan_id"]}) if payment.get("plan_id") else None
    logger.info(f"[SUBSCRIPTION] Plan: {plan}")
    
    from dateutil.relativedelta import relativedelta
    if plan:
        period = plan.get("period", "monthly")
        if period == "monthly":
            end_date = now + relativedelta(months=1)
        elif period == "yearly":
            end_date = now + relativedelta(years=1)
        else:
            end_date = now + relativedelta(months=1)
    else:
        end_date = now + relativedelta(months=1)
    
    logger.info(f"[SUBSCRIPTION] End date: {end_date}")
    
    user_id = payment["user_id"]
    existing_sub = db.subscriptions.find_one({"user_id": user_id})
    logger.info(f"[SUBSCRIPTION] Existing subscription: {existing_sub}")
    
    if existing_sub:
        sub_id = existing_sub["subscription_id"]
        result = db.subscriptions.update_one(
            {"subscription_id": sub_id},
            {"$set": {
                "status": "active",
                "start_date": now,
                "end_date": end_date,
                "next_billing_date": end_date,
                "last_payment_status": "success",
                "updated_at": now,
                "plan_id": payment.get("plan_id"),
                "price_paise_at_purchase": payment["amount_paise"]
            }}
        )
        logger.info(f"[SUBSCRIPTION] Updated subscription {sub_id}: matched={result.matched_count}, modified={result.modified_count}")
    else:
        sub_id = f"sub_{uuid.uuid4().hex[:16]}"
        sub_doc = {
            "subscription_id": sub_id,
            "user_id": user_id,
            "plan_id": payment.get("plan_id"),
            "price_paise_at_purchase": payment["amount_paise"],
            "status": "active",
            "start_date": now,
            "end_date": end_date,
            "next_billing_date": end_date,
            "last_payment_status": "success",
            "razorpay_subscription_id": None,
            "created_at": now,
            "updated_at": now
        }
        logger.info(f"[SUBSCRIPTION] Inserting new subscription: {sub_doc}")
        
        try:
            result = db.subscriptions.insert_one(sub_doc)
            logger.info(f"[SUBSCRIPTION] Created subscription {sub_id} with _id: {result.inserted_id}")
            saved_sub = db.subscriptions.find_one({"subscription_id": sub_id})
            logger.info(f"[SUBSCRIPTION] Verification - saved sub: {saved_sub}")
        except Exception as e:
            logger.error(f"[SUBSCRIPTION] Failed to insert subscription: {e}")
            import traceback
            traceback.print_exc()
            raise
    
    link_result = db.payments.update_one(
        {"razorpay_order_id": razorpay_order_id},
        {"$set": {"subscription_id": sub_id}}
    )
    logger.info(f"[SUBSCRIPTION] Linked payment to subscription: matched={link_result.matched_count}")
    
    final_sub = db.subscriptions.find_one({"user_id": user_id})
    logger.info(f"[SUBSCRIPTION] Final subscription state: {final_sub}")
    
    all_subs = list(db.subscriptions.find({"user_id": user_id}))
    logger.info(f"[SUBSCRIPTION] All subscriptions for user: {len(all_subs)}")
    for sub in all_subs:
        logger.info(f"[SUBSCRIPTION]   - {sub.get('subscription_id')}: {sub.get('status')}")
    
    return sub_id


def _handle_payment_failed(db, payload: dict):
    """Handle payment.failed event"""
    payment_entity = payload["payload"]["payment"]["entity"]
    razorpay_payment_id = payment_entity["id"]
    razorpay_order_id = payment_entity["order_id"]
    error_reason = payment_entity.get("error_description") or payment_entity.get("error_code")

    logger.info(f"[FAILED] Payment failed: {razorpay_payment_id}, Reason: {error_reason}")

    payment = db.payments.find_one_and_update(
        {"razorpay_order_id": razorpay_order_id},
        {"$set": {
            "razorpay_payment_id": razorpay_payment_id,
            "status": "failed",
            "error_reason": error_reason,
            "updated_at": datetime.now(timezone.utc)
        }},
        return_document=True
    )

    if not payment:
        logger.error(f"[FAILED] Payment not found for order: {razorpay_order_id}")
        raise Exception(f"Payment not found for order {razorpay_order_id}")

    db.subscriptions.update_one(
        {"user_id": payment["user_id"]},
        {"$set": {
            "last_payment_status": "failed",
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    logger.info(f"[FAILED] Updated subscription for user: {payment['user_id']}")

def _handle_subscription_charged(db, payload: dict):
    """
    Handle subscription.charged webhook event
    This is for recurring payments
    """
    logger.info("[SUBSCRIPTION] Handling subscription.charged event")
    
    subscription_entity = payload["payload"]["subscription"]["entity"]
    razorpay_subscription_id = subscription_entity["id"]
    payment_entity = payload["payload"]["payment"]["entity"]
    
    # Find subscription in our DB
    subscription = db.subscriptions.find_one({
        "razorpay_subscription_id": razorpay_subscription_id
    })
    
    if not subscription:
        logger.warning(f"[SUBSCRIPTION] Subscription not found: {razorpay_subscription_id}")
        return
    
    # Update subscription
    new_end_date = datetime.fromtimestamp(subscription_entity["end_at"], tz=timezone.utc)
    
    db.subscriptions.update_one(
        {"subscription_id": subscription["subscription_id"]},
        {"$set": {
            "status": "active",
            "end_date": new_end_date,
            "next_billing_date": new_end_date,
            "last_payment_status": "success",
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    # Create payment record
    payment_doc = {
        "payment_id": f"pay_{uuid.uuid4().hex}",
        "user_id": subscription["user_id"],
        "subscription_id": subscription["subscription_id"],
        "razorpay_order_id": payment_entity["order_id"],
        "razorpay_payment_id": payment_entity["id"],
        "amount_paise": payment_entity["amount"],
        "currency": payment_entity["currency"],
        "status": "captured",
        "payment_method": payment_entity.get("method"),
        "error_reason": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }
    
    db.payments.insert_one(payment_doc)
    logger.info(f"[SUBSCRIPTION] Created payment for recurring charge: {payment_doc['payment_id']}")
    
    # === SEND PAYMENT CONFIRMATION EMAIL FOR RECURRING PAYMENT ===
    try:
        user = db.users.find_one({"user_id": subscription["user_id"]})
        if user and user.get("email"):
            notification_service = NotificationService(db)
            
            # Get updated subscription
            updated_subscription = db.subscriptions.find_one({
                "subscription_id": subscription["subscription_id"]
            })
            
            notification_service.send_payment_confirmation(
                user,
                payment_doc,
                updated_subscription
            )
            logger.info(f"[EMAIL] Renewal payment confirmation sent to: {user.get('email')}")
    except Exception as e:
        logger.error(f"[EMAIL] Failed to send renewal confirmation: {e}")

def _handle_subscription_halted(db, payload: dict):
    """
    Handle subscription.halted webhook event
    Payment failed for recurring subscription
    """
    logger.info("[SUBSCRIPTION] Handling subscription.halted event")
    
    subscription_entity = payload["payload"]["subscription"]["entity"]
    razorpay_subscription_id = subscription_entity["id"]
    payment_entity = payload["payload"]["payment"]["entity"]
    
    # Find subscription
    subscription = db.subscriptions.find_one({
        "razorpay_subscription_id": razorpay_subscription_id
    })
    
    if not subscription:
        logger.warning(f"[SUBSCRIPTION] Subscription not found: {razorpay_subscription_id}")
        return
    
    # Update subscription
    db.subscriptions.update_one(
        {"subscription_id": subscription["subscription_id"]},
        {"$set": {
            "last_payment_status": "failed",
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    # Create payment record as failed
    payment_doc = {
        "payment_id": f"pay_{uuid.uuid4().hex}",
        "user_id": subscription["user_id"],
        "subscription_id": subscription["subscription_id"],
        "razorpay_order_id": payment_entity["order_id"],
        "razorpay_payment_id": payment_entity["id"],
        "amount_paise": payment_entity["amount"],
        "currency": payment_entity["currency"],
        "status": "failed",
        "payment_method": payment_entity.get("method"),
        "error_reason": payment_entity.get("error_description", "Payment failed"),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }
    
    db.payments.insert_one(payment_doc)
    logger.warning(f"[SUBSCRIPTION] Recurring payment failed for user: {subscription['user_id']}")
    
def _handle_subscription_cancelled(db, payload: dict):
    """
    Handle subscription.cancelled webhook event
    User or system cancelled the subscription
    """
    logger.info("[SUBSCRIPTION] Handling subscription.cancelled event")
    
    subscription_entity = payload["payload"]["subscription"]["entity"]
    razorpay_subscription_id = subscription_entity["id"]
    
    # Find subscription in our DB
    subscription = db.subscriptions.find_one({
        "razorpay_subscription_id": razorpay_subscription_id
    })
    
    if not subscription:
        logger.warning(f"[SUBSCRIPTION] Subscription not found: {razorpay_subscription_id}")
        return
    
    # Update subscription status to cancelled
    db.subscriptions.update_one(
        {"subscription_id": subscription["subscription_id"]},
        {"$set": {
            "status": "cancelled",
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    logger.info(f"[SUBSCRIPTION] Subscription {subscription['subscription_id']} cancelled")


def _handle_subscription_pending(db, payload: dict):
    """
    Handle subscription.pending webhook event
    Subscription is pending (payment not completed yet)
    """
    logger.info("[SUBSCRIPTION] Handling subscription.pending event")
    
    subscription_entity = payload["payload"]["subscription"]["entity"]
    razorpay_subscription_id = subscription_entity["id"]
    
    # Find subscription in our DB
    subscription = db.subscriptions.find_one({
        "razorpay_subscription_id": razorpay_subscription_id
    })
    
    if not subscription:
        logger.warning(f"[SUBSCRIPTION] Subscription not found: {razorpay_subscription_id}")
        return
    
    # Update subscription status to pending
    db.subscriptions.update_one(
        {"subscription_id": subscription["subscription_id"]},
        {"$set": {
            "status": "pending",
            "last_payment_status": "pending",
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    logger.info(f"[SUBSCRIPTION] Subscription {subscription['subscription_id']} is pending")


def _handle_subscription_paused(db, payload: dict):
    """
    Handle subscription.paused webhook event (optional)
    Subscription was paused by user or system
    """
    logger.info("[SUBSCRIPTION] Handling subscription.paused event")
    
    subscription_entity = payload["payload"]["subscription"]["entity"]
    razorpay_subscription_id = subscription_entity["id"]
    
    # Find subscription in our DB
    subscription = db.subscriptions.find_one({
        "razorpay_subscription_id": razorpay_subscription_id
    })
    
    if not subscription:
        logger.warning(f"[SUBSCRIPTION] Subscription not found: {razorpay_subscription_id}")
        return
    
    # Update subscription status to paused (you may want to add this to schema)
    db.subscriptions.update_one(
        {"subscription_id": subscription["subscription_id"]},
        {"$set": {
            "status": "paused",  # Add 'paused' to your status enum if needed
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    logger.info(f"[SUBSCRIPTION] Subscription {subscription['subscription_id']} paused")
    
def _send_payment_confirmation_email(db, payment):
    """Send simple payment confirmation email"""
    try:
        user = db.users.find_one({"user_id": payment["user_id"]})
        if not user or not user.get("email"):
            return
        
        subscription = db.subscriptions.find_one({"user_id": payment["user_id"]})
        
        notification_service = NotificationService(db)
        notification_service.send_payment_confirmation(
            user,
            payment,
            subscription or {}
        )
        logger.info(f"[EMAIL] Payment confirmation sent to: {user.get('email')}")
    except Exception as e:
        logger.error(f"[EMAIL] Failed to send: {e}")