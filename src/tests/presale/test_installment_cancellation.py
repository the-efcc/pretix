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
from pretix.base.models.orders import OrderPayment, OrderRefund
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment


class InstallmentCancellationTest(TestCase):
    """
    An order that is being paid in installments never reaches ``paid`` until the last
    installment settles, but it does carry the money collected so far. Canceling it is
    therefore governed by the organizer's rules for paid orders, and pays the customer back.
    """

    @scopes_disabled()
    def setUp(self):
        super().setUp()
        self.orga = Organizer.objects.create(name='CCC', slug='ccc')
        self.event = Event.objects.create(
            organizer=self.orga, name='30C3', slug='30c3', date_from=now() + timedelta(days=30),
            plugins='tests.testdummy', live=True,
        )
        self.event.settings.cancel_allow_user_paid = True

        self.quota = Quota.objects.create(event=self.event, name='Tickets', size=5)
        self.ticket = Item.objects.create(event=self.event, name='Ticket', default_price=Decimal('300.00'),
                                          admission=True)
        self.quota.items.add(self.ticket)

        self.order = Order.objects.create(
            code='ABCDE', event=self.event, email='test@example.com', status=Order.STATUS_PENDING,
            datetime=now() - timedelta(days=3), expires=now() + timedelta(days=30),
            total=Decimal('300.00'), locale='en',
            sales_channel=self.orga.sales_channels.get(identifier='web'),
        )
        OrderPosition.objects.create(order=self.order, item=self.ticket, variation=None,
                                     price=Decimal('300.00'))

        self.plan = InstallmentPlan.objects.create(
            order=self.order, payment_provider='testdummy_partialrefund',
            payment_token={'token': 'tok_123'}, total_installments=3, installments_paid=1,
            amount_per_installment=Decimal('100.00'), status=InstallmentPlan.STATUS_ACTIVE,
        )
        self.payment = self.order.payments.create(
            provider='testdummy_partialrefund', amount=Decimal('100.00'),
            state=OrderPayment.PAYMENT_STATE_CONFIRMED, payment_date=now(),
        )
        ScheduledInstallment.objects.create(
            plan=self.plan, installment_number=1, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PAID,
            payment=self.payment, processed_at=now(),
        )
        for i in (2, 3):
            ScheduledInstallment.objects.create(
                plan=self.plan, installment_number=i, amount=Decimal('100.00'),
                due_date=now() + timedelta(days=30 * i), state=ScheduledInstallment.STATE_PENDING,
            )

    def _url(self, action=''):
        return '/%s/%s/order/%s/%s/cancel%s' % (self.orga.slug, self.event.slug, self.order.code,
                                                self.order.secret, action)

    def test_cancel_page_offers_refund(self):
        r = self.client.get(self._url())
        assert r.status_code == 200
        content = r.content.decode()
        assert 'Refund amount:' in content
        assert '100.00' in content

    def test_cancel_refunds_settled_installments(self):
        self.client.post(self._url('/do'), {}, follow=True)

        self.order.refresh_from_db()
        assert self.order.status == Order.STATUS_CANCELED
        with scopes_disabled():
            refund = self.order.refunds.get()
        assert refund.amount == Decimal('100.00')
        assert refund.state == OrderRefund.REFUND_STATE_DONE
        assert refund.payment == self.payment
        assert self.order.pending_sum == Decimal('0.00')

    def test_cancel_stops_the_remaining_installments(self):
        self.client.post(self._url('/do'), {}, follow=True)

        self.plan.refresh_from_db()
        assert self.plan.status == InstallmentPlan.STATUS_CANCELLED
        assert self.plan.payment_token == {}
        with scopes_disabled():
            states = set(self.plan.installments.filter(installment_number__in=(2, 3))
                         .values_list('state', flat=True))
        assert states == {ScheduledInstallment.STATE_CANCELLED}

    def test_cancellation_fee_is_kept_from_what_was_paid(self):
        self.event.settings.cancel_allow_user_paid_keep = Decimal('30.00')

        self.client.post(self._url('/do'), {}, follow=True)

        self.order.refresh_from_db()
        assert self.order.status == Order.STATUS_PAID
        assert self.order.total == Decimal('30.00')
        with scopes_disabled():
            assert self.order.refunds.get().amount == Decimal('70.00')

    def test_cancellation_fee_is_capped_at_what_was_paid(self):
        # A fee derived from the full order total can exceed the installments collected so
        # far. Cancelling must not leave the customer owing money on an order that is gone.
        self.event.settings.cancel_allow_user_paid_keep = Decimal('250.00')

        with scopes_disabled():
            assert self.order.user_cancel_fee == Decimal('100.00')

        self.client.post(self._url('/do'), {}, follow=True)

        self.order.refresh_from_db()
        assert self.order.status == Order.STATUS_PAID
        assert self.order.total == Decimal('100.00')
        assert self.order.pending_sum == Decimal('0.00')
        with scopes_disabled():
            assert not self.order.refunds.exists()

    def test_cancellation_can_require_approval(self):
        self.event.settings.cancel_allow_user_paid_require_approval = True
        self.event.settings.cancel_allow_user_paid_keep = Decimal('30.00')

        self.client.post(self._url('/do'), {}, follow=True)

        self.order.refresh_from_db()
        assert self.order.status == Order.STATUS_PENDING
        self.plan.refresh_from_db()
        assert self.plan.status == InstallmentPlan.STATUS_ACTIVE
        with scopes_disabled():
            assert not self.order.refunds.exists()
            assert self.order.cancellation_requests.get().cancellation_fee == Decimal('30.00')

    def test_not_allowed_unless_the_organizer_allows_cancelling_paid_orders(self):
        self.event.settings.cancel_allow_user_paid = False

        with scopes_disabled():
            assert not self.order.user_cancel_allowed

        assert self.client.get(self._url()).status_code == 302
        self.client.post(self._url('/do'), {}, follow=True)
        self.order.refresh_from_db()
        assert self.order.status == Order.STATUS_PENDING

    def test_partially_paid_order_without_installment_plan_stays_blocked(self):
        with scopes_disabled():
            self.plan.installments.all().delete()
            self.plan.delete()
            order = Order.objects.get(pk=self.order.pk)
            assert not order.user_cancel_allowed

        assert self.client.get(self._url()).status_code == 302
        self.client.post(self._url('/do'), {}, follow=True)
        self.order.refresh_from_db()
        assert self.order.status == Order.STATUS_PENDING

    def test_unpaid_installment_order_follows_the_unpaid_rules(self):
        # Nothing collected yet: this is an ordinary unpaid order, fee and all.
        self.event.settings.cancel_allow_user_paid = False
        self.event.settings.cancel_allow_user = True
        self.event.settings.cancel_allow_user_unpaid_keep = Decimal('10.00')
        with scopes_disabled():
            self.order.payments.all().delete()
            self.plan.installments.update(state=ScheduledInstallment.STATE_PENDING, payment=None)

        with scopes_disabled():
            order = Order.objects.get(pk=self.order.pk)
            assert order.user_cancel_allowed
            assert order.user_cancel_fee == Decimal('10.00')

        self.client.post(self._url('/do'), {}, follow=True)

        self.order.refresh_from_db()
        assert self.order.status == Order.STATUS_PENDING
        assert self.order.total == Decimal('10.00')

    def test_customer_can_choose_a_higher_fee(self):
        self.event.settings.cancel_allow_user_paid_adjust_fees = True
        self.event.settings.cancel_allow_user_paid_keep = Decimal('30.00')

        self.client.post(self._url('/do'), {'cancel_fee': '50.00'}, follow=True)

        self.order.refresh_from_db()
        assert self.order.total == Decimal('50.00')
        with scopes_disabled():
            assert self.order.refunds.get().amount == Decimal('50.00')

    def test_chosen_fee_cannot_exceed_what_was_paid(self):
        self.event.settings.cancel_allow_user_paid_adjust_fees = True
        self.event.settings.cancel_allow_user_paid_keep = Decimal('30.00')

        self.client.post(self._url('/do'), {'cancel_fee': '150.00'}, follow=True)

        self.order.refresh_from_db()
        assert self.order.status == Order.STATUS_PENDING
        assert self.order.total == Decimal('300.00')
        with scopes_disabled():
            assert not self.order.refunds.exists()
