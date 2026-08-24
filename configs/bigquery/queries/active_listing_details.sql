-- Source for `shoebox enhance-listing-titles` when no --input file is given.
-- The view is expected to expose one row per active listing with at least
-- item_id, sku, title, and team (or item_specifics to read the team from).
SELECT
  *
FROM
  `{dataset}.v_active_listing_details`
