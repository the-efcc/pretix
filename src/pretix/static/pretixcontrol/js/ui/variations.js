/*global $, Morris, gettext, formatPrice*/
$(function () {
    // Question view
    if (!$("#item_variations").length) {
        return;
    }

    function update_variation_summary($el) {
        var var_names = Object.fromEntries(
          $el
            .find("input[name*=-value_]")
            .filter(function () {
              return !!this.value;
            })
            .map(function () {
              return [[this.getAttribute("lang"), this.value]];
            })
            .get()
        );
        var var_name = i18nToString(var_names);
        var price = $el.find("input[name*=-default_price]").val();
        if (price) {
            var currency = $el.find("[name*=-default_price] + .input-group-addon").text();
            price = formatPrice(price, currency);
        }

        $el.find(".variation-name").text(var_name);
        $el.find(".variation-price").text(price);
        $el.find(".variation-timeframe").toggleClass("variation-icon-hidden", !(
            !!$el.find("input[name$=-available_from_0]").val() ||
            !!$el.find("input[name$=-available_until_0]").val()
        ));
        $el.find(".variation-name").toggleClass("variation-disabled", !(
            !!$el.find("input[name$=-active]").prop("checked")
        ));
        $el.find(".variation-voucher").toggleClass("variation-icon-hidden", !(
            !!$el.find("input[name$=-hide_without_voucher]").prop("checked")
        ));
        $el.find(".variation-membership").toggleClass("variation-icon-hidden", !(
            !!$el.find("input[name$=-require_membership]").prop("checked")
        ));
        $el.find(".variation-warning").toggleClass("hidden", !(
            $el.find(".alert-warning").length
        ));
        $el.find(".variation-error").toggleClass("hidden", !(
            $el.find(".alert-danger, .has-error").length
        ));
        $el.find("input[name$=-limit_sales_channels]").each(function () {
            $el.find(".variation-channel-" + $(this).val()).toggleClass("variation-icon-hidden", !(
                (
                    $(this).closest("[data-formset-form]").find("input[name$=-all_sales_channels]").prop("checked") ||
                    $(this).prop("checked")
                ) && (
                    $("input[name=all_sales_channels]").prop("checked") ||
                    $("input[name=limit_sales_channels][value=" + $(this).val() + "]").prop("checked")
                )
            ));
        })
    }

    function field_suffix(name) {
        // Turn "prefix-3-default_price" into "default_price"
        var m = name && name.match(/-\d+-(.+)$/);
        return m ? m[1] : null;
    }

    function clone_variation($src) {
        // Add a fresh variation form and copy the source's values into it.
        $("#item_variations [data-formset-add]").first().click();
        var $new = $("#item_variations [data-formset-body] > [data-formset-form]").last();
        $src.find(":input").each(function () {
            var suffix = field_suffix(this.name);
            if (!suffix || suffix === "id" || suffix === "DELETE" || suffix === "ORDER") {
                return;
            }
            var source = this;
            var $target = $new.find(":input").filter(function () {
                if (field_suffix(this.name) !== suffix) {
                    return false;
                }
                // Checkbox/radio groups (e.g. sales channels) share a name, so match on value too.
                if (source.type === "checkbox" || source.type === "radio") {
                    return this.value === source.value;
                }
                return true;
            });
            if (!$target.length) {
                return;
            }
            if (source.type === "checkbox" || source.type === "radio") {
                $target.prop("checked", source.checked);
            } else {
                $target.val($(source).val());
            }
        });
        $new.prop("open", true);
        $new.find(":input").first().trigger("change");
        update_variation_summary($new);
        return $new;
    }

    $("#item_variations").on("click", "[data-variation-clone]", function (e) {
        e.preventDefault();
        clone_variation($(this).closest("[data-formset-form]"));
    });

    $("#item_variations [data-formset-form]").each(function () {
        var $el = $(this);
        update_variation_summary($el);
        $(this).on("change dp.change", "input", function () {update_variation_summary($el)});
    });
    $("input[name=limit_sales_channels] input[name=all_sales_channels]").on("change", function() {
        $("#item_variations [data-formset-form]").each(function () {
            update_variation_summary($(this));
        });
    });
    $("#item_variations").on("formAdded", "details", function (event) {
        var $el = $(event.target);
        update_variation_summary($el);
        $(this).on("change dp.change", "input", function () {update_variation_summary($el)});
        setup_collapsible_details($("#item_variations"));
        form_handlers($(event.target));
    });
});
