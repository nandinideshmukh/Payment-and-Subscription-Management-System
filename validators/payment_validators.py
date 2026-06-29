import os
from dotenv import load_dotenv

load_dotenv()


def validate_env():
    required_vars = [
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_USERNAME",
        "REDIS_PASSWORD",
        "WEBHOOK_SECRET",
        "MONGODB_URL",
        "DATABASE_NAME",
        "ENABLE_SCHEDULER",
        "SCHEDULER_RENEWAL_HOUR",
        "SCHEDULER_EXPIRY_INTERVAL",
        "SCHEDULER_REMINDER_HOUR",
        "SCHEDULER_RUN_ON_STARTUP",
        "SCHEDULER_STARTUP_DELAY",
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "FROM_EMAIL",
        "NOTIFICATION_TEST_MODE",
        "APP_URL",
    ]

    missing = []

    for var in required_vars:
        value = os.getenv(var)
        if not value:
            missing.append(var)

    if missing:
        raise ValueError(
            f"Missing environment variables: {', '.join(missing)}"
        )

    print("✓ Environment Variables Loaded")
    
