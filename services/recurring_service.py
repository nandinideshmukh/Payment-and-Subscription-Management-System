import logging
import uuid
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)


class RecurringService:
    """Handles subscription renewals, expiry, and recurring payments"""
    
    def __init__(self, db, razorpay_client, notification_service=None, redis_client=None):
        self.db = db
        self.razorpay_client = razorpay_client
        self.notification_service = notification_service
        self.redis_client = redis_client
        self.GRACE_PERIOD_DAYS = 7
    
    # ============================================================
    # FIND DUE SUBSCRIPTIONS
    # ============================================================
    
    def find_due_subscriptions(self) -> List[Dict[str, Any]]:
        """Find active subscriptions where next_billing_date <= now()"""
        now = datetime.now(timezone.utc)
        
        due_subs = list(self.db.subscriptions.find({
            "status": "active",
            "next_billing_date": {"$lte": now}
        }))
        
        logger.info(f"[RECURRING] Found {len(due_subs)} subscription(s) due for renewal")
        return due_subs
    
    def find_expiring_soon(self, days_before: int = 3) -> List[Dict[str, Any]]:
        """Find subscriptions that will expire in the next 'days_before' days"""
        now = datetime.now(timezone.utc)
        expiry_threshold = now + timedelta(days=days_before)
        
        expiring_subs = list(self.db.subscriptions.find({
            "status": "active",
            "end_date": {"$lte": expiry_threshold, "$gte": now}
        }))
        
        logger.info(f"[RECURRING] Found {len(expiring_subs)} subscription(s) expiring soon")
        return expiring_subs
    
    # ============================================================
    # PROCESS RENEWALS
    # ============================================================
    
    def process_renewal(self, subscription: Dict[str, Any]) -> Dict[str, Any]:
        """Process a single subscription renewal"""
        logger.info(f"[RENEWAL] Processing renewal for subscription: {subscription['subscription_id']}")
        
        # Get user
        user = self.db.users.find_one({"user_id": subscription["user_id"]})
        if not user:
            logger.error(f"[RENEWAL] User not found: {subscription['user_id']}")
            return {"success": False, "error": "User not found"}
        
        # Get plan
        plan = self.db.plans.find_one({"plan_id": subscription["plan_id"]})
        if not plan:
            logger.error(f"[RENEWAL] Plan not found: {subscription['plan_id']}")
            return {"success": False, "error": "Plan not found"}
        
        # Create Razorpay order for renewal
        try:
            receipt_id = f"renewal_{subscription['subscription_id'][:16]}_{uuid.uuid4().hex[:8]}"
            
            razorpay_order = self.razorpay_client.order.create({
                "amount": subscription["price_paise_at_purchase"],
                "currency": plan.get("currency", "INR"),
                "receipt": receipt_id,
                "payment_capture": 1,
                "notes": {
                    "subscription_id": subscription["subscription_id"],
                    "user_id": subscription["user_id"],
                    "type": "renewal"
                }
            })
            
            logger.info(f"[RENEWAL] Created renewal order: {razorpay_order['id']}")
            
        except Exception as e:
            logger.error(f"[RENEWAL] Failed to create renewal order: {e}")
            return {"success": False, "error": str(e)}
        
        # Create payment record for renewal
        payment_doc = {
            "payment_id": f"pay_{uuid.uuid4().hex}",
            "user_id": subscription["user_id"],
            "subscription_id": subscription["subscription_id"],
            "razorpay_order_id": razorpay_order["id"],
            "razorpay_payment_id": None,
            "amount_paise": subscription["price_paise_at_purchase"],
            "currency": plan.get("currency", "INR"),
            "status": "created",
            "payment_method": None,
            "error_reason": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc)
        }
        
        self.db.payments.insert_one(payment_doc)
        logger.info(f"[RENEWAL] Created renewal payment: {payment_doc['payment_id']}")
        
        # Update subscription status to pending
        self.db.subscriptions.update_one(
            {"subscription_id": subscription["subscription_id"]},
            {"$set": {
                "status": "pending",
                "updated_at": datetime.now(timezone.utc)
            }}
        )
        
        # Send notification
        if self.notification_service:
            try:
                payment_link = f"{os.getenv('APP_URL', 'http://localhost:8000')}/payment/renew/{razorpay_order['id']}"
                plan_name = plan.get("name", subscription["plan_id"])
                amount = subscription["price_paise_at_purchase"] / 100
                
                self.notification_service.send_renewal_payment_link(
                    user=user,
                    payment_link=payment_link,
                    amount=amount,
                    plan_name=plan_name
                )
                logger.info(f"[RENEWAL] Sent renewal payment link to {user.get('email')}")
            except Exception as e:
                logger.error(f"[RENEWAL] Failed to send notification: {e}")
        
        return {
            "success": True,
            "order_id": razorpay_order["id"],
            "payment_id": payment_doc["payment_id"],
            "amount_paise": subscription["price_paise_at_purchase"],
            "user": user
        }
    
    def process_all_renewals(self) -> Dict[str, Any]:
        """Process all due subscriptions for renewal"""
        due_subs = self.find_due_subscriptions()
        
        results = {
            "total_due": len(due_subs),
            "processed": 0,
            "failed": 0,
            "details": []
        }
        
        for sub in due_subs:
            result = self.process_renewal(sub)
            if result["success"]:
                results["processed"] += 1
                results["details"].append({
                    "subscription_id": sub["subscription_id"],
                    "user_id": sub["user_id"],
                    "order_id": result.get("order_id"),
                    "status": "success"
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "subscription_id": sub["subscription_id"],
                    "user_id": sub["user_id"],
                    "error": result.get("error"),
                    "status": "failed"
                })
        
        logger.info(f"[RECURRING] Renewal summary: {results['processed']} processed, {results['failed']} failed")
        return results
    
    # ============================================================
    # HANDLE SUCCESSFUL RENEWAL (Webhook)
    # ============================================================
    
    def handle_successful_renewal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle successful renewal payment from webhook"""
        logger.info("[RENEWAL] Handling successful renewal webhook")
        
        payment_entity = payload["payload"]["payment"]["entity"]
        razorpay_payment_id = payment_entity["id"]
        razorpay_order_id = payment_entity["order_id"]
        
        # Find the payment record
        payment = self.db.payments.find_one({"razorpay_order_id": razorpay_order_id})
        if not payment:
            logger.error(f"[RENEWAL] Payment not found for order: {razorpay_order_id}")
            return {"success": False, "error": "Payment not found"}
        
        # Check if this is a renewal payment
        if not payment.get("subscription_id"):
            logger.info(f"[RENEWAL] Payment {payment['payment_id']} is not a renewal, skipping")
            return {"success": True, "skipped": True}
        
        # Get subscription
        subscription = self.db.subscriptions.find_one({
            "subscription_id": payment["subscription_id"]
        })
        if not subscription:
            logger.error(f"[RENEWAL] Subscription not found: {payment['subscription_id']}")
            return {"success": False, "error": "Subscription not found"}
        
        # Calculate new end date
        now = datetime.now(timezone.utc)
        
        # Get period from plan
        plan = self.db.plans.find_one({"plan_id": subscription["plan_id"]})
        if plan:
            period = plan.get("period", "monthly")
            if period == "monthly":
                new_end_date = now + relativedelta(months=1)
            elif period == "yearly":
                new_end_date = now + relativedelta(years=1)
            else:
                new_end_date = now + relativedelta(months=1)
        else:
            new_end_date = now + timedelta(days=30)
        
        # Update payment status
        self.db.payments.update_one(
            {"payment_id": payment["payment_id"]},
            {"$set": {
                "razorpay_payment_id": razorpay_payment_id,
                "status": "captured",
                "payment_method": payment_entity.get("method"),
                "updated_at": now
            }}
        )
        
        # Update subscription
        self.db.subscriptions.update_one(
            {"subscription_id": subscription["subscription_id"]},
            {"$set": {
                "status": "active",
                "end_date": new_end_date,
                "next_billing_date": new_end_date,
                "last_payment_status": "success",
                "updated_at": now
            }}
        )
        
        # Send notification
        if self.notification_service:
            try:
                user = self.db.users.find_one({"user_id": subscription["user_id"]})
                if user:
                    payment = self.db.payments.find_one({"payment_id": payment["payment_id"]})
                    self.notification_service.send_payment_confirmation(
                        user=user,
                        payment=payment,
                        subscription=subscription
                    )
                    logger.info(f"[RENEWAL] Sent payment confirmation to {user.get('email')}")
            except Exception as e:
                logger.error(f"[RENEWAL] Failed to send notification: {e}")
        
        logger.info(f"[RENEWAL] Subscription {subscription['subscription_id']} renewed until {new_end_date}")
        
        return {
            "success": True,
            "subscription_id": subscription["subscription_id"],
            "new_end_date": new_end_date
        }
    
    # ============================================================
    # HANDLE FAILED RENEWAL
    # ============================================================
    
    def handle_failed_renewal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle failed renewal payment"""
        logger.info("[RENEWAL] Handling failed renewal webhook")
        
        payment_entity = payload["payload"]["payment"]["entity"]
        razorpay_payment_id = payment_entity["id"]
        razorpay_order_id = payment_entity["order_id"]
        error_reason = payment_entity.get("error_description") or payment_entity.get("error_code")
        
        # Find payment
        payment = self.db.payments.find_one({"razorpay_order_id": razorpay_order_id})
        if not payment:
            logger.error(f"[RENEWAL] Payment not found for order: {razorpay_order_id}")
            return {"success": False, "error": "Payment not found"}
        
        # Check if this is a renewal payment
        if not payment.get("subscription_id"):
            logger.info(f"[RENEWAL] Payment {payment['payment_id']} is not a renewal, skipping")
            return {"success": True, "skipped": True}
        
        # Get subscription
        subscription = self.db.subscriptions.find_one({
            "subscription_id": payment["subscription_id"]
        })
        if not subscription:
            logger.error(f"[RENEWAL] Subscription not found: {payment['subscription_id']}")
            return {"success": False, "error": "Subscription not found"}
        
        now = datetime.now(timezone.utc)
        
        # Update payment status
        self.db.payments.update_one(
            {"payment_id": payment["payment_id"]},
            {"$set": {
                "razorpay_payment_id": razorpay_payment_id,
                "status": "failed",
                "error_reason": error_reason,
                "updated_at": now
            }}
        )
        
        # Update subscription
        self.db.subscriptions.update_one(
            {"subscription_id": payment["subscription_id"]},
            {"$set": {
                "last_payment_status": "failed",
                "updated_at": now
            }}
        )
        
        # Send notification
        if self.notification_service:
            try:
                user = self.db.users.find_one({"user_id": subscription["user_id"]})
                if user:
                    self.notification_service.send_payment_failure_alert(
                        user=user,
                        payment=payment,
                        error_reason=error_reason
                    )
                    logger.info(f"[RENEWAL] Sent payment failure alert to {user.get('email')}")
            except Exception as e:
                logger.error(f"[RENEWAL] Failed to send notification: {e}")
        
        logger.warning(f"[RENEWAL] Payment failed for subscription: {payment['subscription_id']}")
        
        return {
            "success": True,
            "subscription_id": payment["subscription_id"],
            "error_reason": error_reason
        }
    
    # ============================================================
    # SUBSCRIPTION EXPIRY & GRACE PERIOD
    # ============================================================
    
    def check_and_expire_subscriptions(self) -> Dict[str, Any]:
        """Check for expired subscriptions and apply grace period"""
        now = datetime.now(timezone.utc)
        
        # Find subscriptions that are active but past end_date
        expired_subs = list(self.db.subscriptions.find({
            "status": {"$in": ["active", "pending"]},
            "end_date": {"$lt": now}
        }))
        
        results = {
            "total_expired": len(expired_subs),
            "expired": 0,
            "in_grace_period": 0,
            "details": []
        }
        
        for sub in expired_subs:
            # Ensure end_date is timezone-aware
            end_date = sub["end_date"]
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            
            days_past_due = (now - end_date).days
            
            # Check if within grace period
            if days_past_due <= self.GRACE_PERIOD_DAYS:
                # In grace period - allow renewal
                self.db.subscriptions.update_one(
                    {"subscription_id": sub["subscription_id"]},
                    {"$set": {
                        "status": "pending",
                        "last_payment_status": "pending",
                        "updated_at": now
                    }}
                )
                results["in_grace_period"] += 1
                results["details"].append({
                    "subscription_id": sub["subscription_id"],
                    "status": "grace_period",
                    "days_past_due": days_past_due,
                    "days_left": self.GRACE_PERIOD_DAYS - days_past_due
                })
                logger.info(f"[EXPIRY] Subscription {sub['subscription_id']} in grace period ({days_past_due} days past due)")
                
                # Send expiry reminder
                if self.notification_service:
                    try:
                        user = self.db.users.find_one({"user_id": sub["user_id"]})
                        if user:
                            days_left = self.GRACE_PERIOD_DAYS - days_past_due
                            self.notification_service.send_subscription_expiry_reminder(
                                user=user,
                                subscription=sub,
                                days_left=days_left
                            )
                            logger.info(f"[EXPIRY] Sent expiry reminder to {user.get('email')}")
                    except Exception as e:
                        logger.error(f"[EXPIRY] Failed to send notification: {e}")
            else:
                # Grace period passed - expire
                self.db.subscriptions.update_one(
                    {"subscription_id": sub["subscription_id"]},
                    {"$set": {
                        "status": "expired",
                        "updated_at": now
                    }}
                )
                results["expired"] += 1
                results["details"].append({
                    "subscription_id": sub["subscription_id"],
                    "status": "expired",
                    "days_past_due": days_past_due
                })
                logger.warning(f"[EXPIRY] Subscription {sub['subscription_id']} expired after {days_past_due} days past due")
        
        return results
    
    # ============================================================
    # NOTIFICATION HELPERS
    # ============================================================
    
    def get_notification_data(self, subscription: Dict[str, Any]) -> Dict[str, Any]:
        """Get data for sending notifications"""
        user = self.db.users.find_one({"user_id": subscription["user_id"]})
        plan = self.db.plans.find_one({"plan_id": subscription["plan_id"]})
        
        return {
            "user": user,
            "plan": plan,
            "subscription": subscription,
            "amount": subscription["price_paise_at_purchase"] / 100
        }