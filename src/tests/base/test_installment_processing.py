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
from django.core import mail
from django.core.management import call_command
from django.utils.formats import date_format
from django.utils.timezone import now
from django_scopes import scope

from pretix.base.models import (
    Event, Item, Order, OrderPayment, OrderPosition, Organizer,
)
from pretix.base.models.orders import OrderRefund
from pretix.base.services.installments import (
    PROCESSING_LEASE, claim_installment, create_installment_plan,
    process_due_installments, process_expired_plans,
    process_single_installment, send_grace_period_warnings,
    send_installment_reminders,
)
from pretix.base.services.orders import (
    OrderChangeManager, cancel_order, expire_orders,
)
from pretix.base.templatetags.money import money_filter
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
        )
        yield event


@pytest.fixture
def order(event):
    order = Order.objects.create(
        code='ABCDE',
        event=event,
        email='test@example.com',
        status=Order.STATUS_PENDING,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal('300.00'),
        locale='en',
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    item = Item.objects.create(event=event, name='Test Ticket', default_price=Decimal('300.00'))
    OrderPosition.objects.create(order=order, item=item, price=Decimal('300.00'), positionid=1)
    return order


@pytest.fixture
def plan(order):
    return InstallmentPlan.objects.create(
        order=order,
        payment_provider='dummy',
        payment_token={'token': 'tok_123'},
        total_installments=3,
        installments_paid=1,
        amount_per_installment=Decimal('100.00'),
        status=InstallmentPlan.STATUS_ACTIVE,
    )


def _mock_provider(execute_result=True, grace_period_days=7, info_data=None):
    p = MagicMock()
    p.installments_supported = True

    def _execute(plan, installment, payment):
        if info_data is not None:
            payment.info_data = info_data
        return execute_result

    p.execute_installment.side_effect = _execute
    p.settings.get.return_value = grace_period_days
    return p


def _patch_providers(provider):
    return patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider})


@pytest.mark.django_db
class TestProcessDueInstallments:

    def test_success(self, event, order, plan):
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_due_installments()

        inst.refresh_from_db()
        plan.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_PAID
        assert inst.payment is not None
        assert inst.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
        assert plan.installments_paid == 2

    def test_ignores_future(self, event, order, plan):
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() + timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_due_installments()

        inst.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_PENDING

    def test_skips_already_paid(self, event, order, plan):
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PAID,
        )
        provider = _mock_provider()

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_due_installments()

        assert not provider.execute_installment.called

    def test_failure_sets_grace_period(self, event, order, plan):
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider(execute_result=False)):
            with scope(organizer=event.organizer):
                process_due_installments()

        inst.refresh_from_db()
        plan.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_FAILED
        assert plan.grace_period_end is not None

    def test_order_is_marked_paid_when_plan_completes(self, event, order, plan):
        """A completed plan must leave the order paid, not merely fully covered."""
        plan.total_installments = 2
        plan.installments_paid = 1
        plan.save()

        order.total = Decimal('200.00')
        order.save()
        p1 = order.payments.create(
            state=OrderPayment.PAYMENT_STATE_CONFIRMED, provider='dummy',
            amount=Decimal('100.00'), payment_date=now(),
        )
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=1, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=30), state=ScheduledInstallment.STATE_PAID,
            payment=p1,
        )
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_due_installments()

        with scope(organizer=event.organizer):
            order.refresh_from_db()
            plan.refresh_from_db()
            assert plan.status == InstallmentPlan.STATUS_COMPLETED
            assert order.pending_sum == Decimal('0.00')
            assert order.status == Order.STATUS_PAID

    def test_order_stays_pending_while_installments_remain(self, event, order, plan):
        """A mid-plan installment must not mark a partially paid order as paid."""
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_due_installments()

        with scope(organizer=event.organizer):
            order.refresh_from_db()
            inst.refresh_from_db()
            assert inst.state == ScheduledInstallment.STATE_PAID
            assert order.status == Order.STATUS_PENDING
            assert order.pending_sum == Decimal('200.00')

    def test_installment_payment_is_logged_as_confirmed(self, event, order, plan):
        """Going through confirm() means the payment shows up in the order log."""
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_due_installments()

        with scope(organizer=event.organizer):
            assert order.all_logentries().filter(
                action_type='pretix.event.order.payment.confirmed'
            ).exists()

    def test_provider_can_record_payment_info(self, event, order, plan):
        """The provider's transaction reference must survive onto the payment."""
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider(info_data={'transaction_id': 'txn_42'})):
            with scope(organizer=event.organizer):
                process_due_installments()

        inst.refresh_from_db()
        assert inst.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
        assert inst.payment.info_data == {'transaction_id': 'txn_42'}

    def test_provider_receives_payment_in_created_state(self, event, order, plan):
        """The payment must exist, and be unconfirmed, while the provider charges it."""
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )
        seen = {}

        provider = _mock_provider()

        def _execute(plan_, installment_, payment):
            seen['state'] = payment.state
            seen['amount'] = payment.amount
            seen['provider'] = payment.provider
            return True

        provider.execute_installment.side_effect = _execute

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_due_installments()

        assert seen['state'] == OrderPayment.PAYMENT_STATE_CREATED
        assert seen['amount'] == Decimal('100.00')
        assert seen['provider'] == 'dummy'

    def test_declined_charge_leaves_a_failed_payment(self, event, order, plan):
        """A decline is recorded as a failed payment carrying the provider's reason."""
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider(execute_result=False,
                                             info_data={'error': 'card_expired'})):
            with scope(organizer=event.organizer):
                process_due_installments()

        inst.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_FAILED
        assert inst.payment.state == OrderPayment.PAYMENT_STATE_FAILED
        assert inst.payment.info_data == {'error': 'card_expired'}

        with scope(organizer=event.organizer):
            order.refresh_from_db()
            # a failed charge must not count towards what has been paid
            assert order.pending_sum == Decimal('300.00')

    def test_no_payment_created_without_a_token(self, event, order, plan):
        """The token check short-circuits before any payment row is built."""
        plan.payment_token = {}
        plan.save()
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_due_installments()

        with scope(organizer=event.organizer):
            assert order.payments.count() == 0

    def test_completes_plan_on_final_installment(self, event, order, plan):
        plan.total_installments = 2
        plan.save()

        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )
        provider = _mock_provider()

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_due_installments()

        plan.refresh_from_db()
        assert plan.status == InstallmentPlan.STATUS_COMPLETED
        assert plan.installments_paid == 2
        provider.revoke_payment_token.assert_called_with(plan)

    def test_management_command_runs_without_scope(self, event, order, plan):
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            call_command('process_installments')

        inst.refresh_from_db()
        plan.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_PAID
        assert inst.payment is not None
        assert plan.installments_paid == 2


