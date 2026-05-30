# AI Code Review

**Verdict:** request changes
**Overall score:** 5.95/10

## Scores
| Dimension | Score |
|-----------|-------|
| Code quality | 7/10 |
| Security | 4/10 |
| Performance | 6/10 |
| Maintainability | 8/10 |

## Summary
Adds a user-lookup endpoint and an order-summary view. The feature works, but the lookup builds SQL via string concatenation (a critical injection risk) and the summary issues a query per order. Address the security issue before merging.

_Intent:_ Add a user-lookup endpoint and an order-summary view.

## Critical findings
- **Possible SQL injection** (app/db.py:42) — User-supplied `user_id` is concatenated directly into a SQL query string. An attacker can inject arbitrary SQL (e.g. `1 OR 1=1`) and read or modify other users' data.

## File reviews
### `app/db.py` (Python)
1 finding(s), 1 critical.
- `critical` **Possible SQL injection** (app/db.py:42) — User-supplied `user_id` is concatenated directly into a SQL query string. An attacker can inject arbitrary SQL (e.g. `1 OR 1=1`) and read or modify other users' data.
  - _Suggestion:_ Use a parameterized query instead of string concatenation.
  - _Fix:_
    ```diff
    - cursor.execute("SELECT * FROM users WHERE id=" + user_id)
    + cursor.execute("SELECT * FROM users WHERE id=%s", (user_id,))
    ```

### `app/services.py` (Python)
2 finding(s).
- `warning` **N+1 query inside loop** (app/services.py:88) — A database query runs once per iteration over `orders`, turning one page load into N+1 round trips.
  - _Suggestion:_ Fetch the related rows in a single query before the loop (e.g. a join or an `IN (...)` batch).
- `warning` **Bare except swallows errors** (app/services.py:12) — `except:` hides every error, including KeyboardInterrupt, and makes failures invisible.
  - _Suggestion:_ Catch the specific exception you expect and log it.

### `app/utils.py` (Python)
1 finding(s).
- `info` **Missing function docstring** (app/utils.py:5) — `compute_total` has no docstring explaining its contract.
  - _Suggestion:_ Add a one-line docstring describing inputs and return value.

---
_model: gemini-2.0-flash (curated sample) · 18.7s · 2026-01-01T00:00:00+00:00_