"""End-to-end send-offers tests with injected collaborators.

No mocking library: main() takes ``ebay``, ``post`` and ``run_batch``
callables, so plain local fakes cover the whole flow (same convention as
test_watch_searches_flow.py).
"""

import asyncio

from shoebox.pipelines.send_offers import (
    decide_reply,
    format_offer_prompt,
    main,
    suggest_offer_price,
)
from shoebox.utils.slack import EXPIRED_STATUS


def details(
    item_id: str,
    title: str = "Michael Jordan Rookie",
    price: float = 3.99,
    quantity="1",
    picture: bool = True,
) -> dict:
    return {
        "item_id": item_id,
        "title": title,
        "price": price,
        "quantity": quantity,
        "view_item_url": f"https://ebay.com/itm/{item_id}",
        "picture_urls": [f"https://i.ebayimg.com/images/g/{item_id}/s-l1600.jpg"]
        if picture
        else [],
    }


class FakeNegotiation:
    def __init__(self, eligible: list[str], fail_ids: set[str] | None = None):
        self.eligible = eligible
        self.fail_ids = fail_ids or set()
        self.sent: list[tuple[str, float, int]] = []

    def find_eligible_items(self) -> list[dict]:
        return [{"listing_id": x} for x in self.eligible]

    def send_offer(self, offer) -> dict:
        if offer.listing_id in self.fail_ids:
            raise RuntimeError("Offer price must be lower than the current price")
        self.sent.append((offer.listing_id, offer.price, offer.quantity))
        return {"offers": [{"offer_id": f"offer-{offer.listing_id}"}], "warnings": []}


class FakeLegacy:
    def __init__(self, by_id: dict[str, dict]):
        self.by_id = by_id

    def get_item_details(self, item_id: str) -> dict:
        return self.by_id[item_id]


class FakeEbay:
    def __init__(self, items: list[dict], fail_ids: set[str] | None = None):
        self.negotiation = FakeNegotiation([d["item_id"] for d in items], fail_ids)
        self.trading = FakeLegacy({d["item_id"]: d for d in items})


class Recorder:
    """Stand-in for notify(). Records posts and hands back fake ts values."""

    def __init__(self):
        self.posts: list[tuple[str, str, str | None]] = []

    def __call__(self, channel, message, thread_ts=None, **kwargs) -> str:
        self.posts.append((channel, message, thread_ts))
        return f"ts{len(self.posts)}"


class ScriptedBatch:
    """Stand-in for notify_batch_and_wait: feeds each prompt its scripted
    replies until one resolves it; prompts with no resolving reply expire."""

    def __init__(self, replies: dict[str, list[str]]):
        self.replies = replies
        self.prompts = None
        self.channel = None
        self.thread_posts: dict[str, list[str]] = {}

    def __call__(self, channel, prompts, on_reply, timeout_s=900):
        self.channel = channel
        self.prompts = prompts
        results = {}
        for prompt in prompts:
            for reply in self.replies.get(prompt.key, []):

                async def post_thread(msg, key=prompt.key):
                    self.thread_posts.setdefault(key, []).append(msg)

                outcome = asyncio.run(on_reply(prompt.key, reply, post_thread))
                if outcome is not None:
                    results[prompt.key] = outcome
                    break
            else:
                results[prompt.key] = EXPIRED_STATUS
        return results


def run(items, replies=None, post=None, batch=None, fail_ids=None, **kwargs):
    ebay = FakeEbay(items, fail_ids)
    post = post if post is not None else Recorder()
    batch = batch if batch is not None else ScriptedBatch(replies or {})
    main(ebay=ebay, post=post, run_batch=batch, **kwargs)
    return ebay, post, batch