@pytest.mark.django_db
class TestProcessDueInstallmentsAfterCheckout:
    """
    The processor run against a plan that ``create_installment_plan`` actually built,
    rather than one assembled by hand. Installment 1 is the interesting one: it is due
    immediately and already carries the payment the customer makes at checkout, so the
    processor has to leave it alone until that payment is out of the picture.
    """

    def _real_plan(self, event, order, provider):
        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                return create_installment_plan(order, 'dummy', installments_count=3)

    def test_does_not_charge_while_the_checkout_payment_is_in_flight(self, event, order):
        provider = _mock_provider()
        plan = self._real_plan(event, order, provider)

        # The provider tokenized the card on the way out to the payment page; the
        # confirmation is still to come back over a webhook.
        plan.payment_token = {'token': 'tok_123'}
        plan.save(update_fields=['payment_token'])

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_due_installments()

        with scope(organizer=event.organizer):
            first = plan.installments.get(installment_number=1)
        assert provider.execute_installment.call_count == 0
        assert first.state == ScheduledInstallment.STATE_PENDING
        assert order.payments.count() == 1

    def test_confirming_the_checkout_payment_settles_the_first_installment(self, event, order):
        provider = _mock_provider()
        plan = self._real_plan(event, order, provider)

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                order.payments.get().confirm(send_mail=False)
                process_due_installments()

        with scope(organizer=event.organizer):
            first = plan.installments.get(installment_number=1)
            plan.refresh_from_db()
        assert first.state == ScheduledInstallment.STATE_PAID
        assert plan.installments_paid == 1
        assert provider.execute_installment.call_count == 0
        assert order.payments.count() == 1

    def test_order_is_valid_once_the_checkout_payment_is_confirmed(self, event, order):
        provider = _mock_provider()
        self._real_plan(event, order, provider)
        assert not order.valid_if_pending

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                order.payments.get().confirm(send_mail=False)

        order.refresh_from_db()
        assert order.status == Order.STATUS_PENDING
        assert order.valid_if_pending
        with scope(organizer=event.organizer):
            assert order.all_logentries().filter(action_type='pretix.event.order.valid_if_pending.set').count() == 1

    def test_order_is_not_valid_when_the_checkout_payment_fails(self, event, order):
        provider = _mock_provider()
        self._real_plan(event, order, provider)

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                order.payments.get().fail(send_mail=False)

        order.refresh_from_db()
        assert order.status == Order.STATUS_PENDING
        assert not order.valid_if_pending

    def test_a_failed_checkout_payment_stops_the_plan(self, event, order):
        """
        The customer cancelled or was declined at checkout, so nothing was collected: the
        plan stops as never started, and the order is an ordinary unpaid one again. The
        processor charges nothing, and no grace period starts: the order's own expiry
        date applies.
        """
        provider = _mock_provider()
        plan = self._real_plan(event, order, provider)
        plan.payment_token = {'token': 'tok_123'}
        plan.save(update_fields=['payment_token'])

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                order.payments.get().fail(send_mail=False)
                process_due_installments()

        with scope(organizer=event.organizer):
            states = dict(plan.installments.values_list('installment_number', 'state'))
        plan.refresh_from_db()
        assert plan.status == InstallmentPlan.STATUS_FAILED
        assert plan.payment_token == {}
        assert plan.grace_period_end is None
        assert states == {
            1: ScheduledInstallment.STATE_FAILED,
            2: ScheduledInstallment.STATE_CANCELLED,
            3: ScheduledInstallment.STATE_CANCELLED,
        }
        assert provider.execute_installment.call_count == 0
        # The token the failed payment left behind is no use to anyone.
        provider.revoke_payment_token.assert_called_once()

    def test_later_installments_are_still_charged_on_their_due_date(self, event, order):
        provider = _mock_provider()
        plan = self._real_plan(event, order, provider)
        plan.payment_token = {'token': 'tok_123'}
        plan.save(update_fields=['payment_token'])

        with scope(organizer=event.organizer):
            second = plan.installments.get(installment_number=2)
            second.due_date = now() - timedelta(days=1)
            second.save(update_fields=['due_date'])

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_due_installments()

        with scope(organizer=event.organizer):
            second.refresh_from_db()
        assert second.state == ScheduledInstallment.STATE_PAID
        assert provider.execute_installment.call_count == 1


