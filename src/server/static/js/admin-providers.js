"use strict";

/**
 * 模型厂商页的连通性测试。
 *
 * 只做这一件事：点按钮向 /api/admin/providers/test 发一次最小对话请求，把结果写进
 * 状态行。增删改停全部走普通表单提交，因此禁用脚本时页面依然完全可用，只是没有测试按钮。
 *
 * 测试请求只传厂商名，不传密钥：页面上密钥不回显，服务端会取该档案已存的密钥。
 * 这样密钥既不出现在页面里，也不在这条请求里往回走一遍。
 */
(function () {
    var status = document.getElementById("provider-test-status");
    var buttons = document.querySelectorAll(".provider-test");
    if (!status || buttons.length === 0) {
        return;
    }

    /** 从任意表单取一个可用的 CSRF 令牌：接口对写请求强制校验。 */
    function csrfToken() {
        var field = document.querySelector('input[name="csrf_token"]');
        return field ? field.value : "";
    }

    function setStatus(text, isError) {
        status.textContent = text;
        status.classList.toggle("status-error", Boolean(isError));
    }

    Array.prototype.forEach.call(buttons, function (button) {
        button.addEventListener("click", function () {
            var name = button.dataset.providerName;
            button.disabled = true;
            setStatus("正在测试「" + name + "」……", false);
            fetch("/api/admin/providers/test", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": csrfToken()
                },
                body: JSON.stringify({ name: name })
            })
                .then(function (response) {
                    return response.json().then(function (payload) {
                        return { ok: response.ok, payload: payload };
                    });
                })
                .then(function (result) {
                    if (!result.ok) {
                        var reason = (result.payload.error || {}).message || "请求失败";
                        setStatus("「" + name + "」测试失败：" + reason, true);
                        return;
                    }
                    var data = result.payload.data || {};
                    var prefix = "「" + name + "」" + (data.ok ? "连通正常" : "连通失败");
                    setStatus(prefix + "：" + data.message + "（端点 " + data.endpoint + "）", !data.ok);
                })
                .catch(function (error) {
                    setStatus("「" + name + "」测试请求异常：" + error.message, true);
                })
                .then(function () {
                    button.disabled = false;
                });
        });
    });
})();
