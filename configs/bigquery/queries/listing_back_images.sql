-- Source for `shoebox add-combined-images --restore-backs`: the back picture
-- each SKU was last created with by `create-listings` (imageUrls[1] of the
-- logged request). Rows created with only a front have no back_url.
SELECT
  sku,
  JSON_VALUE(request, '$.data.inventory_item.product.imageUrls[1]') AS back_url
FROM
  `{dataset}.ebay_listings`
WHERE
  success
QUALIFY
  ROW_NUMBER() OVER (PARTITION BY sku ORDER BY created_at_utc DESC) = 1