@pytest.mark.django_db
class TestInstallmentClaiming:
    """
    Nothing serializes the callers of process_single_installment -- the periodic task, the
    management command, the control-panel button and the API endpoint can all reach the
    same installment at once. The claim is what stops that becoming a second charge.
    """

    def _pending(self, plan, **kwargs):
        kwargs.setdefault('state', ScheduledInstallment.STATE_PENDING)
        return ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), **kwargs
        )

    def test_a_claim_excludes_everyone_else(self, event, order, plan):
        inst = self._pending(plan)

        with scope(organizer=event.organizer):
            assert claim_installment(inst) is True
            assert claim_installment(ScheduledInstallment.objects.get(pk=inst.pk)) is False

        inst.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_PROCESSING

    def test_a_second_processor_does_not_charge_a_claimed_installment(self, event, order, plan):
        inst = self._pending(plan)
        provider = _mock_provider()

        with scope(organizer=event.organizer):
            claim_installment(ScheduledInstallment.objects.get(pk=inst.pk))
            with _patch_providers(provider):
                assert process_single_installment(inst) is False

        assert provider.execute_installment.call_count == 0
        assert order.payments.count() == 0

    def test_a_claim_from_a_dead_run_expires(self, event, order, plan):
        inst = self._pending(
            plan,
            state=ScheduledInstallment.STATE_PROCESSING,
            processed_at=now() - PROCESSING_LEASE - timedelta(minutes=1),
        )

        with scope(organizer=event.organizer):
            assert claim_installment(inst) is True

    def test_a_fresh_claim_does_not_expire(self, event, order, plan):
        inst = self._pending(
            plan,
            state=ScheduledInstallment.STATE_PROCESSING,
            processed_at=now(),
        )

        with scope(organizer=event.organizer):
            assert claim_installment(inst) is False

    def test_the_charge_still_settles_the_installment(self, event, order, plan):
        """The claim moves the installment through `processing`; confirm() must still see it."""
        inst = self._pending(plan)

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                assert process_single_installment(inst) is True

        inst.refresh_from_db()
        plan.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_PAID
        assert plan.installments_paid == 2

    def test_a_declined_charge_hands_the_installment_back(self, event, order, plan):
        inst = self._pending(plan)

        with _patch_providers(_mock_provider(execute_result=False)):
            with scope(organizer=event.organizer):
                process_single_installment(inst)

        inst.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_FAILED

    def test_a_missing_provider_hands_the_installment_back(self, event, order, plan):
        inst = self._pending(plan)

        with patch('pretix.base.models.Event.get_payment_providers', return_value={}):
            with scope(organizer=event.organizer):
                assert process_single_installment(inst) is False

        inst.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_PENDING


@pytest.mark.django_db
class TestChargeSurvivesLaterFailures:
    """
    Money leaves the customer inside execute_installment. Nothing that happens afterwards
    is allowed to erase the record of it -- a rolled-back payment row means pretix has no
    idea it took the money.
    """

    def _pending(self, plan):
        return ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

    @pytest.mark.django_db(transaction=True)
    def test_the_payment_survives_a_confirm_that_blows_up(self, event, order, plan):
        # transaction=True so the test sees real commit boundaries: wrapped in the usual
        # test transaction, a rollback of the payment row would be invisible here.
        inst = self._pending(plan)

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                with patch.object(OrderPayment, 'confirm', side_effect=RuntimeError('boom')):
                    assert process_single_installment(inst) is True

        with scope(organizer=event.organizer):
            inst.refresh_from_db()
            assert order.payments.count() == 1
            payment = order.payments.get()
            assert payment.amount == Decimal('100.00')
            assert inst.payment == payment

    def test_the_payment_exists_before_the_provider_is_called(self, event, order, plan):
        """The provider is handed a payment that is already committed, not one in limbo."""
        inst = self._pending(plan)
        seen = {}

        def _execute(plan_, installment_, payment_):
            # A separate connection would see this row; within the test we can at least
            # assert it has been written and carries the installment link.
            seen['pk'] = payment_.pk
            seen['linked'] = ScheduledInstallment.objects.get(pk=installment_.pk).payment_id
            return True

        provider = _mock_provider()
        provider.execute_installment.side_effect = _execute

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_single_installment(inst)

        assert seen['pk'] is not None
        assert seen['linked'] == seen['pk']

    def test_a_provider_that_raises_is_treated_as_a_decline(self, event, order, plan):
        inst = self._pending(plan)
        provider = _mock_provider()
        provider.execute_installment.side_effect = RuntimeError('network gone')

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                assert process_single_installment(inst) is False

        with scope(organizer=event.organizer):
            inst.refresh_from_db()
            plan.refresh_from_db()
            assert inst.state == ScheduledInstallment.STATE_FAILED
            assert order.payments.get().state == OrderPayment.PAYMENT_STATE_FAILED
            assert plan.grace_period_end is not None


