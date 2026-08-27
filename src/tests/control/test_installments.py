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
    Event, Item, Order, OrderPosition, Organizer, Team, User,
)
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment


@pytest.fixture
def event():
    o = Organizer.objects.create(name='Dummy', slug='dummy')
    with scope(organizer=o):
        event = Event.objects.create(
            organizer=o, name='Dummy Event', slug='dummy',
            date_from=now() + timedelta(days=365),
        )
        yield event


@pytest.fixture
def orders(event):
    with scope(organizer=event.organizer):
        item = Item.objects.create(event=event, name='Ticket', default_price=Decimal('100.00'))

        def _position(order):
            OrderPosition.objects.create(
                order=order, item=item, price=order.total, positionid=1,
            )

        order_no_plan = Order.objects.create(
            code='NOPLAN', event=event, email='test1@example.com',
            status=Order.STATUS_PAID, datetime=now(),
            expires=now() + timedelta(days=10), total=Decimal('100.00'),
            locale='en',
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )
        order_active = Order.objects.create(
            code='ACTIVE', event=event, email='test2@example.com',
            status=Order.STATUS_PENDING, datetime=now(),
            expires=now() + timedelta(days=10), total=Decimal('300.00'),
            locale='en',
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )
        InstallmentPlan.objects.create(
            order=order_active, payment_provider='dummy',
            payment_token={'token': 'tok_active'}, total_installments=3,
            installments_paid=1, amount_per_installment=Decimal('100.00'),
            status=InstallmentPlan.STATUS_ACTIVE,
        )
        order_completed = Order.objects.create(
            code='COMPLETED', event=event, email='test3@example.com',
            status=Order.STATUS_PAID, datetime=now(),
            expires=now() + timedelta(days=10), total=Decimal('300.00'),
            locale='en',
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )
        InstallmentPlan.objects.create(
            order=order_completed, payment_provider='dummy',
            payment_token={}, total_installments=3,
            installments_paid=3, amount_per_installment=Decimal('100.00'),
            status=InstallmentPlan.STATUS_COMPLETED,
        )
        order_failed = Order.objects.create(
            code='FAILED', event=event, email='test4@example.com',
            status=Order.STATUS_PENDING, datetime=now(),
            expires=now() + timedelta(days=10), total=Decimal('300.00'),
            locale='en',
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )
        plan_failed = InstallmentPlan.objects.create(
            order=order_failed, payment_provider='dummy',
            payment_token={'token': 'tok_failed'}, total_installments=3,
            installments_paid=1, amount_per_installment=Decimal('100.00'),
            status=InstallmentPlan.STATUS_ACTIVE,
            grace_period_end=now() + timedelta(days=5),
        )
        ScheduledInstallment.objects.create(
            plan=plan_failed, installment_number=2, amount=Decimal('100.00'),
            due_date=now() - timedelta(days=2), state=ScheduledInstallment.STATE_FAILED,
        )

        for o in (order_no_plan, order_active, order_completed, order_failed):
            _position(o)

        yield {
            'no_plan': order_no_plan,
            'active': order_active,
            'completed': order_completed,
            'failed': order_failed,
        }


@pytest.mark.django_db
class TestInstallmentFilters:

    def test_filter_no_plan(self, orders):
        with scope(organizer=orders['no_plan'].event.organizer):
            qs = Order.objects.filter(installment_plan__isnull=True)
            assert orders['no_plan'] in qs
            assert orders['active'] not in qs

    def test_filter_active(self, orders):
        with scope(organizer=orders['active'].event.organizer):
            qs = Order.objects.filter(
                installment_plan__status=InstallmentPlan.STATUS_ACTIVE,
                installment_plan__grace_period_end__isnull=True,
            )
            assert orders['active'] in qs
            assert orders['no_plan'] not in qs
            assert orders['completed'] not in qs
            assert orders['failed'] not in qs

    def test_filter_completed(self, orders):
        with scope(organizer=orders['completed'].event.organizer):
            qs = Order.objects.filter(
                installment_plan__status=InstallmentPlan.STATUS_COMPLETED,
            )
            assert orders['completed'] in qs
            assert orders['active'] not in qs

    def test_filter_grace_period(self, orders):
        with scope(organizer=orders['failed'].event.organizer):
            qs = Order.objects.filter(
                installment_plan__grace_period_end__isnull=False,
            )
            assert orders['failed'] in qs
            assert orders['active'] not in qs
            assert orders['completed'] not in qs


@pytest.fixture
def staff_client(client, event):
    user = User.objects.create_user('dummy@dummy.dummy', 'dummy')
    team = Team.objects.create(organizer=event.organizer, all_event_permissions=True,
                               all_organizer_permissions=True)
    team.members.add(user)
    team.limit_events.add(event)
    client.login(email='dummy@dummy.dummy', password='dummy')
    return client


def _order_url(order, suffix=''):
    return '/control/event/{}/{}/orders/{}/{}'.format(
        order.event.organizer.slug, order.event.slug, order.code, suffix,
    )


