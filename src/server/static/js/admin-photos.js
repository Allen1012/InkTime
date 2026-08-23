"use strict";

/**
 * 照片管理页批量操作栏。
 *
 * 采用上下文操作栏模式：未勾选任何照片时不占版面，勾选后出现并显示已选数量。
 * 操作栏在模板中默认可见，只有脚本可用（<html class="js">）时才由样式收起，
 * 因此禁用脚本的环境仍能使用批量改分类。
 *
 * 「新值」字段随操作类型联动：设置分类是文本框，设置分析状态与设置收录状态各是
 * 一个中文下拉。三个控件同名 value，因此未启用的必须 disabled，否则会一起提交、
 * 服务端只会取到第一个，表现为「选了收录却改了分类」这种难查的错。
 */
(function () {
    var form = document.getElementById("photo-batch-form");
    var bar = document.getElementById("bulk-bar");
    if (!form || !bar) {
        return;
    }

    var counter = document.getElementById("bulk-count");
    var clearButton = document.getElementById("bulk-clear");
    var actionSelect = document.getElementById("bulk-action");
    var categoryField = document.getElementById("bulk-value-category");
    var categoryInput = document.getElementById("bulk-value-category-input");
    var statusField = document.getElementById("bulk-value-status");
    var statusInput = document.getElementById("bulk-value-status-input");
    var curationField = document.getElementById("bulk-value-curation");
    var curationInput = document.getElementById("bulk-value-curation-input");
    var selectAll = document.getElementById("select-all-photos");

    function allBoxes() {
        return form.querySelectorAll('input[name="selected"]');
    }

    function selectedBoxes() {
        return form.querySelectorAll('input[name="selected"]:checked');
    }

    /** 同步表头全选框：部分选中时用 indeterminate 表达，避免误导为全选。 */
    function renderSelectAll(total, selected) {
        if (!selectAll) {
            return;
        }
        selectAll.checked = total > 0 && selected === total;
        selectAll.indeterminate = selected > 0 && selected < total;
    }

    function renderSelection() {
        var total = allBoxes().length;
        var count = selectedBoxes().length;
        if (counter) {
            counter.textContent = String(count);
        }
        bar.classList.toggle("is-active", count > 0);
        if (clearButton) {
            clearButton.hidden = count === 0;
        }
        renderSelectAll(total, count);
    }

    /** 只保留当前操作对应的取值控件，其余禁用以免重复提交同名字段。 */
    function renderValueField() {
        if (!actionSelect || !categoryField || !statusField) {
            return;
        }
        var action = actionSelect.value;
        var isStatus = action === "set_analysis_status";
        var isCuration = action === "set_curation";
        var isCategory = !isStatus && !isCuration;
        categoryField.hidden = !isCategory;
        statusField.hidden = !isStatus;
        if (curationField) {
            curationField.hidden = !isCuration;
        }
        if (categoryInput) {
            categoryInput.disabled = !isCategory;
        }
        if (statusInput) {
            statusInput.disabled = !isStatus;
        }
        if (curationInput) {
            curationInput.disabled = !isCuration;
        }
    }

    // 勾选框由服务端渲染，用表单级事件委托即可覆盖两种视图
    form.addEventListener("change", function (event) {
        var target = event.target;
        if (target === selectAll) {
            var shouldCheck = selectAll.checked;
            Array.prototype.forEach.call(allBoxes(), function (box) {
                box.checked = shouldCheck;
            });
            renderSelection();
            return;
        }
        if (target && target.name === "selected") {
            renderSelection();
        }
        if (target === actionSelect) {
            renderValueField();
        }
    });

    if (clearButton) {
        clearButton.addEventListener("click", function () {
            Array.prototype.forEach.call(selectedBoxes(), function (box) {
                box.checked = false;
            });
            renderSelection();
        });
    }

    renderValueField();
    renderSelection();
})();
