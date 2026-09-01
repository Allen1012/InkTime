"use strict";

/**
 * 模型厂商页的连通性测试与编辑页的模型清单行管理。
 *
 * 增删改停与保存全部走普通表单提交，因此禁用脚本时两个页面依然可用：列表页只是少了
 * 测试按钮，编辑页少了「再加一个模型」（末尾预留的空行仍能新增一个）。
 *
 * 列表页的测试只传厂商名，服务端取该档案已存的密钥与当前启用模型；密钥既不出现在页面
 * 里，也不在请求里往回走一遍。编辑页的测试要传本页正在填写的地址与该行模型名——不然
 * 改完必须先保存才能验，而保存错配置正是要避免的事；密钥仍然留空，由服务端取已存值。
 */
(function () {
    /** 从任意表单取一个可用的 CSRF 令牌：接口对写请求强制校验。 */
    function csrfToken() {
        var field = document.querySelector('input[name="csrf_token"]');
        return field ? field.value : "";
    }

    /**
     * 向连通性测试接口发一次请求，并把结论交给回调渲染。
     *
     * @param {Object} payload 请求体，至少含厂商名或地址与模型名。
     * @param {function(string, boolean): void} render 接收结论文本与是否失败。
     */
    function probe(payload, render) {
        fetch("/api/admin/providers/test", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken()
            },
            body: JSON.stringify(payload)
        })
            .then(function (response) {
                return response.json().then(function (body) {
                    return { ok: response.ok, body: body };
                });
            })
            .then(function (result) {
                if (!result.ok) {
                    render((result.body.error || {}).message || "请求失败", true);
                    return;
                }
                var data = result.body.data || {};
                render(data.message + "（端点 " + data.endpoint + "）", !data.ok);
            })
            .catch(function (error) {
                render("测试请求异常：" + error.message, true);
            });
    }

    /** 列表页：每行一个按钮，测该档案当前启用的模型，结论汇总到页面底部状态行。 */
    function bindListingTests() {
        var status = document.getElementById("provider-test-status");
        var buttons = document.querySelectorAll(".provider-test");
        if (!status || buttons.length === 0) {
            return;
        }
        Array.prototype.forEach.call(buttons, function (button) {
            button.addEventListener("click", function () {
                var name = button.dataset.providerName;
                button.disabled = true;
                status.textContent = "正在测试「" + name + "」……";
                status.classList.remove("status-error");
                probe({ name: name }, function (text, failed) {
                    status.textContent = "「" + name + "」" + text;
                    status.classList.toggle("status-error", failed);
                    button.disabled = false;
                });
            });
        });
    }

    /**
     * 编辑页：按行测试，并支持动态增行。
     *
     * 增行后重排全部单选按钮的值，让序号与实际行顺序保持一致——服务端按序号从提交上来的
     * 同名字段列表里取启用模型，序号一旦有缺口就会取错行。
     */
    function bindEditorRows() {
        var form = document.querySelector(".provider-form[data-provider-name]");
        var list = form && form.querySelector(".provider-model-list");
        if (!list) {
            return;
        }
        var providerName = form.dataset.providerName;
        var addButton = form.querySelector(".provider-model-add");

        function renumber() {
            var radios = list.querySelectorAll('input[name="active_model_index"]');
            Array.prototype.forEach.call(radios, function (radio, index) {
                radio.value = String(index);
            });
        }

        function runRowTest(button) {
            var row = button.closest(".provider-model-row");
            var input = row.querySelector(".provider-model-input");
            var status = row.querySelector(".provider-row-status");
            var modelName = (input.value || "").trim();
            var baseUrl = (form.querySelector('input[name="base_url"]').value || "").trim();
            status.classList.remove("status-error", "is-success");
            if (!modelName) {
                status.textContent = "请先填写模型名";
                status.classList.add("status-error");
                return;
            }
            button.disabled = true;
            status.textContent = "测试中……";
            probe(
                { name: providerName, base_url: baseUrl, model_name: modelName },
                function (text, failed) {
                    status.textContent = text;
                    status.classList.toggle("status-error", failed);
                    status.classList.toggle("is-success", !failed);
                    button.disabled = false;
                }
            );
        }

        // 事件委托挂在列表上：动态加进来的行不需要再单独绑定。
        list.addEventListener("click", function (event) {
            var button = event.target.closest(".provider-row-test");
            if (button && list.contains(button)) {
                runRowTest(button);
            }
        });

        if (!addButton) {
            return;
        }
        addButton.hidden = false;
        addButton.addEventListener("click", function () {
            var blank = list.querySelector(".provider-model-row-blank");
            var row = blank.cloneNode(true);
            var input = row.querySelector(".provider-model-input");
            var radio = row.querySelector('input[name="active_model_index"]');
            var status = row.querySelector(".provider-row-status");
            input.value = "";
            radio.checked = false;
            status.textContent = "";
            status.classList.remove("status-error", "is-success");
            list.appendChild(row);
            renumber();
            input.focus();
        });
    }

    bindListingTests();
    bindEditorRows();
})();
