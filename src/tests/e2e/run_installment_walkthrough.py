#!/usr/bin/env python
#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020  Raphael Michel and contributors
# Copyright (C) 2020-today pretix GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#
"""
Browser walkthrough of push-based bank transfer installments.

Buys a ticket in a real browser, pays it off one bank transfer at a time, and screenshots
every page the customer and the organizer see along the way. See README.md for how to run it.

The pytest suite covers the same behaviour; this exists to check what the pages and emails
actually look like, which is where the wording bugs turn up.
"""
import argparse
import os
import pathlib
import sys
from datetime import timedelta
from decimal import Decimal

import django

CHROMIUM = os.environ.get('E2E_CHROMIUM', '/opt/pw-browsers/chromium-1194/chrome-linux/chrome')

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base-url', default='http://127.0.0.1:8000',
                    help='where the pretix dev server is listening')
parser.add_argument('--shots', default='e2e-shots', help='directory to write screenshots to')
parser.add_argument('--organizer', default='efcc')
parser.add_argument('--event', default='congress')
args = parser.parse_args()

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pretix.settings')
# Playwright's sync API drives the browser from an event loop, and we do our database work
# (importing transfers, ageing the schedule) from inside it. This script is single-threaded
# and talks to the database one call at a time, so the async guard has nothing to protect.
os.environ.setdefault('DJANGO_ALLOW_ASYNC_UNSAFE', '1')
django.setup()

from django.conf import settings  # noqa: E402
from django.core import mail  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.utils.timezone import now  # noqa: E402
from django_scopes import scopes_disabled  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from pretix.base.models import (  # noqa: E402
    Event, Item, Order, Organizer, Quota, Team, User,
)
from pretix.plugins.banktransfer.models import BankImportJob  # noqa: E402
from pretix.plugins.banktransfer.tasks import \
    process_banktransfers  # noqa: E402

# Emails are part of what we are checking, so keep them where we can read them.
settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

SHOTS = pathlib.Path(args.shots)
SHOTS.mkdir(parents=True, exist_ok=True)
FAILURES = []
_step = [0]


def shot(page, name):
    _step[0] += 1
    path = SHOTS / f'{_step[0]:02d}-{name}.png'
    page.screenshot(path=str(path), full_page=True)
    print(f'    screenshot -> {path.name}')


def check(condition, message):
    print(f'    [{"PASS" if condition else "FAIL"}] {message}')
    if not condition:
        FAILURES.append(message)


def seed():
    """A live event selling one 300 EUR ticket, payable by bank transfer in installments."""
    with scopes_disabled():
        if Organizer.objects.filter(slug=args.organizer).exists():
            sys.exit(f'Organizer "{args.organizer}" already exists — start from an empty database.')

        user = User.objects.create_user('admin@example.org', 'admin1234')
        # banktransfer is a hybrid plugin: it has to be enabled on the organizer as well.
        orga = Organizer.objects.create(name='EFCC', slug=args.organizer,
                                        plugins='pretix.plugins.banktransfer')
        team = Team.objects.create(
            organizer=orga, name='Admins', all_events=True,
            all_event_permissions=True, all_organizer_permissions=True,
        )
        team.members.add(user)

        event = Event.objects.create(
            organizer=orga, name='Congress 2027', slug=args.event,
            date_from=now() + timedelta(days=365), live=True, currency='EUR',
            plugins='pretix.plugins.banktransfer',
        )
        team.limit_events.add(event)

        s = event.settings
        s.payment_banktransfer__enabled = True
        s.payment_banktransfer__restrict_to_sales_channels = ['web']
        s.payment_banktransfer_ack = True
        s.payment_banktransfer_bank_details_type = 'sepa'
        s.payment_banktransfer_bank_details_sepa_name = 'EFCC e.V.'
        s.payment_banktransfer_bank_details_sepa_iban = 'DE27520521540534534466'
        s.payment_banktransfer_bank_details_sepa_bic = 'HELADEF1MEG'
        s.payment_banktransfer_bank_details_sepa_bank = 'Sparkasse Musterstadt'
        s.installments_enabled = True
        s.installments_count = 3
        s.installments_grace_period_days = 7
        s.installments_reminder_days = 3
        s.attendee_names_asked = False
        s.invoice_address_asked = False
        s.waiting_list_enabled = False

        item = Item.objects.create(event=event, name='Full conference ticket',
                                   default_price=Decimal('300.00'), admission=True)
        Quota.objects.create(event=event, name='Tickets', size=100).items.add(item)
        return event


def the_order(event):
    with scopes_disabled():
        return Order.objects.filter(event=event).first()


def transfer(event, amount, date):
    """Import an incoming bank transfer the way an organizer's statement import would."""
    with scopes_disabled():
        order = the_order(event)
        job = BankImportJob.objects.create(event=event).pk
        process_banktransfers(job, [{
            'payer': 'Bea Buyer',
            'reference': f'{event.slug.upper()}-{order.code}',
            'date': date,
            'amount': str(amount),
        }])
        order.refresh_from_db()
        plan = order.installment_plan
        print(f'    transferred {amount}: paid={plan.installments_paid}/{plan.total_installments} '
              f'left={plan.remaining_amount} order={order.get_status_display()}')


def make_due(event, number):
    """Pull an installment's due date into the past, standing in for a month going by."""
    with scopes_disabled():
        inst = the_order(event).installment_plan.installments.get(installment_number=number)
        inst.due_date = now() - timedelta(days=1)
        inst.save(update_fields=['due_date'])


