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
Buying a ticket in installments paid by bank transfer, from the shop to a paid order.

The unit tests in tests/plugins/banktransfer/test_installments.py cover the state machine.
What this adds is the rendering: whether the pages and emails a customer actually reads say
the right thing. Push installments are settled by the customer, so a page that promises an
automatic charge — as the confirmation page once did — costs somebody their order.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail as djmail
from django.core.management import call_command
from django.utils.timezone import now
from django_scopes import scopes_disabled
from playwright.sync_api import Page, expect

from pretix.base.models import Event, Item, Order, Organizer, Quota
from pretix.plugins.banktransfer.models import BankImportJob
from pretix.plugins.banktransfer.tasks import process_banktransfers

IBAN = 'DE27520521540534534466'
#: The IBAN as the bank transfer provider renders it, in groups of four.
IBAN_SHOWN = 'DE27 5205 2154 0534 5344 66'


@pytest.fixture
@scopes_disabled()
def installment_event(db):
    """An event selling one 300 EUR ticket, payable by bank transfer in three installments."""
    organizer = Organizer.objects.create(
        name='Test Organizer', slug='testorg',
        # banktransfer is a hybrid plugin: it has to be enabled on the organizer as well.
        plugins='pretix.plugins.banktransfer',
    )
    event = Event.objects.create(
        organizer=organizer, name='Installment Event', slug='installments',
        date_from=now() + timedelta(days=365),
        currency='EUR', live=True, plugins='pretix.plugins.banktransfer',
    )
    event.set_defaults()

    s = event.settings
    s.set('timezone', 'UTC')
    s.set('locale', 'en')
    s.set('locales', ['en'])
    s.payment_banktransfer__enabled = True
    s.payment_banktransfer__restrict_to_sales_channels = ['web']
    s.payment_banktransfer_ack = True
    s.payment_banktransfer_bank_details_type = 'sepa'
    s.payment_banktransfer_bank_details_sepa_name = 'Test Organizer e.V.'
    s.payment_banktransfer_bank_details_sepa_iban = IBAN
    s.payment_banktransfer_bank_details_sepa_bic = 'HELADEF1MEG'
    s.payment_banktransfer_bank_details_sepa_bank = 'Sparkasse Musterstadt'
    s.installments_enabled = True
    s.installments_count = 3
    s.installments_grace_period_days = 7
    s.installments_reminder_days = 3
    s.attendee_names_asked = False
    s.invoice_address_asked = False
    s.waiting_list_enabled = False

    item = Item.objects.create(
        event=event, name='Full conference ticket',
        default_price=Decimal('300.00'), admission=True, active=True,
    )
    Quota.objects.create(event=event, name='Tickets', size=100).items.add(item)
    return event


def _submit(page: Page, label='Continue'):
    page.click(f'button[type=submit]:has-text("{label}")')
    page.wait_for_load_state('networkidle')


@scopes_disabled()
def _the_order(event):
    return Order.objects.filter(event=event).first()


@scopes_disabled()
def _transfer(event, amount, date):
    """Import an incoming bank transfer the way an organizer's statement import would."""
    order = _the_order(event)
    job = BankImportJob.objects.create(event=event).pk
    process_banktransfers(job, [{
        'payer': 'Bea Buyer',
        'reference': f'{event.slug.upper()}-{order.code}',
        'date': date,
        'amount': str(amount),
    }])


@scopes_disabled()
def _make_due(event, number):
    """Pull an installment's due date into the past, standing in for a month going by."""
    inst = _the_order(event).installment_plan.installments.get(installment_number=number)
    inst.due_date = now() - timedelta(days=1)
    inst.save(update_fields=['due_date'])


@pytest.mark.django_db
class TestPushInstallmentJourney:

    def test_buying_and_paying_off_a_bank_transfer_installment_plan(
        self, page: Page, live_server_url: str, installment_event
    ):
        event = installment_event
        shop = f'{live_server_url}/{event.organizer.slug}/{event.slug}/'

        # -- The customer picks a three-installment plan --------------------------------
        page.goto(shop)
        page.fill('input[type=number]', '1')
        page.click('button:has-text("Add to cart")')
        page.wait_for_load_state('networkidle')
        page.click('button:has-text("Proceed with checkout")')
        page.wait_for_load_state('networkidle')

        if '/questions/' in page.url:
            page.fill('input[name=email]', 'buyer@example.org')
            _submit(page)

        expect(page.locator('#pay_in_installments')).to_be_visible()
        options = page.locator('#installments_count option').all_text_contents()
        assert any('3 monthly installments' in o for o in options)
        page.check('#pay_in_installments')
        page.select_option('#installments_count', '3')
        page.check('input[name=payment][value=banktransfer]')
        _submit(page)

        # -- The confirmation page has to say who does the paying ------------------------
        body = page.inner_text('body')
        assert 'You have selected to pay in installments' in body
        assert 'You send each of these payments yourself' in body, \
            'the confirmation page must not leave the customer waiting for a charge'
        assert 'automatically charged monthly' not in body
        assert '100.00' in body

        _submit(page, 'Place binding order')
        page.wait_for_url('**/order/**', timeout=60000)
        order_url = page.url.split('?')[0]

        # -- The order page hands over the details for the first installment -------------
        body = page.inner_text('body')
        assert 'which you send us yourself' in body
        assert 'Please transfer installment 1 of 3' in body
        assert IBAN_SHOWN in body
        assert '2 more installments remain, which you also transfer yourself' in body

        with scopes_disabled():
            plan = _the_order(event).installment_plan
            assert plan.is_push
            assert plan.remaining_amount == Decimal('300.00')

        # -- Money arrives, and settles exactly one installment ---------------------------
        _transfer(event, '100.00', '2026-01-26')
        page.goto(order_url)
        body = page.inner_text('body')
        assert 'Installments paid' in body and '1 of 3' in body
        assert 'Left to pay' in body and '€200.00' in body

        # -- The next one comes due and is chased with the bank details ------------------
        _make_due(event, 2)
        djmail.outbox = []
        call_command('process_installments')

        assert len(djmail.outbox) == 1
        mail_body = djmail.outbox[0].body
        assert 'Please transfer installment 2 of 3' in mail_body
        assert IBAN_SHOWN in mail_body
        assert 'You transfer it yourself as well' in mail_body
        # A raw datetime would leak microseconds and a UTC offset into the email, and an
        # unset free-text bank details setting used to render as a literal "None".
        assert '+00:00' not in mail_body
        assert 'None' not in mail_body

        page.goto(order_url)
        body = page.inner_text('body')
        assert 'Installment 2 of 3' in body
        assert 'Please make sure your payment reaches us before' in body

        # -- Paying it off finishes the plan and the order --------------------------------
        _transfer(event, '100.00', '2026-02-26')
        _transfer(event, '100.00', '2026-03-26')

        page.goto(order_url)
        body = page.inner_text('body')
        assert 'Installments paid' in body and '3 of 3' in body
        assert 'Left to pay' in body and '€0.00' in body
        assert 'Payment history' in body

        with scopes_disabled():
            order = _the_order(event)
            assert order.status == Order.STATUS_PAID
            assert order.installment_plan.remaining_amount == Decimal('0.00')
