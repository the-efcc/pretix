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

import json
import logging
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import List

from dateutil.relativedelta import relativedelta
from django.db import models, transaction
from django.dispatch import receiver
from django.utils.timezone import now
from django_scopes import scopes_disabled

from pretix.base.email import get_email_context
from pretix.base.i18n import language
from pretix.base.models import Order, OrderFee, OrderPayment, Quota
from pretix.base.signals import order_canceled, periodic_task
from pretix.efcc.models import InstallmentPlan, ScheduledInstallment
from pretix.helpers.periodic import minimum_interval
from pretix.multidomain.urlreverse import eventreverse_absolute

logger = logging.getLogger(__name__)


def get_max_installments_for_event(event, reference_date=None) -> int:
    """
    Calculate the maximum number of installments allowed by event settings.

    :param event: The event to evaluate
    :param reference_date: Reference date (defaults to now())
    :return: Maximum allowed installments (0 means not allowed)
    """
    max_installments = event.settings.get('installments_count', as_type=int, default=3)

    if not event.settings.get('installments_limit_by_event_date', as_type=bool):
        return max_installments

    if reference_date is None:
        reference_date = now()

    event_date = event.date_from
    if not event_date:
        return max_installments

    # Ensure both dates are timezone-aware and in same timezone
    if event_date.tzinfo:
        current_date = reference_date.astimezone(event_date.tzinfo)
    else:
        current_date = reference_date

    months_diff = (event_date.year - current_date.year) * 12 + (event_date.month - current_date.month)

    # Adjust for day-of-month: if we're on or past the event's day in the current month,
    # we're "within" that many months, not "past" them
    if current_date.day >= event_date.day:
        months_diff -= 1

    if months_diff <= 0:
        return 0

    return min(months_diff, max_installments)


def installments_available_for_event(event, provider, cart_total: Decimal) -> bool:
    """
    Check if installments are available for an event, provider, and cart total.

    :param event: The event to evaluate
    :param provider: The payment provider instance
    :param cart_total: The total cart/order value
    :return: True if installments are available
    """
    if not provider or not getattr(provider, 'installments_supported', False):
        return False

    if not event.settings.get('installments_enabled', as_type=bool, default=False):
        return False

    min_value = event.settings.get('installments_min_order_value', as_type=Decimal)
    if min_value and cart_total < min_value:
        return False

    return get_max_installments_for_event(event) > 1


def calculate_installment_amounts(total_amount: Decimal, count: int) -> List[Decimal]:
    """
    Calculates the amounts for each installment payment.

        The calculation divides the total amount into `count` installments.
        It uses floor rounding for the base installment amount and adds any
        remainder to the final installment.

        Example: 100.00 / 3 -> [33.33, 33.33, 33.34]

    :param total_amount: The total amount to be split
    :param count: The number of installments
    :return: A list of Decimal amounts
    :raises ValueError: If count is less than 1
    """
    if count < 1:
        raise ValueError("Installment count must be at least 1")

    if count == 1:
        return [total_amount]

    per_installment = (total_amount / count).quantize(Decimal('0.01'), rounding=ROUND_FLOOR)
    installments = [per_installment] * (count - 1)
    last_installment = total_amount - sum(installments)
    installments.append(last_installment)

    return installments