@pytest.mark.django_db
class TestAbandonedPlansWindDown:
    """
    An order with an active plan is excluded from expire_orders, so the grace period is
    the only thing that ever ends it. A failure path that doesn't start one leaves the
    order immortal, holding its quota for good.
    """

    def test_a_missing_token_starts_the_grace_period(self, event, order, plan):
        plan.payment_token = {}
        plan.save(update_fields=['payment_token'])
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                assert process_single_installment(inst) is False

        inst.refresh_from_db()
        plan.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_FAILED
        assert plan.grace_period_end is not None

    def test_an_abandoned_checkout_expires_like_any_unpaid_order(self, event, order):
        """
        The customer never came back from the payment page: the checkout payment failed.
        With the plan stopped, the order is no longer kept out of expiry, and expires on
        its own date like any other unpaid order.
        """
        event.settings.payment_term_expire_automatically = True
        provider = _mock_provider()
        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                plan = create_installment_plan(order, 'dummy', installments_count=3)
                order.payments.get().fail(send_mail=False)
                order.expires = now() - timedelta(minutes=1)
                order.save(update_fields=['expires'])

                expire_orders(None)

        plan.refresh_from_db()
        order.refresh_from_db()
        assert plan.status == InstallmentPlan.STATUS_FAILED
        assert order.status == Order.STATUS_EXPIRED

    def test_an_abandoned_checkout_gets_the_ordinary_payment_failed_email(self, event, order):
        """
        The checkout payment failed. The order is an ordinary unpaid one again, so the
        customer gets pretix's ordinary "payment failed" email: its link leads to the order
        page, which offers to pay again -- at once, or in installments.
        """
        provider = _mock_provider()
        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                create_installment_plan(order, 'dummy', installments_count=3)
                mail.outbox = []
                order.payments.get().fail()

                assert len(mail.outbox) == 1
                assert 'Payment failed for your order' in mail.outbox[0].subject
                assert '/installment-recovery/' not in mail.outbox[0].body

                # Nor does the next processor run charge it, or say anything more.
                process_due_installments()

        provider.execute_installment.assert_not_called()
        assert len(mail.outbox) == 1

    def test_a_failed_payment_of_a_running_plan_still_fails_its_installment(self, event, order, plan):
        """
        Once a plan has collected an installment, a pending installment whose own payment
        fails fails with it, and starts the grace period.
        """
        plan.installments_paid = 1
        plan.save(update_fields=['installments_paid'])
        with scope(organizer=event.organizer):
            payment = order.payments.create(
                provider='dummy', amount=Decimal('100.00'), state=OrderPayment.PAYMENT_STATE_CREATED,
            )
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() + timedelta(days=1), state=ScheduledInstallment.STATE_PENDING, payment=payment,
        )
        mail.outbox = []
        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                payment.fail()

        inst.refresh_from_db()
        plan.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_FAILED
        assert plan.status == InstallmentPlan.STATUS_ACTIVE
        assert plan.grace_period_end is not None
        assert len(mail.outbox) == 1
        # The same email as for any failed payment, linking to the order page, which offers
        # to retry it.
        assert 'Payment failed for your order' in mail.outbox[0].subject
        assert '/order/%s/%s/\n' % (order.code, order.secret) in mail.outbox[0].body
        assert '/installment-recovery/' not in mail.outbox[0].body

    def test_a_failed_recovery_payment_gets_the_usual_email(self, event, order, plan):
        """
        The installment has already failed and the customer is on the recovery page;
        pretix's own email, pointing at the order page and its recovery link, is right.
        """
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_FAILED,
        )
        with scope(organizer=event.organizer):
            payment = order.payments.create(
                provider='dummy', amount=Decimal('100.00'), state=OrderPayment.PAYMENT_STATE_CREATED,
            )
            inst.payment = payment
            inst.save(update_fields=['payment'])
            mail.outbox = []

            payment.fail()

        assert len(mail.outbox) == 1
        assert 'Payment failed for your order' in mail.outbox[0].subject

    def test_a_retry_without_a_token_sends_no_mail(self, event, order, plan):
        """The control panel's retry button stays quiet, as it does for a decline."""
        plan.payment_token = {}
        plan.save(update_fields=['payment_token'])
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                assert process_single_installment(inst) is False

        assert len(mail.outbox) == 0

    def test_a_declined_charge_tells_the_customer_where_to_pay(self, event, order, plan):
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )
        mail.outbox = []

        with _patch_providers(_mock_provider(execute_result=False)):
            with scope(organizer=event.organizer):
                process_due_installments()

        assert len(mail.outbox) == 1
        assert order.code in mail.outbox[0].subject
        # The same email as for any failed payment, linking to the order page, which offers
        # to retry it.
        assert 'Payment failed for your order' in mail.outbox[0].subject
        assert '/order/%s/%s/\n' % (order.code, order.secret) in mail.outbox[0].body
        assert '/installment-recovery/' not in mail.outbox[0].body

    def test_the_grace_period_is_not_pushed_back_by_a_second_failure(self, event, order, plan):
        plan.payment_token = {}
        plan.save(update_fields=['payment_token'])
        first_end = now() + timedelta(days=3)
        plan.grace_period_end = first_end
        plan.save(update_fields=['grace_period_end'])

        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )
        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_single_installment(inst)

        plan.refresh_from_db()
        assert plan.grace_period_end == first_end

    def test_the_grace_period_survives_while_an_installment_is_still_failed(self, event, order, plan):
        """
        Settling one installment must not end the deadline the other one is waiting on.
        A plan that leaves its grace period with an installment still failed is collected
        by nothing -- the processor only charges pending ones -- and cancelled by nothing,
        so its order would stay pending and hold its quota for good.
        """
        plan.grace_period_end = now() + timedelta(days=40)
        plan.grace_warning_sent = True
        plan.save(update_fields=['grace_period_end', 'grace_warning_sent'])
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=40), state=ScheduledInstallment.STATE_FAILED,
        )
        third = ScheduledInstallment.objects.create(
            plan=plan, installment_number=3, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=10), state=ScheduledInstallment.STATE_FAILED,
        )

        with scope(organizer=event.organizer):
            payment = order.payments.create(
                provider='dummy', amount=Decimal('100.00'), state=OrderPayment.PAYMENT_STATE_CREATED,
            )
            third.payment = payment
            third.save(update_fields=['payment'])
            with _patch_providers(_mock_provider()):
                payment.confirm(send_mail=False)

        plan.refresh_from_db()
        assert plan.installments_paid == 2
        assert plan.grace_period_end is not None
        assert plan.grace_warning_sent

    def test_refunding_a_recovery_payment_gives_back_every_installment_it_settled(self, event, order, plan):
        plan.installments_paid = 1
        plan.save(update_fields=['installments_paid'])
        installments = [
            ScheduledInstallment.objects.create(
                plan=plan, installment_number=number, amount=Decimal('100.00'),
                due_date=now() - timedelta(days=30), state=ScheduledInstallment.STATE_FAILED,
            )
            for number in (2, 3)
        ]

        with scope(organizer=event.organizer):
            payment = order.payments.create(
                provider='dummy', amount=Decimal('200.00'), state=OrderPayment.PAYMENT_STATE_CREATED,
            )
            ScheduledInstallment.objects.filter(pk__in=[i.pk for i in installments]).update(payment=payment)
            with _patch_providers(_mock_provider()):
                payment.confirm(send_mail=False)
            plan.refresh_from_db()
            assert plan.installments_paid == 3

            order.refunds.create(
                payment=payment, provider='dummy', amount=Decimal('200.00'),
                source=OrderRefund.REFUND_SOURCE_ADMIN, state=OrderRefund.REFUND_STATE_CREATED,
            ).done()

        plan.refresh_from_db()
        assert plan.installments_paid == 1
        with scope(organizer=event.organizer):
            assert set(
                plan.installments.filter(pk__in=[i.pk for i in installments]).values_list('state', flat=True)
            ) == {ScheduledInstallment.STATE_REFUNDED}


