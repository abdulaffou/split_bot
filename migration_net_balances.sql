-- Migration: collapse bidirectional balance pairs into a single net figure.
--
-- WHY: the old add_debt used a raw-accumulate model, so a pair could owe each
-- other in BOTH directions (e.g. A owes B ₹333 AND B owes A ₹333). The new code
-- keeps balances netted going forward, but existing rows must be collapsed once.
--
-- RUN THIS TOGETHER WITH THE CODE DEPLOY. Until it runs, old doubled balances
-- stay wrong. This rewrites live money data and DELETEs rows that net to zero —
-- eyeball `SELECT * FROM balances ORDER BY user_who_owes;` before and after.
-- It is IDEMPOTENT: a second run finds no bidirectional pairs and does nothing.

BEGIN;

-- Capture the ORIGINAL amounts for every pair that owes both ways, before any
-- update mutates them (each unordered pair appears once via the < filter).
CREATE TEMP TABLE _net ON COMMIT DROP AS
SELECT
    a.user_who_owes                        AS x,
    a.user_who_is_owed                     AS y,
    GREATEST(a.net_balance - b.net_balance, 0) AS new_xy,
    GREATEST(b.net_balance - a.net_balance, 0) AS new_yx
FROM balances a
JOIN balances b
  ON a.user_who_owes    = b.user_who_is_owed
 AND a.user_who_is_owed = b.user_who_owes
WHERE a.user_who_owes < a.user_who_is_owed;

-- Apply the netted amounts to both directions.
UPDATE balances
   SET net_balance = n.new_xy
  FROM _net n
 WHERE balances.user_who_owes = n.x AND balances.user_who_is_owed = n.y;

UPDATE balances
   SET net_balance = n.new_yx
  FROM _net n
 WHERE balances.user_who_owes = n.y AND balances.user_who_is_owed = n.x;

-- Drop any row that netted to zero (or was already <= 0).
DELETE FROM balances WHERE net_balance <= 0;

COMMIT;