@transaction.atomic
def create_installment_plan(
    order: Order,
    provider_name: str,
    installments_count: int,
    fee=None,
    info_data=None,
    amount=None,
) -> InstallmentPlan:
    """
    Creates an installment plan for an order.

    :param order: The Order object
    :param provider_name: The identifier of the payment provider
    :param installments_count: Number of installments
    :param fee: Optional payment fee for the first installment
    :param info_data: Optional payment info dict for the first installment
    :param amount: Explicit amount for the installment plan, inclusive of ``fee``. If None,
                   uses order.total minus payment fees. Use this when multi-use payments
                   (e.g. gift cards) cover part of the order.
    :return: The created InstallmentPlan
    :raises ValueError: If the provider does not support installments
    """
    event = order.event
    provider = event.get_payment_providers().get(provider_name)

    if not provider or not getattr(provider, 'installments_supported', False):
        raise ValueError(f"Provider '{provider_name}' does not support installments or is not active.")

    max_allowed = get_max_installments_for_event(event, reference_date=order.datetime)
    if installments_count > max_allowed:
        raise ValueError(
            f"Requested {installments_count} installments exceeds the maximum of {max_allowed} "
            f"allowed based on the event date."
        )

    # Only the ticket value is split; the payment fee is charged once, up front, with the
    # first installment. That is what the customer was shown at checkout, and spreading it
    # instead would quote them one figure and charge another.
    #
    # `amount` arrives fee-inclusive (it is the payment's share of the order total), so the
    # fee has to come back out before the split. `fee` is the OrderFee attached to this very
    # payment, which is exactly the part of `amount` that is fee.
    if amount is not None:
        payment_fees = fee.value if fee is not None else Decimal('0.00')
        base_total = amount - payment_fees
    else:
        payment_fees = order.fees.filter(fee_type=OrderFee.FEE_TYPE_PAYMENT).aggregate(
            total=models.Sum('value')
        )['total'] or Decimal('0.00')
        base_total = order.total - payment_fees

    amounts = calculate_installment_amounts(base_total, installments_count)

    plan = InstallmentPlan.objects.create(
        order=order,
        payment_provider=provider_name,
        payment_token={},
        total_installments=installments_count,
        installments_paid=0,
        amount_per_installment=amounts[0],
        status=InstallmentPlan.STATUS_ACTIVE
    )

    first_payment_amount = amounts[0] + payment_fees
    payment = order.payments.create(
        state=OrderPayment.PAYMENT_STATE_CREATED,
        provider=provider_name,
        amount=first_payment_amount,
        fee=fee,
        info=json.dumps(info_data) if info_data else '{}',
        process_initiated=False,
    )

    ScheduledInstallment.objects.create(
        plan=plan,
        installment_number=1,
        amount=first_payment_amount,
        due_date=now(),
        state=ScheduledInstallment.STATE_PENDING,
        payment=payment,
    )

    for i, amount in enumerate(amounts[1:], start=2):
        due_date = now() + relativedelta(months=i - 1)
        ScheduledInstallment.objects.create(
            plan=plan,
            installment_number=i,
            amount=amount,
            due_date=due_date,
            state=ScheduledInstallment.STATE_PENDING
        )

    return plan


#: How long an installment may sit in ``processing`` before another run is allowed to
#: take it over. Long enough that no provider call is still plausibly running, short
#: enough that a worker killed mid-charge does not wedge the installment for good.
PROCESSING_LEASE = timedelta(hours=1)


def start_grace_period(plan: InstallmentPlan, event) -> None:
    """
    Put a plan into its grace period after a failed charge, if it isn't already in one.

    The grace period is what eventually ends a plan that cannot be collected:
    :py:func:`send_grace_period_warnings` warns the customer, then
    :py:func:`process_expired_plans` cancels. An installment order is excluded from
    ``expire_orders`` precisely because its remaining payments are scheduled, so this is
    the *only* thing that stops a plan running forever -- every failure path has to start
    it, or the order becomes immortal and holds its quota indefinitely.
    """
    if plan.grace_period_end:
        return
    days = event.settings.get('installments_grace_period_days', as_type=int, default=7)
    plan.grace_period_end = now() + timedelta(days=days)
    plan.save(update_fields=['grace_period_end'])


def claim_installment(installment: ScheduledInstallment) -> bool:
    """
    Take exclusive ownership of an installment before charging it.

    Nothing serializes the callers of :py:func:`process_single_installment`. The periodic
    task, the ``process_installments`` command, the control-panel retry button and the API
    retry endpoint can all reach the same installment at the same time, and
    ``minimum_interval`` is explicit that its locking "should not be relied upon". Two
    callers getting through at once means charging the customer twice.

    So the state is the lock: a single conditional UPDATE moves the installment into
    ``processing``, and the database guarantees exactly one caller sees it succeed. An
    installment left in ``processing`` past :py:data:`PROCESSING_LEASE` is taken to belong
    to a run that died, and can be claimed again -- otherwise a killed worker would strand
    it in a state no retry path can reach.

    :return: True if this caller now owns the installment
    """
    with scopes_disabled():
        claimed = ScheduledInstallment.objects.filter(
            models.Q(state__in=(
                ScheduledInstallment.STATE_PENDING,
                ScheduledInstallment.STATE_FAILED,
            )) | models.Q(
                state=ScheduledInstallment.STATE_PROCESSING,
                processed_at__lt=now() - PROCESSING_LEASE,
            ),
            pk=installment.pk,
        ).update(state=ScheduledInstallment.STATE_PROCESSING, processed_at=now())

    if not claimed:
        return False
    installment.state = ScheduledInstallment.STATE_PROCESSING
    return True