@pytest.mark.django_db
class TestProcessorRespectsPlanAndOrderState:
    """
    Cancelling an order cancels its installments through the order_canceled receiver, but
    that is not the only way an order stops expecting money.
    """

    def _due(self, plan):
        return ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

    def _run(self, event, provider):
        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_due_installments()

    def test_an_expired_order_is_not_charged(self, event, order, plan):
        inst = self._due(plan)
        order.status = Order.STATUS_EXPIRED
        order.save(update_fields=['status'])
        provider = _mock_provider()

        self._run(event, provider)

        inst.refresh_from_db()
        assert provider.execute_installment.call_count == 0
        assert inst.state == ScheduledInstallment.STATE_PENDING

    def test_a_canceled_order_is_not_charged(self, event, order, plan):
        inst = self._due(plan)
        order.status = Order.STATUS_CANCELED
        order.save(update_fields=['status'])
        provider = _mock_provider()

        self._run(event, provider)

        assert provider.execute_installment.call_count == 0
        inst.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_PENDING

    def test_a_cancelled_plan_is_not_charged(self, event, order, plan):
        inst = self._due(plan)
        plan.status = InstallmentPlan.STATUS_CANCELLED
        plan.save(update_fields=['status'])
        provider = _mock_provider()

        self._run(event, provider)

        assert provider.execute_installment.call_count == 0
        inst.refresh_from_db()
        assert inst.state == ScheduledInstallment.STATE_PENDING

    def test_a_pending_order_on_an_active_plan_is_charged(self, event, order, plan):
        inst = self._due(plan)
        provider = _mock_provider()

        self._run(event, provider)

        inst.refresh_from_db()
        assert provider.execute_installment.call_count == 1
        assert inst.state == ScheduledInstallment.STATE_PAID


