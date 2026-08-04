CREATE OR REPLACE TABLE `ebay.orders_current` AS
SELECT
  *
FROM
  `ebay.orders`
QUALIFY
  ROW_NUMBER() OVER(PARTITION BY order_id, line_item_id ORDER BY file_date DESC) = 1
;