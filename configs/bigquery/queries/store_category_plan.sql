-- Source for `shoebox plan-store-categories`.
--
-- One row per active listing with just the fields the store-category rules
-- read. Everything but `autographed` is already flattened by the view; that
-- one is pulled straight off item_specifics so the view needs no change.
SELECT
  item_id,
  sku,
  title,
  price,
  quantity,
  sport,
  league,
  team,
  set_name,
  insert_set,
  subset_type,
  subset_name,
  parallel_variety,
  print_run,
  features,
  card_number,
  JSON_VALUE(item_specifics, '$."Autographed"[0]') AS autographed
FROM
  `{dataset}.v_active_listing_details`
ORDER BY
  item_id