def _mock_provider(execute_result=True):
    p = MagicMock()
    p.installments_supported = True
    p.execute_installment.return_value = execute_result
    p.settings.get.return_value = 7
    return p


def _patch_providers(provider):
    return patch('pretix.base.models.Event.get_payment_providers', return_value={'dummy': provider})


@pytest.mark.django_db
class TestInstallmentPanelControls:
    """
    The panel's two buttons were guarded by a permission name that is not in the new-style
    permission set. EventPermissionSet raises on an unknown name and Django's {% if %}
    swallows that exception, so the check was silently always false and the buttons never
    rendered for anyone.
    """

    def test_buttons_render_for_a_user_who_can_change_orders(self, staff_client, orders):
        r = staff_client.get(_order_url(orders['failed']))
        content = r.content.decode()
        assert r.status_code == 200
        assert 'installment_plan.cancel'.replace('.', '/') not in content  # sanity: url is a path
        assert _order_url(orders['failed'], 'installment-plan/cancel') in content
        assert _order_url(orders['failed'], 'installment-plan/retry') in content

    def test_retry_button_is_hidden_without_a_failed_installment(self, staff_client, orders):
        r = staff_client.get(_order_url(orders['active']))
        content = r.content.decode()
        assert _order_url(orders['active'], 'installment-plan/cancel') in content
        assert _order_url(orders['active'], 'installment-plan/retry') not in content

    def test_no_panel_without_a_plan(self, staff_client, orders):
        r = staff_client.get(_order_url(orders['no_plan']))
        assert _order_url(orders['no_plan'], 'installment-plan/cancel') not in r.content.decode()

    def test_buttons_are_hidden_once_the_plan_is_no_longer_active(self, staff_client, orders):
        r = staff_client.get(_order_url(orders['completed']))
        content = r.content.decode()
        assert 'Installment Plan' in content
        assert _order_url(orders['completed'], 'installment-plan/cancel') not in content


@pytest.mark.django_db
class TestInstallmentPlanCancelView:

    def test_cancel_plan_only(self, staff_client, orders):
        order = orders['active']
        with _patch_providers(_mock_provider()):
            r = staff_client.post(_order_url(order, 'installment-plan/cancel'), {})
        assert r.status_code == 302

        with scope(organizer=order.event.organizer):
            order.refresh_from_db()
            assert order.installment_plan.status == InstallmentPlan.STATUS_CANCELLED
            assert order.installment_plan.payment_token == {}
            assert order.status == Order.STATUS_PENDING

    def test_cancel_plan_and_order(self, staff_client, orders):
        order = orders['active']
        with _patch_providers(_mock_provider()):
            r = staff_client.post(_order_url(order, 'installment-plan/cancel'),
                                  {'cancel_order': 'on'})
        assert r.status_code == 302

        with scope(organizer=order.event.organizer):
            order.refresh_from_db()
            assert order.installment_plan.status == InstallmentPlan.STATUS_CANCELLED
            assert order.status == Order.STATUS_CANCELED

    def test_refuses_an_inactive_plan(self, staff_client, orders):
        order = orders['completed']
        r = staff_client.post(_order_url(order, 'installment-plan/cancel'), {})
        assert r.status_code == 302

        with scope(organizer=order.event.organizer):
            order.refresh_from_db()
            assert order.installment_plan.status == InstallmentPlan.STATUS_COMPLETED

    def test_404_without_a_plan(self, staff_client, orders):
        r = staff_client.post(_order_url(orders['no_plan'], 'installment-plan/cancel'), {})
        assert r.status_code == 302  # redirected back with an error message


@pytest.mark.django_db
class TestInstallmentRetryView:

    def test_retry_charges_the_failed_installment(self, staff_client, orders):
        order = orders['failed']
        provider = _mock_provider()
        with _patch_providers(provider):
            r = staff_client.post(_order_url(order, 'installment-plan/retry'), {})
        assert r.status_code == 302
        assert provider.execute_installment.call_count == 1

        with scope(organizer=order.event.organizer):
            inst = ScheduledInstallment.objects.get(plan__order=order, installment_number=2)
            assert inst.state == ScheduledInstallment.STATE_PAID

    def test_a_declined_retry_leaves_the_installment_failed(self, staff_client, orders):
        order = orders['failed']
        with _patch_providers(_mock_provider(execute_result=False)):
            r = staff_client.post(_order_url(order, 'installment-plan/retry'), {})
        assert r.status_code == 302

        with scope(organizer=order.event.organizer):
            inst = ScheduledInstallment.objects.get(plan__order=order, installment_number=2)
            assert inst.state == ScheduledInstallment.STATE_FAILED

    def test_nothing_to_retry(self, staff_client, orders):
        order = orders['active']
        provider = _mock_provider()
        with _patch_providers(provider):
            r = staff_client.post(_order_url(order, 'installment-plan/retry'), {})
        assert r.status_code == 302
        assert provider.execute_installment.call_count == 0
