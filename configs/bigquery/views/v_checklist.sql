CREATE OR REPLACE VIEW cards.v_checklist AS 

WITH base AS (
  SELECT
    CONCAT(set_name,"|",subset_name,"|",card_number) AS card_id,
    SPLIT(set_name,' ')[OFFSET(0)] AS set_year,
    CONCAT(set_name,"|",subset_name) AS subset_id,
    set_name,
    subset_name,
    subset_type,
    CASE
      WHEN CONTAINS_SUBSTR(card_number,"-") THEN SPLIT(card_number,"-")[OFFSET(1)]
      WHEN LEFT(card_number,3) = "USC" THEN RIGHT(card_number,(LENGTH(card_number)-3))
      WHEN LEFT(card_number,2) = "US" THEN RIGHT(card_number,(LENGTH(card_number)-2))
        ELSE card_number END AS derived_card_number,
    card_number,
    player,
    team,
    note
  FROM
    `cards.checklist`
),

RC_check AS (
  SELECT
    set_year,
    player,
    MAX(note) AS note
  FROM
    base
  WHERE
    note = "RC"
  AND
    subset_name = "Base"
  GROUP BY
    1,2
)

SELECT
  b.*,
  CASE WHEN rc.note = "RC" THEN "RC" ELSE NULL END AS note_check
FROM
  base b
    LEFT JOIN RC_check rc
      ON b.set_year = rc.set_year
      AND b.player = rc.player

ORDER BY
  set_name,
  subset_type,
  subset_name,
  SAFE_CAST(derived_card_number AS INT64),
  derived_card_number
;