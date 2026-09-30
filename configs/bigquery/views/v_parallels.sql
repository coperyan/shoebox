CREATE OR REPLACE VIEW `cards.v_parallels` AS 

SELECT
  CONCAT(set_name,"|",subset_name,"|",parallel_variety) AS parallel_id,
  SPLIT(set_name,' ')[OFFSET(0)] AS set_year,
  CONCAT(set_name,"|",subset_name) AS subset_id,
  set_name,
  subset_name,
  parallel_variety,
  print_run,
  other_note
FROM
  `cards.parallels`
ORDER BY
1
;