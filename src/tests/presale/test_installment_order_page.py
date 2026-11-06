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

from django import forms
from django.test import TestCase
from django.utils.timezone import now
from django_scopes import scopes_disabled
from tests.testdummy.payment import DummyPaymentProvider

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

    @scopes_disabled()
    def _fail_first_installment(self):
        self.first.state = ScheduledInstallment.STATE_FAILED
        self.first.save(update_fields=['state'])

    def test_recovery_goes_straight_to_a_provider_with_nothing_to_fill_in(self):
        # A hosted-payment-page provider has no form fields: an intermediate recovery
        # page would hold nothing but a second "Update payment method" button.
        self._fail_first_installment()
        content = self.client.get(self._url()).content.decode()
        assert 'action="%s"' % self._url('installment-recovery/') in content
        assert '<input type="hidden" name="payment" value="testdummy">' in content
        assert 'href="%s"' % self._url('installment-recovery/') not in content

    def test_recovery_page_is_shown_when_the_provider_asks_for_input(self):
        self._fail_first_installment()
        fields = {'card': forms.CharField(label='Card')}
        with patch.object(DummyPaymentProvider, 'payment_form_fields', new_callable=PropertyMock,
                          return_value=fields):
            content = self.client.get(self._url()).content.decode()
        assert 'href="%s"' % self._url('installment-recovery/') in content
        assert 'action="%s"' % self._url('installment-recovery/') not in content

    def test_recovery_page_is_shown_when_the_provider_cannot_be_inspected(self):
        self._fail_first_installment()
        with patch.object(DummyPaymentProvider, 'payment_form_fields', new_callable=PropertyMock,
                          side_effect=RuntimeError('boom')):
            content = self.client.get(self._url()).content.decode()
        assert 'href="%s"' % self._url('installment-recovery/') in content

    def test_retrying_a_recovery_payment_goes_back_to_recovery(self):
        # A hosted payment page returns a cancelled or declined customer to the regular
        # retry page for the payment; for a failed installment that is the recovery page.
        self._fail_first_installment()
        r = self.client.get(self._url('pay/%d/' % self.payment.pk))
        self.assertRedirects(r, self._url('installment-recovery/'), fetch_redirect_response=False)
        r = self.client.post(self._url('pay/%d/' % self.payment.pk), {'payment': 'testdummy'})
        self.assertRedirects(r, self._url('installment-recovery/'), fetch_redirect_response=False)
        with scopes_disabled():
            self.payment.refresh_from_db()
            assert self.payment.state == OrderPayment.PAYMENT_STATE_CREATED

    def test_retrying_the_first_installment_keeps_the_regular_page(self):
        r = self.client.get(self._url('pay/%d/' % self.payment.pk))
        assert r.status_code == 200

    @scopes_disabled()
    def test_retrying_after_the_plan_is_cancelled_keeps_the_regular_page(self):
        self.first.state = ScheduledInstallment.STATE_FAILED
        self.first.save(update_fields=['state'])
        self.plan.status = InstallmentPlan.STATUS_CANCELLED
        self.plan.save(update_fields=['status'])
        r = self.client.get(self._url('pay/%d/' % self.payment.pk))
        assert r.status_code == 200

    def test_the_payment_box_is_hidden_while_the_plan_is_active(self):
        content = self.client.get(self._url()).content.decode()
        assert 'href="%s"' % self._url('pay/change') not in content

    @scopes_disabled()
    def test_a_cancelled_plan_offers_the_ordinary_payment_again(self):
        # The organizer cancelled the plan and kept the order: the open balance is to be
        # paid the ordinary way, and /pay/change takes it, so the page has to link there.
        self.plan.status = InstallmentPlan.STATUS_CANCELLED
        self.plan.installments_paid = 1
        self.plan.save(update_fields=['status', 'installments_paid'])
        self.first.state = ScheduledInstallment.STATE_CANCELLED
        self.first.save(update_fields=['state'])

        content = self.client.get(self._url()).content.decode()
        assert 'href="%s"' % self._url('pay/change') in content
        assert 'The installment plan has been canceled.' in content
        assert 'We are still waiting for your first installment payment' not in content

    # -- Paying the rest at once instead --

    @scopes_disabled()
    def _fail_first_installment_with_grace(self, grace_end):
        self._fail_first_installment()
        self.plan.grace_period_end = grace_end
        self.plan.save(update_fields=['grace_period_end'])
        self.payment.state = OrderPayment.PAYMENT_STATE_FAILED
        self.payment.save(update_fields=['state'])

    def test_a_failed_installment_offers_to_pay_in_full(self):
        self._fail_first_installment_with_grace(now() + timedelta(days=7))
        link = 'href="%s"' % self._url('installment-recovery/pay-in-full/')
        assert link in self.client.get(self._url()).content.decode()
        assert link in self.client.get(self._url('installment-recovery/')).content.decode()

    def test_pay_in_full_is_confirmed_before_anything_changes(self):
        self._fail_first_installment_with_grace(now() + timedelta(days=7))
        r = self.client.get(self._url('installment-recovery/pay-in-full/'))
        assert r.status_code == 200
        assert 'the remaining €300.00 at once' in r.content.decode()
        with scopes_disabled():
            self.plan.refresh_from_db()
        assert self.plan.status == InstallmentPlan.STATUS_ACTIVE

    def test_pay_in_full_needs_a_failed_installment(self):
        # While the plan is collecting normally there is nothing to switch away from.
        assert self.client.get(self._url('installment-recovery/pay-in-full/')).status_code == 404
        assert self.client.post(self._url('installment-recovery/pay-in-full/')).status_code == 404
        with scopes_disabled():
            self.plan.refresh_from_db()
        assert self.plan.status == InstallmentPlan.STATUS_ACTIVE

    def test_paying_in_full_cancels_the_plan_and_opens_the_ordinary_payment(self):
        # An installment order is kept out of expiry by its plan, so its own expiry date
        # is easily long past by the time an installment fails.
        with scopes_disabled():
            self.order.expires = now() - timedelta(days=1)
            self.order.save(update_fields=['expires'])
        grace_end = now() + timedelta(days=7)
        self._fail_first_installment_with_grace(grace_end)

        r = self.client.post(self._url('installment-recovery/pay-in-full/'))
        self.assertRedirects(r, self._url('pay/change'), fetch_redirect_response=False)

        with scopes_disabled():
            self.plan.refresh_from_db()
            self.first.refresh_from_db()
            self.order.refresh_from_db()
            assert self.order.all_logentries().filter(
                action_type='pretix.event.order.installment_plan.canceled',
            ).get().parsed_data['by_customer'] is True
        assert self.plan.status == InstallmentPlan.STATUS_CANCELLED
        assert self.first.state == ScheduledInstallment.STATE_CANCELLED
        # The order stays open as long as the plan would have, whatever its own expiry.
        assert self.order.expires >= grace_end
        assert self.order.status == Order.STATUS_PENDING

        # And the ordinary payment page now takes the whole open balance.
        r = self.client.get(self._url('pay/change'))
        assert r.status_code == 200
        assert 'href="%s"' % self._url('pay/change') in self.client.get(self._url()).content.decode()

    @scopes_disabled()
    def test_paying_in_full_does_not_shorten_a_later_expiry(self):
        later = now() + timedelta(days=60)
        self.order.expires = later
        self.order.save(update_fields=['expires'])
        self._fail_first_installment_with_grace(now() + timedelta(days=7))

        self.client.post(self._url('installment-recovery/pay-in-full/'))

        self.order.refresh_from_db()
        assert self.order.expires == later

    # -- A failed installment of a running plan is shown as a pending payment --

    @scopes_disabled()
    def _running_plan_with_a_failed_installment(self):
        self.plan.installments_paid = 1
        self.plan.grace_period_end = now() + timedelta(days=7)
        self.plan.save(update_fields=['installments_paid', 'grace_period_end'])
        self.first.state = ScheduledInstallment.STATE_PAID
        self.first.save(update_fields=['state'])
        return ScheduledInstallment.objects.create(
            plan=self.plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_FAILED,
        )

    def test_a_failed_installment_is_a_pending_payment_like_any_other(self):
        self._running_plan_with_a_failed_installment()
        content = self.client.get(self._url()).content.decode()
        assert 'A payment of €100.00 is still pending for this order.' in content
        assert 'Please complete your payment before' in content
        assert 'Re-try payment' in content
        # The plan itself is only information now: no alert of its own.
        assert 'alert-danger' not in content
        assert 'panel-danger' not in content

    def test_the_recovery_page_is_laid_out_like_the_payment_page(self):
        self._running_plan_with_a_failed_installment()
        content = self.client.get(self._url('installment-recovery/')).content.decode()
        assert 'Pay order: ABCDE' in content
        assert 'A payment of €100.00 is still pending for this order.' in content
        assert 'checkout-button-row' in content
        assert 'Your payment method will be saved and used for future installment payments.' in content
        assert 'href="%s"' % self._url('installment-recovery/pay-in-full/') in content

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
