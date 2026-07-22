WITH active_listings AS (
  SELECT
    l.item_id,
    l.title,
    l.sku,
    DATE(l.start_time,"America/Los_Angeles") AS start_date,
    DATE_DIFF(CURRENT_DATE("America/Los_Angeles"),DATE(l.start_time,"America/Los_Angeles"), DAY) AS listing_age,
    l.quantity,
    l.price,
    l.watchers,
    l.impression_count,
    l.view_count,
    l.quantity_sold,
    ld.item_specifics,
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
    JSON_VALUE(item_specifics,'$."Print Run"[0]') AS print_run
  FROM
    `ebay.active_listings` l
      LEFT JOIN `ebay.active_listing_details` ld
        ON l.item_id = ld.item_id
  QUALIFY
    DENSE_RANK() OVER(ORDER BY l.file_date DESC) = 1
  AND
    DENSE_RANK() OVER(ORDER BY ld.file_date DESC) = 1
)


SELECT
  a.*,
  CASE WHEN insert_set IS NULL THEN 'Base'
    ELSE 'Insert' END AS subset_type,
  CASE WHEN insert_set IS NULL THEN 'Base'
    ELSE insert_set END AS subset_name,
  CONCAT(
    set_name,
    '|',
    CASE WHEN insert_set IS NULL THEN 'Base'
      ELSE insert_set END,
    '|',
    CASE WHEN parallel_variety IS NOT NULL THEN CONCAT(parallel_variety,'|')
      ELSE '' END,
    card_number
  ) AS card_id
FROM
  active_listings a
ORDER BY
  1