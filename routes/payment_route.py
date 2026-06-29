import json
import os
import logging
import sys
import uuid
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse

from services.payment_service import (
    create_order,
    verify_checkout_signature,
    verify_webhook_signature,
    handle_webhook_event
)
from schemas.schemas import (
    CreateOrderRequest,
    CreateOrderResponse,
    VerifyPaymentRequest,
    VerifyPaymentResponse
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/payment/create-order")
async def create_payment_order(body: CreateOrderRequest, request: Request):
    razorpay_client = request.app.state.razorpay
    redis_client = request.app.state.redis
    db = request.app.state.db

    logger.info(f"[CREATE ORDER] User: {body.user_id}, Plan: {body.plan_id}")

    result = create_order(
        razorpay_client=razorpay_client,
        redis_client=redis_client,
        db=db,
        user_id=body.user_id,
        plan_id=body.plan_id
    )

    if result.get("idempotent") and result.get("cached"):
        logger.info(f"[IDEMPOTENCY] Returning cached order for user: {body.user_id}")
    elif result.get("ok"):
        logger.info(f"[IDEMPOTENCY] New order created for user: {body.user_id}")

    if not result["ok"]:
        if result["error"] == "duplicate_request":
            raise HTTPException(status_code=409, detail=result["message"])
        if result["error"] == "plan_not_found":
            raise HTTPException(status_code=404, detail=result["message"])
        if result["error"] == "subscription_active":
            raise HTTPException(status_code=403, detail=result["message"])
        if result["error"] == "user_not_found":
            raise HTTPException(status_code=404, detail=result["message"])
        raise HTTPException(status_code=500, detail=result["message"])

    logger.info(f"[CREATE ORDER] Order created: {result['data']['order_id']}")
    
    return {
        "order_id": result["data"]["order_id"],
        "amount_paise": result["data"]["amount_paise"],
        "currency": result["data"]["currency"],
        "key_id": os.getenv("RAZORPAY_KEY_ID")
    }


@router.post("/payment/verify")
async def verify_payment(body: VerifyPaymentRequest, request: Request):
    db = request.app.state.db
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")

    logger.info(f"[VERIFY] Verifying payment for order: {body.razorpay_order_id}")

    result = verify_checkout_signature(
        db=db,
        key_secret=key_secret,
        razorpay_order_id=body.razorpay_order_id,
        razorpay_payment_id=body.razorpay_payment_id,
        razorpay_signature=body.razorpay_signature
    )

    if not result["ok"]:
        if result["error"] == "invalid_signature":
            raise HTTPException(status_code=400, detail=result["message"])
        if result["error"] == "payment_not_found":
            raise HTTPException(status_code=404, detail=result["message"])
        raise HTTPException(status_code=500, detail=result["message"])

    payment = db.payments.find_one({"razorpay_order_id": body.razorpay_order_id})
    logger.info(f"[VERIFY] Payment verified successfully for order: {body.razorpay_order_id}")
    
    return {
        "success": True,
        "payment_id": payment["payment_id"] if payment else "",
        "message": result["message"]
    }


@router.post("/razorpay/webhook")
async def razorpay_webhook(request: Request):
    raw_body = await request.body()
    
    logger.info("=" * 80)
    logger.info("[WEBHOOK] Webhook received!")
    
    try:
        data = json.loads(raw_body)
        event_type = data.get('event')
        
        logger.info(f"[WEBHOOK] Event: {event_type}")
        
        # Extract order and payment IDs
        order_id = None
        payment_id = None
        subscription_id = None
        
        if 'payment' in data.get('payload', {}):
            payment_entity = data.get('payload', {}).get('payment', {}).get('entity', {})
            payment_id = payment_entity.get('id')
            order_id = payment_entity.get('order_id')
        elif 'order' in data.get('payload', {}):
            order_entity = data.get('payload', {}).get('order', {}).get('entity', {})
            order_id = order_entity.get('id')
        
        if 'subscription' in data.get('payload', {}):
            subscription_entity = data.get('payload', {}).get('subscription', {}).get('entity', {})
            subscription_id = subscription_entity.get('id')
        
        # ✅ GENERATE COMPOSITE EVENT ID IN ROUTE
        # Use combination of event_type + payment_id + order_id
        if payment_id and order_id:
            event_id = f"composite_{event_type}_{payment_id}_{order_id}"
        elif subscription_id and event_type:
            event_id = f"composite_{event_type}_{subscription_id}"
        elif order_id:
            event_id = f"composite_{event_type}_{order_id}"
        else:
            # Fallback: use timestamp (not ideal but better than None)
            import time
            event_id = f"fallback_{event_type}_{int(time.time() * 1000)}"
            logger.warning(f"[WEBHOOK] ⚠️ Using fallback ID: {event_id}")
        
        logger.info(f"[WEBHOOK] Generated Event ID: {event_id}")
        logger.info(f"[WEBHOOK] Order ID: {order_id}")
        logger.info(f"[WEBHOOK] Payment ID: {payment_id}")
        logger.info(f"[WEBHOOK] Subscription ID: {subscription_id}")
        
        # ✅ Add the generated event_id to payload
        data['id'] = event_id
        
    except Exception as e:
        logger.error(f"[WEBHOOK] Failed to parse JSON: {e}")
        return {"status": "ignored", "reason": "invalid_json"}
    
    logger.info("=" * 80)
    
    # Signature verification
    signature = request.headers.get("X-Razorpay-Signature")
    if not signature:
        logger.warning("[WEBHOOK] Missing signature")
        return {"status": "ignored", "reason": "missing_signature"}

    webhook_secret = os.getenv("WEBHOOK_SECRET")
    is_valid = verify_webhook_signature(
        raw_body=raw_body,
        signature=signature,
        webhook_secret=webhook_secret
    )

    if not is_valid:
        logger.warning("[WEBHOOK] Invalid signature")
        return {"status": "ignored", "reason": "invalid_signature"}

    logger.info("[WEBHOOK] Signature verified successfully")

    db = request.app.state.db
    result = handle_webhook_event(
        db=db, 
        raw_body=raw_body, 
        payload=data  # ✅ Pass the modified payload with event_id
    )
    
    logger.info(f"[WEBHOOK] Handler result: {result}")
    logger.info("=" * 80)

    return {"status": "ok", "detail": result.get("message")}


@router.get("/payment/renew/{order_id}")
async def renew_payment(order_id: str, request: Request):
    """Handle renewal payment link click"""
    db = request.app.state.db
    
    payment = db.payments.find_one({"razorpay_order_id": order_id})
    if not payment:
        return HTMLResponse("""
        <html><body><h2>Payment not found</h2></body></html>
        """, status_code=404)
    
    if payment["status"] != "created":
        return HTMLResponse("""
        <html><body><h2>Payment already processed</h2></body></html>
        """, status_code=400)
    
    # Return HTML with auto-open checkout
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <script src="https://checkout.razorpay.com/v1/checkout.js"></script>
    </head>
    <body>
        <div style="text-align:center; margin-top:50px;">
            <h2>Processing your renewal...</h2>
            <p>Please wait, redirecting to payment...</p>
        </div>
        <script>
            const options = {{
                key: "{os.getenv('RAZORPAY_KEY_ID')}",
                amount: {payment["amount_paise"]},
                currency: "{payment["currency"]}",
                name: "RubixKube",
                description: "Renew Subscription",
                order_id: "{order_id}",
                theme: {{ color: "#7c3aed" }}
            }};
            const rzp = new Razorpay(options);
            rzp.open();
        </script>
    </body>
    </html>
    """)
    
@router.post("/debug/test-webhook")
async def debug_test_webhook(request: Request):
    """Debug endpoint to test webhooks without signature verification"""
    db = request.app.state.db
    data = await request.json()
    
    payload = data.get("payload")
    skip_signature = data.get("skip_signature", False)
    
    if not payload:
        return {"status": "error", "detail": "No payload provided"}
    
    # For testing, process directly
    from services.payment_service import handle_webhook_event
    import json
    
    raw_body = json.dumps(payload).encode('utf-8')
    
    # ✅ Add a test ID if missing (only for testing)
    if not payload.get("id") and skip_signature:
        logger.warning(f"[DEBUG] Webhook missing ID - will be rejected by handle_webhook_event")
        # Don't add ID here - let the service reject it
        # payload["id"] = f"test_{uuid.uuid4().hex[:16]}"
    
    result = handle_webhook_event(
        db=db,
        raw_body=raw_body,
        payload=payload
    )
    
    return {
        "status": "ok" if result.get("ok") else "error",
        "detail": result.get("message", "processed")
    }

@router.get("/debug/webhook-duplicates")
async def debug_webhook_duplicates(request: Request):
    """Check for duplicate webhook events"""
    db = request.app.state.db
    
    events = list(db.webhook_events.find({}).sort("created_at", -1))
    
    event_id_counts = {}
    for event in events:
        event_id = event.get("razorpay_event_id")
        if event_id:
            event_id_counts[event_id] = event_id_counts.get(event_id, 0) + 1
    
    duplicates = {k: v for k, v in event_id_counts.items() if v > 1}
    
    for event in events:
        event["_id"] = str(event["_id"])
    
    return {
        "total_events": len(events),
        "unique_event_ids": len(event_id_counts),
        "duplicates_found": len(duplicates) > 0,
        "duplicate_events": duplicates,
        "recent_events": events[:10]
    }

@router.delete("/debug/webhook-cleanup")
async def cleanup_webhook_events(request: Request):
    """Clean up test webhook events"""
    db = request.app.state.db
    
    result = db.webhook_events.delete_many({
        "razorpay_event_id": {"$regex": "^(evt_test_|composite_|test_)"}
    })
    
    return {
        "success": True,
        "deleted_count": result.deleted_count
    }

@router.get("/debug/webhook-logs")
async def get_webhook_logs(request: Request):
    """Get recent webhook logs"""
    db = request.app.state.db
    
    events = list(db.webhook_events.find({}).sort("created_at", -1).limit(20))
    
    for event in events:
        event["_id"] = str(event["_id"])
    
    return {
        "success": True,
        "events": events
    }