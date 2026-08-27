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
from unittest.mock import MagicMock, patch

import pytest
from django.utils.timezone import now
from django_scopes import scope

from pretix.base.models import (
    CartPosition, Event, Item, Order, OrderFee, OrderPayment, Organizer, Quota,
)
from pretix.base.services.installments import (
    calculate_installment_amounts, create_installment_plan,
)
from pretix.base.services.orders import (
    OrderError, _create_order, perform_order,
)
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment


@pytest.fixture
def event():
    o = Organizer.objects.create(name='Dummy', slug='dummy')
    with scope(organizer=o):
        event = Event.objects.create(
            organizer=o,
            name='Dummy Event',
            slug='dummy',
            date_from=now() + timedelta(days=365),
            plugins='tests.testdummy',
        )
        yield event


@pytest.fixture
def order(event):
    return Order.objects.create(
        code='ABCDE',
        event=event,
        email='test@example.com',
        status=Order.STATUS_PENDING,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal('300.00'),
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )


@pytest.fixture
def item(event):
    return Item.objects.create(event=event, name="Ticket", default_price=Decimal('100.00'))


@pytest.fixture
def quota(event, item):
    q = Quota.objects.create(event=event, name="Quota", size=None)
    q.items.add(item)
    return q


@pytest.fixture
def cart_position(event, item, quota):
    return CartPosition.objects.create(
        event=event,
        item=item,
        price=Decimal('100.00'),
        expires=now() + timedelta(days=1),
    )


def _mock_provider():
    p = MagicMock()
    p.installments_supported = True
    return p


def _mock_order_provider():
    p = MagicMock()
    p.installments_supported = True
    p.calculate_fee.return_value = Decimal('0.00')
    p.payment_form_fields = {}
    p.is_implicit = lambda x: False
    return p


@pytest.mark.django_db
class TestCreateInstallmentPlan:

    def test_happy_path(self, event, order):
        with scope(organizer=event.organizer):
            with patch.object(event, 'get_payment_providers', return_value={'dummy': _mock_provider()}):
                plan = create_installment_plan(order, 'dummy', installments_count=3)

            assert plan.total_installments == 3
            assert plan.status == InstallmentPlan.STATUS_ACTIVE
            assert plan.amount_per_installment == Decimal('100.00')
            assert plan.payment_token == {}

            installments = list(plan.installments.order_by('installment_number'))
            assert len(installments) == 3
            assert installments[0].installment_number == 1
            assert installments[0].amount == Decimal('100.00')
            assert installments[0].payment == order.payments.first()
            assert installments[1].installment_number == 2
            assert installments[1].amount == Decimal('100.00')
            assert installments[1].state == ScheduledInstallment.STATE_PENDING

            first_payment = order.payments.first()
            assert first_payment.amount == Decimal('100.00')
            assert first_payment.installment.get().plan == plan

    def test_rounding(self, event, order):
        order.total = Decimal('100.00')
        order.save()

        with scope(organizer=event.organizer):
            with patch.object(event, 'get_payment_providers', return_value={'dummy': _mock_provider()}):
                plan = create_installment_plan(order, 'dummy', installments_count=3)

            assert plan.amount_per_installment == Decimal('33.33')

            installments = list(plan.installments.order_by('installment_number'))
            assert len(installments) == 3
            assert installments[0].amount == Decimal('33.33')
            assert installments[2].amount == Decimal('33.34')

            first_payment = OrderPayment.objects.get(order=order)
            assert first_payment.amount == Decimal('33.33')

    def test_unsupported_provider_raises(self, event, order):
        with scope(organizer=event.organizer):
            with pytest.raises(ValueError, match="does not support installments"):
                create_installment_plan(order, 'banktransfer', installments_count=3)

    def test_exceeds_max_installments_raises(self, event, order):
        event.settings.set('installments_count', 2)

        with scope(organizer=event.organizer):
            with patch.object(event, 'get_payment_providers', return_value={'dummy': _mock_provider()}):
                with pytest.raises(ValueError, match="exceeds the maximum"):
                    create_installment_plan(order, 'dummy', installments_count=3)

    def test_calendar_month_due_dates(self, event, order):
        from freezegun import freeze_time

        event.settings.set('installments_count', 4)

        with freeze_time("2025-01-31 12:00:00"):
            with scope(organizer=event.organizer):
                with patch.object(event, 'get_payment_providers', return_value={'dummy': _mock_provider()}):
                    plan = create_installment_plan(order, 'dummy', installments_count=4)

                installments = list(plan.installments.order_by('installment_number'))
                assert installments[0].due_date.month == 1
                assert installments[0].due_date.day == 31
                assert installments[1].due_date.month == 2
                assert installments[1].due_date.day == 28
                assert installments[2].due_date.month == 3
                assert installments[2].due_date.day == 31
                assert installments[3].due_date.month == 4
                assert installments[3].due_date.day == 30

    def test_info_data_preserved(self, order):
        with scope(organizer=order.event.organizer):
            with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': _mock_provider()}):
                plan = create_installment_plan(
                    order, 'dummy', installments_count=3,
                    info_data={'card_last4': '4242', 'transaction_id': 'txn_123'},
                )

            payment = plan.order.payments.first()
            assert payment.info == '{"card_last4": "4242", "transaction_id": "txn_123"}'
            assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