@pytest.mark.django_db
class TestOrderChangedDuringThePlan:
    """
    A plan's amounts are fixed when it is laid out. An order change moves the total, and
    the installments still to come have to follow it.
    """

    def _plan_one_paid(self, order, plan, second_state=ScheduledInstallment.STATE_PENDING):
        paid = order.payments.create(provider='dummy', amount=Decimal('100.00'),
                                     state=OrderPayment.PAYMENT_STATE_CONFIRMED)
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=1, amount=Decimal('100.00'), due_date=now() - timedelta(days=30),
            state=ScheduledInstallment.STATE_PAID, payment=paid,
        )
        for number, state in ((2, second_state), (3, ScheduledInstallment.STATE_PENDING)):
            ScheduledInstallment.objects.create(
                plan=plan, installment_number=number, amount=Decimal('100.00'),
                due_date=now() + timedelta(days=30 * (number - 1)), state=state,
            )

    def _change_price(self, event, order, price):
        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                ocm = OrderChangeManager(order, notify=False)
                ocm.change_price(order.positions.get(), Decimal(price))
                ocm.commit(check_quotas=False)

    def _amounts(self, plan):
        return [i.amount for i in plan.installments.filter(installment_number__gt=1)]

    def test_a_price_cut_lowers_the_installments_to_come(self, event, order, plan):
        self._plan_one_paid(order, plan)
        self._change_price(event, order, '240.00')
        assert self._amounts(plan) == [Decimal('70.00'), Decimal('70.00')]

    def test_a_price_rise_raises_them(self, event, order, plan):
        self._plan_one_paid(order, plan)
        self._change_price(event, order, '360.00')
        assert self._amounts(plan) == [Decimal('130.00'), Decimal('130.00')]

    def test_a_failed_installment_is_owed_as_it_stands(self, event, order, plan):
        self._plan_one_paid(order, plan, second_state=ScheduledInstallment.STATE_FAILED)
        self._change_price(event, order, '240.00')
        assert self._amounts(plan) == [Decimal('100.00'), Decimal('40.00')]

    def test_a_change_that_pays_the_order_off_completes_the_plan(self, event, order, plan):
        self._plan_one_paid(order, plan)
        self._change_price(event, order, '100.00')

        plan.refresh_from_db()
        assert plan.status == InstallmentPlan.STATUS_COMPLETED
        assert plan.installments.filter(state=ScheduledInstallment.STATE_PENDING).count() == 0


@pytest.mark.django_db
class TestProcessExpiredPlans:

    def test_cancels_order_and_plan(self, event, order, plan):
        plan.grace_period_end = now() - timedelta(days=1)
        plan.save()
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=5), state=ScheduledInstallment.STATE_PENDING,
        )
        provider = _mock_provider()

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_expired_plans()

        order.refresh_from_db()
        plan.refresh_from_db()
        inst.refresh_from_db()
        assert order.status == Order.STATUS_CANCELED
        assert plan.status == InstallmentPlan.STATUS_CANCELLED
        assert inst.state == ScheduledInstallment.STATE_CANCELLED
        provider.revoke_payment_token.assert_called_with(plan)

    def test_sends_cancellation_email(self, event, order, plan):
        plan.grace_period_end = now() - timedelta(days=1)
        plan.save()
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_expired_plans()

        assert len(mail.outbox) == 1
        assert order.code in mail.outbox[0].subject

    def _expired_plan_with_payments(self, order, plan, paid=Decimal('200.00')):
        plan.grace_period_end = now() - timedelta(days=1)
        plan.save()
        order.payments.create(
            state=OrderPayment.PAYMENT_STATE_CONFIRMED, provider='dummy',
            amount=paid, payment_date=now(),
        )
        return plan

    def test_the_cancellation_email_does_not_promise_a_refund(self, event, order, plan):
        """
        Nothing is refunded here by design -- the customer has to take it up with the
        organizer -- so the mail must not tell them money is on its way back.
        """
        self._expired_plan_with_payments(order, plan)
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_expired_plans()

        body = mail.outbox[0].body
        assert 'not refunded' in body
        assert 'get in touch' in body
        assert 'will be refunded' not in body

    def test_the_cancellation_email_states_what_was_paid(self, event, order, plan):
        self._expired_plan_with_payments(order, plan, paid=Decimal('200.00'))
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_expired_plans()

        body = mail.outbox[0].body
        assert '200.00' in body
        assert '{paid_amount}' not in body  # the placeholder actually resolved

    def test_no_refund_is_created(self, event, order, plan):
        self._expired_plan_with_payments(order, plan)

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_expired_plans()

        with scope(organizer=event.organizer):
            order.refresh_from_db()
            assert order.status == Order.STATUS_CANCELED
            assert order.refunds.count() == 0
            # The money is still recorded against the order for the organizer to act on.
            assert order.payment_refund_sum == Decimal('200.00')


