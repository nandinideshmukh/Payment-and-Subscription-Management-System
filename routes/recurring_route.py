import logging
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException
from datetime import datetime, timezone, timedelta
import uuid
from services.payment_notification_service import NotificationService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/recurring", tags=["Recurring"])


# ============================================================
# SUBSCRIPTION MANAGEMENT
# ============================================================

@router.get("/subscription/{user_id}")
async def get_subscription(user_id: str, request: Request):
    """Get user's subscription details"""
    db = request.app.state.db
    
    subscription = db.subscriptions.find_one({"user_id": user_id})
    if not subscription:
        raise HTTPException(status_code=404, detail="No subscription found for this user")
    
    # Convert ObjectId to string for JSON response
    subscription["_id"] = str(subscription["_id"])
    
    return {
        "success": True,
        "subscription": subscription
    }


@router.post("/subscription/cancel/{subscription_id}")
async def cancel_subscription(
    subscription_id: str, 
    request: Request, 
    background_tasks: BackgroundTasks
):
    """
    Cancel a subscription (sets status to cancelled) and send confirmation email
    """
    db = request.app.state.db
    
    try:
        # Get subscription
        subscription = db.subscriptions.find_one({"subscription_id": subscription_id})
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        
        # Check if already cancelled
        if subscription.get("status") == "cancelled":
            return {
                "success": True,
                "message": "Subscription already cancelled",
                "subscription_id": subscription_id
            }
        
        # Get user details
        user = db.users.find_one({"user_id": subscription.get("user_id")})
        
        # Update status to cancelled
        db.subscriptions.update_one(
            {"subscription_id": subscription_id},
            {"$set": {
                "status": "cancelled",
                "cancelled_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }}
        )
        
        # Get updated subscription
        updated_subscription = db.subscriptions.find_one({"subscription_id": subscription_id})
        
        # Send cancellation email (if user exists)
        if user and user.get("email"):
            try:
                # Initialize notification service
                notification_service = NotificationService(db)
                
                # Send email in background
                background_tasks.add_task(
                    notification_service.send_subscription_cancelled,
                    user,
                    updated_subscription
                )
                logger.info(f"[CANCELLATION] Email queued for: {user.get('email')}")
                email_status = "queued"
            except Exception as e:
                logger.error(f"[CANCELLATION] Failed to queue email: {e}")
                email_status = "failed"
        else:
            logger.warning(f"[CANCELLATION] No user/email for subscription: {subscription_id}")
            email_status = "skipped_no_email"
        
        logger.info(f"[SUBSCRIPTION] Cancelled subscription: {subscription_id}")
        
        return {
            "success": True,
            "message": "Subscription cancelled successfully",
            "subscription_id": subscription_id,
            "email_notification": email_status
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CANCELLATION] Error cancelling subscription: {e}")
        raise HTTPException(status_code=500, detail=f"Cancellation failed: {str(e)}")


@router.post("/subscription/retry-payment/{payment_id}")
async def retry_payment(payment_id: str, request: Request):
    """
    Retry a failed payment
    """
    db = request.app.state.db
    razorpay_client = request.app.state.razorpay
    
    payment = db.payments.find_one({"payment_id": payment_id})
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    if payment["status"] != "failed":
        raise HTTPException(status_code=400, detail="Only failed payments can be retried")
    
    # Create a new order for the same amount
    try:
        receipt_id = f"retry_{payment_id[:16]}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        new_order = razorpay_client.order.create({
            "amount": payment["amount_paise"],
            "currency": payment["currency"],
            "receipt": receipt_id,
            "payment_capture": 1,
            "notes": {
                "original_payment_id": payment_id,
                "user_id": payment["user_id"],
                "type": "retry"
            }
        })
        
        # Create new payment record for retry
        new_payment_doc = {
            "payment_id": f"pay_{uuid.uuid4().hex}",
            "user_id": payment["user_id"],
            "subscription_id": payment.get("subscription_id"),
            "razorpay_order_id": new_order["id"],
            "razorpay_payment_id": None,
            "amount_paise": payment["amount_paise"],
            "currency": payment["currency"],
            "status": "created",
            "payment_method": None,
            "error_reason": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc)
        }
        
        db.payments.insert_one(new_payment_doc)
        
        logger.info(f"[RETRY] Created retry order for payment: {payment_id}")
        
        return {
            "success": True,
            "order_id": new_order["id"],
            "payment_id": new_payment_doc["payment_id"],
            "amount_paise": payment["amount_paise"],
            "currency": payment["currency"]
        }
        
    except Exception as e:
        logger.error(f"[RETRY] Failed to create retry order: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to create retry: {str(e)}")


# ============================================================
# DUE SUBSCRIPTIONS (For admin/monitoring)
# ============================================================

@router.post("/process-renewals")
async def process_renewals(request: Request):
    """Process all due subscriptions for renewal"""
    db = request.app.state.db
    razorpay_client = request.app.state.razorpay
    notification_service = request.app.state.notification_service
    
    # Create a fresh instance with notification service
    from services.recurring_service import RecurringService
    recurring_service = RecurringService(
        db=db,
        razorpay_client=razorpay_client,
        notification_service=notification_service
    )
    
    result = recurring_service.process_all_renewals()
    
    return {
        "success": True,
        "message": f"Processed {result['processed']} renewals, {result['failed']} failed",
        "details": result
    }