@pytest.mark.django_db
class TestPerformOrderWithInstallments:

    def _perform(self, event, cart_position, provider, installments_count=None):
        event.settings.set('installments_enabled', True)

        payment_request = {
            'provider': 'dummy',
            'payment_amount': Decimal('100.00'),
            'info_data': {},
            'pay_in_installments': True,
        }
        if installments_count is not None:
            payment_request['installments_count'] = installments_count

        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            return perform_order(
                event=event.id,
                payments=[payment_request],
                positions=[cart_position.id],
                meta_info={},
                email='test@example.com',
                locale='en',
            )

    def test_creates_plan(self, event, cart_position):
        with scope(organizer=event.organizer):
            result = self._perform(event, cart_position, _mock_order_provider())
            order = Order.objects.get(pk=result['order_id'])

            assert order.installment_plan is not None
            assert order.installment_plan.total_installments == 3
            assert order.payments.first().amount == Decimal('33.33')

    def test_uses_event_default_count(self, event, cart_position):
        event.settings.set('installments_count', 4)

        with scope(organizer=event.organizer):
            result = self._perform(event, cart_position, _mock_order_provider())
            order = Order.objects.get(pk=result['order_id'])

            assert order.installment_plan.total_installments == 4
            assert order.payments.first().amount == Decimal('25.00')

    def test_order_is_valid_while_pending(self, event, cart_position):
        """
        A plan only settles its order once the last installment is collected, so the
        order stays pending for the whole term. Its tickets are valid from the start
        anyway -- that is what buying in installments is for. ``_valid_if_pending`` is
        pinned off on the provider so only the installment plan can be the reason.
        """
        provider = _mock_order_provider()
        provider.settings.get.return_value = False

        with scope(organizer=event.organizer):
            result = self._perform(event, cart_position, provider)
            order = Order.objects.get(pk=result['order_id'])

            assert order.status == Order.STATUS_PENDING
            assert order.valid_if_pending

    def test_order_paid_in_full_is_not_valid_while_pending(self, event, cart_position):
        event.settings.set('installments_enabled', True)
        provider = _mock_order_provider()
        provider.settings.get.return_value = False

        with scope(organizer=event.organizer):
            with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
                result = perform_order(
                    event=event.id,
                    payments=[{
                        'provider': 'dummy',
                        'payment_amount': Decimal('100.00'),
                        'info_data': {},
                    }],
                    positions=[cart_position.id],
                    meta_info={},
                    email='test@example.com',
                    locale='en',
                )
            order = Order.objects.get(pk=result['order_id'])

            assert not order.valid_if_pending

    def test_uses_user_selected_count(self, event, cart_position):
        event.settings.set('installments_count', 5)

        with scope(organizer=event.organizer):
            result = self._perform(event, cart_position, _mock_order_provider(), installments_count=5)
            order = Order.objects.get(pk=result['order_id'])

            assert order.installment_plan.total_installments == 5
            assert order.payments.first().amount == Decimal('20.00')

    def test_caps_at_event_max(self, event, cart_position):
        event.settings.set('installments_count', 4)

        with scope(organizer=event.organizer):
            result = self._perform(
                event, cart_position,
                _mock_order_provider(),
                installments_count=10,
            )
            order = Order.objects.get(pk=result['order_id'])

            assert order.installment_plan.total_installments == 4
            assert order.payments.first().amount == Decimal('25.00')

    def test_with_multi_use_payment(self, event, cart_position):
        event.settings.set('installments_enabled', True)

        provider = _mock_order_provider()
        gc_provider = MagicMock()
        gc_provider.calculate_fee.return_value = Decimal('0.00')
        gc_provider.payment_form_fields = {}
        gc_provider.is_implicit = lambda x: False

        gift_card_payment = {
            'provider': 'multiuse',
            'payment_amount': Decimal('0.00'),
            'max_value': '40.00',
            'info_data': {},
            'multi_use_supported': True,
        }
        installment_payment = {
            'provider': 'dummy',
            'payment_amount': Decimal('0.00'),
            'info_data': {},
            'pay_in_installments': True,
            'installments_count': 3,
        }
        providers = {'dummy': provider, 'multiuse': gc_provider}
        with scope(organizer=event.organizer):
            with patch('pretix.base.models.Event.get_payment_providers', return_value=providers):
                result = perform_order(
                    event=event.id,
                    payments=[gift_card_payment, installment_payment],
                    positions=[cart_position.id],
                    meta_info={},
                    email='test@example.com',
                    locale='en',
                )
            order = Order.objects.get(pk=result['order_id'])

            assert order.payments.count() == 2
            gc_payment = order.payments.get(provider='multiuse')
            assert gc_payment.amount == Decimal('40.00')

            assert order.installment_plan is not None
            assert order.installment_plan.total_installments == 3
            assert order.installment_plan.amount_per_installment == Decimal('20.00')
            first_installment_payment = order.payments.filter(
                installment__plan=order.installment_plan
            ).first()
            assert first_installment_payment.amount == Decimal('20.00')
            assert order.installment_plan.first_payment == first_installment_payment