class TestInteractiveFlow:
    def test_valid_reply_sends_offer_at_that_amount(self):
        ebay, post, batch = run([details("a")], {"a": ["3.49"]})
        assert ebay.negotiation.sent == [("a", 3.49, 1)]
        # Parent summary + threaded tally.
        assert len(post.posts) == 2
        assert "1 listing(s) eligible" in post.posts[0][1]
        assert post.posts[1][2] == "ts1"
        assert "1 offer(s) sent, 0 skipped, 0 expired" in post.posts[1][1]

    def test_dollar_sign_reply_parses(self):
        ebay, _, _ = run([details("a")], {"a": ["$2.99"]})
        assert ebay.negotiation.sent == [("a", 2.99, 1)]

    def test_skip_resolves_without_sending(self):
        ebay, post, _ = run([details("a")], {"a": ["skip"]})
        assert ebay.negotiation.sent == []
        assert "0 offer(s) sent, 1 skipped, 0 expired" in post.posts[-1][1]

    def test_unparseable_reply_hints_then_next_reply_sends(self):
        ebay, _, batch = run([details("a")], {"a": ["huh?", "2.49"]})
        assert ebay.negotiation.sent == [("a", 2.49, 1)]
        assert any("Couldn't parse" in m for m in batch.thread_posts["a"])

    def test_amount_at_or_above_current_price_stays_pending(self):
        ebay, post, batch = run([details("a", price=3.99)], {"a": ["3.99"]})
        assert ebay.negotiation.sent == []
        assert any("below the current" in m for m in batch.thread_posts["a"])
        assert "0 offer(s) sent, 0 skipped, 1 expired" in post.posts[-1][1]

    def test_ebay_rejection_posts_error_and_allows_retry(self):
        # First run: every send fails -> error in thread, item expires unsent.
        ebay, _, batch = run([details("a")], {"a": ["3.49"]}, fail_ids={"a"})
        assert ebay.negotiation.sent == []
        assert any("eBay rejected" in m for m in batch.thread_posts["a"])

    def test_unreplied_prompt_expires(self):
        ebay, post, _ = run([details("a")], {})
        assert ebay.negotiation.sent == []
        assert "0 offer(s) sent, 0 skipped, 1 expired" in post.posts[-1][1]

    def test_no_eligible_items_posts_nothing(self):
        _, post, batch = run([], {})
        assert post.posts == []
        assert batch.prompts is None

    def test_prompts_go_to_offers_channel_with_pricing_fallback(self, monkeypatch):
        from shoebox.settings import get_settings

        slack = get_settings().slack
        monkeypatch.setattr(slack, "offers_channel", "C_OFFERS")
        _, _, batch = run([details("a")], {"a": ["skip"]})
        assert batch.channel == "C_OFFERS"

        monkeypatch.setattr(slack, "offers_channel", "")
        _, _, batch = run([details("a")], {"a": ["skip"]})
        assert batch.channel == slack.pricing_channel


class TestDryRunAndAuto:
    def test_dry_run_touches_nothing(self):
        ebay, post, batch = run([details("a")], {"a": ["3.49"]}, dry_run=True)
        assert ebay.negotiation.sent == []
        assert post.posts == []
        assert batch.prompts is None

    def test_auto_sends_matrix_price_without_prompting(self):
        # 3.99 falls in the 5.49 tier -> $0.50 off.
        ebay, post, batch = run([details("a", price=3.99)], auto=True)
        assert ebay.negotiation.sent == [("a", 3.49, 1)]
        assert batch.prompts is None
        assert "auto-sent 1 offer(s)" in post.posts[0][1]


class TestHelpers:
    def test_decide_reply_skip_keywords(self):
        for word in ("skip", "SKIP", " no ", "pass"):
            assert decide_reply(3.99, word)[0] == "skip"

    def test_decide_reply_send(self):
        assert decide_reply(3.99, "3.49") == ("send", 3.49, None)

    def test_decide_reply_bounds(self):
        action, _, msg = decide_reply(3.99, "0.50")
        assert action == "retry" and "at least" in msg
        action, _, _ = decide_reply(3.99, "4.50")
        assert action == "retry"

    def test_suggest_offer_price_above_matrix_falls_back(self):
        assert suggest_offer_price(3.99) == 3.49
        assert suggest_offer_price(40.00) == 38.00  # 5% off, no ValueError

    def test_format_offer_prompt_blocks(self):
        prompt = format_offer_prompt(details("a", title="Card", price=3.99))
        assert prompt.key == "a"
        types = [b["type"] for b in prompt.blocks]
        assert types == ["section", "image", "context"]
        section = prompt.blocks[0]["text"]["text"]
        assert "<https://ebay.com/itm/a|Card>" in section
        assert "$3.99" in section

    def test_format_offer_prompt_without_picture(self):
        prompt = format_offer_prompt(details("a", picture=False))
        assert [b["type"] for b in prompt.blocks] == ["section", "context"]
