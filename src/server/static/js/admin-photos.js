"use strict";

/**
 * 照片管理页批量操作栏。
 *
 * 采用上下文操作栏模式：未勾选任何照片时不占版面，勾选后出现并显示已选数量。
 * 操作栏在模板中默认可见，只有脚本可用（<html class="js">）时才由样式收起，
 * 因此禁用脚本的环境仍能使用全部批量字段。
 *
 * 三个字段各自独立提交，默认都是「不修改」，服务端按「键存在即要改」处理，所以
 * 这里不需要任何互斥逻辑。唯一的联动是分类：只有选了「覆盖为」才需要文本框，
 * 选「清空」或「不修改」时把它藏起来并禁用，避免提交一个与当前意图无关的旧值。
 */
(function () {
    var form = document.getElementById("photo-batch-form");
    var bar = document.getElementById("bulk-bar");
    if (!form || !bar) {
        return;
    }

    var counter = document.getElementById("bulk-count");
    var clearButton = document.getElementById("bulk-clear");
    var categoryMode = document.getElementById("bulk-category-mode");
    var categoryValueField = document.getElementById("bulk-category-value");
    var categoryInput = document.getElementById("bulk-category-input");
    var selectAll = document.getElementById("select-all-photos");
    // 工具栏里的全选按钮：表头复选框只存在于表格视图，网格视图没有任何全选入口，
    // 而批量操作栏要勾选后才出现，没有这个按钮就只能一张张点。
    var selectAllToolbar = document.getElementById("select-all-toolbar");

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

    /** 全选按钮兼作取消全选：文案与 aria-pressed 随当前是否已全选切换。 */
    function renderToolbarButton(total, selected) {
        if (!selectAllToolbar) {
            return;
        }
        var allChecked = total > 0 && selected === total;
        selectAllToolbar.textContent = allChecked ? "取消全选" : "全选本页";
        selectAllToolbar.setAttribute("aria-pressed", allChecked ? "true" : "false");
        selectAllToolbar.disabled = total === 0;
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
        renderToolbarButton(total, count);
    }

    /** 统一设置本页所有复选框，供表头全选框与工具栏按钮共用。 */
    function setAll(checked) {
        Array.prototype.forEach.call(allBoxes(), function (box) {
            box.checked = checked;
        });
        renderSelection();
    }

    /** 只有「覆盖为」需要分类文本框；其余模式下隐藏并禁用，避免提交无关的旧值。 */
    function renderCategoryField() {
        if (!categoryMode || !categoryValueField) {
            return;
        }
        var needsValue = categoryMode.value === "set";
        categoryValueField.hidden = !needsValue;
        if (categoryInput) {
            categoryInput.disabled = !needsValue;
            if (!needsValue) {
                categoryInput.value = "";
            }
        }
    }

    // 勾选框由服务端渲染，用表单级事件委托即可覆盖两种视图
    form.addEventListener("change", function (event) {
        var target = event.target;
        if (target === selectAll) {
            setAll(selectAll.checked);
            return;
        }
        if (target && target.name === "selected") {
            renderSelection();
        }
        if (target === categoryMode) {
            renderCategoryField();
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

    if (selectAllToolbar) {
        // 模板里默认 hidden：无脚本环境下这个按钮点了没反应，不该出现
        selectAllToolbar.hidden = false;
        selectAllToolbar.addEventListener("click", function () {
            var total = allBoxes().length;
            setAll(selectedBoxes().length !== total);
        });
    }

    var curationSelect = document.getElementById("bulk-curation");
    // 「改为已收录」在自动排队模式下会立刻按张调用模型，一次提交最多一百张，是本页
    // 放大倍数最高的付费动作，因此加一道二次确认。隐藏照片走的是模板里的内联确认，
    // 这里必须写在脚本里：确认文案要带上本次选中的张数，模板渲染时还不知道这个数。
    form.addEventListener("submit", function (event) {
        if (form.dataset.autoAnalyze !== "true") {
            return;
        }
        // 隐藏照片是另一个提交按钮，它自己已有确认，不能被这条逻辑再拦一次
        if (event.submitter && event.submitter.name === "batch_soft_delete") {
            return;
        }
        if (!curationSelect || curationSelect.value !== "included") {
            return;
        }
        var count = selectedBoxes().length;
        if (count === 0) {
            return;
        }
        var confirmed = window.confirm(
            "把选中的 " + count + " 张照片改为已收录后，会立即为其中尚未分析的照片排队分析，" +
            "每张都会调用视觉模型并产生费用。确认继续吗？"
        );
        if (!confirmed) {
            event.preventDefault();
        }
    });

    renderCategoryField();
    renderSelection();
})();
