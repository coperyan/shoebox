
CREATE VIEW `ebay.v_orders_current` AS


WITH
  sku_to_metadata AS (

      SELECT
        RPAD(UPPER(TO_HEX(SHA1(
        ARRAY_TO_STRING([
          TRIM(IFNULL(CAST(c.set_year AS STRING), '')),
          TRIM(IFNULL(c.set_name, '')),
          TRIM(IFNULL(c.subset_name, '')),
          TRIM(IFNULL(p.parallel_variety, '')),
          TRIM(IFNULL(c.card_number, '')),
          TRIM(IFNULL(c.player, ''))
        ], '|') 
        ))), 50, '0') AS sku,
        c.card_id,
        c.set_year,
        c.set_name,
        c.subset_name,
        c.subset_type,
        c.card_number,
        c.player,
        c.team,
        c.note,
        p.parallel_id,
        p.parallel_variety,
        p.print_run
      FROM `cards.v_checklist` c
      LEFT JOIN `cards.v_parallels` p
        ON  c.set_name    = p.set_name
        AND c.subset_name = p.subset_name

  )



SELECT
  TO_HEX(SHA256(CONCAT(order_id,'|',line_item_id))) as order_line_key,
  TO_HEX(SHA256(order_id)) as order_key,
  order_id,
  sales_record_reference,
  line_item_id,
  DATE(order_creation_date,"America/Los_Angeles") as order_created_date,
  DATE(order_last_modified_date,"America/Los_Angeles") as order_last_modified_date,
  DATE(ship_by_date,"America/Los_Angeles") as ship_by_date,
  DATE(min_estimated_delivery_date,"America/Los_Angeles") as min_estimated_delivery_date,
  DATE(max_estimated_delivery_date,"America/Los_Angeles") as max_estimated_delivery_date,
  order_fulfillment_status,
  order_payment_status,
  line_item_fulfillment_status,
  seller_id,
  buyer_username,
  buyer_city,
  buyer_state,
  buyer_postal_code,
  buyer_country,
  listing_marketplace_id,
  purchase_marketplace_id,
  legacy_item_id,
  legacy_variation_id,
  title,
  o.sku,
  case when stm.sku is not null then 1 else 0 end as is_matched_to_meta,
  stm.card_id,
  stm.parallel_id,
  case when variation_aspects_json is not null then 1 else 0 end as is_variation,
  variation_aspects_insert,
  variation_card,
  sold_format,
  quantity,
  item_price,
  item_currency,
  shipping_price,
  tax_total,
  line_item_total
FROM
  `ebay-sports-cards-automation.ebay.orders_current` o
    LEFT JOIN sku_to_metadata stm
      ON o.sku = stm.sku
WHERE
  1=1
AND
  DATE(order_creation_date,"America/Los_Angeles") >= '2026-07-28'