@router.post("/check-expiry")
async def check_expiry(request: Request):
    """Check and expire subscriptions"""
    db = request.app.state.db
    razorpay_client = request.app.state.razorpay
    notification_service = request.app.state.notification_service
    
    from services.recurring_service import RecurringService
    recurring_service = RecurringService(
        db=db,
        razorpay_client=razorpay_client,
        notification_service=notification_service
    )
    
    cutoff_time = datetime.now(timezone.utc)
    result = recurring_service.check_and_expire_subscriptions()

    if notification_service:
        try:
            expired_subs = list(db.subscriptions.find({
                "status": "expired",
                "updated_at": {"$gte": cutoff_time}
            }))
            for sub in expired_subs:
                user = db.users.find_one({"user_id": sub.get("user_id")})
                if user and user.get("email"):
                    notification_service.send_subscription_expired(user=user, subscription=sub)
                    logger.info(f"[EXPIRY] Sent expiry email to {user.get('email')}")
        except Exception as e:
            logger.error(f"[EXPIRY] Failed to send expiry notification: {e}")
    
    return {
        "success": True,
        "message": f"Expired {result['expired']} subscriptions, {result['in_grace_period']} in grace period",
        "details": result
    }


@router.get("/due-subscriptions")
async def get_due_subscriptions(request: Request):
    """Get all due subscriptions"""
    db = request.app.state.db
    razorpay_client = request.app.state.razorpay
    notification_service = request.app.state.notification_service
    
    from services.recurring_service import RecurringService
    recurring_service = RecurringService(
        db=db,
        razorpay_client=razorpay_client,
        notification_service=notification_service
    )
    
    due_subs = recurring_service.find_due_subscriptions()
    
    for sub in due_subs:
        sub["_id"] = str(sub["_id"])
    
    return {
        "success": True,
        "count": len(due_subs),
        "subscriptions": due_subs
    }


@router.get("/expiring-soon")
async def get_expiring_soon(request: Request, days: int = 3):
    """Get subscriptions expiring soon"""
    db = request.app.state.db
    razorpay_client = request.app.state.razorpay
    notification_service = request.app.state.notification_service
    
    from services.recurring_service import RecurringService
    recurring_service = RecurringService(
        db=db,
        razorpay_client=razorpay_client,
        notification_service=notification_service
    )
    
    expiring_subs = recurring_service.find_expiring_soon(days)
    
    for sub in expiring_subs:
        sub["_id"] = str(sub["_id"])
    
    return {
        "success": True,
        "count": len(expiring_subs),
        "subscriptions": expiring_subs
    }


# ============================================================
# DEBUG ENDPOINTS (For testing)
# ============================================================

@router.get("/debug/subscription/{user_id}")
async def debug_subscription(user_id: str, request: Request):
    """Debug subscription details"""
    db = request.app.state.db
    
    subscription = db.subscriptions.find_one({"user_id": user_id})
    if not subscription:
        return {"error": "No subscription found"}
    
    subscription["_id"] = str(subscription["_id"])
    
    now = datetime.now(timezone.utc)
    
    return {
        "subscription": {
            "subscription_id": subscription.get("subscription_id"),
            "user_id": subscription.get("user_id"),
            "status": subscription.get("status"),
            "plan_id": subscription.get("plan_id"),
            "start_date": subscription.get("start_date"),
            "end_date": subscription.get("end_date"),
            "next_billing_date": subscription.get("next_billing_date"),
            "last_payment_status": subscription.get("last_payment_status"),
            "price_paise_at_purchase": subscription.get("price_paise_at_purchase")
        },
        "current_time": now,
        "is_due": subscription.get("next_billing_date") and subscription.get("next_billing_date") <= now if subscription.get("next_billing_date") else False,
        "has_next_billing_date": "next_billing_date" in subscription
    }


@router.post("/manual-renew/{user_id}")
async def manual_renew(user_id: str, request: Request):
    """Manually trigger renewal for testing"""
    db = request.app.state.db
    razorpay_client = request.app.state.razorpay
    notification_service = request.app.state.notification_service
    
    from services.recurring_service import RecurringService
    
    # Get subscription
    subscription = db.subscriptions.find_one({"user_id": user_id})
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    
    # Force next_billing_date to yesterday to trigger renewal
    now = datetime.now(timezone.utc)
    db.subscriptions.update_one(
        {"user_id": user_id},
        {"$set": {
            "next_billing_date": now - timedelta(days=1),
            "updated_at": now
        }}
    )
    
    # Get updated subscription
    updated_sub = db.subscriptions.find_one({"user_id": user_id})
    
    # Process renewal
    recurring_service = RecurringService(
        db=db,
        razorpay_client=razorpay_client,
        notification_service=notification_service
    )
    
    result = recurring_service.process_renewal(updated_sub)
    
    return {
        "success": True,
        "message": "Renewal triggered",
        "result": result,
        "subscription": {
            "subscription_id": updated_sub.get("subscription_id"),
            "next_billing_date": updated_sub.get("next_billing_date"),
            "status": updated_sub.get("status")
        }
    }


@router.post("/fix-subscription/{user_id}")
async def fix_subscription(user_id: str, request: Request):
    """Fix subscription by setting next_billing_date"""
    db = request.app.state.db
    
    subscription = db.subscriptions.find_one({"user_id": user_id})
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    
    now = datetime.now(timezone.utc)
    
    # Set next_billing_date if not set
    if not subscription.get("next_billing_date"):
        db.subscriptions.update_one(
            {"user_id": user_id},
            {"$set": {
                "next_billing_date": now,
                "updated_at": now
            }}
        )
        return {
            "success": True,
            "message": "next_billing_date set to now",
            "subscription_id": subscription.get("subscription_id")
        }
    else:
        return {
            "success": True,
            "message": "next_billing_date already exists",
            "next_billing_date": subscription.get("next_billing_date")
        }