"""Send negotiation offers to interested buyers, with Slack-driven pricing.

Interactive by default: every eligible listing is posted to Slack up front
(title, current price), then each threaded reply drives that listing's offer —
a price sends the offer immediately, "skip" resolves it without sending, and
anything unparseable gets a threaded hint and stays pending. Listings still
pending at the deadline simply reappear next run (eBay keeps returning them
while buyers remain interested).

``--auto`` skips the prompts and sends the discount-matrix price for every
eligible listing. Unlike the old headless pipeline there is no ``--max-price``
cap anymore: listings priced above the matrix's top tier get a flat 5%-off
offer instead of being skipped.
"""

import asyncio
import logging
from collections.abc import Callable

from shoebox.clients.ebay.client import EbayClient
from shoebox.models.ebay.negotiation_offer import NegotiationOffer
from shoebox.settings import get_settings
from shoebox.utils.pricing import calculate_new_price, parse_price_reply
from shoebox.utils.slack import BatchPrompt, notify, notify_batch_and_wait

logger = logging.getLogger(__name__)

MIN_OFFER_PRICE = 0.99
SKIP_KEYWORDS = {"skip", "no", "pass"}

PostFn = Callable[..., str]
BatchFn = Callable[..., dict[str, str]]


def suggest_offer_price(price: float) -> float:
    """Discount-matrix price, with a 5%-off fallback for prices above the
    matrix's top tier (calculate_new_price raises there)."""
    try:
        return calculate_new_price(price)
    except ValueError:
        return round(price * 0.95, 2)


def format_offer_prompt(details: dict) -> BatchPrompt:
    """Build the Slack prompt for one eligible listing.

    mrkdwn (no code fences — links don't render inside them), with the first
    listing photo as an explicit image block since bot messages don't unfurl.
    """
    title = details.get("title") or details["item_id"]
    url = details.get("view_item_url")
    price = float(details["price"])
    qty = details.get("quantity")

    headline = f"*<{url}|{title}>*" if url else f"*{title}*"
    line = f"Current: ${price:.2f}"
    if qty is not None:
        line += f" · Qty {int(qty)}"

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{headline}\n{line}"}}
    ]
    pics = details.get("picture_urls") or []
    if pics:
        blocks.append({"type": "image", "image_url": pics[0], "alt_text": title})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Reply in this thread with an offer amount (e.g. 3.49) or 'skip'.",
                }
            ],
        }
    )
    return BatchPrompt(
        key=details["item_id"],
        text=f"{title} — current ${price:.2f}. Reply with an offer amount or 'skip'.",
        blocks=blocks,
    )


def decide_reply(current_price: float, reply_text: str) -> tuple[str, float | None, str | None]:
    """Classify a threaded reply for a listing priced at ``current_price``.

    Returns (action, amount, thread_msg): action is "skip", "send" (with
    amount), or "retry" (with a hint/validation message to post in-thread,
    leaving the prompt pending).
    """
    if reply_text.strip().lower() in SKIP_KEYWORDS:
        return "skip", None, None
    amount = parse_price_reply(reply_text)
    if amount is None:
        return "retry", None, "Couldn't parse that — reply with an amount like 3.49, or 'skip'."
    if not MIN_OFFER_PRICE <= amount < current_price:
        return (
            "retry",
            amount,
            f"Offer must be at least ${MIN_OFFER_PRICE:.2f} and below the current "
            f"${current_price:.2f} — reply with another amount or 'skip'.",
        )
    return "send", amount, None


def _send_offer(ebay: EbayClient, details: dict, amount: float) -> dict:
    offer = NegotiationOffer.from_api(
        listing_id=details["item_id"], price=amount, quantity=int(details["quantity"])
    )
    resp = ebay.negotiation.send_offer(offer) or {}
    offer_ids = [o.get("offer_id") or o.get("offerId") for o in resp.get("offers") or []]
    logger.info("Sent $%.2f offer for %s (offers=%s)", amount, details["item_id"], offer_ids)
    for warning in resp.get("warnings") or []:
        logger.warning("send_offer warning for %s: %s", details["item_id"], warning)
    return resp


def main(
    dry_run: bool = False,
    auto: bool = False,
    timeout_s: int = 900,
    *,
    ebay: EbayClient | None = None,
    post: PostFn = notify,
    run_batch: BatchFn = notify_batch_and_wait,
) -> None:
    """Offer discounts to watchers of every offer-eligible listing."""
    settings = get_settings()
    ebay = ebay or EbayClient()

    eligible = ebay.negotiation.find_eligible_items()
    details = [
        ebay.legacy_api.get_item_details(item_id=listing["listing_id"]) for listing in eligible
    ]
    logger.info("%d eligible listing(s)", len(details))
    if not details:
        return

    if dry_run:
        for d in details:
            logger.info(
                "[dry_run] Would prompt: %s — current $%.2f (suggested $%.2f)",
                d["title"],
                float(d["price"]),
                suggest_offer_price(float(d["price"])),
            )
        return

    channel = settings.slack.offers_channel or settings.slack.pricing_channel

    if auto:
        for d in details:
            _send_offer(ebay, d, suggest_offer_price(float(d["price"])))
        post(channel, f"📤 *send-offers* — auto-sent {len(details)} offer(s).")
        return

    parent_ts = post(
        channel,
        f"📤 *send-offers* — {len(details)} listing(s) eligible. "
        "Reply to each with an offer amount (e.g. 3.49) or 'skip'.",
    )

    by_id = {d["item_id"]: d for d in details}
    # Rapid replies handle concurrently; the shared ebay_rest client isn't
    # guaranteed thread-safe (token refresh), so serialize the actual sends.
    send_lock = asyncio.Lock()

    async def on_reply(key: str, reply_text: str, post_thread) -> str | None:
        d = by_id[key]
        action, amount, thread_msg = decide_reply(float(d["price"]), reply_text)
        if action == "skip":
            return "⏭️ Skipped"
        if action == "retry":
            await post_thread(thread_msg)
            return None
        try:
            async with send_lock:
                await asyncio.to_thread(_send_offer, ebay, d, amount)
        except Exception as e:
            logger.warning("send_offer failed for %s: %s", key, e)
            await post_thread(
                f"⚠️ eBay rejected the offer: {e} — reply with a different amount or 'skip'."
            )
            return None
        return f"✅ Sent ${amount:.2f} offer"

    results = run_batch(
        channel,
        [format_offer_prompt(d) for d in details],
        on_reply,
        timeout_s=timeout_s,
    )

    sent = sum(1 for v in results.values() if v.startswith("✅"))
    skipped = sum(1 for v in results.values() if v.startswith("⏭️"))
    expired = len(details) - sent - skipped
    logger.info("send-offers done: %d sent, %d skipped, %d expired", sent, skipped, expired)
    post(
        channel,
        f"Done — {sent} offer(s) sent, {skipped} skipped, {expired} expired.",
        thread_ts=parent_ts,
    )
