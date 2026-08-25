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

import pytest
from django.core import mail as djmail
from django.core.management import call_command
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled

from pretix.base.models import (
    CartPosition, Event, Item, Order, OrderPayment, OrderPosition, Organizer,
    Quota, Team, User,
)
from pretix.base.services.installments import (
    create_installment_plan, installments_available_for_event,
    process_due_push_installments, process_expired_plans,
    request_push_installment, send_grace_period_warnings,
    send_installment_reminders,
)
from pretix.base.services.orders import perform_order
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment
from pretix.plugins.banktransfer.models import BankImportJob
from pretix.plugins.banktransfer.tasks import process_banktransfers


@pytest.fixture
def event():
    o = Organizer.objects.create(name='Dummy', slug='dummy', plugins='pretix.plugins.banktransfer')
    with scope(organizer=o):
        event = Event.objects.create(
            organizer=o, name='Dummy', slug='dummy',
            date_from=now() + timedelta(days=365),
            plugins='pretix.plugins.banktransfer',
            live=True,
        )
        event.settings.payment_banktransfer__enabled = True
        event.settings.payment_banktransfer_bank_details_type = 'sepa'
        event.settings.payment_banktransfer_bank_details_sepa_name = 'Test Org'
        event.settings.payment_banktransfer_bank_details_sepa_iban = 'DE27520521540534534466'
        event.settings.payment_banktransfer_bank_details_sepa_bic = 'HELADEF1MEG'
        event.settings.payment_banktransfer_bank_details_sepa_bank = 'Test Bank'
        event.settings.installments_enabled = True
        event.settings.installments_count = 3
        event.settings.installments_grace_period_days = 7
        event.settings.installments_reminder_days = 3
        yield event


