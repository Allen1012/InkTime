"use strict";

/**
 * 照片管理页批量操作栏。
 *
 * 采用上下文操作栏模式：未勾选任何照片时不占版面，勾选后出现并显示已选数量。
 * 操作栏在模板中默认可见，只有脚本可用（<html class="js">）时才由样式收起，
 * 因此禁用脚本的环境仍能使用批量改分类。
 *
 * 「新值」字段随操作类型联动：设置分类时是文本框，设置分析状态时是中文下拉。
 * 两个控件同名 value，因此未启用的那个必须 disabled，否则会一起提交。
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

    /** 只保留当前操作对应的取值控件，另一个禁用以免重复提交同名字段。 */
    function renderValueField() {
        if (!actionSelect || !categoryField || !statusField) {
            return;
        }
        var isStatus = actionSelect.value === "set_analysis_status";
        categoryField.hidden = isStatus;
        statusField.hidden = !isStatus;
        if (categoryInput) {
            categoryInput.disabled = isStatus;
        }
        if (statusInput) {
            statusInput.disabled = !isStatus;
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