def submit(page, label='Continue'):
    page.click(f'button[type=submit]:has-text("{label}")')
    page.wait_for_load_state('networkidle')


def login_control(page):
    page.goto(f'{args.base_url}/control/login')
    page.fill('input[name=email]', 'admin@example.org')
    page.fill('input[name=password]', 'admin1234')
    submit(page, 'Log in')


def control_order(page, order):
    page.goto(f'{args.base_url}/control/event/{args.organizer}/{args.event}/orders/{order.code}/')
    page.wait_for_load_state('networkidle')
    return page.inner_text('body')


def main():
    event = seed()
    shop = f'{args.base_url}/{args.organizer}/{args.event}/'

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})

        print('\n1. Shop: put a ticket in the cart')
        page.goto(shop)
        page.fill('input[type=number]', '1')
        shot(page, 'shop')
        page.click('button:has-text("Add to cart")')
        page.wait_for_load_state('networkidle')
        shot(page, 'cart')
        page.click('button:has-text("Proceed with checkout")')
        page.wait_for_load_state('networkidle')

        print('\n2. Checkout: choose to pay in three installments')
        if '/questions/' in page.url:
            page.fill('input[name=email]', 'buyer@example.org')
            submit(page)
        shot(page, 'checkout-payment')
        options = page.locator('#installments_count option').all_text_contents()
        check(any('3 monthly installments' in o for o in options),
              'checkout offers a three-installment plan')
        page.check('#pay_in_installments')
        page.select_option('#installments_count', '3')
        page.check('input[name=payment][value=banktransfer]')
        shot(page, 'checkout-installments-selected')
        submit(page)

        print('\n3. Confirm page: what are we promising?')
        body = page.inner_text('body')
        shot(page, 'checkout-confirm')
        check('You send each of these payments yourself' in body,
              'confirm page says the CUSTOMER sends each payment')
        check('automatically charged monthly' not in body,
              'confirm page does not promise automatic charges')
        check('100.00' in body, 'confirm page shows the first installment as due today')
        submit(page, 'Place binding order')
        # Placing the order can bounce through a "please wait" page, so let the browser tell
        # us when the order exists rather than racing it in the database.
        page.wait_for_url('**/order/**', timeout=60000)
        order_url = page.url.split('?')[0]
        order = the_order(event)

        print('\n4. Order placed: first installment with bank details')
        body = page.inner_text('body')
        shot(page, 'order-installment-1-pending')
        check('which you send us yourself' in body, 'order page says the customer sends the money')
        check('Please transfer installment 1 of 3' in body, 'the pending payment is installment 1 of 3')
        check('DE27 5205 2154 0534 5344 66' in body, 'bank details are shown')
        check('2 more installments remain, which you also transfer yourself' in body,
              'the remaining installments are announced')

        print('\n5. First transfer arrives')
        transfer(event, '100.00', '2026-01-26')
        page.goto(order_url)
        body = page.inner_text('body')
        shot(page, 'order-installment-1-paid')
        check('1 of 3' in body, 'installment 1 is marked paid')
        check('Left to pay' in body and '€200.00' in body, 'left to pay is 200.00')

        print('\n6. Organizer view while the plan is on track')
        login_control(page)
        body = control_order(page, order)
        shot(page, 'control-on-track')
        check('Already paid' in body and 'Left to pay' in body,
              'control page shows paid and remaining amounts')
        check('paid by the customer' in body, 'control page names the collection mode')
        check('Retry failed installment' not in body,
              'control page does not offer the tokenized retry')

        print('\n7. Installment 2 comes due and does not arrive')
        make_due(event, 2)
        mail.outbox = []
        call_command('process_installments')
        check(len(mail.outbox) == 1, 'exactly one payment request was emailed')
        mail_body = mail.outbox[0].body if mail.outbox else ''
        check('Please transfer installment 2 of 3' in mail_body, 'the email carries the bank details')
        check('You transfer it yourself as well' in mail_body,
              'the email says the customer sends the next one too')
        check('+00:00' not in mail_body, 'the deadline is formatted, not a raw datetime')
        check('None' not in mail_body, 'the email does not end in a stray None')
        (SHOTS / 'installment-due-email.txt').write_text(mail_body)
        print('    email -> installment-due-email.txt')

        page.goto(order_url)
        body = page.inner_text('body')
        shot(page, 'order-installment-2-due')
        check('Installment 2 of 3' in body, 'order page now asks for installment 2')
        check('Please make sure your payment reaches us before' in body,
              'order page states the deadline before cancellation')

        print('\n8. Organizer view with an overdue installment')
        body = control_order(page, order)
        shot(page, 'control-overdue')
        check('overdue' in body, 'control page flags the overdue installment')
        check('Request installment payment' in body,
              'control page offers to send the request again')
        check('Grace period expires' in body, 'control page shows the grace period')

        print('\n9. The rest of the money arrives')
        transfer(event, '100.00', '2026-02-26')
        transfer(event, '100.00', '2026-03-26')
        page.goto(order_url)
        body = page.inner_text('body')
        shot(page, 'order-completed')
        check('3 of 3' in body, 'all three installments are paid')
        check('Left to pay' in body and '€0.00' in body, 'nothing is left to pay')
        check('Payment history' in body, 'the payment history lists the installments')
        order.refresh_from_db()
        check(order.status == Order.STATUS_PAID, 'the order is marked paid')

        browser.close()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} check(s) FAILED:')
        for f in FAILURES:
            print(f'  - {f}')
        sys.exit(1)
    print(f'All checks passed. Screenshots in {SHOTS}/')


main()
