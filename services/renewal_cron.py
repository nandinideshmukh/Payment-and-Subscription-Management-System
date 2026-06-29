import logging
import os
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class SchedulerService:
    """Background scheduler for recurring tasks"""
    
    def __init__(self, db, razorpay_client, notification_service=None):
        self.db = db
        self.razorpay_client = razorpay_client
        self.notification_service = notification_service
        self.scheduler = None
        self.is_running = False
        
        # Config from environment
        self.enabled = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
        self.renewal_hour = int(os.getenv("SCHEDULER_RENEWAL_HOUR", "0"))
        self.expiry_interval = int(os.getenv("SCHEDULER_EXPIRY_INTERVAL", "6"))
        self.reminder_hour = int(os.getenv("SCHEDULER_REMINDER_HOUR", "10"))
        self.run_on_startup = os.getenv("SCHEDULER_RUN_ON_STARTUP", "true").lower() == "true"
        self.startup_delay = int(os.getenv("SCHEDULER_STARTUP_DELAY", "30"))
        
        logger.info(f"[SCHEDULER] Enabled: {self.enabled}")
        logger.info(f"[SCHEDULER] Renewal hour: {self.renewal_hour}:00")
        logger.info(f"[SCHEDULER] Expiry interval: every {self.expiry_interval} hours")
        logger.info(f"[SCHEDULER] Reminder hour: {self.reminder_hour}:00")
    
    def start(self):
        """Start the scheduler"""
        if not self.enabled:
            logger.info("[SCHEDULER] Scheduler is disabled. Set ENABLE_SCHEDULER=true to enable.")
            return
        
        if self.scheduler and self.scheduler.running:
            logger.warning("[SCHEDULER] Scheduler already running")
            return
        
        logger.info("[SCHEDULER] Starting scheduler...")
        self.scheduler = BackgroundScheduler()
        
        # 1. Daily renewal processing at midnight
        self.scheduler.add_job(
            func=self._run_daily_renewals,
            trigger=CronTrigger(hour=self.renewal_hour, minute=0),
            id="daily_renewals",
            replace_existing=True,
            max_instances=1
        )
        logger.info(f"[SCHEDULER] Added daily renewal job ({self.renewal_hour}:00)")
        
        # 2. Check expired subscriptions every N hours
        self.scheduler.add_job(
            func=self._run_expiry_check,
            trigger=IntervalTrigger(hours=self.expiry_interval),
            id="expiry_check",
            replace_existing=True,
            max_instances=1
        )
        logger.info(f"[SCHEDULER] Added expiry check job (every {self.expiry_interval} hours)")
        
        # 3. Send expiry reminders daily at reminder_hour
        self.scheduler.add_job(
            func=self._run_expiry_reminders,
            trigger=CronTrigger(hour=self.reminder_hour, minute=0),
            id="expiry_reminders",
            replace_existing=True,
            max_instances=1
        )
        logger.info(f"[SCHEDULER] Added expiry reminder job ({self.reminder_hour}:00)")
        
        # 4. Run on startup (after delay)
        if self.run_on_startup:
            run_time = datetime.now(timezone.utc) + timedelta(seconds=self.startup_delay)
            self.scheduler.add_job(
                func=self._run_startup_tasks,
                trigger='date',
                run_date=run_time,
                id="startup_tasks",
                replace_existing=True,
                max_instances=1
            )
            logger.info(f"[SCHEDULER] Added startup task (in {self.startup_delay} seconds)")
        
        # Start the scheduler
        self.scheduler.start()
        self.is_running = True
        logger.info("[SCHEDULER] Scheduler started successfully")
    
    def shutdown(self):
        """Shutdown the scheduler"""
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown()
            self.is_running = False
            logger.info("[SCHEDULER] Scheduler shutdown")
    
    def get_jobs(self):
        """Get list of scheduled jobs"""
        if not self.scheduler or not self.scheduler.running:
            return []
        return self.scheduler.get_jobs()
    
    def is_running(self):
        """Check if scheduler is running"""
        return self.is_running
    
    # ============================================================
    # JOB EXECUTION METHODS
    # ============================================================
    
    def _run_daily_renewals(self):
        """Daily renewal processing"""
        from services.recurring_service import RecurringService
        
        try:
            logger.info("[SCHEDULER] Starting daily renewal processing")
            recurring_service = RecurringService(
                self.db, 
                self.razorpay_client, 
                self.notification_service
            )
            result = recurring_service.process_all_renewals()
            
            logger.info(f"[SCHEDULER] Renewal summary: {result['processed']} processed, {result['failed']} failed")
            
            # Log failures
            if result['failed'] > 0:
                logger.warning(f"[SCHEDULER] {result['failed']} renewals failed!")
                for detail in result['details']:
                    if detail['status'] == 'failed':
                        logger.warning(f"[SCHEDULER] Failed: {detail['subscription_id']} - {detail.get('error', 'Unknown error')}")
            
        except Exception as e:
            logger.error(f"[SCHEDULER] Error in daily renewals: {e}")
            import traceback
            traceback.print_exc()
    
    def _run_expiry_check(self):
        """Check and expire subscriptions past grace period"""
        from services.recurring_service import RecurringService
        
        try:
            logger.info("[SCHEDULER] Checking expired subscriptions")
            recurring_service = RecurringService(
                self.db, 
                self.razorpay_client, 
                self.notification_service
            )
            result = recurring_service.check_and_expire_subscriptions()
            
            if result['expired'] > 0:
                logger.info(f"[SCHEDULER] Expired {result['expired']} subscriptions")
                for detail in result['details']:
                    if detail['status'] == 'expired':
                        logger.info(f"[SCHEDULER] Expired: {detail['subscription_id']} ({detail['days_past_due']} days past due)")
            
            if result['in_grace_period'] > 0:
                logger.info(f"[SCHEDULER] {result['in_grace_period']} subscriptions in grace period")
                for detail in result['details']:
                    if detail['status'] == 'grace_period':
                        logger.info(f"[SCHEDULER] Grace period: {detail['subscription_id']} ({detail['days_left']} days left)")
                
        except Exception as e:
            logger.error(f"[SCHEDULER] Error checking expiry: {e}")
            import traceback
            traceback.print_exc()
    
    def _run_expiry_reminders(self):
        """Send expiry reminders to expiring subscriptions"""
        from services.recurring_service import RecurringService
        
        try:
            logger.info("[SCHEDULER] Sending expiry reminders")
            recurring_service = RecurringService(
                self.db, 
                self.razorpay_client, 
                self.notification_service
            )
            expiring_subs = recurring_service.find_expiring_soon(days_before=3)
            
            sent_count = 0
            for sub in expiring_subs:
                user = self.db.users.find_one({"user_id": sub["user_id"]})
                if user and user.get("email") and self.notification_service:
                    days_left = (sub["end_date"] - datetime.now(timezone.utc)).days
                    self.notification_service.send_subscription_expiry_reminder(
                        user=user, 
                        subscription=sub, 
                        days_left=days_left
                    )
                    sent_count += 1
            
            logger.info(f"[SCHEDULER] Sent {sent_count} expiry reminders")
            
        except Exception as e:
            logger.error(f"[SCHEDULER] Error sending reminders: {e}")
            import traceback
            traceback.print_exc()
    
    def _run_startup_tasks(self):
        """Run tasks on startup"""
        logger.info("[SCHEDULER] Running startup tasks")
        
        # Run renewal check on startup
        self._run_daily_renewals()
        
        # Run expiry check on startup
        self._run_expiry_check()
        
        # Run expiry reminders on startup (if within 3 days)
        self._run_expiry_reminders()
    
    # ============================================================
    # MANUAL TRIGGER METHODS (for admin/testing)
    # ============================================================
    
    def trigger_renewals(self):
        """Manually trigger renewal processing"""
        logger.info("[SCHEDULER] Manual trigger: Renewals")
        return self._run_daily_renewals()
    
    def trigger_expiry_check(self):
        """Manually trigger expiry check"""
        logger.info("[SCHEDULER] Manual trigger: Expiry check")
        return self._run_expiry_check()
    
    def trigger_reminders(self):
        """Manually trigger expiry reminders"""
        logger.info("[SCHEDULER] Manual trigger: Expiry reminders")
        return self._run_expiry_reminders()