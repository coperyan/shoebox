-- Sold eBay line items to reconcile against the TCDB collection: every
-- non-variation sale created on or after {since} (an America/Los_Angeles
-- date, as v_orders_current reports it), with the checklist attributes the
-- listing's SKU resolves to. Variation ("You Pick") listings are left out:
-- those cards are not tracked on TCDB. Rows whose SKU matches no checklist
-- card come back with is_matched_to_meta = 0 and empty card columns.
SELECT
  o.order_line_key,
  o.order_id,
  o.line_item_id,
  o.order_created_date,
  o.title AS ebay_title,
  o.sku,
  o.quantity,
  o.legacy_item_id,
  o.is_matched_to_meta,
  c.set_year,
  c.set_name,
  c.subset_name,
  c.subset_type,
  c.card_number,
  c.player,
  c.team,
  p.parallel_variety,
  p.print_run
FROM `ebay.v_orders_current` o
LEFT JOIN `cards.v_checklist` c
  ON o.card_id = c.card_id
LEFT JOIN `cards.v_parallels` p
  ON o.parallel_id = p.parallel_id
WHERE
  o.is_variation = 0
  AND o.variation_card IS NULL
  AND o.legacy_variation_id IS NULL
  AND o.order_created_date >= DATE '{since}'
ORDER BY
  o.order_created_date,
  o.order_id,
  o.line_item_id
