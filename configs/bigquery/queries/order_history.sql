select
  order_id,
  datetime(order_creation_date,"America/Los_Angeles") AS order_datetime_local,
  order_fulfillment_status,
  title,
  quantity,
  sold_format,
  line_item_total,
from ebay.orders
where datetime(order_creation_date,"America/Los_Angeles") >= DATE_SUB(CURRENT_DATE("America/Los_Angeles"), INTERVAL 30 DAY)
and variation_aspects_json IS NULL
order by order_creation_date desc;