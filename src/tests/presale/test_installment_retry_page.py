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
from unittest.mock import PropertyMock, patch

from django.test import TestCase
from django.utils.timezone import now
from django_scopes import scopes_disabled
from tests.testdummy.payment import DummyPaymentProvider

from pretix.base.models import (
    Event, Item, Order, OrderPosition, Organizer, Quota,
)
from pretix.base.models.orders import OrderPayment
from pretix.base.services.installments import create_installment_plan
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment


class InstallmentRetryPageTest(TestCase):
    """
    The ordinary payment page offers the same choice as checkout: pay at once, or in
    installments. A customer whose payment failed gets that choice back -- whether they
    had paid at once or chosen installments -- as long as nothing has been collected.
    """

    @scopes_disabled()
    def setUp(self):
        super().setUp()
        self.orga = Organizer.objects.create(name='CCC', slug='ccc')
        self.event = Event.objects.create(
            organizer=self.orga, name='30C3', slug='30c3', date_from=now() + timedelta(days=200),
            plugins='tests.testdummy', live=True,
        )
        self.event.settings.set('payment_testdummy__enabled', True)
        self.event.settings.set('installments_enabled', True)
        self.event.settings.set('installments_count', 3)

        quota = Quota.objects.create(event=self.event, name='Tickets', size=5)
        ticket = Item.objects.create(event=self.event, name='Ticket', default_price=Decimal('300.00'),
                                     admission=True)
        quota.items.add(ticket)

        self.order = Order.objects.create(
            code='ABCDE', event=self.event, email='test@example.com', status=Order.STATUS_PENDING,
            datetime=now(), expires=now() + timedelta(days=10),
            total=Decimal('300.00'), locale='en',
            sales_channel=self.orga.sales_channels.get(identifier='web'),
        )
        OrderPosition.objects.create(order=self.order, item=ticket, variation=None, price=Decimal('300.00'))

        supported = patch.object(DummyPaymentProvider, 'installments_supported', new_callable=PropertyMock,
                                 return_value=True)
        supported.start()
        self.addCleanup(supported.stop)

    def _url(self, suffix=''):
        return '/%s/%s/order/%s/%s/%s' % (self.orga.slug, self.event.slug, self.order.code,
                                          self.order.secret, suffix)

    @scopes_disabled()
    def _failed_first_installment(self, count=3):
        """Checkout with installments, and the first payment failed."""
        plan = create_installment_plan(self.order, 'testdummy', count)
        plan.first_payment.fail(send_mail=False)
        plan.refresh_from_db()
        return plan

    def _choose(self, installments=None):
        data = {'payment': 'testdummy'}
        if installments:
            data.update({'pay_in_installments': 'on', 'installments_count': str(installments)})
        return self.client.post(self._url('pay/change'), data)

    # -- What the page offers --

    def test_an_order_paid_at_once_can_switch_to_installments(self):
        content = self.client.get(self._url('pay/change')).content.decode()
        assert 'name="pay_in_installments"' in content
        assert 'checked="checked">\n                    <strong>Pay in installments' not in content
        assert '<option value="2"' in content and '<option value="3"' in content
        assert 'data-installments-available="true"' in content

    def test_no_choice_where_installments_are_off(self):
        with scopes_disabled():
            self.event.settings.set('installments_enabled', False)
        assert 'name="pay_in_installments"' not in self.client.get(self._url('pay/change')).content.decode()

    def test_a_failed_first_installment_offers_its_choice_again(self):
        self._failed_first_installment(count=3)

        content = self.client.get(self._url()).content.decode()
        # An ordinary unpaid order: the payment box, no installment plan to speak of.
        assert 'href="%s"' % self._url('pay/change') in content
        assert 'Installment plan' not in content

        content = self.client.get(self._url('pay/change')).content.decode()
        assert 'id="pay_in_installments"\n                           checked="checked"' in content
        assert '<option value="3" selected' in content
        assert 'value="testdummy"\n                                        checked="checked"' in content

    def test_a_failed_first_installment_is_not_recovered(self):
        # It is paid the ordinary way now, not through the installment recovery page.
        self._failed_first_installment()
        assert self.client.get(self._url('installment-recovery/')).status_code == 404

    @scopes_disabled()
    def test_a_plan_that_collected_money_is_not_offered_again(self):
        plan = create_installment_plan(self.order, 'testdummy', 3)
        plan.status = InstallmentPlan.STATUS_CANCELLED
        plan.installments_paid = 1
        plan.save(update_fields=['status', 'installments_paid'])
        assert 'name="pay_in_installments"' not in self.client.get(self._url('pay/change')).content.decode()

    # -- What choosing does --

    def test_switching_to_installments_sets_up_a_plan(self):
        r = self._choose(installments=2)
        assert r.status_code == 302

        with scopes_disabled():
            plan = self.order.installment_plan
            first = plan.installments.get(installment_number=1)
            assert plan.status == InstallmentPlan.STATUS_ACTIVE
            assert plan.total_installments == 2
            assert list(plan.installments.values_list('amount', flat=True).order_by('installment_number')) == [
                Decimal('150.00'), Decimal('150.00'),
            ]
            assert first.payment.amount == Decimal('150.00')
            assert first.payment.state == OrderPayment.PAYMENT_STATE_CREATED
            assert r.url.endswith('/pay/%d/confirm' % first.payment.pk)

    def test_the_payment_fee_rides_on_the_first_installment(self):
        with scopes_disabled():
            self.event.settings.set('payment_testdummy__fee_abs', '5.00')
        self._choose(installments=2)

        with scopes_disabled():
            self.order.refresh_from_db()
            first = self.order.installment_plan.installments.get(installment_number=1)
            second = self.order.installment_plan.installments.get(installment_number=2)
        assert self.order.total == Decimal('305.00')
        assert first.amount == first.payment.amount == Decimal('155.00')
        assert second.amount == Decimal('150.00')

    def test_paying_at_once_after_a_failed_first_installment(self):
        plan = self._failed_first_installment()
        r = self._choose()
        assert r.status_code == 302

        with scopes_disabled():
            plan.refresh_from_db()
            new = self.order.payments.order_by('pk').last()
            assert not ScheduledInstallment.objects.filter(payment=new).exists()
        assert plan.status == InstallmentPlan.STATUS_CANCELLED
        assert new.amount == Decimal('300.00')

    def test_picking_another_plan_after_a_failed_first_installment(self):
        plan = self._failed_first_installment(count=3)
        with scopes_disabled():
            failed_payment = plan.first_payment
        self._choose(installments=2)

        with scopes_disabled():
            plan.refresh_from_db()
            failed_payment.refresh_from_db()
            numbers = list(plan.installments.values_list('installment_number', flat=True).order_by('installment_number'))
            first = plan.installments.get(installment_number=1)
        assert plan.status == InstallmentPlan.STATUS_ACTIVE
        assert plan.total_installments == 2
        assert numbers == [1, 2]
        assert first.payment != failed_payment
        # The failed attempt stays on the order, which is what records it.
        assert failed_payment.state == OrderPayment.PAYMENT_STATE_FAILED

    def test_an_invalid_plan_changes_nothing(self):
        r = self._choose(installments=7)
        assert r.status_code == 200
        assert 'Please select a valid installment plan.' in r.content.decode()
        with scopes_disabled():
            assert not InstallmentPlan.objects.filter(order=self.order).exists()
            assert not self.order.payments.exists()

    def test_a_plan_that_cannot_be_set_up_rolls_the_payment_change_back(self):
        """
        The event can move too close for the chosen number of installments between the page
        loading and the submit. Everything the payment change did goes back with the plan:
        the order's total, its payment fee, its payment and its invoice.
        """
        with scopes_disabled():
            self.event.settings.set('payment_testdummy__fee_abs', '5.00')
            self.event.settings.set('invoice_generate', 'True')

        with patch('pretix.presale.views.order.set_up_installment_plan',
                   side_effect=ValueError('no longer available')):
            r = self._choose(installments=2)

        assert r.status_code == 200
        assert 'Please select a valid installment plan.' in r.content.decode()
        with scopes_disabled():
            self.order.refresh_from_db()
            assert self.order.total == Decimal('300.00')
            assert not self.order.fees.exists()
            assert not self.order.payments.exists()
            assert not self.order.invoices.exists()
            assert not InstallmentPlan.objects.filter(order=self.order).exists()

    def test_a_failure_that_is_not_about_installments_is_not_swallowed(self):
        # The handler used to report any ValueError from the payment change as a bad
        # installment plan, on a page that need not even offer installments.
        with patch('pretix.presale.views.order.change_payment_provider',
                   side_effect=ValueError('something else entirely')):
            with self.assertRaises(ValueError):
                self._choose()

    def test_the_checkout_choice_is_the_same_markup(self):
        # Both pages render the shared fragment, which installments.js relies on.
        content = self.client.get(self._url('pay/change')).content.decode()
        for marker in ('id="installments_count_group"', 'id="installments_count"', 'data-payment-provider'):
            assert marker in content