@pytest.mark.django_db
class TestOrderSettledOutsideThePlan:
    """
    An order can be paid off without the plan hearing about it: a bank transfer the
    importer matches, a payment an organizer marks as received, a gift card covering the
    rest. Only a payment that belongs to a scheduled installment advances the plan, so
    without this the plan stayed active and went on charging the customer's saved card
    month after month for an order that owed nothing.
    """

    def _remaining_installments(self, plan):
        for num, due in ((2, now() - timedelta(days=1)), (3, now() + timedelta(days=30))):
            ScheduledInstallment.objects.create(
                plan=plan, installment_number=num, amount=Decimal('100.00'),
                due_date=due, state=ScheduledInstallment.STATE_PENDING,
            )

    def _settle_from_outside(self, event, order, provider, on_commit):
        """Pay the order off with a payment of its own, as the bank transfer importer and
        an organizer's "mark as paid" both do. Revoking the token is deferred to commit,
        which a test transaction never reaches on its own."""
        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                payment = order.payments.create(
                    provider='banktransfer', amount=Decimal('300.00'),
                    state=OrderPayment.PAYMENT_STATE_CREATED,
                )
                with on_commit(execute=True):
                    payment.confirm(send_mail=False)

    def test_the_plan_stops_collecting(self, event, order, plan, django_capture_on_commit_callbacks):
        self._remaining_installments(plan)
        provider = _mock_provider()

        self._settle_from_outside(event, order, provider, django_capture_on_commit_callbacks)

        order.refresh_from_db()
        plan.refresh_from_db()
        assert order.status == Order.STATUS_PAID
        assert plan.status == InstallmentPlan.STATUS_COMPLETED
        # Left at what the plan really collected, not rounded up to the total: that is what
        # happened, and the order page and the API both report it.
        assert plan.installments_paid == 1
        assert plan.payment_token == {}
        with scope(organizer=event.organizer):
            assert set(plan.installments.values_list('state', flat=True)) == {
                ScheduledInstallment.STATE_CANCELLED
            }
        provider.revoke_payment_token.assert_called_once()

    def test_the_saved_card_is_never_charged_again(self, event, order, plan,
                                                   django_capture_on_commit_callbacks):
        self._remaining_installments(plan)
        provider = _mock_provider()

        self._settle_from_outside(event, order, provider, django_capture_on_commit_callbacks)
        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                process_due_installments()

        provider.execute_installment.assert_not_called()

    def test_a_grace_period_is_not_left_behind(self, event, order, plan,
                                               django_capture_on_commit_callbacks):
        # The customer had an installment fail, then paid the order off at the counter.
        plan.grace_period_end = now() + timedelta(days=7)
        plan.grace_warning_sent = True
        plan.save(update_fields=['grace_period_end', 'grace_warning_sent'])
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_FAILED,
        )

        self._settle_from_outside(event, order, _mock_provider(), django_capture_on_commit_callbacks)

        plan.refresh_from_db()
        inst.refresh_from_db()
        assert plan.grace_period_end is None
        assert not plan.grace_warning_sent
        assert inst.state == ScheduledInstallment.STATE_CANCELLED

    def test_the_plan_is_told_about_it_in_the_order_log(
        self, event, order, plan, django_capture_on_commit_callbacks
    ):
        self._remaining_installments(plan)

        self._settle_from_outside(event, order, _mock_provider(), django_capture_on_commit_callbacks)

        with scope(organizer=event.organizer):
            entry = order.all_logentries().get(
                action_type='pretix.event.order.installment_plan.completed'
            )
        assert entry.parsed_data['installments_paid'] == 1

    def test_the_last_installment_completes_the_plan_as_before(self, event, order, plan):
        # The ordinary ending: the final installment is what pays the order, so the signal
        # fires for a plan that record_successful_payment has already completed.
        plan.total_installments = 2
        plan.save(update_fields=['total_installments'])
        order.total = Decimal('200.00')
        order.save(update_fields=['total'])
        p1 = order.payments.create(
            state=OrderPayment.PAYMENT_STATE_CONFIRMED, provider='dummy',
            amount=Decimal('100.00'), payment_date=now(),
        )
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=1, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=30), state=ScheduledInstallment.STATE_PAID, payment=p1,
        )
        last = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=1), state=ScheduledInstallment.STATE_PENDING,
        )

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                process_due_installments()

        order.refresh_from_db()
        plan.refresh_from_db()
        last.refresh_from_db()
        assert order.status == Order.STATUS_PAID
        assert plan.status == InstallmentPlan.STATUS_COMPLETED
        assert plan.installments_paid == 2
        # Paid, not swept up as cancelled by the signal.
        assert last.state == ScheduledInstallment.STATE_PAID


