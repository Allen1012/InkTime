"use strict";

/**
 * 后台侧边菜单折叠控制。
 *
 * 后台是多页应用，每次跳转都会重建 DOM，因此折叠状态写入 localStorage，
 * 由 base.html 头部的内联脚本在首屏渲染前恢复，避免展开态一闪而过。
 */
(function () {
    var STORAGE_KEY = "inktime-admin-sidebar";
    var COLLAPSED_CLASS = "sidebar-collapsed";

    var root = document.documentElement;
    var toggle = document.getElementById("sidebar-toggle");
    if (!toggle) {
        return;
    }
    var navItems = document.querySelectorAll("#admin-sidebar .nav-item");

    // 按钮在模板中默认隐藏，只有脚本可用时才呈现，避免无 JS 环境出现失效控件。
    toggle.hidden = false;

    function isCollapsed() {
        return root.classList.contains(COLLAPSED_CLASS);
    }

    function render() {
        var collapsed = isCollapsed();
        var description = collapsed ? "展开侧边菜单" : "收起侧边菜单";
        toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
        toggle.setAttribute("title", description);
        // 按钮文字与菜单项同款展示，收起后由 CSS 视觉隐藏但保留可访问名称。
        var buttonLabel = toggle.querySelector(".nav-label");
        if (buttonLabel) {
            buttonLabel.textContent = collapsed ? "展开" : "收起";
        }
        // 收起后文字不可见，用浏览器原生 tooltip 提示；展开时移除以免与可见文字重复。
        Array.prototype.forEach.call(navItems, function (item) {
            var itemLabel = item.getAttribute("data-nav-label");
            if (!itemLabel) {
                return;
            }
            if (collapsed) {
                item.setAttribute("title", itemLabel);
            } else {
                item.removeAttribute("title");
            }
        });
    }

    function persist(collapsed) {
        try {
            window.localStorage.setItem(STORAGE_KEY, collapsed ? "collapsed" : "expanded");
        } catch (error) {
            // 隐私模式或存储被禁用时忽略，仅当前页面生效。
        }
    }

    toggle.addEventListener("click", function () {
        var collapsed = root.classList.toggle(COLLAPSED_CLASS);
        persist(collapsed);
        render();
    });

    render();
})();

/**
 * 顶部用户菜单，按 W3C ARIA APG 的 disclosure 模式实现：
 * 真实按钮配 aria-expanded 控制一段内容，Escape 关闭并归还焦点，
 * 焦点移出区域或点击外部同样关闭。
 *
 * 菜单在模板中默认可见，仅当脚本可用（<html class="js">）时才由样式收起，
 * 这样禁用脚本的环境依然能够退出登录。
 */
(function () {
    var trigger = document.getElementById("account-menu-button");
    var menu = document.getElementById("account-menu");
    if (!trigger || !menu) {
        return;
    }
    var container = trigger.closest(".admin-account");
    if (!container) {
        return;
    }

    function isOpen() {
        return menu.classList.contains("is-open");
    }

    function setOpen(open) {
        menu.classList.toggle("is-open", open);
        trigger.setAttribute("aria-expanded", open ? "true" : "false");
    }

    trigger.addEventListener("click", function () {
        setOpen(!isOpen());
    });

    container.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && isOpen()) {
            setOpen(false);
            trigger.focus();
        }
    });

    container.addEventListener("focusout", function (event) {
        if (!container.contains(event.relatedTarget)) {
            setOpen(false);
        }
    });

    document.addEventListener("click", function (event) {
        if (!container.contains(event.target)) {
            setOpen(false);
        }
    });

    setOpen(false);
})();
