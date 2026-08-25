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
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _, pgettext_lazy
from django_scopes import ScopedManager, scopes_disabled


class InstallmentPlan(models.Model):
    """
    Represents an installment payment plan for an order.

    :param order: The order this installment plan belongs to (one-to-one relationship)
    :type order: Order
    :param payment_provider: The payment provider handling the installments
    :type payment_provider: str
    :param payment_token: Tokenized payment method data (provider-specific)
    :type payment_token: dict
    :param total_installments: Total number of installments in the plan
    :type total_installments: int
    :param installments_paid: Number of installments successfully paid
    :type installments_paid: int
    :param amount_per_installment: The amount for each installment payment
    :type amount_per_installment: Decimal
    :param status: Current status of the installment plan
    :type status: str
    :param grace_period_end: End of grace period for failed payments (nullable)
    :type grace_period_end: datetime
    :param created_at: When this plan was created
    :type created_at: datetime
    :param updated_at: When this plan was last updated
    :type updated_at: datetime
    """
    STATUS_ACTIVE = 'active'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'

    STATUS_CHOICES = (
        (STATUS_ACTIVE, pgettext_lazy('installment_status', 'active')),
        (STATUS_COMPLETED, pgettext_lazy('installment_status', 'completed')),
        (STATUS_FAILED, pgettext_lazy('installment_status', 'failed')),
        (STATUS_CANCELLED, pgettext_lazy('installment_status', 'cancelled')),
    )

    order = models.OneToOneField(
        'pretixbase.Order',
        verbose_name=_("Order"),
        related_name='installment_plan',
        on_delete=models.PROTECT
    )
    payment_provider = models.CharField(
        max_length=255,
        verbose_name=_("Payment provider")
    )
    payment_token = models.JSONField(
        verbose_name=_("Payment token"),
        help_text=_("Tokenized payment method data")
    )
    grace_warning_sent = models.BooleanField(
        default=False,
        verbose_name=_("Grace warning sent")
    )
    total_installments = models.PositiveIntegerField(
        verbose_name=_("Total installments")
    )
    installments_paid = models.PositiveIntegerField(
        default=0,
        verbose_name=_("Installments paid")
    )
    amount_per_installment = models.DecimalField(
        decimal_places=2,
        max_digits=13,
        verbose_name=_("Amount per installment")
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        verbose_name=_("Status")
    )
    grace_period_end = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Grace period end")
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Created at")
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name=_("Updated at")
    )

    objects = ScopedManager(organizer='order__event__organizer')

    class Meta:
        ordering = ('-created_at',)

    @transaction.atomic
    def record_successful_payment(self):
        with scopes_disabled():
            plan = InstallmentPlan.objects.select_for_update().get(pk=self.pk)
        plan.installments_paid += 1
        plan.grace_period_end = None
        plan.grace_warning_sent = False
        if plan.installments_paid >= plan.total_installments:
            plan.status = InstallmentPlan.STATUS_COMPLETED
        plan.save(update_fields=['installments_paid', 'grace_period_end', 'grace_warning_sent', 'status'])
        for field in self._meta.concrete_fields:
            if not field.is_relation:
                setattr(self, field.attname, getattr(plan, field.attname))
        return plan.status == InstallmentPlan.STATUS_COMPLETED

    def store_payment_token(self, token_data):
        """
        Store the payment token for this installment plan.

        This should be called by payment providers after successfully processing
        the first installment payment and obtaining tokenization data for future charges.

        :param token_data: Provider-specific token data (dict)
        :raises ValueError: If token_data is None or empty
        """
        if not token_data:
            raise ValueError("Token data cannot be None or empty")

        self.payment_token = token_data
        self.save(update_fields=['payment_token'])

    def __str__(self):
        return f"InstallmentPlan for {self.order.code}"


class ScheduledInstallment(models.Model):
    """
    Represents a scheduled installment payment within an installment plan.

    :param plan: The installment plan this installment belongs to
    :type plan: InstallmentPlan
    :param installment_number: The sequential number of this installment (1-based)
    :type installment_number: int
    :param amount: The amount for this specific installment
    :type amount: Decimal
    :param due_date: When this installment payment is due
    :type due_date: datetime
    :param state: Current state of this installment
    :type state: str
    :param payment: The OrderPayment created when this installment is paid (nullable)
    :type payment: OrderPayment
    :param processed_at: When this installment was processed (nullable)
    :type processed_at: datetime
    :param failure_reason: Reason for payment failure if applicable (nullable)
    :type failure_reason: str
    """
    STATE_PENDING = 'pending'
    STATE_PROCESSING = 'processing'
    STATE_PAID = 'paid'
    STATE_FAILED = 'failed'
    STATE_CANCELLED = 'cancelled'

    STATE_CHOICES = (
        (STATE_PENDING, pgettext_lazy('installment_state', 'pending')),
        (STATE_PROCESSING, pgettext_lazy('installment_state', 'processing')),
        (STATE_PAID, pgettext_lazy('installment_state', 'paid')),
        (STATE_FAILED, pgettext_lazy('installment_state', 'failed')),
        (STATE_CANCELLED, pgettext_lazy('installment_state', 'cancelled')),
    )

    plan = models.ForeignKey(
        InstallmentPlan,
        verbose_name=_("Installment plan"),
        related_name='installments',
        on_delete=models.CASCADE
    )
    installment_number = models.PositiveIntegerField(
        verbose_name=_("Installment number")
    )
    amount = models.DecimalField(
        decimal_places=2,
        max_digits=13,
        verbose_name=_("Amount")
    )
    due_date = models.DateTimeField(
        verbose_name=_("Due date")
    )
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_PENDING,
        verbose_name=_("State"),
        db_index=True
    )
    payment = models.ForeignKey(
        'pretixbase.OrderPayment',
        null=True,
        blank=True,
        related_name='installment',
        on_delete=models.SET_NULL,
        verbose_name=_("Payment")
    )
    processed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Processed at")
    )
    failure_reason = models.TextField(
        null=True,
        blank=True,
        verbose_name=_("Failure reason")
    )
    reminder_sent = models.BooleanField(
        default=False,
        verbose_name=_("Reminder sent")
    )

    objects = ScopedManager(organizer='plan__order__event__organizer')

    class Meta:
        ordering = ('installment_number',)
        unique_together = [('plan', 'installment_number')]
        indexes = [
            models.Index(fields=['due_date', 'state']),
        ]

    def __str__(self):
        return f"Installment {self.installment_number} for {self.plan.order.code}"