@pytest.mark.django_db
class TestOrderCancellation:

    def test_cancels_plan_and_installments(self, event, order, plan):
        for num in [2, 3]:
            ScheduledInstallment.objects.create(
                plan=plan, installment_number=num, amount=Decimal('100.00'),
                due_date=now() + timedelta(days=30 * num),
                state=ScheduledInstallment.STATE_PENDING,
            )
        provider = _mock_provider()

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                cancel_order(order)

        plan.refresh_from_db()
        assert plan.status == InstallmentPlan.STATUS_CANCELLED
        assert plan.installments.filter(state=ScheduledInstallment.STATE_PENDING).count() == 0
        assert plan.installments.filter(state=ScheduledInstallment.STATE_CANCELLED).count() == 2
        provider.revoke_payment_token.assert_called_with(plan)

    def test_paid_order_cancellation(self, event, order, plan):
        for num in [2, 3]:
            ScheduledInstallment.objects.create(
                plan=plan, installment_number=num, amount=Decimal('100.00'),
                due_date=now() + timedelta(days=30 * num),
                state=ScheduledInstallment.STATE_PENDING,
            )
        order.status = Order.STATUS_PAID
        order.save()
        provider = _mock_provider()

        with _patch_providers(provider):
            with scope(organizer=event.organizer):
                cancel_order(order)

        plan.refresh_from_db()
        assert plan.status == InstallmentPlan.STATUS_CANCELLED
        assert plan.installments.filter(state=ScheduledInstallment.STATE_CANCELLED).count() == 2
        provider.revoke_payment_token.assert_called_with(plan)


@pytest.mark.django_db
class TestInstallmentReminders:

    def test_sends_reminder(self, event, order, plan):
        event.settings.installments_reminder_days = 3
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() + timedelta(days=3), state=ScheduledInstallment.STATE_PENDING,
        )
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                send_installment_reminders()

        assert len(mail.outbox) == 1

    def test_reminder_formats_amount_and_due_date(self, event, order, plan):
        event.settings.installments_reminder_days = 3
        due = now() + timedelta(days=3)
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=due, state=ScheduledInstallment.STATE_PENDING,
        )
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                send_installment_reminders()

        body = mail.outbox[0].body
        assert money_filter(Decimal('100.00'), event.currency) in body
        assert date_format(due.astimezone(event.timezone), 'SHORT_DATE_FORMAT') in body
        assert str(due) not in body
        assert due.astimezone(event.timezone).strftime('%H:%M') not in body

    def test_no_reminder_when_too_early(self, event, order, plan):
        event.settings.installments_reminder_days = 3
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() + timedelta(days=10), state=ScheduledInstallment.STATE_PENDING,
        )
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                send_installment_reminders()

        assert len(mail.outbox) == 0

    def test_no_duplicate_reminder(self, event, order, plan):
        event.settings.installments_reminder_days = 3
        ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() + timedelta(days=3), state=ScheduledInstallment.STATE_PENDING,
            reminder_sent=True,
        )
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                send_installment_reminders()

        assert len(mail.outbox) == 0

    def test_no_reminder_unless_configured(self, event, order, plan):
        # Reminders are off until the event asks for them, and an installment passed
        # over while they are off is not marked as reminded -- switching them on later
        # still reminds about it.
        inst = ScheduledInstallment.objects.create(
            plan=plan, installment_number=2, amount=Decimal('100.00'),
            due_date=now() + timedelta(days=3), state=ScheduledInstallment.STATE_PENDING,
        )
        mail.outbox = []

        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                send_installment_reminders()

        assert len(mail.outbox) == 0
        inst.refresh_from_db()
        assert inst.reminder_sent is False

        event.settings.installments_reminder_days = 3
        with _patch_providers(_mock_provider()):
            with scope(organizer=event.organizer):
                send_installment_reminders()

        assert len(mail.outbox) == 1


@pytest.mark.django_db
class TestGracePeriodWarnings:

    def test_sends_warning(self, event, order, plan):
        plan.grace_period_end = now() + timedelta(hours=23)
        plan.save()
        mail.outbox = []

        with scope(organizer=event.organizer):
            send_grace_period_warnings()

        assert len(mail.outbox) == 1
        plan.refresh_from_db()
        assert plan.grace_warning_sent is True

    def test_no_warning_when_too_early(self, event, order, plan):
        plan.grace_period_end = now() + timedelta(days=2)
        plan.save()
        mail.outbox = []

        with scope(organizer=event.organizer):
            send_grace_period_warnings()

        assert len(mail.outbox) == 0

    def test_no_duplicate_warning(self, event, order, plan):
        plan.grace_period_end = now() + timedelta(hours=23)
        plan.grace_warning_sent = True
        plan.save()
        mail.outbox = []

        with scope(organizer=event.organizer):
            send_grace_period_warnings()

        assert len(mail.outbox) == 0


@pytest.mark.django_db
@pytest.mark.parametrize('action_type', [
    'pretix.event.order.installment_plan.canceled',
    'pretix.event.order.installment_plan.completed',
    'pretix.event.order.installment.reminder',
    'pretix.event.order.installment.failed',
    'pretix.event.order.installment.grace_warning',
    'pretix.event.order.installment.cancelled',
])
def test_installment_log_entries_are_displayed_as_sentences(event, order, action_type):
    with scope(organizer=event.organizer):
        order.log_action(action_type)
        logentry = order.all_logentries().get(action_type=action_type)
        assert logentry.display() != action_type
