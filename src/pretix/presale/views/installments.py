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
from decimal import Decimal

from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from pretix.base.services.installments import (
    calculate_installment_amounts, get_max_installments_for_event,
)


class InstallmentChoiceMixin:
    """
    The "Pay in installments" choice, shared by checkout and the ordinary payment page.

    Both render ``pretixpresale/event/fragment_installment_choice.html`` and both drive
    ``ui/installments.js``, so the figures behind the picker, the validation of what comes
    back from it and the context it reads belong in one place -- otherwise the fragment's
    contract lives in two views that have to be changed together.

    A page supplies two things: :py:attr:`installment_base_amount`, the amount the
    installments split, and :py:meth:`installment_previous_choice`, the number of
    installments the customer chose last. It also has to provide ``provider_forms``, each
    entry carrying ``installments_available``.
    """

    @property
    def installment_base_amount(self) -> Decimal:
        """
        The amount the installments split, with the payment fee left out.

        The fee is charged once, up front, with the first installment, so it must not be
        spread across them -- and ``installments_min_order_value`` is a floor on the
        ticket value, not on the value plus whatever the chosen method costs.
        """
        raise NotImplementedError()

    def installment_previous_choice(self) -> int:
        """
        The number of installments chosen before, or 0 if there was no such choice.

        It pre-selects the picker when the page is first shown: the cart's choice during
        checkout, the stopped plan's on the payment page.
        """
        return 0

    @cached_property
    def installment_options(self):
        """
        The plans on offer: ``{'count': n, 'amount': first installment}`` each.

        Empty when no enabled payment method supports installments, which is also how
        "not offered" is expressed -- the fragment and every check below read it that way.
        """
        if not any(p['installments_available'] for p in self.provider_forms):
            return []
        # Read once: for an order this is a query, and there is one option per count.
        base = self.installment_base_amount
        max_installments = get_max_installments_for_event(self.request.event)
        return [
            {'count': count, 'amount': calculate_installment_amounts(base, count)[0]}
            for count in range(2, max_installments + 1)
        ]

    @cached_property
    def installment_counts(self):
        return [o['count'] for o in self.installment_options]

    @cached_property
    def pay_in_installments_selected(self) -> bool:
        if self.request.method == 'POST':
            return self.request.POST.get('pay_in_installments') == 'on'
        return self.installment_previous_choice() >= 2

    @cached_property
    def selected_installments_count(self) -> int:
        counts = self.installment_counts
        if not counts:
            return 0
        if self.request.method == 'POST':
            try:
                count = int(self.request.POST.get('installments_count', '0'))
            except (ValueError, TypeError):
                count = 0
            if count in counts:
                return count
        previous = self.installment_previous_choice()
        if previous:
            # As many as before, or as many as are still possible now the event is closer.
            return min(max(previous, counts[0]), counts[-1])
        return counts[0]

    def installments_count_from_post(self, provider_entry):
        """
        How many installments this POST asks for, validated against what is on offer.

        :param provider_entry: The ``provider_forms`` entry for the chosen payment method
        :return: ``(count, error)``, where ``count`` is 0 when the customer is paying at
                 once and ``error`` is the message to show when their choice cannot be
                 honoured -- the number is no longer possible, or the method cannot do
                 installments at all.
        """
        if not self.installment_options or not self.pay_in_installments_selected:
            return 0, None
        if not provider_entry['installments_available']:
            return 0, _('Please select a payment method that supports installments.')
        try:
            count = int(self.request.POST.get('installments_count', '0'))
        except (ValueError, TypeError):
            count = 0
        if count not in self.installment_counts:
            return 0, _('Please select a valid installment plan.')
        return count, None

    def installment_context(self):
        """The context ``fragment_installment_choice.html`` reads."""
        available = bool(self.installment_options)
        return {
            'installments_available': available,
            'pay_in_installments': available and self.pay_in_installments_selected,
            'installment_options': self.installment_options,
            'selected_installments_count': self.selected_installments_count,
        }