def process_single_installment(installment: ScheduledInstallment, send_mail: bool = False) -> bool:
    """
    Processes a single installment payment.

    On success the payment is created and then confirmed through
    ``OrderPayment.confirm()``, so that the order reaches ``paid`` once the
    payments cover its total. Confirming is also what settles the scheduled
    installment and advances the plan.

    The installment is claimed first (see :py:func:`claim_installment`); if another run
    already owns it this returns ``False`` without charging anything.

    Deliberately **not** wrapped in a transaction. Money leaves the customer's account
    inside ``execute_installment``, and a transaction spanning that call means any later
    failure rolls back the record of a charge that really happened -- leaving pretix with
    no idea it took the money. So the payment row is committed before the provider is
    called, and every step after the charge either manages its own transaction
    (``confirm``, ``fail``) or is allowed to fail loudly without touching it. It also
    keeps a database transaction from being held open for the length of a provider
    round-trip.

    :param installment: The ScheduledInstallment to process
    :param send_mail: Whether to notify the customer — the failure notice on a
                      declined charge, the paid confirmation when the order is
                      settled (default False)
    :return: True if the charge succeeded, False otherwise
    """
    if not claim_installment(installment):
        logger.info(
            "Installment %s is already being processed elsewhere, skipping.",
            installment.pk,
        )
        return False

    with scopes_disabled():
        plan = installment.plan
        order = plan.order
        event = order.event

        provider = event.get_payment_providers().get(plan.payment_provider)
        if not provider:
            installment.state = ScheduledInstallment.STATE_PENDING
            installment.save(update_fields=['state'])
            return False

        if not plan.payment_token or plan.payment_token == {}:
            logger.error(
                "Cannot process installment %s for order %s: no payment token available.",
                installment.pk, order.code,
            )
            installment.state = ScheduledInstallment.STATE_FAILED
            installment.failure_reason = "No payment token available"
            installment.processed_at = now()
            installment.save(update_fields=['state', 'failure_reason', 'processed_at'])
            # A missing token is a dead end -- there is nothing to retry with -- so this
            # has to start the grace period like any other failure. Without it the plan
            # sits ACTIVE forever, and because expire_orders skips orders with an active
            # plan, so does the order.
            start_grace_period(plan, event)
            return False

        # Build the payment up front so the provider can record what it needs to act
        # on the charge later — a transaction ID to refund against, above all. It is
        # also what links the charge to the installment for OrderPayment.confirm().
        # This is committed before the charge, so that a crash mid-charge still leaves
        # evidence that we asked the provider for money.
        with transaction.atomic():
            payment = OrderPayment.objects.create(
                order=order,
                state=OrderPayment.PAYMENT_STATE_CREATED,
                amount=installment.amount,
                provider=plan.payment_provider,
            )
            installment.payment = payment
            installment.save(update_fields=['payment'])

        success = False
        try:
            success = provider.execute_installment(plan, installment, payment)
        except Exception:
            logger.exception(
                "Failed to execute installment %s for order %s",
                installment.pk, order.code,
            )

        if success:
            try:
                payment.confirm(send_mail=send_mail)
            except Quota.QuotaExceededException:
                # The money is already captured and the payment is confirmed; only the
                # order status could not be advanced. Not something we can resolve here.
                logger.warning(
                    "Installment %s for order %s was charged but the order could not be "
                    "marked as paid because quota is exhausted.",
                    installment.pk, order.code,
                )
            except Exception:
                # Same again, for anything else: the charge happened and the payment row
                # survives it, so this needs a human rather than a retry -- retrying would
                # charge the customer a second time.
                logger.exception(
                    "Installment %s for order %s was charged but payment %s could not be "
                    "confirmed. The charge is recorded and needs to be settled by hand.",
                    installment.pk, order.code, payment.full_id,
                )

            installment.refresh_from_db()
            plan.refresh_from_db()

            if plan.status == InstallmentPlan.STATUS_COMPLETED:
                try:
                    provider.revoke_payment_token(plan)
                except Exception:
                    logger.warning(
                        "Failed to revoke payment token for completed plan %s",
                        plan.pk,
                    )
                plan.payment_token = {}
                plan.save(update_fields=['payment_token'])

        else:
            # Keep the failed charge on the order, carrying whatever the provider
            # recorded about the decline. send_mail is off because we send our own
            # installment-specific notice below, with the recovery link.
            payment.fail(info=payment.info_data or None, send_mail=False,
                         log_data={'installment': installment.pk})

            installment.state = ScheduledInstallment.STATE_FAILED
            installment.save(update_fields=['state'])

            start_grace_period(plan, event)

            if send_mail:
                with language(order.locale, event.settings.region):
                    context = get_email_context(event=event, order=order)
                    context.update({
                        'failure_reason': installment.failure_reason or '',
                        'expire_date': plan.grace_period_end,
                        'url': eventreverse_absolute(
                            event, 'presale:event.order.installment.recovery',
                            kwargs={'order': order.code, 'secret': order.secret}
                        ),
                    })
                    try:
                        order.send_mail(
                            event.settings.mail_subject_installment_failed,
                            event.settings.mail_text_installment_failed,
                            context,
                            'pretix.event.order.installment.failed',
                        )
                    except Exception:
                        logger.warning(
                            "Failed to send installment failure email for order %s",
                            order.code,
                        )

        return success


