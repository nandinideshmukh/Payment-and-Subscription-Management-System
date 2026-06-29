# ============================================================
# SERVER — entry point
# Run: uvicorn server:app --reload
# ============================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.payment_route import router as payment_router
from routes.recurring_route import router as recurring_router
from routes.cron_route import router as scheduler_router
from core.db import connect_to_mongodb,connect_to_redis,connect_to_razorpay
from services.recurring_service import RecurringService
from services.payment_notification_service import NotificationService
import logging,sys
from validators.payment_validators import validate_env
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


app = FastAPI(
    title="Billing System — Manual Recurring",
    description="Subscription billing with manual recurring logic (no DB, for testing)",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    app.state.db = connect_to_mongodb()
    app.state.redis = connect_to_redis()
    app.state.razorpay = connect_to_razorpay()
    validate_env()
    
    notification_service = NotificationService(app.state.db)
    # Set test mode to true for testing (logs instead of sending)
    notification_service.test_mode = False
    app.state.notification_service = notification_service

    # Initialize recurring service with notification service
    app.state.recurring_service = RecurringService(
        db=app.state.db,
        razorpay_client=app.state.razorpay,
        notification_service=notification_service,
        redis_client=app.state.redis
    )
    
    from services.renewal_cron import SchedulerService
    scheduler = SchedulerService(
        db=app.state.db,
        razorpay_client=app.state.razorpay,
        notification_service=notification_service
    )
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info("Scheduler initialized and started")
    
    logger.info("Idempotency keys will auto-expire via Redis TTL")
    


@app.get("/", tags=["Health"])
def health():
    return {"status": "healthy", "service": "payment-service"}



app.include_router(
    payment_router,
    tags=["Payments"]
)

app.include_router(
    recurring_router,
    tags=["Recurring"]
)

app.include_router(scheduler_router, tags=["Scheduler"])


@app.on_event("shutdown")
async def shutdown():
    # Shutdown scheduler
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler:
        scheduler.shutdown()
        logger.info("Scheduler shutdown")
