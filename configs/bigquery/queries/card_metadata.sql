SELECT
  CASE WHEN p.parallel_variety IS NOT NULL THEN
    CONCAT(c.set_name,"|",c.subset_name,"|",p.parallel_variety,"|",c.card_number)
      ELSE c.card_id END AS card_id,
  c.card_id AS checklist_id,
  c.subset_id,
  p.parallel_id,
  c.set_year,
  c.set_name,
  c.subset_name,
  c.subset_type,
  p.parallel_variety,
  c.derived_card_number,
  c.card_number,
  c.player,
  c.team,
  c.note,
  c.note_check,
  p.print_run,
  p.other_note AS parallel_note
FROM
  `cards.v_checklist` c
    LEFT JOIN
      `cards.v_parallels` p
        ON c.set_name = p.set_name
        AND c.subset_name = p.subset_name
        AND p.parallel_variety = '{parallel_variety}'
WHERE
  c.set_name = '{set_name}'
AND
  c.subset_name = '{subset_name}'
AND
  c.card_number = '{card_number}'
;