@pytest.fixture
def order(event):
    order = Order.objects.create(
        code='1Z3AS', event=event, email='admin@localhost',
        status=Order.STATUS_PENDING,
        datetime=now(), expires=now() + timedelta(days=10),
        total=Decimal('300.00'), locale='en',
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    quota = Quota.objects.create(name="Test", size=10, event=event)
    item = Item.objects.create(event=event, name="Ticket", default_price=Decimal('300.00'))
    quota.items.add(item)
    OrderPosition.objects.create(order=order, item=item, price=Decimal('300.00'), positionid=1)
    return order


@pytest.fixture
def plan(order):
    return create_installment_plan(order, 'banktransfer', 3)


@pytest.fixture
def job(event):
    return BankImportJob.objects.create(event=event).pk


def _transfer(job, amount, code='1Z3AS', date='2026-01-26'):
    process_banktransfers(job, [{
        'payer': 'Karla Kundin',
        'reference': 'Bestellung DUMMY%s' % code,
        'date': date,
        'amount': str(amount),
    }])


#: The bank details as the bank transfer provider renders them.
IBAN_IN_MAIL = 'DE27 5205 2154 0534 5344 66'


@pytest.mark.django_db
class TestPlanCreation:

    def test_bank_transfer_offers_push_installments(self, event, order):
        provider = event.get_payment_providers()['banktransfer']
        assert provider.push_installments_supported is True
        assert provider.installments_supported is False

    def test_plan_is_created_in_push_mode(self, event, plan):
        assert plan.mode == InstallmentPlan.MODE_PUSH
        assert plan.is_push
        with scopes_disabled():
            assert plan.installments.count() == 3
            assert [i.amount for i in plan.installments.order_by('installment_number')] == [
                Decimal('100.00'), Decimal('100.00'), Decimal('100.00')
            ]

    def test_only_the_first_installment_is_payable_right_away(self, event, plan):
        with scopes_disabled():
            first = plan.installments.get(installment_number=1)
            assert first.payment is not None
            assert first.payment.amount == Decimal('100.00')
            assert first.payment.state == OrderPayment.PAYMENT_STATE_CREATED
            assert plan.installments.filter(payment__isnull=True).count() == 2

    def test_order_expiry_covers_the_whole_schedule(self, event, order, plan):
        order.refresh_from_db()
        with scopes_disabled():
            last_due = plan.installments.order_by('installment_number').last().due_date
        assert order.expires >= last_due

    def test_no_payment_token_is_needed(self, event, plan):
        assert plan.payment_token == {}


@pytest.mark.django_db
class TestIncomingTransfers:

    def test_transfer_settles_first_installment(self, event, order, plan, job):
        _transfer(job, '100.00')

        plan.refresh_from_db()
        order.refresh_from_db()
        assert order.status == Order.STATUS_PENDING
        assert plan.installments_paid == 1
        assert plan.status == InstallmentPlan.STATUS_ACTIVE
        with scopes_disabled():
            assert plan.installments.get(installment_number=1).state == ScheduledInstallment.STATE_PAID
            assert plan.installments.get(installment_number=2).state == ScheduledInstallment.STATE_PENDING

    def test_one_transfer_can_settle_two_installments(self, event, order, plan, job):
        _transfer(job, '200.00')

        plan.refresh_from_db()
        assert plan.installments_paid == 2
        with scopes_disabled():
            assert plan.installments.get(installment_number=2).state == ScheduledInstallment.STATE_PAID
            assert plan.installments.get(installment_number=3).state == ScheduledInstallment.STATE_PENDING

    def test_partial_transfer_settles_nothing(self, event, order, plan, job):
        _transfer(job, '60.00')

        plan.refresh_from_db()
        assert plan.installments_paid == 0
        with scopes_disabled():
            assert plan.installments.get(installment_number=1).state == ScheduledInstallment.STATE_PENDING
        # The money did arrive, it just does not add up to an installment yet.
        assert plan.paid_amount == Decimal('60.00')
        assert plan.remaining_amount == Decimal('240.00')

    def test_two_partial_transfers_settle_an_installment(self, event, order, plan, job):
        _transfer(job, '60.00')
        _transfer(job, '40.00', date='2026-02-26')

        plan.refresh_from_db()
        assert plan.installments_paid == 1
        assert plan.paid_amount == Decimal('100.00')
        assert plan.remaining_amount == Decimal('200.00')

    def test_paying_everything_completes_the_plan(self, event, order, plan, job):
        _transfer(job, '300.00')

        plan.refresh_from_db()
        order.refresh_from_db()
        assert plan.status == InstallmentPlan.STATUS_COMPLETED
        assert plan.installments_paid == 3
        assert order.status == Order.STATUS_PAID

    def test_no_incomplete_payment_mail_while_the_plan_runs(self, event, order, plan, job):
        djmail.outbox = []
        _transfer(job, '100.00')

        subjects = [m.subject for m in djmail.outbox]
        assert not any('incomplete' in s.lower() for s in subjects)

    def test_next_installment_stays_payable_after_a_transfer(self, event, order, plan, job):
        _transfer(job, '100.00')
        with scopes_disabled():
            second = plan.installments.get(installment_number=2)
        second.due_date = now() - timedelta(days=1)
        second.save(update_fields=['due_date'])
        with scope(organizer=event.organizer):
            request_push_installment(second, send_mail=False)
        second.refresh_from_db()
        next_payment = second.payment
        assert next_payment.state == OrderPayment.PAYMENT_STATE_CREATED

        # A transfer for a *different* amount must not sweep away the open ask.
        _transfer(job, '20.00', date='2026-02-26')

        next_payment.refresh_from_db()
        assert next_payment.state == OrderPayment.PAYMENT_STATE_CREATED

    def test_only_one_installment_is_open_for_payment_at_a_time(self, event, order, plan, job):
        with scopes_disabled():
            for number in (2, 3):
                inst = plan.installments.get(installment_number=number)
                inst.due_date = now() - timedelta(days=1)
                inst.save(update_fields=['due_date'])
                with scope(organizer=event.organizer):
                    request_push_installment(inst, send_mail=False)

        with scopes_disabled():
            open_payments = order.payments.filter(
                state__in=(OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING)
            )
            assert open_payments.count() == 1


@pytest.mark.django_db
class TestDueProcessing:

    def _make_due(self, plan, number=2, days_ago=1):
        with scopes_disabled():
            installment = plan.installments.get(installment_number=number)
        installment.due_date = now() - timedelta(days=days_ago)
        installment.save(update_fields=['due_date'])
        return installment

    def test_due_installment_opens_a_payment_and_notifies(self, event, order, plan, job):
        _transfer(job, '100.00')
        installment = self._make_due(plan)
        djmail.outbox = []

        with scope(organizer=event.organizer):
            process_due_push_installments()

        installment.refresh_from_db()
        plan.refresh_from_db()
        assert installment.overdue_notice_sent is True
        assert installment.payment is not None
        assert installment.payment.amount == Decimal('100.00')
        assert plan.grace_period_end is not None
        assert len(djmail.outbox) == 1
        assert order.code in djmail.outbox[0].subject
        assert IBAN_IN_MAIL in djmail.outbox[0].body

    def test_notice_is_only_sent_once(self, event, order, plan, job):
        _transfer(job, '100.00')
        self._make_due(plan)
        with scope(organizer=event.organizer):
            process_due_push_installments()
        djmail.outbox = []

        with scope(organizer=event.organizer):
            process_due_push_installments()

        assert len(djmail.outbox) == 0

    def test_only_the_earliest_open_installment_is_chased(self, event, order, plan, job):
        _transfer(job, '100.00')
        second = self._make_due(plan, number=2, days_ago=40)
        third = self._make_due(plan, number=3, days_ago=10)
        djmail.outbox = []

        with scope(organizer=event.organizer):
            process_due_push_installments()

        second.refresh_from_db()
        third.refresh_from_db()
        assert second.overdue_notice_sent is True
        assert third.overdue_notice_sent is False
        assert len(djmail.outbox) == 1

    def test_installment_paid_in_the_meantime_is_not_chased(self, event, order, plan, job):
        self._make_due(plan, number=1, days_ago=1)
        _transfer(job, '100.00')
        djmail.outbox = []

        with scope(organizer=event.organizer):
            process_due_push_installments()

        assert len(djmail.outbox) == 0

    def test_nothing_is_charged_automatically(self, event, order, plan, job):
        _transfer(job, '100.00')
        self._make_due(plan)

        with scope(organizer=event.organizer):
            process_due_push_installments()

        order.refresh_from_db()
        plan.refresh_from_db()
        assert order.status == Order.STATUS_PENDING
        assert plan.installments_paid == 1

    def test_paying_after_the_notice_clears_the_grace_period(self, event, order, plan, job):
        _transfer(job, '100.00')
        self._make_due(plan)
        with scope(organizer=event.organizer):
            process_due_push_installments()
        plan.refresh_from_db()
        assert plan.grace_period_end is not None

        _transfer(job, '100.00', date='2026-02-26')

        plan.refresh_from_db()
        assert plan.grace_period_end is None
        assert plan.installments_paid == 2


@pytest.mark.django_db
class TestReminders:

    def test_reminder_before_due_date_carries_bank_details(self, event, order, plan, job):
        _transfer(job, '100.00')
        with scopes_disabled():
            second = plan.installments.get(installment_number=2)
        second.due_date = now() + timedelta(days=1)
        second.save(update_fields=['due_date'])
        djmail.outbox = []

        with scope(organizer=event.organizer):
            send_installment_reminders()

        second.refresh_from_db()
        assert second.reminder_sent is True
        assert second.payment is not None
        assert len(djmail.outbox) == 1
        assert IBAN_IN_MAIL in djmail.outbox[0].body

    def test_grace_period_warning_is_sent(self, event, order, plan):
        plan.grace_period_end = now() + timedelta(hours=2)
        plan.save(update_fields=['grace_period_end'])
        djmail.outbox = []

        with scope(organizer=event.organizer):
            send_grace_period_warnings()

        plan.refresh_from_db()
        assert plan.grace_warning_sent is True
        assert len(djmail.outbox) == 1


@pytest.mark.django_db
class TestExpiry:

    def test_order_is_not_expired_while_the_plan_runs(self, event, order, plan):
        from pretix.base.services.orders import expire_orders

        order.expires = now() - timedelta(days=1)
        order.save(update_fields=['expires'])
        event.settings.payment_term_expire_automatically = True

        expire_orders(None)

        order.refresh_from_db()
        assert order.status == Order.STATUS_PENDING

    def test_order_is_cancelled_after_the_grace_period(self, event, order, plan):
        plan.grace_period_end = now() - timedelta(hours=1)
        plan.save(update_fields=['grace_period_end'])

        with scope(organizer=event.organizer):
            process_expired_plans()

        order.refresh_from_db()
        plan.refresh_from_db()
        assert order.status == Order.STATUS_CANCELED
        assert plan.status == InstallmentPlan.STATUS_CANCELLED


@pytest.fixture
def admin_client(event, client):
    user = User.objects.create_user('dummy@dummy.dummy', 'dummy')
    team = Team.objects.create(organizer=event.organizer, all_event_permissions=True)
    team.members.add(user)
    team.limit_events.add(event)
    client.login(email='dummy@dummy.dummy', password='dummy')
    return client


@pytest.mark.django_db
class TestControlView:

    def test_order_page_shows_paid_and_remaining(self, event, order, plan, job, admin_client):
        _transfer(job, '100.00')

        r = admin_client.get('/control/event/dummy/dummy/orders/1Z3AS/')
        content = r.content.decode()
        assert r.status_code == 200
        assert 'Already paid' in content
        assert 'Left to pay' in content
        assert '€200.00' in content

    def test_order_page_offers_to_request_a_due_installment(self, event, order, plan, job, admin_client):
        _transfer(job, '100.00')
        with scopes_disabled():
            second = plan.installments.get(installment_number=2)
        second.due_date = now() - timedelta(days=1)
        second.save(update_fields=['due_date'])

        r = admin_client.get('/control/event/dummy/dummy/orders/1Z3AS/')
        content = r.content.decode()
        assert 'Request installment payment' in content
        assert 'Retry failed installment' not in content

    def test_requesting_a_due_installment_mails_the_customer(self, event, order, plan, job, admin_client):
        _transfer(job, '100.00')
        with scopes_disabled():
            second = plan.installments.get(installment_number=2)
        second.due_date = now() - timedelta(days=1)
        second.save(update_fields=['due_date'])
        djmail.outbox = []

        r = admin_client.post('/control/event/dummy/dummy/orders/1Z3AS/installment-plan/remind')

        assert r.status_code == 302
        assert len(djmail.outbox) == 1
        assert IBAN_IN_MAIL in djmail.outbox[0].body

    def test_confirmation_page_for_the_request_renders(self, event, order, plan, job, admin_client):
        r = admin_client.get('/control/event/dummy/dummy/orders/1Z3AS/installment-plan/remind')
        assert r.status_code == 200
        assert 'Request installment payment' in r.content.decode()

    def test_requesting_when_nothing_is_due_does_nothing(self, event, order, plan, job, admin_client):
        with scopes_disabled():
            first = plan.installments.get(installment_number=1)
        first.due_date = now() + timedelta(days=3)
        first.save(update_fields=['due_date'])
        djmail.outbox = []

        r = admin_client.post('/control/event/dummy/dummy/orders/1Z3AS/installment-plan/remind')

        assert r.status_code == 302
        assert len(djmail.outbox) == 0


@pytest.mark.django_db
class TestPresaleView:

    def test_order_page_shows_the_next_installment_with_bank_details(self, event, order, plan, client):
        # Follow the redirect that completes the freshly created first payment.
        r = client.get('/dummy/dummy/order/%s/%s/' % (order.code, order.secret), follow=True)
        content = r.content.decode()
        assert r.status_code == 200
        assert 'Next installment' in content
        assert 'DE27 5205 2154 0534 5344 66' in content
        assert 'Left to pay' in content


@pytest.mark.django_db
class TestOrderPlacement:

    def _cart_position(self, event):
        quota = Quota.objects.create(name="Checkout", size=10, event=event)
        item = Item.objects.create(event=event, name="Checkout ticket", default_price=Decimal('300.00'))
        quota.items.add(item)
        return CartPosition.objects.create(
            event=event, item=item, price=Decimal('300.00'),
            expires=now() + timedelta(days=1),
        )

    def test_installments_are_offered_for_bank_transfer(self, event):
        provider = event.get_payment_providers()['banktransfer']
        assert installments_available_for_event(event, provider, Decimal('300.00')) is True

    def test_installments_are_not_offered_when_disabled(self, event):
        event.settings.installments_enabled = False
        provider = event.get_payment_providers()['banktransfer']
        assert installments_available_for_event(event, provider, Decimal('300.00')) is False

    def test_choosing_installments_at_checkout_creates_a_push_plan(self, event):
        cart_position = self._cart_position(event)

        result = perform_order(
            event=event.id,
            payments=[{
                'provider': 'banktransfer',
                'payment_amount': Decimal('300.00'),
                'info_data': {},
                'pay_in_installments': True,
                'installments_count': 3,
            }],
            positions=[cart_position.id],
            meta_info={},
            email='buyer@example.com',
            locale='en',
        )

        placed = Order.objects.get(pk=result['order_id'])
        assert placed.installment_plan.mode == InstallmentPlan.MODE_PUSH
        assert placed.installment_plan.total_installments == 3
        assert placed.payments.first().amount == Decimal('100.00')
        assert placed.installment_plan.remaining_amount == Decimal('300.00')


@pytest.mark.django_db
def test_management_command_drives_the_push_flow(event, order, plan, job):
    _transfer(job, '100.00')
    with scopes_disabled():
        second = plan.installments.get(installment_number=2)
    second.due_date = now() - timedelta(days=1)
    second.save(update_fields=['due_date'])
    djmail.outbox = []

    call_command('process_installments')

    second.refresh_from_db()
    assert second.overdue_notice_sent is True
    assert len(djmail.outbox) == 1
