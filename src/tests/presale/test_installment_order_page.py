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
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.base.models import (
    Event, Item, Order, OrderPosition, Organizer, Quota,
)
from pretix.base.models.orders import OrderPayment
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment


class InstallmentOrderPageTest(TestCase):
    """
    What the customer's order page offers for an order paid in installments: the plan
    collects the money, so the regular payment page stays out of the way, and the
    confirmation only claims a payment that has actually been received.
    """

    @scopes_disabled()
    def setUp(self):
        super().setUp()
        self.orga = Organizer.objects.create(name='CCC', slug='ccc')
        self.event = Event.objects.create(
            organizer=self.orga, name='30C3', slug='30c3', date_from=now() + timedelta(days=30),
            plugins='tests.testdummy', live=True,
        )
        self.event.settings.set('payment_testdummy__enabled', True)

        self.quota = Quota.objects.create(event=self.event, name='Tickets', size=5)
        self.ticket = Item.objects.create(event=self.event, name='Ticket', default_price=Decimal('300.00'),
                                          admission=True)
        self.quota.items.add(self.ticket)

        self.order = Order.objects.create(
            code='ABCDE', event=self.event, email='test@example.com', status=Order.STATUS_PENDING,
            datetime=now(), expires=now() + timedelta(days=30),
            total=Decimal('300.00'), locale='en',
            sales_channel=self.orga.sales_channels.get(identifier='web'),
        )
        OrderPosition.objects.create(order=self.order, item=self.ticket, variation=None,
                                     price=Decimal('300.00'))

        self.plan = InstallmentPlan.objects.create(
            order=self.order, payment_provider='testdummy', payment_token={}, total_installments=3, installments_paid=0,
            amount_per_installment=Decimal('100.00'), status=InstallmentPlan.STATUS_ACTIVE,
        )
        self.payment = self.order.payments.create(
            provider='testdummy', amount=Decimal('100.00'), state=OrderPayment.PAYMENT_STATE_CREATED,
        )
        self.first = ScheduledInstallment.objects.create(
            plan=self.plan, installment_number=1, amount=Decimal('100.00'),
            due_date=now(), state=ScheduledInstallment.STATE_PENDING, payment=self.payment,
        )

    def _url(self, suffix=''):
        return '/%s/%s/order/%s/%s/%s' % (self.orga.slug, self.event.slug, self.order.code,
                                          self.order.secret, suffix)

    def test_regular_payment_page_is_refused_while_the_plan_is_active(self):
        r = self.client.get(self._url('pay/change'))
        self.assertRedirects(r, self._url(), fetch_redirect_response=False)
        r = self.client.post(self._url('pay/change'), {'payment': 'testdummy'})
        self.assertRedirects(r, self._url(), fetch_redirect_response=False)
        with scopes_disabled():
            assert self.order.payments.count() == 1

    @scopes_disabled()
    def test_regular_payment_page_sends_a_failed_installment_to_recovery(self):
        self.first.state = ScheduledInstallment.STATE_FAILED
        self.first.save(update_fields=['state'])

        r = self.client.get(self._url('pay/change'))
        self.assertRedirects(r, self._url('installment-recovery/'), fetch_redirect_response=False)

    @scopes_disabled()
    def test_regular_payment_page_is_open_once_the_plan_is_cancelled(self):
        # The organizer can cancel a plan and keep the order, which then has to be paid
        # off the ordinary way.
        self.plan.status = InstallmentPlan.STATUS_CANCELLED
        self.plan.save(update_fields=['status'])

        r = self.client.get(self._url('pay/change'))
        assert r.status_code == 200

    def test_thank_you_does_not_claim_an_uncollected_first_installment(self):
        content = self.client.get(self._url() + '?thanks=1').content.decode()
        assert 'We are still waiting for your first installment payment to be confirmed.' in content
        assert 'Your first installment has been received.' not in content

    @scopes_disabled()
    def test_thank_you_confirms_a_collected_first_installment(self):
        self.plan.installments_paid = 1
        self.plan.save(update_fields=['installments_paid'])

        content = self.client.get(self._url() + '?thanks=1').content.decode()
        assert 'Your first installment has been received.' in content
        assert 'We are still waiting for your first installment payment' not in content
