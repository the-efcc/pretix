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
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django import forms
from django.test import TestCase
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.base.models import Event, Order, Organizer
from pretix.base.models.orders import OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment


class FormInstallmentProvider(BasePaymentProvider):
    """
    A provider that leaves payment_form / payment_form_render / payment_prepare to the
    base implementation, so the request goes through pretix's real form binding.
    """
    identifier = 'dummy'
    verbose_name = 'Dummy'
    installments_supported = True
    execute_payment_needs_user = False

    @property
    def payment_form_fields(self):
        return OrderedDict([
            ('token', forms.CharField(label='Token', required=True)),
        ])

    def execute_payment(self, request, payment):
        return None


class TestInstallmentRecovery(TestCase):

    @scopes_disabled()
    def setUp(self):
        super().setUp()
        self.orga = Organizer.objects.create(name='Dummy', slug='dummy')
        self.orga.settings.customer_accounts = False
        self.event = Event.objects.create(
            organizer=self.orga, name='Dummy Event', slug='dummy',
            date_from=now() + timedelta(days=365), live=True,
        )
        self.order = Order.objects.create(
            code='ABCDE', event=self.event, email='test@example.com',
            status=Order.STATUS_PENDING, datetime=now(),
            expires=now() + timedelta(days=10), total=Decimal('300.00'),
            locale='en',
            sales_channel=self.orga.sales_channels.get(identifier="web"),
            customer=None,
        )
        self.plan = InstallmentPlan.objects.create(
            order=self.order, payment_provider='dummy',
            payment_token={'token': 'tok_123'}, total_installments=3,
            installments_paid=1, amount_per_installment=Decimal('100.00'),
            status=InstallmentPlan.STATUS_ACTIVE,
            grace_period_end=now() + timedelta(days=5),
        )
        self.installment = ScheduledInstallment.objects.create(
            plan=self.plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=2), state=ScheduledInstallment.STATE_FAILED,
        )

    def _url(self):
        return f'/{self.orga.slug}/{self.event.slug}/order/{self.order.code}/{self.order.secret}/installment-recovery/'

    def _mock_provider(self, **overrides):
        defaults = {
            'installments_supported': True,
            'payment_prepare': True,
            'execute_payment_needs_user': False,
            'execute_payment': None,
            'payment_form_render': '<form></form>',
        }
        defaults.update(overrides)
        provider = MagicMock()
        provider.installments_supported = defaults['installments_supported']
        provider.execute_payment_needs_user = defaults['execute_payment_needs_user']
        provider.payment_prepare.return_value = defaults['payment_prepare']
        provider.execute_payment.return_value = defaults['execute_payment']
        provider.payment_form_render.return_value = defaults['payment_form_render']
        return provider

    # -- Dispatch / access control --

    @scopes_disabled()
    def test_404_without_plan(self):
        self.plan.delete()
        assert self.client.get(self._url()).status_code == 404

    @scopes_disabled()
    def test_404_without_failed_installment(self):
        self.installment.state = ScheduledInstallment.STATE_PAID
        self.installment.save()
        assert self.client.get(self._url()).status_code == 404

    def test_404_with_wrong_secret(self):
        url = f'/{self.orga.slug}/{self.event.slug}/order/{self.order.code}/WRONGSECRET/installment-recovery/'
        assert self.client.get(url).status_code == 404

    @scopes_disabled()
    def test_asks_for_every_failed_installment(self):
        third = ScheduledInstallment.objects.create(
            plan=self.plan, installment_number=3, amount=Decimal('100.00'),
            due_date=now() + timedelta(days=5), state=ScheduledInstallment.STATE_FAILED,
        )
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': self._mock_provider()}):
            response = self.client.get(self._url())
        assert response.status_code == 200
        assert [i.pk for i in response.context['failed_installments']] == [self.installment.pk, third.pk]
        assert response.context['amount'] == self.installment.amount + third.amount

    # -- GET rendering --

    def test_get_renders_200(self):
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': self._mock_provider()}):
            assert self.client.get(self._url()).status_code == 200

    def test_get_shows_payment_form(self):
        provider = self._mock_provider(payment_form_render='<form>test</form>')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.get(self._url())
        assert response.context['payment_form_html'] == '<form>test</form>'

    def test_get_error_when_provider_missing(self):
        with patch('pretix.base.models.Event.get_payment_providers', return_value={}):
            response = self.client.get(self._url())
        assert response.status_code == 200
        assert 'payment_form_error' in response.context

    def test_get_error_when_installments_not_supported(self):
        provider = self._mock_provider(installments_supported=False)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.get(self._url())
        assert response.status_code == 200
        assert 'payment_form_error' in response.context

    def test_get_error_when_render_raises(self):
        provider = self._mock_provider()
        provider.payment_form_render.side_effect = Exception('render error')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.get(self._url())
        assert response.status_code == 200
        assert 'payment_form_error' in response.context

    def test_provider_error_text_is_not_shown_to_the_customer(self):
        provider = self._mock_provider()
        provider.payment_form_render.side_effect = Exception('AcquirerRef 0x81: token vault unreachable')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.get(self._url())
        assert response.status_code == 200
        assert 'AcquirerRef' not in response.content.decode()
        assert 'token vault' not in response.content.decode()

    # -- The form talks to the provider the way pretix's own pay page does --

    def test_form_names_the_provider(self):
        # BasePaymentProvider.payment_form only binds a POST whose `payment` field is the
        # provider's identifier; Stripe's and PayPal's JS only initialise inside
        # .payment-redo-form.
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': self._mock_provider()}):
            content = self.client.get(self._url()).content.decode()
        assert '<input type="hidden" name="payment" value="dummy">' in content
        assert 'payment-redo-form' in content

    @scopes_disabled()
    def test_post_with_a_form_based_provider_reaches_the_provider(self):
        provider = FormInstallmentProvider(self.event)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}), \
                patch.object(OrderPayment, 'confirm') as confirm:
            response = self.client.post(self._url(), {
                'payment': 'dummy',
                'payment_dummy-token': 'tok_new',
            })
        assert response.status_code == 302
        assert confirm.called
        assert self.client.session['payment_dummy_token'] == 'tok_new'

    @scopes_disabled()
    def test_post_with_a_form_based_provider_rejects_missing_input(self):
        provider = FormInstallmentProvider(self.event)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.post(self._url(), {'payment': 'dummy'})
        assert response.status_code == 200
        assert not OrderPayment.objects.filter(order=self.order).exists()

    # -- The original failed charge is not lost by an abandoned recovery attempt --

    @scopes_disabled()
    def _with_original_failed_payment(self):
        original = self.order.payments.create(
            provider='dummy', amount=Decimal('100.00'),
            state=OrderPayment.PAYMENT_STATE_FAILED,
        )
        self.installment.payment = original
        self.installment.save(update_fields=['payment'])
        return original

    def test_abandoned_recovery_restores_the_original_failed_payment(self):
        original = self._with_original_failed_payment()
        provider = self._mock_provider(payment_prepare=False)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            assert self.client.post(self._url()).status_code == 200

        with scopes_disabled():
            self.installment.refresh_from_db()
            assert self.installment.payment == original
            assert self.order.payments.count() == 1

    def test_a_crashed_recovery_restores_the_original_failed_payment(self):
        original = self._with_original_failed_payment()
        provider = self._mock_provider()
        provider.execute_payment.side_effect = RuntimeError('unexpected')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            assert self.client.post(self._url()).status_code == 200

        with scopes_disabled():
            self.installment.refresh_from_db()
            assert self.installment.payment == original

    def test_resubmitting_reuses_the_open_payment(self):
        """A customer who hits the button twice should not litter the order with payments."""
        provider = self._mock_provider(execute_payment_needs_user=True)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            first = self.client.post(self._url())
            second = self.client.post(self._url())

        assert first.status_code == 302
        assert second.status_code == 302
        with scopes_disabled():
            assert self.order.payments.count() == 1
            self.installment.refresh_from_db()
            assert self.installment.payment == self.order.payments.get()

    def test_a_failed_attempt_does_not_block_a_later_one(self):
        """Only an attempt still open is reused; a failed one starts a fresh payment."""
        provider = self._mock_provider()
        provider.execute_payment.side_effect = PaymentException('declined')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            self.client.post(self._url())

        with scopes_disabled():
            assert self.order.payments.get().state == OrderPayment.PAYMENT_STATE_FAILED

        provider = self._mock_provider(execute_payment_needs_user=True)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            assert self.client.post(self._url()).status_code == 302

        with scopes_disabled():
            assert self.order.payments.count() == 2

    def test_abandoned_recovery_without_an_original_leaves_no_link(self):
        provider = self._mock_provider(payment_prepare=False)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            assert self.client.post(self._url()).status_code == 200

        with scopes_disabled():
            self.installment.refresh_from_db()
            assert self.installment.payment is None

    # -- POST success --

    @scopes_disabled()
    def test_post_success(self):
        provider = self._mock_provider()
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.post(self._url())

        assert response.status_code == 302
        assert self.order.code in response.url

        payment = OrderPayment.objects.get(order=self.order)
        assert payment.provider == 'dummy'
        assert payment.amount == Decimal('100.00')
        assert payment.installment.get().plan == self.plan
        assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

        self.installment.refresh_from_db()
        assert self.installment.state == ScheduledInstallment.STATE_PAID
        assert self.installment.payment == payment

        self.plan.refresh_from_db()
        assert self.plan.installments_paid == 2
        assert self.plan.grace_period_end is None
        assert self.plan.grace_warning_sent is False

    @scopes_disabled()
    def test_post_completes_plan_on_last_installment(self):
        self.plan.installments_paid = 2
        self.plan.save(update_fields=['installments_paid'])

        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': self._mock_provider()}):
            self.client.post(self._url())

        self.plan.refresh_from_db()
        assert self.plan.status == InstallmentPlan.STATUS_COMPLETED

    @scopes_disabled()
    def test_post_does_not_complete_plan_when_not_last(self):
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': self._mock_provider()}):
            self.client.post(self._url())

        self.plan.refresh_from_db()
        assert self.plan.status == InstallmentPlan.STATUS_ACTIVE

    @scopes_disabled()
    def test_post_recovers_every_failed_installment(self):
        second_failed = ScheduledInstallment.objects.create(
            plan=self.plan, installment_number=3, amount=Decimal('100.00'),
            due_date=now() + timedelta(days=5), state=ScheduledInstallment.STATE_FAILED,
        )
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': self._mock_provider()}):
            self.client.post(self._url())

        self.installment.refresh_from_db()
        second_failed.refresh_from_db()
        assert self.installment.state == ScheduledInstallment.STATE_PAID
        assert second_failed.state == ScheduledInstallment.STATE_PAID

    # -- POST failure paths --

    def test_post_provider_unavailable_redirects(self):
        with patch('pretix.base.models.Event.get_payment_providers', return_value={}):
            assert self.client.post(self._url()).status_code == 302

    def test_post_installments_not_supported_redirects(self):
        provider = self._mock_provider(installments_supported=False)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            assert self.client.post(self._url()).status_code == 302

    @scopes_disabled()
    def test_post_payment_prepare_false_rerenders(self):
        provider = self._mock_provider(payment_prepare=False)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.post(self._url())
        assert response.status_code == 200
        assert not OrderPayment.objects.filter(order=self.order).exists()

    def test_post_payment_prepare_returns_url_redirects(self):
        provider = self._mock_provider(payment_prepare='https://pay.example.com/')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.post(self._url())
        assert response.status_code == 302
        assert response.url == 'https://pay.example.com/'
        with scopes_disabled():
            payment = OrderPayment.objects.get(order=self.order)
        self.installment.refresh_from_db()
        assert self.installment.payment == payment

    def test_post_payment_prepare_with_user_execution_redirects_to_confirm(self):
        provider = self._mock_provider(payment_prepare=True, execute_payment_needs_user=True)
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.post(self._url())

        assert response.status_code == 302
        with scopes_disabled():
            payment = OrderPayment.objects.get(order=self.order)
        assert f'/order/{self.order.code}/{self.order.secret}/pay/{payment.pk}/confirm' in response.url
        self.installment.refresh_from_db()
        assert self.installment.payment == payment

    @scopes_disabled()
    def test_post_payment_exception_marks_failed(self):
        provider = self._mock_provider()
        provider.execute_payment.side_effect = PaymentException('Card declined')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.post(self._url())

        assert response.status_code == 302
        payment = OrderPayment.objects.get(order=self.order)
        assert payment.state == OrderPayment.PAYMENT_STATE_FAILED
        self.installment.refresh_from_db()
        assert self.installment.state == ScheduledInstallment.STATE_FAILED

    def test_post_execute_payment_returns_url_redirects(self):
        provider = self._mock_provider(execute_payment='https://confirm.example.com/')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            response = self.client.post(self._url())
        assert response.status_code == 302
        assert response.url == 'https://confirm.example.com/'

    def test_post_unexpected_exception_rerenders(self):
        provider = self._mock_provider()
        provider.execute_payment.side_effect = RuntimeError('unexpected')
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            assert self.client.post(self._url()).status_code == 200
