"""
app/routes/payments.py
Stripe payment integration: create a $5 checkout session and handle webhooks
to upgrade users to premium after successful payment.
"""
import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import (
    BASE_URL,
    STRIPE_PREMIUM_PRICE_CENTS,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
)
from app.security import get_current_user
from db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/payments", tags=["Payments"])


@router.post("/create-checkout", summary="Create a Stripe Checkout session for premium upgrade ($5)")
def create_checkout(user=Depends(get_current_user)):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=501, detail="Payments are not configured on this server.")

    if user["is_premium"]:
        raise HTTPException(status_code=400, detail="You are already a premium member.")

    stripe.api_key = STRIPE_SECRET_KEY
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "VoiceWave Premium",
                        "description": "Unlimited AI song generation & voice cloning",
                    },
                    "unit_amount": STRIPE_PREMIUM_PRICE_CENTS,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=f"{BASE_URL}/payment-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/payment-cancelled",
            client_reference_id=str(user["id"]),
            customer_email=user["email"],
            metadata={"user_id": str(user["id"])},
        )
    except stripe.StripeError as exc:
        logger.error("Stripe error for user %s: %s", user["id"], exc)
        raise HTTPException(status_code=502, detail=f"Payment provider error: {exc.user_message}")

    return {"checkout_url": session.url, "session_id": session.id}


@router.post("/webhook", include_in_schema=False)
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=501, detail="Webhook not configured.")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    stripe.api_key = STRIPE_SECRET_KEY
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except stripe.SignatureVerificationError:
        logger.warning("Invalid Stripe webhook signature")
        raise HTTPException(status_code=400, detail="Invalid signature.")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if event["type"] == "checkout.session.completed":
        session_obj = event["data"]["object"]
        user_id = (
            session_obj.get("metadata", {}).get("user_id")
            or session_obj.get("client_reference_id")
        )
        if user_id:
            conn = get_db()
            conn.execute(
                "UPDATE users SET plan='premium', is_premium=1, credits=-1 WHERE id=?",
                (int(user_id),),
            )
            conn.commit()
            conn.close()
            logger.info("User %s upgraded to premium via Stripe", user_id)

    return {"received": True}


@router.get("/status", summary="Check current user's premium status")
def payment_status(user=Depends(get_current_user)):
    return {
        "is_premium": bool(user["is_premium"]),
        "plan": user["plan"],
        "price_usd": STRIPE_PREMIUM_PRICE_CENTS / 100,
    }
