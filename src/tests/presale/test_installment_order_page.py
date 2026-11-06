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
from contextlib import ExitStack
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
from pretix.multidomain.models import KnownDomain


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

    @scopes_disabled()
    def test_a_completed_plan_is_not_called_cancelled(self):
        # Every order that pays off its installments ends with a completed plan, and the
        # panel used to tell all of them their plan had been canceled.
        self.plan.status = InstallmentPlan.STATUS_COMPLETED
        self.plan.installments_paid = 3
        self.plan.save(update_fields=['status', 'installments_paid'])
        self.first.state = ScheduledInstallment.STATE_PAID
        self.first.save(update_fields=['state'])
        self.order.status = Order.STATUS_PAID
        self.order.save(update_fields=['status'])

        content = self.client.get(self._url()).content.decode()
        assert 'Installment plan' in content
        assert 'The installment plan has been canceled.' not in content

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
        # A running plan is retried, not given up on: switching to paying at once is for a
        # failed first payment, which the ordinary payment page offers.
        assert 'pay-in-full' not in content
        # The plan itself is only information now: no alert of its own.
        assert 'alert-danger' not in content
        assert 'panel-danger' not in content

    @scopes_disabled()
    def test_the_pending_amount_counts_every_failed_installment(self):
        # A grace period longer than a month lets the next installment come due and fail
        # too, against the same dead token. The page says what the order owes, not what
        # the first of them was for.
        self._running_plan_with_a_failed_installment()
        self.plan.grace_period_end = now() + timedelta(days=40)
        self.plan.save(update_fields=['grace_period_end'])
        ScheduledInstallment.objects.create(
            plan=self.plan, installment_number=3, amount=Decimal('100.00'),
            due_date=now(), state=ScheduledInstallment.STATE_FAILED,
        )

        content = self.client.get(self._url()).content.decode()
        assert 'A payment of €200.00 is still pending for this order.' in content

    def test_the_recovery_page_is_laid_out_like_the_payment_page(self):
        self._running_plan_with_a_failed_installment()
        content = self.client.get(self._url('installment-recovery/')).content.decode()
        assert 'Pay order: ABCDE' in content
        assert 'A payment of €100.00 is still pending for this order.' in content
        assert 'checkout-button-row' in content
        assert 'Your payment method will be saved and used for future installment payments.' in content

    # -- Recovery collects every installment that has failed --

    @scopes_disabled()
    def _two_failed_installments(self):
        """
        A grace period longer than a month: installment 2 failed, then 3 came due and was
        charged against the same dead payment method, and failed too.
        """
        self.plan.installments_paid = 1
        self.plan.grace_period_end = now() + timedelta(days=40)
        self.plan.grace_warning_sent = True
        self.plan.save(update_fields=['installments_paid', 'grace_period_end', 'grace_warning_sent'])
        self.payment.state = OrderPayment.PAYMENT_STATE_CONFIRMED
        self.payment.payment_date = now()
        self.payment.save(update_fields=['state', 'payment_date'])
        self.first.state = ScheduledInstallment.STATE_PAID
        self.first.save(update_fields=['state'])
        return [
            ScheduledInstallment.objects.create(
                plan=self.plan, installment_number=number, amount=Decimal('100.00'),
                due_date=now() - timedelta(days=days), state=ScheduledInstallment.STATE_FAILED,
            )
            for number, days in ((2, 40), (3, 10))
        ]

    def _post_recovery(self, **provider_attrs):
        # The recovery page only serves a provider that does installments.
        provider_attrs.setdefault('installments_supported', True)
        with ExitStack() as stack:
            for name, value in provider_attrs.items():
                stack.enter_context(patch.object(
                    DummyPaymentProvider, name, new_callable=PropertyMock, return_value=value,
                ))
            return self.client.post(self._url('installment-recovery/'), {'payment': 'testdummy'})

    def test_recovery_asks_for_every_failed_installment(self):
        self._two_failed_installments()
        content = self.client.get(self._url('installment-recovery/')).content.decode()
        assert 'A payment of €200.00 is still pending for this order.' in content

    def test_recovery_settles_every_failed_installment(self):
        second, third = self._two_failed_installments()

        # execute_payment_needs_user=False so the provider settles the charge server-side
        # and the view confirms it, rather than sending the customer to a confirm page.
        r = self._post_recovery(execute_payment_needs_user=False)
        self.assertRedirects(r, self._url(), fetch_redirect_response=False)

        with scopes_disabled():
            second.refresh_from_db()
            third.refresh_from_db()
            self.plan.refresh_from_db()
            self.order.refresh_from_db()
            payment = self.order.payments.order_by('pk').last()
        assert payment.amount == Decimal('200.00')
        assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
        assert second.state == third.state == ScheduledInstallment.STATE_PAID
        assert second.payment == third.payment == payment
        assert self.plan.installments_paid == 3
        assert self.plan.status == InstallmentPlan.STATUS_COMPLETED
        # Nothing is left failed, so the deadline goes.
        assert self.plan.grace_period_end is None
        assert not self.plan.grace_warning_sent
        assert self.order.status == Order.STATUS_PAID

    def test_a_second_attempt_reuses_the_first_payment(self):
        # Customers resubmit: they hit back, they double-click, the provider bounces them
        # home. One open attempt, not an order littered with abandoned `created` rows.
        self._two_failed_installments()
        self._post_recovery(execute_payment_needs_user=True)
        self._post_recovery(execute_payment_needs_user=True)

        with scopes_disabled():
            assert self.order.payments.filter(
                state=OrderPayment.PAYMENT_STATE_CREATED, amount=Decimal('200.00')
            ).count() == 1

    @scopes_disabled()
    def test_the_retry_links_work_on_an_event_of_its_own_domain(self):
        # Under an event's own domain the presale URLs carry no organizer or event slug,
        # so a link built with {% url %} and those slugs raises NoReverseMatch -- a 500 on
        # the one page the customer needs in order to pay.
        KnownDomain.objects.create(domainname='tickets.example.org', organizer=self.orga,
                                   event=self.event, mode=KnownDomain.MODE_EVENT_DOMAIN)
        self._running_plan_with_a_failed_installment()

        # On its own domain an event's links are absolute, as the rest of the page's are.
        base = 'http://tickets.example.org/order/%s/%s/' % (self.order.code, self.order.secret)
        r = self.client.get('/order/%s/%s/' % (self.order.code, self.order.secret),
                            HTTP_HOST='tickets.example.org')
        assert r.status_code == 200
        assert base + 'installment-recovery/' in r.content.decode()

        r = self.client.get('/order/%s/%s/installment-recovery/' % (self.order.code, self.order.secret),
                            HTTP_HOST='tickets.example.org')
        assert r.status_code == 200
        assert 'href="%s"' % base in r.content.decode()

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
