# Interview Questions: Payment & Subscription Management System

This document contains interview questions related to **Redis**, **Webhooks**, and the overall architecture of the Payment and Subscription Management System.

---

## Table of Contents
1. [Redis Questions](#redis-questions)
2. [Webhooks Questions](#webhooks-questions)
3. [Architecture & System Design](#architecture--system-design)
4. [API Integration (Razorpay)](#api-integration-razorpay)
5. [Security & Best Practices](#security--best-practices)

---

## Redis Questions

### Q1: What is Redis and why is it used in this payment system?

**Answer:**
Redis is an in-memory data structure store used as a database, cache, and message broker. In the Payment and Subscription Management System, Redis is used for:

- **Session Management**: Storing user session data with fast retrieval
- **Caching**: Reducing database load by caching subscription plans, user data, and recurring payment schedules
- **Rate Limiting**: Implementing API rate limiting for payment endpoints
- **Job Queue**: Managing background tasks like sending recurring payment reminders
- **Temporary Data Storage**: Storing payment request states and webhook acknowledgments
- **Real-time Notifications**: Publishing events for subscription status changes

**Example Use Case:**
```python
import redis

# Initialize Redis connection
cache = redis.Redis(host='localhost', port=6379, db=0)

# Cache subscription plan
cache.setex('plan:premium', 3600, json.dumps(premium_plan_data))

# Retrieve cached data
cached_plan = cache.get('plan:premium')
```

---

### Q2: Explain the differences between Redis and traditional databases like MongoDB. When would you use each?

**Answer:**

| Aspect | Redis | MongoDB |
|--------|-------|---------|
| **Storage** | In-memory (volatile) | Disk-based (persistent) |
| **Speed** | Extremely fast (microseconds) | Slower (milliseconds) |
| **Use Case** | Caching, sessions, real-time operations | Long-term data persistence |
| **Data Durability** | Requires persistence mechanism (RDB/AOF) | Built-in persistence |
| **Scalability** | Single instance or cluster | Better for large datasets |

**In this system:**
- **MongoDB**: Stores persistent data like user profiles, subscription history, payment records
- **Redis**: Caches frequently accessed data, manages session tokens, tracks webhook retries

```python
# MongoDB: Persistent storage
db.users.insert_one({
    'user_id': 123,
    'email': 'user@example.com',
    'subscription_plan': 'premium'
})

# Redis: Fast access cache
cache.set(f'user:{user_id}', user_data, ex=3600)  # 1-hour expiry
```

---

### Q3: How would you implement a distributed rate limiting system using Redis for payment API endpoints?

**Answer:**

Using Redis with a sliding window or token bucket algorithm:

```python
import redis
import time

cache = redis.Redis(host='localhost', port=6379, db=0)

def rate_limit_check(user_id: str, max_requests: int = 100, window: int = 60):
    """
    Rate limiting using Redis INCR with expiration.
    Args:
        user_id: Unique identifier for the user
        max_requests: Maximum requests allowed
        window: Time window in seconds
    """
    key = f"rate_limit:{user_id}"
    
    # Increment the counter
    current = cache.incr(key)
    
    # Set expiration on first increment
    if current == 1:
        cache.expire(key, window)
    
    # Check if limit exceeded
    if current > max_requests:
        return False, f"Rate limit exceeded. Retry after {cache.ttl(key)} seconds"
    
    return True, f"Requests remaining: {max_requests - current}"

# Usage in Flask endpoint
@app.route('/api/payments', methods=['POST'])
def create_payment():
    user_id = request.user_id
    allowed, message = rate_limit_check(user_id)
    
    if not allowed:
        return {'error': message}, 429  # Too Many Requests
    
    # Process payment
    return process_payment(request.json)
```

---

### Q4: Explain Redis persistence mechanisms (RDB and AOF). Which would you choose for this system?

**Answer:**

**RDB (Redis Database Snapshot):**
- Takes point-in-time snapshots of the dataset
- Compact and faster recovery
- Risk: Can lose data between snapshots
- Best for: Non-critical cache data

**AOF (Append Only File):**
- Logs every write operation
- Better durability and data loss prevention
- Slower but safer
- Best for: Critical data like payment tracking

**Recommendation for Payment System:**
```python
# Redis Configuration (redis.conf)

# Use AOF for critical payment data
appendonly yes
appendfsync everysec  # Sync every second (balance between speed & safety)

# Also enable RDB for recovery speed
save 900 1      # Save after 900 seconds if at least 1 key changed
save 300 10     # Save after 300 seconds if at least 10 keys changed
save 60 10000   # Save after 60 seconds if at least 10000 keys changed
```

**Implementation:**
```python
class RedisPaymentCache:
    def __init__(self):
        self.cache = redis.Redis(
            host='localhost',
            port=6379,
            db=0,
            decode_responses=True
        )
    
    def save_payment_attempt(self, payment_id: str, data: dict):
        """Critical data - uses AOF persistence"""
        key = f"payment:attempt:{payment_id}"
        self.cache.set(key, json.dumps(data), ex=86400)  # 24-hour retention
```

---

### Q5: How would you handle Redis connection pooling and failover in production?

**Answer:**

```python
from redis import Redis, ConnectionPool, Sentinel
import logging

# Connection Pool for efficiency
pool = ConnectionPool(
    host='localhost',
    port=6379,
    db=0,
    max_connections=50,
    socket_keepalive=True,
    socket_keepalive_options={1: (1, 3)}  # TCP_KEEPIDLE, TCP_KEEPINTVL
)
cache = Redis(connection_pool=pool)

# Redis Sentinel for High Availability
sentinels = [('sentinel1', 26379), ('sentinel2', 26379), ('sentinel3', 26379)]
sentinel = Sentinel(sentinels, socket_timeout=0.1)
cache = sentinel.master_for('mymaster', socket_timeout=0.1)

# Error handling and retry logic
class ResilientRedisCache:
    def __init__(self):
        self.cache = cache
        self.logger = logging.getLogger(__name__)
    
    def get_with_fallback(self, key: str):
        """Get from Redis with fallback to database"""
        try:
            value = self.cache.get(key)
            if value:
                return json.loads(value)
        except Exception as e:
            self.logger.error(f"Redis connection failed: {e}")
        
        # Fallback to MongoDB
        return self.fetch_from_database(key)
    
    def fetch_from_database(self, key: str):
        """Fallback to MongoDB"""
        # MongoDB query logic here
        pass
```

---

## Webhooks Questions

### Q6: What is a webhook and why is it essential for recurring payment systems?

**Answer:**

A **webhook** is an HTTP callback that sends real-time notifications from one service to another when an event occurs.

**Why webhooks are critical for recurring payments:**

1. **Real-time Updates**: Immediate notification of payment success/failure
2. **Asynchronous Processing**: Handle payment confirmations without polling
3. **Reliability**: Multiple retry mechanisms ensure data consistency
4. **Cost Efficiency**: No need for constant polling of payment provider

**Flow in this system:**
```
Razorpay Payment Gateway
    ↓ (Payment Processed)
    ├→ Webhook Event Triggered
    ├→ HTTP POST to Webhook URL (ngrok tunnel)
    ↓
Your Application
    ├→ Verify Webhook Signature
    ├→ Update Database (MongoDB)
    ├→ Send Email Notification (SMTP)
    ├→ Cache Update (Redis)
    └→ Return 200 OK Response
```

---

### Q7: How would you implement secure webhook handling with signature verification?

**Answer:**

Webhook security involves verifying that the request truly came from Razorpay using HMAC-SHA256 signatures:

```python
import hmac
import hashlib
import json
from flask import request, jsonify

class WebhookHandler:
    def __init__(self, razorpay_secret: str):
        self.razorpay_secret = razorpay_secret
    
    def verify_signature(self, webhook_body: str, signature: str) -> bool:
        """
        Verify Razorpay webhook signature.
        
        Args:
            webhook_body: Raw webhook payload
            signature: X-Razorpay-Signature header value
        
        Returns:
            True if signature is valid, False otherwise
        """
        expected_signature = hmac.new(
            self.razorpay_secret.encode(),
            webhook_body.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(signature, expected_signature)
    
    def handle_webhook(self, request_data):
        """
        Main webhook handler endpoint.
        """
        # Get signature from headers
        signature = request.headers.get('X-Razorpay-Signature')
        webhook_body = request.get_data(as_text=True)
        
        # Verify signature
        if not self.verify_signature(webhook_body, signature):
            return jsonify({'error': 'Invalid signature'}), 401
        
        # Parse payload
        event_data = json.loads(webhook_body)
        
        # Process based on event type
        return self.process_event(event_data)
    
    def process_event(self, event_data: dict):
        """Process different webhook events"""
        event_type = event_data.get('event')
        payload = event_data.get('payload', {}).get('payment', {})
        
        if event_type == 'payment.authorized':
            return self.handle_payment_authorized(payload)
        elif event_type == 'payment.failed':
            return self.handle_payment_failed(payload)
        elif event_type == 'subscription.authenticated':
            return self.handle_subscription_authenticated(payload)
        
        return jsonify({'status': 'received'}), 200

# Flask endpoint
@app.route('/webhooks/razorpay', methods=['POST'])
def razorpay_webhook():
    handler = WebhookHandler(os.getenv('RAZORPAY_SECRET'))
    return handler.handle_webhook(request)
```

---

### Q8: How would you implement webhook retries and idempotency?

**Answer:**

Webhook retries ensure that failed deliveries are re-attempted, and idempotency ensures duplicate events don't create duplicate records:

```python
from datetime import datetime, timedelta
import uuid

class WebhookManager:
    def __init__(self, db, cache, email_service):
        self.db = db  # MongoDB
        self.cache = cache  # Redis
        self.email_service = email_service
    
    def store_webhook_event(self, event_id: str, event_data: dict):
        """
        Store webhook event for idempotency check.
        Uses Redis for fast lookups and MongoDB for persistence.
        """
        # Check if already processed (idempotency key)
        cache_key = f"webhook:processed:{event_id}"
        if self.cache.get(cache_key):
            return {'status': 'already_processed'}, 200
        
        # Store in MongoDB
        self.db.webhook_events.insert_one({
            '_id': event_id,
            'data': event_data,
            'processed_at': datetime.utcnow(),
            'status': 'processed'
        })
        
        # Mark as processed in Redis (24-hour expiry)
        self.cache.setex(cache_key, 86400, '1')
        
        return True
    
    def handle_payment_with_retry(self, payment_data: dict):
        """
        Process payment with automatic retry mechanism.
        """
        event_id = payment_data.get('id')
        
        # Check idempotency
        if self.cache.get(f"webhook:processed:{event_id}"):
            return {'status': 'duplicate'}, 200
        
        try:
            # Process payment
            self.update_subscription(payment_data)
            self.send_confirmation_email(payment_data)
            
            # Mark as processed
            self.store_webhook_event(event_id, payment_data)
            
            return {'status': 'success'}, 200
        
        except Exception as e:
            # Store for retry
            self.schedule_webhook_retry(event_id, payment_data, attempt=1)
            return {'status': 'retry_scheduled'}, 202
    
    def schedule_webhook_retry(self, event_id: str, data: dict, attempt: int = 1):
        """
        Schedule webhook retry with exponential backoff.
        """
        if attempt > 5:  # Max 5 retries
            self.db.failed_webhooks.insert_one({
                'event_id': event_id,
                'data': data,
                'attempts': attempt,
                'failed_at': datetime.utcnow()
            })
            return
        
        # Exponential backoff: 2^attempt minutes
        retry_delay = timedelta(minutes=2 ** attempt)
        retry_time = datetime.utcnow() + retry_delay
        
        # Store in Redis with sorted set for scheduling
        score = retry_time.timestamp()
        self.cache.zadd(
            'webhook:retry:queue',
            {f"{event_id}:{attempt}": score}
        )
        
        # Store payload
        self.cache.hset(f"webhook:retry:{event_id}", mapping=data)

# Background task (using Celery or APScheduler)
def process_webhook_retries():
    """Background job to process scheduled retries"""
    current_time = datetime.utcnow().timestamp()
    
    # Get all due retries
    due_retries = cache.zrangebyscore(
        'webhook:retry:queue',
        0,
        current_time
    )
    
    for retry_key in due_retries:
        event_id, attempt = retry_key.rsplit(':', 1)
        data = cache.hgetall(f"webhook:retry:{event_id}")
        
        # Retry
        result = webhook_manager.handle_payment_with_retry(data)
        
        if result[1] == 200:
            cache.zrem('webhook:retry:queue', retry_key)
        else:
            # Schedule next retry
            webhook_manager.schedule_webhook_retry(event_id, data, int(attempt) + 1)
```

---

### Q9: Explain ngrok's role in webhook development. How would you test webhooks locally?

**Answer:**

**What is ngrok?**
ngrok creates secure tunnels to expose your local development server to the internet, allowing webhook providers like Razorpay to send POST requests to your local machine.

**Setup for local webhook testing:**

```bash
# Install ngrok
brew install ngrok  # macOS
# or download from https://ngrok.com

# Start ngrok tunnel (forward localhost:5000 to internet)
ngrok http 5000

# Output:
# Forwarding                    https://abc123.ngrok.io -> http://localhost:5000
```

**Flask application setup:**
```python
from flask import Flask, request, jsonify
import os

app = Flask(__name__)

# Webhook endpoint
@app.route('/webhooks/razorpay', methods=['POST'])
def razorpay_webhook():
    # In development, register this URL with Razorpay:
    # https://abc123.ngrok.io/webhooks/razorpay
    
    data = request.get_json()
    print(f"Received webhook: {data}")
    
    # Process webhook
    signature = request.headers.get('X-Razorpay-Signature')
    if verify_signature(request.get_data(as_text=True), signature):
        process_payment(data)
        return {'status': 'success'}, 200
    
    return {'error': 'Invalid signature'}, 401

if __name__ == '__main__':
    app.run(debug=True, port=5000)
```

**Testing with curl:**
```bash
# Simulate webhook request
curl -X POST https://abc123.ngrok.io/webhooks/razorpay \
  -H "Content-Type: application/json" \
  -H "X-Razorpay-Signature: test_signature" \
  -d '{
    "event": "payment.authorized",
    "payload": {
      "payment": {
        "entity": "payment",
        "id": "pay_123456",
        "amount": 50000,
        "currency": "INR",
        "status": "authorized"
      }
    }
  }'
```

---

### Q10: How would you monitor and log webhook events? What metrics are important?

**Answer:**

```python
import logging
from datetime import datetime
from functools import wraps
import json

class WebhookLogger:
    def __init__(self, db, cache):
        self.db = db
        self.cache = cache
        self.logger = logging.getLogger('webhooks')
    
    def log_webhook_metrics(func):
        """Decorator to log webhook metrics"""
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            start_time = datetime.utcnow()
            
            try:
                result = func(self, *args, **kwargs)
                duration = (datetime.utcnow() - start_time).total_seconds()
                
                # Log success
                self.db.webhook_metrics.insert_one({
                    'event_type': func.__name__,
                    'status': 'success',
                    'duration_ms': duration * 1000,
                    'timestamp': start_time
                })
                
                # Cache metrics for dashboard
                self.cache.incr(f"webhook:success:{func.__name__}")
                
                return result
            
            except Exception as e:
                duration = (datetime.utcnow() - start_time).total_seconds()
                
                # Log error
                self.db.webhook_metrics.insert_one({
                    'event_type': func.__name__,
                    'status': 'error',
                    'error': str(e),
                    'duration_ms': duration * 1000,
                    'timestamp': start_time
                })
                
                # Cache error count
                self.cache.incr(f"webhook:error:{func.__name__}")
                
                raise
        
        return wrapper
    
    @log_webhook_metrics
    def handle_payment_authorized(self, payload):
        """Process payment.authorized event"""
        # Implementation here
        pass

# Important metrics to track:
# 1. Success rate: webhook:success / (webhook:success + webhook:error)
# 2. Average response time: Total duration / Number of events
# 3. Failed events: Stored in failed_webhooks collection
# 4. Retry attempts: Tracked in webhook:retry:queue
# 5. Processing latency: Time from event trigger to completion
```

---

## Architecture & System Design

### Q11: Draw and explain the complete flow of a recurring payment in your system.

**Answer:**

```
┌─────────────────────────────────────────────────────────────────┐
│                    SUBSCRIPTION CREATION FLOW                   │
└─────────────────────────────────────────────────────────────────┘

1. USER INITIATES SUBSCRIPTION
   User Selection → Frontend (HTML)
        ↓
   Sends subscription plan ID, amount to backend
        ↓
   ┌──────────────────────────────────────────┐
   │   Python Backend (Flask/Django)          │
   │   - Validate subscription plan (Redis)   │
   │   - Create Razorpay order                │
   │   - Store order in MongoDB               │
   └──────────────────────────────────────────┘
        ↓
2. PAYMENT INITIATION
   ┌──────────────────────────────────────────┐
   │   Razorpay Payment Gateway               │
   │   - User enters payment details          │
   │   - Bank/Card authorization              │
   └──────────────────────────────────────────┘
        ↓
3. PAYMENT VERIFICATION (Webhook)
   Razorpay → HTTPS POST → ngrok → Localhost:5000/webhooks/razorpay
        ↓
   ┌──────────────────────────────────────────┐
   │   Webhook Handler                        │
   │   - Verify HMAC signature                │
   │   - Check idempotency (Redis)            │
   │   - Update payment status (MongoDB)      │
   └──────────────────────────────────────────┘
        ↓
4. RECURRING SETUP
   ┌──────────────────────────────────────────┐
   │   Redis Job Queue                        │
   │   - Schedule next recurring payment      │
   │   - Store subscription details           │
   │   - Set payment reminders                │
   └──────────────────────────────────────────┘
        ↓
5. NOTIFICATION
   ┌──────────────────────────────────────────┐
   │   Email Service (SMTP)                   │
   │   - Send subscription confirmation       │
   │   - Payment receipt                      │
   │   - Next billing date                    │
   └──────────────────────────────────────────┘
        ↓
   User receives email confirmation
```

---

## API Integration (Razorpay)

### Q12: How would you integrate Razorpay API for recurring payments?

**Answer:**

```python
import razorpay
from datetime import datetime, timedelta

class RazorpayIntegration:
    def __init__(self, key_id: str, key_secret: str):
        self.client = razorpay.Client(auth=(key_id, key_secret))
    
    def create_subscription(self, user_id: str, plan_id: str, customer_email: str):
        """
        Create a subscription in Razorpay.
        """
        try:
            subscription = self.client.subscription.create({
                'plan_id': plan_id,
                'customer_id': user_id,
                'quantity': 1,
                'total_count': 0,  # Infinite subscription
                'start_at': int((datetime.utcnow() + timedelta(days=1)).timestamp())
            })
            
            return subscription
        
        except Exception as e:
            print(f"Error creating subscription: {e}")
            raise
    
    def create_order(self, amount: int, currency: str = 'INR'):
        """
        Create an order for one-time payment or first subscription payment.
        """
        order = self.client.order.create({
            'amount': amount * 100,  # Razorpay expects amount in paise
            'currency': currency,
            'receipt': f'receipt_{datetime.utcnow().timestamp()}',
            'payment_capture': 1  # Auto-capture payment
        })
        
        return order
    
    def verify_payment(self, payment_id: str, order_id: str, signature: str):
        """
        Verify payment signature to confirm authenticity.
        """
        attributes = {
            'order_id': order_id,
            'payment_id': payment_id,
            'signature': signature
        }
        
        return self.client.utility.verify_payment_signature(attributes)

# Usage in Flask
@app.route('/api/subscriptions', methods=['POST'])
def create_subscription_endpoint():
    data = request.get_json()
    user_id = request.user_id
    
    razorpay = RazorpayIntegration(
        os.getenv('RAZORPAY_KEY_ID'),
        os.getenv('RAZORPAY_KEY_SECRET')
    )
    
    # Create Razorpay subscription
    subscription = razorpay.create_subscription(
        user_id,
        data['plan_id'],
        data['email']
    )
    
    # Store in MongoDB
    db.subscriptions.insert_one({
        'user_id': user_id,
        'razorpay_subscription_id': subscription['id'],
        'plan_id': data['plan_id'],
        'status': subscription['status'],
        'created_at': datetime.utcnow()
    })
    
    # Cache for quick lookup
    cache.setex(
        f"subscription:{user_id}",
        86400,
        json.dumps(subscription)
    )
    
    return jsonify(subscription), 201
```

---

## Security & Best Practices

### Q13: What are the security best practices for handling payment data in this system?

**Answer:**

```python
import os
from dotenv import load_dotenv
import secrets

load_dotenv()

# 1. ENVIRONMENT VARIABLES
# Store all sensitive data in .env file (never commit to git)
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID')
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET')
MONGODB_URI = os.getenv('MONGODB_URI')
REDIS_URL = os.getenv('REDIS_URL')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')

# 2. HTTPS ENFORCEMENT
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

@app.before_request
def enforce_https():
    """Ensure all requests use HTTPS in production"""
    if not os.getenv('DEBUG', False) and request.url.startswith('http://'):
        return redirect(request.url.replace('http://', 'https://', 1), code=301)

# 3. INPUT VALIDATION
from pydantic import BaseModel, EmailStr, validator

class PaymentRequest(BaseModel):
    amount: float
    email: EmailStr
    plan_id: str
    
    @validator('amount')
    def amount_must_be_positive(cls, v):
        if v <= 0:
            raise ValueError('Amount must be positive')
        return v

# 4. RATE LIMITING
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(app, key_func=get_remote_address)

@app.route('/api/payments', methods=['POST'])
@limiter.limit("10 per minute")
def create_payment():
    pass

# 5. WEBHOOK SIGNATURE VERIFICATION
def verify_razorpay_signature(request_body: str, signature: str) -> bool:
    """
    CRITICAL: Always verify webhook signatures to prevent unauthorized requests
    """
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        request_body.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(signature, expected_signature)

# 6. ENCRYPTION FOR SENSITIVE DATA
from cryptography.fernet import Fernet

class EncryptedField:
    def __init__(self, key: str):
        self.cipher = Fernet(key.encode())
    
    def encrypt(self, data: str) -> str:
        return self.cipher.encrypt(data.encode()).decode()
    
    def decrypt(self, encrypted_data: str) -> str:
        return self.cipher.decrypt(encrypted_data.encode()).decode()

# Store encrypted customer data
encryption = EncryptedField(os.getenv('ENCRYPTION_KEY'))
encrypted_card = encryption.encrypt(card_number)
db.customers.insert_one({
    'user_id': user_id,
    'card_number': encrypted_card  # Never store plain card numbers
})

# 7. AUDIT LOGGING
class AuditLogger:
    def __init__(self, db):
        self.db = db
    
    def log_payment_action(self, user_id: str, action: str, details: dict):
        """Log all payment-related actions for audit trail"""
        self.db.audit_logs.insert_one({
            'user_id': user_id,
            'action': action,
            'details': details,
            'ip_address': request.remote_addr,
            'timestamp': datetime.utcnow()
        })

# 8. PCI COMPLIANCE
# - Never store full credit card numbers
# - Never transmit unencrypted payment data
# - Use Razorpay's tokenization for card details
# - Implement TLS/SSL for all connections
# - Regular security audits and penetration testing

# 9. CSRF PROTECTION
from flask_wtf.csrf import CSRFProtect

csrf = CSRFProtect(app)

@app.route('/api/payments', methods=['POST'])
@csrf.protect
def create_payment():
    pass
```

---

### Q14: How would you handle failed payments and recover from errors?

**Answer:**

```python
class PaymentRecovery:
    def __init__(self, db, cache, email_service):
        self.db = db
        self.cache = cache
        self.email_service = email_service
    
    def handle_failed_payment(self, payment_id: str, error: str):
        """
        Handle failed payment with recovery strategy.
        """
        # 1. Log the failure
        self.db.failed_payments.insert_one({
            'payment_id': payment_id,
            'error': error,
            'failed_at': datetime.utcnow(),
            'status': 'pending_retry'
        })
        
        # 2. Increment retry counter
        retry_count = self.cache.incr(f"retry:count:{payment_id}")
        
        if retry_count > 3:
            # Max retries exceeded
            self.handle_max_retries_exceeded(payment_id)
            return
        
        # 3. Schedule automatic retry
        retry_delay = self._get_retry_delay(retry_count)
        self.schedule_retry(payment_id, retry_delay)
        
        # 4. Notify user
        self.email_service.send_payment_failed_notification(payment_id)
    
    def _get_retry_delay(self, retry_count: int) -> timedelta:
        """Exponential backoff strategy"""
        delays = [
            timedelta(minutes=5),      # First retry: 5 minutes
            timedelta(minutes=30),     # Second retry: 30 minutes
            timedelta(hours=1)         # Third retry: 1 hour
        ]
        return delays[retry_count - 1]
    
    def schedule_retry(self, payment_id: str, delay: timedelta):
        """Schedule payment retry in Redis"""
        retry_time = datetime.utcnow() + delay
        score = retry_time.timestamp()
        
        self.cache.zadd(
            'payment:retry:queue',
            {payment_id: score}
        )
    
    def handle_max_retries_exceeded(self, payment_id: str):
        """Suspend subscription when max retries exceeded"""
        payment = self.db.failed_payments.find_one({'payment_id': payment_id})
        user_id = payment.get('user_id')
        
        # Suspend subscription
        self.db.subscriptions.update_one(
            {'user_id': user_id},
            {'$set': {'status': 'suspended', 'suspended_at': datetime.utcnow()}}
        )
        
        # Notify user
        self.email_service.send_subscription_suspended_notification(user_id)
        
        # Cache update
        self.cache.delete(f"subscription:{user_id}")

# Background job to retry failed payments
def process_failed_payment_retries():
    """
    Periodically check for and process payment retries.
    Run this job using Celery or APScheduler.
    """
    current_time = datetime.utcnow().timestamp()
    
    # Get all due retries
    due_payments = cache.zrangebyscore(
        'payment:retry:queue',
        0,
        current_time
    )
    
    for payment_id in due_payments:
        try:
            # Attempt retry
            payment = db.failed_payments.find_one({'payment_id': payment_id})
            razorpay = RazorpayIntegration(
                os.getenv('RAZORPAY_KEY_ID'),
                os.getenv('RAZORPAY_KEY_SECRET')
            )
            
            # Retry payment
            new_order = razorpay.create_order(
                payment['amount'],
                payment['currency']
            )
            
            # Mark as retried
            db.failed_payments.update_one(
                {'payment_id': payment_id},
                {'$set': {'status': 'retried', 'new_order_id': new_order['id']}}
            )
            
            # Remove from retry queue
            cache.zrem('payment:retry:queue', payment_id)
        
        except Exception as e:
            print(f"Retry failed for {payment_id}: {e}")
```

---

## Summary Table

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Backend** | Python (Flask/Django) | API endpoints, business logic |
| **Database** | MongoDB | Persistent storage of users, subscriptions, payments |
| **Cache** | Redis | Session management, caching, job queues |
| **Payment Gateway** | Razorpay API | Process recurring payments |
| **Webhooks** | Razorpay → ngrok → Local App | Real-time payment notifications |
| **Email Notifications** | SMTP Protocol | Send receipts and confirmations |
| **Frontend** | HTML/CSS/JavaScript | User interface for subscriptions |

---

## Additional Resources

- **Razorpay Webhooks Documentation**: https://razorpay.com/docs/webhooks/
- **Redis Documentation**: https://redis.io/documentation
- **ngrok Documentation**: https://ngrok.com/docs
- **MongoDB Documentation**: https://docs.mongodb.com/
- **Flask Documentation**: https://flask.palletsprojects.com/

---

**Last Updated**: September 2026
**Author**: Interview Question Generator
