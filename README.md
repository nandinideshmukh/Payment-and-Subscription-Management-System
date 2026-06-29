# Description

FastAPI-based subscription and payment system integrated with Razorpay. It handles payment order creation, payment verification, webhook processing, recurring subscriptions, renewal automation, and notification emails.

## Project Structure

- Routes define the HTTP endpoints exposed by the API.
- Services contain the business logic for payments, renewals, notifications, and scheduling.
- Schemas define request/response models.
- Utils contain helper functions used by the services.

## Routes

### Payment routes

Defined in [routes/payment_route.py](routes/payment_route.py)

- POST /payment/create-order
  - Creates a Razorpay order for a selected plan.
  - Validates the user and plan.
  - Prevents duplicate requests using idempotency support.

- POST /payment/verify
  - Verifies the Razorpay checkout signature.
  - Updates the payment record to authorized state.
  - Returns payment verification success or an error.

- POST /razorpay/webhook
  - Receives Razorpay webhook events.
  - Verifies the webhook signature.
  - Routes payment and subscription events to the correct handlers.

- GET /payment/renew/{order_id}
  - Opens a checkout page for renewal payments.
  - Displays a Razorpay payment pop-up for a previously created order.

- POST /debug/test-webhook
  - Test endpoint for triggering webhook handling without full signature validation.

- GET /debug/webhook-duplicates
  - Lists recent webhook events and highlights possible duplicates.

- DELETE /debug/webhook-cleanup
  - Removes test or debug webhook entries from storage.

- GET /debug/webhook-logs
  - Returns recent webhook activity for debugging.

### Recurring subscription routes

Defined in [routes/recurring_route.py](routes/recurring_route.py)

- GET /recurring/subscription/{user_id}
  - Fetches a user's subscription details.

- POST /recurring/subscription/cancel/{subscription_id}
  - Cancels an active subscription.
  - Updates the subscription status and queues a cancellation email notification.

- POST /recurring/subscription/retry-payment/{payment_id}
  - Creates a new Razorpay order for a failed payment so the user can retry.

- POST /recurring/process-renewals
  - Processes all subscriptions that are due for renewal.

- POST /recurring/check-expiry
  - Checks expired subscriptions and applies grace-period logic.
  - Sends an expiry notification email when a subscription is marked expired.

- GET /recurring/due-subscriptions
  - Returns subscriptions whose renewal date has arrived.

- GET /recurring/expiring-soon
  - Returns subscriptions that are close to their expiration date.

- GET /recurring/debug/subscription/{user_id}
  - Provides a debug view of a subscription's lifecycle fields.

- POST /recurring/manual-renew/{user_id}
  - Forces a renewal flow for testing or manual intervention.

- POST /recurring/fix-subscription/{user_id}
  - Ensures a subscription has a next billing date set.

### Scheduler routes

Defined in [routes/cron_route.py](routes/cron_route.py)

- GET /scheduler/status
  - Returns whether the scheduler is initialized and running.

- GET /scheduler/jobs
  - Lists scheduled jobs and their next run times.

- POST /scheduler/trigger/renewals
  - Manually triggers renewal processing.

- POST /scheduler/trigger/expiry
  - Manually triggers the expiry check process.

- POST /scheduler/trigger/reminders
  - Manually triggers reminder notifications.

- POST /scheduler/pause
  - Pauses the scheduler.

- POST /scheduler/resume
  - Resumes the scheduler.

## Services

### Payment service

Defined in [services/payment_service.py](services/payment_service.py)

- create_order(...)
  - Creates Razorpay orders for subscriptions or plan purchases.
  - Uses idempotency helpers to prevent duplicate orders.

- verify_checkout_signature(...)
  - Verifies the signature returned from Razorpay checkout.
  - Updates the payment record as authorized or already processed.

- verify_webhook_signature(...)
  - Validates webhook signatures from Razorpay.

- handle_webhook_event(...)
  - Processes webhook payloads and dispatches the correct event handler.
  - Prevents duplicate processing using stored webhook event IDs.

- Webhook handlers
  - payment.authorized
  - payment.captured
  - order.paid
  - payment.failed
  - subscription.charged
  - subscription.halted
  - subscription.cancelled
  - subscription.pending

### Recurring service

Defined in [services/recurring_service.py](services/recurring_service.py)

- find_due_subscriptions()
  - Finds subscriptions that are due for renewal.

- find_expiring_soon(days_before)
  - Finds subscriptions that are approaching expiry.

- process_renewal(subscription)
  - Creates a renewal order and payment entry for one subscription.

- process_all_renewals()
  - Processes all due subscriptions in one batch.

- handle_successful_renewal(payload)
  - Updates a subscription after a successful renewal payment.

- handle_failed_renewal(payload)
  - Updates a subscription after a failed renewal payment.

- check_and_expire_subscriptions()
  - Applies expiry logic and grace-period behavior.

### Notification service

Defined in [services/payment_notification_service.py](services/payment_notification_service.py)

- send_payment_confirmation(...)
  - Sends a confirmation email after a successful payment.

- send_payment_failure_alert(...)
  - Sends an alert when a payment fails.

- send_subscription_expiry_reminder(...)
  - Sends renewal reminder emails before expiry.

- send_renewal_payment_link(...)
  - Sends a direct payment link for subscription renewals.

- send_subscription_cancelled(...)
  - Sends a cancellation confirmation email.

### Renewal scheduler

Defined in [services/renewal_cron.py](services/renewal_cron.py)

- start()
  - Starts the recurring background scheduler.

- shutdown()
  - Stops the scheduler safely.

- trigger_renewals()
  - Starts renewal processing manually.

- trigger_expiry_check()
  - Runs the expiration check flow.

- trigger_reminders()
  - Sends reminder notifications for expiring subscriptions.

## Notes

- The notification service can logs emails in test mode as well as real mode unless SMTP credentials are configured.
- Most endpoints depend on application state such as the database, Razorpay client, and scheduler instance.
- The system is designed for subscription renewals, recurring billing, and payment lifecycle management.
