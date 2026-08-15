from shoebox.models.ebay.item_summary import ItemSummary

BASE = "https://i.ebayimg.com/images/g/abc"


def item(**overrides) -> ItemSummary:
    return ItemSummary(item_id="v1|123|0", title="card", **overrides)


class TestThumbnail:
    def test_prefers_thumbnail_images(self):
        out = item(
            thumbnail_images=[{"image_url": f"{BASE}/s-l225.jpg"}],
            image={"image_url": f"{BASE}/other.jpg"},
        ).thumbnail()
        assert out == f"{BASE}/s-l225.jpg"

    def test_falls_back_to_image(self):
        assert item(image={"image_url": f"{BASE}/s-l225.jpg"}).thumbnail() == f"{BASE}/s-l225.jpg"

    def test_none_when_the_listing_has_no_photo(self):
        assert item().thumbnail() is None
        assert item().thumbnail(500) is None

    def test_size_rewrites_the_segment(self):
        out = item(thumbnail_images=[{"image_url": f"{BASE}/s-l225.jpg"}]).thumbnail(500)
        assert out == f"{BASE}/s-l500.jpg"

    def test_size_preserves_the_extension(self):
        out = item(thumbnail_images=[{"image_url": f"{BASE}/s-l140.webp"}]).thumbnail(500)
        assert out == f"{BASE}/s-l500.webp"

    def test_url_without_a_size_segment_is_left_alone(self):
        # Not every eBay image URL carries s-l<n>; returning it unchanged beats
        # mangling it into a 404.
        out = item(thumbnail_images=[{"image_url": f"{BASE}/plain.jpg"}]).thumbnail(500)
        assert out == f"{BASE}/plain.jpg"

    def test_no_size_returns_the_url_verbatim(self):
        out = item(thumbnail_images=[{"image_url": f"{BASE}/s-l1600.jpg"}]).thumbnail()
        assert out == f"{BASE}/s-l1600.jpg"

    def test_explicit_null_thumbnails_do_not_raise(self):
        # eBay sends `null` rather than omitting the key on some listings.
        assert ItemSummary(item_id="x", thumbnail_images=None).thumbnail() is None
