# Browser walkthroughs

Scripts here drive a real browser against a running pretix, screenshot every page, and
assert on what is actually rendered. They are **not** collected by pytest: they need a
server and a database of their own, and they take a minute rather than a second.

They earn their keep on things the unit tests cannot see — wording, page layout, and how
an email actually reads. Both bugs fixed alongside `run_installment_walkthrough.py` (a
deadline rendered as a raw `datetime`, and an unset setting printing a literal `None` at
the end of an email) were found this way and were invisible to the test suite.

## Running one

You need a built frontend (`make -C src staticfiles`) so the control panel renders, and
Chromium. Playwright is already configured to find the bundled browser; override the path
with `E2E_CHROMIUM` if yours lives elsewhere.

```
cd src

# 1. A throwaway database, seeded by the script itself.
export DATA_DIR=$(mktemp -d)
python manage.py migrate

# 2. A server against that same database.
python manage.py runserver 127.0.0.1:8000 --noreload &

# 3. The walkthrough.
python tests/e2e/run_installment_walkthrough.py --shots /tmp/shots
```

The script seeds its own organizer, event, and ticket, so it refuses to run against a
database that already has one. Point `--base-url` at the server if it is not on
`127.0.0.1:8000`.

Screenshots and the rendered emails land in `--shots`.

## `run_installment_walkthrough.py`

Push-based installments over bank transfer: buys a 300 EUR ticket in three monthly
installments, then pays it off one imported bank transfer at a time. It covers the
customer's path (checkout, confirmation, order page, the payment-due email) and the
organizer's (the installment panel, the overdue badge, the payment request button), and
checks that nothing anywhere promises the customer an automatic charge that will never
happen.