def due_installments_queryset():
    """
    The installments the automatic processor is allowed to charge.

    Due and pending is not enough on its own. Installment 1 is created during checkout
    already pointing at the payment the customer is about to make, and it is due
    immediately -- so between order placement and the payment being confirmed it matches
    "due and pending" while a charge for it is already in flight. Charging it here would
    take the money a second time. The same holds for a recovery payment the customer has
    started but not finished.

    So an installment is only ours to charge once no payment of its own is still live.
    ``created`` and ``pending`` are the live states; ``confirmed`` settles the installment
    through :py:meth:`OrderPayment.confirm`, and the remaining states are dead ends that
    leave the installment to us.
    """
    return ScheduledInstallment.objects.filter(
        state=ScheduledInstallment.STATE_PENDING,
        due_date__lte=now(),
    ).exclude(
        payment__isnull=False,
        payment__state__in=(
            OrderPayment.PAYMENT_STATE_CREATED,
            OrderPayment.PAYMENT_STATE_PENDING,
        ),
    )


def process_due_installments():
    """
    Processes all scheduled installments that are due and pending.
    """
    with scopes_disabled():
        qs = due_installments_queryset().select_related(
            'plan', 'plan__order', 'plan__order__event'
        )

    for installment in qs:
        try:
            process_single_installment(installment, send_mail=True)
        except Exception:
            logger.exception(
                "Error processing installment %s for order %s",
                installment.pk, installment.plan.order.code,
            )


def process_expired_plans():
    """
    Processes all installment plans where the grace period has expired.
    Cancels the order and sends notification emails.
    """
    with scopes_disabled():
        qs = InstallmentPlan.objects.filter(
            status=InstallmentPlan.STATUS_ACTIVE,
            grace_period_end__lt=now()
        ).select_related('order', 'order__event')

    for plan in qs:
        try:
            order = plan.order
            event = order.event

            cancel_installment_plan(plan, cancel_order=True, user=None, log=True, send_mail=False)

            with language(order.locale, event.settings.region):
                email_subject = event.settings.mail_subject_installment_cancelled
                email_template = event.settings.mail_text_installment_cancelled

                context = get_email_context(event=event, order=order)

                try:
                    order.send_mail(
                        email_subject, email_template, context,
                        'pretix.event.order.installment.cancelled'
                    )
                except Exception:
                    logger.exception(
                        "Failed to send cancellation email for order %s", order.code,
                    )
        except Exception:
            logger.exception(
                "Error processing expired plan %s for order %s",
                plan.pk, plan.order.code,
            )


