CREATE OR REPLACE VIEW ebay.v_watch_list AS 

WITH watch_list AS (
  SELECT
    item_id,
    title,
    seller,
    listing_type,
    listing_status,
    price,
    currency,
    buy_it_now_price,
    bid_count,
    quantity,
    quantity_sold,
    time_left,
    DATETIME(start_time,"America/Los_Angeles") AS start_time,
    DATETIME(end_time,"America/Los_Angeles") AS end_time,
    view_item_url,
    gallery_url,
    category_id,
    category_name,
    condition_id,
    item_specifics,
    ARRAY_TO_STRING(JSON_VALUE_ARRAY(item_specifics, '$."Features"'), ' | ') AS features,
    ARRAY_TO_STRING(JSON_VALUE_ARRAY(item_specifics, '$."Player/Athlete"'), ' | ') AS player,
    ARRAY_TO_STRING(JSON_VALUE_ARRAY(item_specifics, '$."Team"'), ' | ') AS team,
    JSON_VALUE(item_specifics,'$."League"[0]') AS league,
    JSON_VALUE(item_specifics,'$."Sport"[0]') AS sport,
    JSON_VALUE(item_specifics,'$."Season"[0]') AS season,
    JSON_VALUE(item_specifics,'$."Manufacturer"[0]') AS manufacturer,
    JSON_VALUE(item_specifics,'$."Set"[0]') AS set_name,
    JSON_VALUE(item_specifics,'$."Insert Set"[0]') AS insert_set,
    CASE WHEN
      JSON_VALUE(item_specifics,'$."Parallel/Variety"[0]') IN ('Base','[Base]') THEN NULL 
        ELSE JSON_VALUE(item_specifics,'$."Parallel/Variety"[0]') END AS parallel_variety,
    JSON_VALUE(item_specifics,'$."Card Number"[0]') AS card_number,
    JSON_VALUE(item_specifics,'$."Print Run"[0]') AS print_run,
    JSON_VALUE(item_specifics,'$."Autographed"[0]') AS autographed,
    picture_urls,
    file_date
  FROM
    ebay.watch_list
  QUALIFY DENSE_RANK() OVER(ORDER BY file_date DESC) = 1
)

SELECT
  category_id,
  item_id,
  view_item_url,
  title,
  seller,
  listing_type,
  listing_status,
  price,
  bid_count,
  quantity,
  quantity_sold,
  start_time,
  end_time,
  player,
  team,
  season,
  set_name,
  insert_set,
  features,
  parallel_variety,
  print_run,
  autographed,
  league,
  sport,
FROM watch_list
;

