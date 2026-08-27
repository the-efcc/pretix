function init_installment_filtering() {
    var installmentToggle = document.getElementById('pay_in_installments');
    if (!installmentToggle) {
        return;
    }

    var countGroup = document.getElementById('installments_count_group');
    var countSelect = document.getElementById('installments_count');
    var providerPanels = Array.prototype.slice.call(document.querySelectorAll('[data-payment-provider]'));

    var syncInstallmentMode = function () {
        var useInstallments = installmentToggle.checked;
        var firstVisibleRadio = null;
        // Only true if hiding a panel took away a choice the customer had already made.
        // Picking a provider for someone who has not picked one is not this script's job:
        // doing it on page load silently pre-selects a payment method they never chose.
        var clearedTheirChoice = false;

        countGroup.hidden = !useInstallments;
        countSelect.disabled = !useInstallments;

        providerPanels.forEach(function (panel) {
            var supportsInstallments = panel.getAttribute('data-installments-available') === 'true';
            var shouldShow = !useInstallments || supportsInstallments;
            var radio = panel.querySelector('input[name="payment"]');

            panel.hidden = !shouldShow;
            panel.style.display = shouldShow ? '' : 'none';

            if (shouldShow && radio && !firstVisibleRadio) {
                firstVisibleRadio = radio;
            }

            if (radio && radio.checked && !shouldShow) {
                radio.checked = false;
                clearedTheirChoice = true;
            }
        });

        if (clearedTheirChoice && firstVisibleRadio) {
            firstVisibleRadio.checked = true;
        }
    };

    installmentToggle.addEventListener('change', syncInstallmentMode);
    syncInstallmentMode();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init_installment_filtering);
} else {
    init_installment_filtering();
}