def send_installment_reminders():
    """
    Sends reminder emails for upcoming installments.
    """
    with scopes_disabled():
        qs = ScheduledInstallment.objects.filter(
            state=ScheduledInstallment.STATE_PENDING,
            reminder_sent=False,
            due_date__gte=now(),
            due_date__lte=now() + timedelta(days=30)
        ).select_related('plan', 'plan__order', 'plan__order__event')

    for installment in qs:
        order = installment.plan.order
        event = order.event

        provider = event.get_payment_providers().get(installment.plan.payment_provider)
        if not provider:
            continue

        days = event.settings.get('installments_reminder_days', as_type=int, default=3)

        if now() >= installment.due_date - timedelta(days=days):
            with language(order.locale, event.settings.region):
                email_subject = event.settings.mail_subject_installment_reminder
                email_template = event.settings.mail_text_installment_reminder

                context = get_email_context(event=event, order=order)
                context.update({
                    'amount': installment.amount,
                    'date': installment.due_date,
                    'installment_number': installment.installment_number,
                })

                try:
                    order.send_mail(
                        email_subject, email_template, context,
                        'pretix.event.order.installment.reminder'
                    )
                    installment.reminder_sent = True
                    installment.save(update_fields=['reminder_sent'])
                except Exception:
                    logger.warning(
                        "Failed to send reminder email for installment %s, order %s",
                        installment.pk, order.code,
                    )


def send_grace_period_warnings():
    """
    Sends warnings for installment plans where the grace period is about to expire.
    """
    with scopes_disabled():
        qs = InstallmentPlan.objects.filter(
            status=InstallmentPlan.STATUS_ACTIVE,
            grace_period_end__isnull=False,
            grace_period_end__lte=now() + timedelta(hours=24),
            grace_period_end__gt=now(),
            grace_warning_sent=False
        ).select_related('order', 'order__event')

    for plan in qs:
        order = plan.order
        event = order.event

        with language(order.locale, event.settings.region):
            email_subject = event.settings.mail_subject_installment_grace_warning
            email_template = event.settings.mail_text_installment_grace_warning

            context = get_email_context(event=event, order=order)
            context.update({
                'expire_date': plan.grace_period_end,
            })

            try:
                order.send_mail(
                    email_subject, email_template, context,
                    'pretix.event.order.installment.grace_warning'
                )
                plan.grace_warning_sent = True
                plan.save(update_fields=['grace_warning_sent'])
            except Exception:
                logger.warning(
                    "Failed to send grace period warning for plan %s, order %s",
                    plan.pk, order.code,
                )


def cancel_installment_plan(plan: InstallmentPlan, cancel_order: bool = False, user=None, log: bool = True, send_mail: bool = True):
    """
    Cancels an installment plan.

    Revoking the token at the provider is a network call, and is kept outside the
    transaction that records the cancellation so that a slow provider does not hold a
    database transaction open for the length of its round-trip.

    :param plan: The InstallmentPlan to cancel
    :param cancel_order: Whether to also cancel the order
    :param user: The user performing the action (for logging)
    :param log: Whether to log the cancellation action (default True)
    :param send_mail: Whether to send order cancellation email (only used if cancel_order=True)
    """
    with scopes_disabled():
        if plan.status == InstallmentPlan.STATUS_CANCELLED:
            return

        order = plan.order
        event = order.event

        provider = event.get_payment_providers().get(plan.payment_provider)
        if provider:
            try:
                provider.revoke_payment_token(plan)
            except Exception:
                logger.warning(
                    "Failed to revoke payment token for cancelled plan %s", plan.pk,
                )

        with transaction.atomic():
            plan.status = InstallmentPlan.STATUS_CANCELLED
            plan.payment_token = {}
            plan.save(update_fields=['status', 'payment_token'])

            ScheduledInstallment.objects.filter(
                plan=plan,
                state__in=[
                    ScheduledInstallment.STATE_PENDING,
                    ScheduledInstallment.STATE_PROCESSING,
                    ScheduledInstallment.STATE_FAILED,
                ]
            ).update(state=ScheduledInstallment.STATE_CANCELLED)

            if log:
                order.log_action(
                    'pretix.event.order.installment_plan.canceled',
                    data={'installment_plan_id': plan.pk},
                    user=user
                )

        if cancel_order:
            from pretix.base.services.orders import (
                cancel_order as cancel_order_service,
            )
            cancel_order_service(order.pk, user=user.pk if user else None, send_mail=send_mail)


@receiver(signal=order_canceled)
def handle_order_cancellation(sender, **kwargs):
    order = kwargs.get('order')

    try:
        plan = order.installment_plan
    except InstallmentPlan.DoesNotExist:
        return

    cancel_installment_plan(plan, cancel_order=False, user=None, log=False)


@receiver(signal=periodic_task)
@scopes_disabled()
@minimum_interval(minutes_after_success=10, minutes_after_error=2)
def run_installment_processing(sender, **kwargs):
    process_due_installments()
    process_expired_plans()
    send_installment_reminders()
    send_grace_period_warnings()