@pytest.mark.django_db
class TestPaymentFeeIsChargedUpFront:
    """
    The checkout preview (CartMixin.get_cart) quotes the first payment as
    "one installment of the ticket value, plus the whole payment fee". What actually gets
    charged has to agree with that figure.
    """

    def _fee_provider(self, fee):
        p = _mock_order_provider()
        p.calculate_fee.return_value = fee
        p.identifier = 'dummy'  # lands on OrderFee.internal_type, so it has to be a string
        return p

    def test_first_installment_carries_the_whole_fee(self, event, cart_position):
        event.settings.set('installments_enabled', True)
        provider = self._fee_provider(Decimal('3.00'))

        payment_request = {
            'provider': 'dummy',
            'payment_amount': Decimal('0.00'),
            'info_data': {},
            'pay_in_installments': True,
            'installments_count': 3,
        }
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            result = perform_order(
                event=event.id, payments=[payment_request], positions=[cart_position.id],
                meta_info={}, email='test@example.com', locale='en',
            )

        with scope(organizer=event.organizer):
            order = Order.objects.get(pk=result['order_id'])
            plan = order.installment_plan
            amounts = [i.amount for i in plan.installments.order_by('installment_number')]

            # EUR 100 ticket + EUR 3 fee. The ticket splits 33.33 / 33.33 / 33.34 and the
            # fee rides on the first payment -- not 34.33 / 34.33 / 34.34.
            assert order.total == Decimal('103.00')
            assert amounts == [Decimal('36.33'), Decimal('33.33'), Decimal('33.34')]
            assert sum(amounts) == order.total

    def test_preview_and_charge_agree(self, event, cart_position):
        """The figure CartMixin shows and the amount actually charged are the same."""
        event.settings.set('installments_enabled', True)
        provider = self._fee_provider(Decimal('3.00'))

        payment_request = {
            'provider': 'dummy',
            'payment_amount': Decimal('0.00'),
            'info_data': {},
            'pay_in_installments': True,
            'installments_count': 3,
        }
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            result = perform_order(
                event=event.id, payments=[payment_request], positions=[cart_position.id],
                meta_info={}, email='test@example.com', locale='en',
            )

        with scope(organizer=event.organizer):
            order = Order.objects.get(pk=result['order_id'])
            fees = list(order.fees.all())
            payment_fees = sum(f.value for f in fees if f.fee_type == OrderFee.FEE_TYPE_PAYMENT)
            preview = calculate_installment_amounts(order.total - payment_fees, 3)[0] + payment_fees

            assert order.installment_plan.first_payment.amount == preview

    def test_amount_per_installment_is_the_recurring_amount(self, event, cart_position):
        """
        The stored figure is the recurring amount. The first installment carries the
        payment fee and the last absorbs the rounding remainder, so it deliberately does
        not describe either of them.
        """
        event.settings.set('installments_enabled', True)
        provider = self._fee_provider(Decimal('3.00'))

        payment_request = {
            'provider': 'dummy', 'payment_amount': Decimal('0.00'), 'info_data': {},
            'pay_in_installments': True, 'installments_count': 3,
        }
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            result = perform_order(
                event=event.id, payments=[payment_request], positions=[cart_position.id],
                meta_info={}, email='test@example.com', locale='en',
            )

        with scope(organizer=event.organizer):
            plan = Order.objects.get(pk=result['order_id']).installment_plan
            scheduled = list(plan.installments.order_by('installment_number'))

            assert plan.amount_per_installment == Decimal('33.33')
            assert scheduled[1].amount == plan.amount_per_installment
            assert scheduled[0].amount != plan.amount_per_installment  # carries the fee
            assert scheduled[2].amount != plan.amount_per_installment  # carries the remainder

    def test_no_fee_leaves_the_split_untouched(self, event, cart_position):
        event.settings.set('installments_enabled', True)
        provider = self._fee_provider(Decimal('0.00'))

        payment_request = {
            'provider': 'dummy',
            'payment_amount': Decimal('0.00'),
            'info_data': {},
            'pay_in_installments': True,
            'installments_count': 3,
        }
        with patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider}):
            result = perform_order(
                event=event.id, payments=[payment_request], positions=[cart_position.id],
                meta_info={}, email='test@example.com', locale='en',
            )

        with scope(organizer=event.organizer):
            order = Order.objects.get(pk=result['order_id'])
            amounts = [i.amount for i in order.installment_plan.installments.order_by('installment_number')]
            assert amounts == [Decimal('33.33'), Decimal('33.33'), Decimal('33.34')]


