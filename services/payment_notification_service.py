import logging
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class NotificationService:
    """Handles email and SMS notifications with test mode support"""
    
    def __init__(self, db):
        self.db = db
        
        # Email config (from environment)
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.from_email = os.getenv("FROM_EMAIL", "noreply@billing.com")
        
        # Test mode - log instead of sending real emails
        self.test_mode = False
        # self.test_mode = os.getenv("NOTIFICATION_TEST_MODE", "true").lower() == "true"
        logger.info(f"[NOTIFICATION] Test mode: {self.test_mode}")
        
        
    
    # ============================================================
    # EMAIL NOTIFICATIONS (with test mode)
    # ============================================================
    
    def send_email(self, to_email: str, subject: str, html_body: str, text_body: str = None) -> bool:
        """Send an email (or log if in test mode)"""
        
        # Test mode - just log
        if self.test_mode:
            logger.info("=" * 70)
            logger.info("[TEST EMAIL] ==============================================")
            logger.info(f"[TEST EMAIL] To: {to_email}")
            logger.info(f"[TEST EMAIL] Subject: {subject}")
            logger.info(f"[TEST EMAIL] HTML Body: {html_body[:500]}...")
            if text_body:
                logger.info(f"[TEST EMAIL] Text Body: {text_body[:500]}...")
            logger.info("[TEST EMAIL] ==============================================")
            return True
        
        # Real email sending
        try:
            if not self.smtp_user or not self.smtp_password:
                logger.warning("[EMAIL] SMTP not configured, skipping email send")
                return False
            
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.from_email
            msg["To"] = to_email
            
            # Plain text version
            if text_body:
                part_text = MIMEText(text_body, "plain")
                msg.attach(part_text)
            
            # HTML version
            part_html = MIMEText(html_body, "html")
            msg.attach(part_html)
            
            # Send email
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.from_email, to_email, msg.as_string())
            
            logger.info(f"[EMAIL] Sent to {to_email}: {subject}")
            return True
            
        except Exception as e:
            logger.error(f"[EMAIL] Failed to send email: {e}")
            return False
    
    # ============================================================
    # PAYMENT NOTIFICATIONS
    # ============================================================
    
    def send_payment_confirmation(self, user: Dict[str, Any], payment: Dict[str, Any], subscription: Dict[str, Any]) -> bool:
        """Send payment confirmation email"""
        subject = f"Payment Confirmed - {subscription.get('plan_id', 'Plan')} Subscription"
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #22c55e;">✅ Payment Confirmed!</h2>
            <p>Hi {user.get('name', 'User')},</p>
            <p>Your payment of <strong>₹{payment['amount_paise']/100}</strong> has been confirmed.</p>
            <p>Your subscription is now active and will auto-renew.</p>
            <hr style="border: 1px solid #eee; margin: 20px 0;">
            <p><strong>Plan:</strong> {subscription.get('plan_id', 'N/A')}</p>
            <p><strong>Valid until:</strong> {subscription.get('end_date', 'N/A')}</p>
            <hr style="border: 1px solid #eee; margin: 20px 0;">
            <p style="color: #666; font-size: 14px;">Thanks for being a valued customer!</p>
        </body>
        </html>
        """
        
        text_body = f"""
        Payment Confirmed!
        
        Hi {user.get('name', 'User')},
        
        Your payment of ₹{payment['amount_paise']/100} has been confirmed.
        Your subscription is now active and will auto-renew.
        
        Plan: {subscription.get('plan_id', 'N/A')}
        Valid until: {subscription.get('end_date', 'N/A')}
        
        Thanks for being a valued customer!
        """
        
        return self.send_email(user["email"], subject, html_body, text_body)
    
    def send_payment_failure_alert(self, user: Dict[str, Any], payment: Dict[str, Any], error_reason: str) -> bool:
        """Send payment failure alert email"""
        subject = "⚠️ Payment Failed - Action Required"
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #ef4444;">⚠️ Payment Failed</h2>
            <p>Hi {user.get('name', 'User')},</p>
            <p>Your payment of <strong>₹{payment['amount_paise']/100}</strong> has failed.</p>
            <p><strong>Reason:</strong> {error_reason or 'Unknown error'}</p>
            <p>Please update your payment method or contact support to avoid service interruption.</p>
            <br/>
            <p><a href="{os.getenv('APP_URL', 'http://localhost:8000')}/payment/retry/{payment['payment_id']}" 
                  style="background: #6c47ff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; display: inline-block;">
                Retry Payment
            </a></p>
            <br/>
            <p style="color: #666; font-size: 14px;">If you need help, please contact our support team.</p>
        </body>
        </html>
        """
        
        return self.send_email(user["email"], subject, html_body)
    
    def send_subscription_expiry_reminder(self, user: Dict[str, Any], subscription: Dict[str, Any], days_left: int) -> bool:
        """Send subscription expiry reminder"""
        subject = f"⏰ Your subscription expires in {days_left} days"
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #f59e0b;">⏰ Subscription Expiring Soon</h2>
            <p>Hi {user.get('name', 'User')},</p>
            <p>Your subscription will expire in <strong>{days_left} days</strong>.</p>
            <p><strong>Plan:</strong> {subscription.get('plan_id', 'N/A')}</p>
            <p><strong>Expires on:</strong> {subscription.get('end_date', 'N/A')}</p>
            <br/>
            <p>To continue enjoying our service, please ensure your payment method is up to date.</p>
            <p><a href="{os.getenv('APP_URL', 'http://localhost:8000')}/subscription/renew/{subscription['subscription_id']}"
                  style="background: #6c47ff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; display: inline-block;">
                Renew Now
            </a></p>
            <br/>
            <p style="color: #666; font-size: 14px;">If you have any issues, please contact our support team.</p>
        </body>
        </html>
        """
        
        return self.send_email(user["email"], subject, html_body)
    
    def send_renewal_payment_link(self, user: Dict[str, Any], payment_link: str, amount: float, plan_name: str) -> bool:
        """Send payment link for renewal"""
        subject = f"🔄 Renew Your {plan_name} Subscription"
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #6c47ff;">🔄 Time to Renew Your Subscription</h2>
            <p>Hi {user.get('name', 'User')},</p>
            <p>Your subscription is due for renewal.</p>
            <p><strong>Plan:</strong> {plan_name}</p>
            <p><strong>Amount:</strong> ₹{amount}</p>
            <br/>
            <p><a href="{payment_link}" 
                  style="background: #6c47ff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 8px; display: inline-block;">
                Pay Now
            </a></p>
            <br/>
            <p style="color: #666; font-size: 14px;">If you have any issues, please contact our support team.</p>
        </body>
        </html>
        """
        
        return self.send_email(user["email"], subject, html_body)
    
    def send_subscription_expired(self, user: Dict[str, Any], subscription: Dict[str, Any]) -> bool:
        """Send subscription expiry notification email"""
        subject = "Subscription Expired"
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #ef4444;">Subscription Expired</h2>
            <p>Hi {user.get('name', 'User')},</p>
            <p>Your subscription has expired.</p>
            <p><strong>Plan:</strong> {subscription.get('plan_id', 'N/A')}</p>
            <p><strong>Expired on:</strong> {subscription.get('end_date', 'N/A')}</p>
            <br/>
            <p>Please renew your subscription to continue enjoying our service.</p>
            <p style="color: #666; font-size: 14px;">If you have any questions, please contact our support team.</p>
        </body>
        </html>
        """
        
        return self.send_email(user["email"], subject, html_body)
    
    def send_subscription_cancelled(self, user: Dict[str, Any], subscription: Dict[str, Any]) -> bool:
        """Send subscription cancellation confirmation"""
        subject = "Subscription Cancelled"
        
        html_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #ef4444;">Subscription Cancelled</h2>
            <p>Hi {user.get('name', 'User')},</p>
            <p>Your subscription has been cancelled as requested.</p>
            <p><strong>Plan:</strong> {subscription.get('plan_id', 'N/A')}</p>
            <p><strong>Active until:</strong> {subscription.get('end_date', 'N/A')}</p>
            <br/>
            <p>You will still have access until the end of your current billing period.</p>
            <p style="color: #666; font-size: 14px;">We hope to see you again soon!</p>
        </body>
        </html>
        """
        
        return self.send_email(user["email"], subject, html_body)
    
    