@pytest.mark.django_db
class TestCreateOrderReturnsThePaymentsItCreated:
    """
    ``_create_order`` returns the payments the checkout flow then acts on: it executes
    them, and gift card payments get executed early as a special case. A payment that
    appears twice in that list is charged twice, and one that is missing is never
    charged at all -- so the list has to line up exactly with what was created.
    """

    def _providers(self):
        inst = _mock_order_provider()
        multiuse = MagicMock()
        multiuse.calculate_fee.return_value = Decimal('0.00')
        multiuse.payment_form_fields = {}
        multiuse.is_implicit = lambda x: False
        return inst, multiuse

    def _place(self, event, cart_position, requests, providers):
        with patch('pretix.base.models.Event.get_payment_providers', return_value=providers):
            positions = list(CartPosition.objects.filter(pk=cart_position.pk))
            return _create_order(
                event,
                email='test@example.com',
                positions=positions,
                now_dt=now(),
                payment_requests=requests,
                locale='en',
                address=None,
                meta_info={},
                sales_channel=event.organizer.sales_channels.get(identifier='web'),
            )

    def test_installments_alongside_a_multi_use_payment(self, event, cart_position):
        event.settings.set('installments_enabled', True)
        inst, multiuse = self._providers()
        requests = [
            {'provider': 'multiuse', 'payment_amount': Decimal('0.00'), 'max_value': '40.00',
             'info_data': {}, 'multi_use_supported': True, 'pprov': multiuse},
            {'provider': 'dummy', 'payment_amount': Decimal('0.00'), 'info_data': {},
             'pay_in_installments': True, 'installments_count': 3, 'pprov': inst},
        ]

        with scope(organizer=event.organizer):
            order, payments = self._place(event, cart_position, requests,
                                          {'dummy': inst, 'multiuse': multiuse})

            assert [p.provider for p in payments] == ['multiuse', 'dummy']
            assert len(payments) == len({p.pk for p in payments})
            assert {p.pk for p in payments} == set(order.payments.values_list('pk', flat=True))
            assert payments[1] == order.installment_plan.first_payment

    def test_two_installment_requests_are_refused(self, event, cart_position):
        """
        Not reachable from the checkout UI, which allows one non-multi-use payment. But
        keeping the last one and dropping the rest would leave the order short by whatever
        the dropped payment covered, so it fails loudly instead.
        """
        event.settings.set('installments_enabled', True)
        inst, _ = self._providers()
        requests = [
            {'provider': 'dummy', 'payment_amount': Decimal('0.00'), 'info_data': {},
             'pay_in_installments': True, 'installments_count': 3, 'pprov': inst},
            {'provider': 'dummy', 'payment_amount': Decimal('0.00'), 'info_data': {},
             'pay_in_installments': True, 'installments_count': 2, 'pprov': inst},
        ]

        with scope(organizer=event.organizer):
            with pytest.raises(OrderError, match='only have one installment plan'):
                self._place(event, cart_position, requests, {'dummy': inst})

    def test_installments_on_their_own(self, event, cart_position):
        event.settings.set('installments_enabled', True)
        inst, _ = self._providers()
        requests = [
            {'provider': 'dummy', 'payment_amount': Decimal('0.00'), 'info_data': {},
             'pay_in_installments': True, 'installments_count': 3, 'pprov': inst},
        ]

        with scope(organizer=event.organizer):
            order, payments = self._place(event, cart_position, requests, {'dummy': inst})

            assert [p.provider for p in payments] == ['dummy']
            assert payments[0] == order.installment_plan.first_payment
