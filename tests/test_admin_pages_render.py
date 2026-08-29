"""六个后台页面在登录后的真实渲染回归测试。"""

from __future__ import annotations

import re

from src.configuration import ConfigurationActor
from src.server.app import create_app
from tests.support import TemporaryDatabaseTestCase


ADMIN_USERNAME = "regression-admin"
ADMIN_PASSWORD = "inktime-regression-password"


class AdminLoginMixin:
    """为后台页面测试提供一个完成真实表单登录的客户端。"""

    def logged_in_client(self):
        """创建应用、真实管理员账号并完成带跨站请求伪造令牌的表单登录。"""
        app = create_app(self.application_config())
        with app.app_context():
            app.extensions["inktime_services"]["auth"].create_admin(
                ADMIN_USERNAME, ADMIN_PASSWORD
            )
        client = app.test_client()
        form_page = client.get("/admin/login")
        self.assertEqual(200, form_page.status_code)
        token = re.search(
            r'name="csrf_token"[^>]*value="([^"]+)"', form_page.get_data(as_text=True)
        )
        self.assertIsNotNone(token, "登录表单必须包含跨站请求伪造令牌")
        response = client.post(
            "/admin/login",
            data={
                "username": ADMIN_USERNAME,
                "password": ADMIN_PASSWORD,
                "csrf_token": token.group(1),
            },
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (302, 303))
        return app, client


class AdminPagesRenderTestCase(AdminLoginMixin, TemporaryDatabaseTestCase):
    """用真实登录会话确认后台六个页面都能正常渲染。"""


    def test_all_admin_pages_render(self) -> None:
        """验证概览、照片、上传、回收站、任务与配置页均返回 200。"""
        self.create_photo("rendered.jpg")
        _, client = self.logged_in_client()

        for path in (
            "/admin",
            "/admin/photos",
            "/admin/photos/upload",
            "/admin/trash",
            "/admin/jobs",
            "/admin/settings",
        ):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(200, response.status_code)
                self.assertIn("text/html", response.headers["Content-Type"])
                self.assertGreater(len(response.get_data(as_text=True)), 500)

    def test_settings_page_contains_stage_four_sections(self) -> None:
        """验证配置页真实响应中包含目录状态表与分类标签面板。"""
        app, client = self.logged_in_client()

        body = client.get("/admin/settings").get_data(as_text=True)

        self.assertIn("照片目录状态", body)
        self.assertIn('role="tablist"', body)
        self.assertIn('id="settings-panel-model"', body)
        self.assertIn(str(self.image_directory), body)
        self.assertIn("设备下载地址", body)
        self.assertIn(
            f'/static/inktime/{app.config["DOWNLOAD_KEY"]}/latest.bin',
            body,
        )
        self.assertIn('id="device-download-url"', body)
        self.assertIn("readonly", body)
        self.assertIn('id="copy-device-download-url"', body)

    def test_settings_page_shows_display_window_summary_and_presets(self) -> None:
        """验证配置页展示生效时间段摘要、次数估算与常用预设按钮。"""
        app, client = self.logged_in_client()
        with app.app_context():
            configuration = app.extensions["inktime_services"]["configuration"]
            configuration.update_batch(
                {
                    "DISPLAY_ACTIVE_WINDOWS": "Mon-Fri@09:00-22:30",
                    "DISPLAY_ROTATE_MODE": "hourly",
                },
                configuration.list_admin_settings()["version"],
                ConfigurationActor(self.create_admin_user("window-admin"), "window-admin"),
            )

        body = client.get("/admin/settings").get_data(as_text=True)

        self.assertIn("展示生效时间段", body)
        self.assertIn("周一至周五 09:00 到 22:30", body)
        self.assertIn("周六、周日 全天休息", body)
        # 次数估算要能暴露区间右开的影响
        self.assertIn("周一 14 次", body)
        self.assertIn("周六 0 次", body)
        self.assertIn("settings-preset", body)
        self.assertIn('data-value="Mon-Fri@09:00-22:30"', body)
        self.assertIn("js/admin-settings.js", body)
        self.assertIn('id="setting-DISPLAY_ACTIVE_WINDOWS"', body)


    def test_settings_audit_has_its_own_tab(self) -> None:
        """验证配置审计是独立标签，面板里不含配置控件，也不再多包一层信息块。"""
        _, client = self.logged_in_client()

        body = client.get("/admin/settings").get_data(as_text=True)

        self.assertIn('id="settings-tab-audit"', body)
        self.assertIn("配置审计", body)
        panel_at = body.find('id="settings-panel-audit"')
        self.assertNotEqual(-1, panel_at, "审计必须有自己的面板")
        panel = body[panel_at : body.find("</section>", panel_at)]
        self.assertNotIn("settings-field", panel)
        # 面板本身已经是卡片，审计内容不再套 .settings-info-block 第二层框。
        self.assertNotIn("settings-info-block", panel)
        # 纯记录标签不显示项数角标，避免出现「0」。
        audit_tab = body[body.find('id="settings-tab-audit"') :]
        audit_tab = audit_tab[: audit_tab.find("</button>")]
        self.assertNotIn("settings-tab-count", audit_tab)

    def test_settings_field_shows_key_and_source_only_where_useful(self) -> None:
        """验证说明、键名与来源收进悬停层，键名是可点复制的按钮。

        常显时每项都要占三四行，70 项直接把页面刷满。现在标签行只留中文名，
        说明、键名与来源都在 `.settings-field-tip` 里，靠 CSS 的 `:hover` 与
        `:focus-within` 展开，无脚本环境也看得到。键名不能删：只读项要照它去部署
        环境改，校验错误列表与 `/api/admin/settings` 都以键名为标识。
        旧版逐项复述的「来源：environment」没有信息量——Web 进程把 Flask 默认值
        也算作启动值，几乎每项都是 environment，所以来源只在反直觉时才说。
        """
        app, client = self.logged_in_client()
        with app.app_context():
            state = app.extensions["inktime_services"][
                "configuration"
            ].list_admin_settings()
        settings = {item["key"]: item for item in state["settings"]}

        body = client.get("/admin/settings").get_data(as_text=True)

        # 常显的两行（说明、键名与来源）已经整体移入悬停层。
        self.assertNotIn("settings-field-hint", body)
        self.assertNotIn("settings-field-meta", body)
        field = body[body.find('data-setting-key="TIMEOUT"') :]
        field = field[: field.find("</label>")]
        tip_at = field.find('class="settings-field-tip"')
        self.assertNotEqual(-1, tip_at, "每个配置项都要有悬停层")
        before_tip = field[:tip_at]
        self.assertIn("模型请求超时秒数", before_tip)
        self.assertIn('class="settings-field-name"', before_tip)
        # 说明与键名都在悬停层内部，不再常显（说明文本仍留在 data-search-text 里供搜索）。
        self.assertNotIn("settings-field-desc", before_tip)
        self.assertNotIn("settings-field-key", before_tip)
        self.assertIn("模型请求超时时间。", field[tip_at:])
        # 键名是按钮而非纯文本，带复制用的数据属性，且排在说明上方。
        self.assertIn(
            '<button type="button" class="settings-field-key" data-copy-text="TIMEOUT"',
            field[tip_at:],
        )
        self.assertLess(
            field.find("settings-field-key", tip_at),
            field.find("settings-field-desc", tip_at),
        )
        self.assertIn("timeout", field, "键名仍须落在 data-search-text 里")
        # 复制结果播报给读屏软件的活动区域。
        self.assertIn('id="settings-copy-status"', body)
        # 旧的逐项「来源：<英文枚举>」已经不存在。
        for stale in ("来源：database", "来源：default", "来源：environment"):
            self.assertNotIn(stale, body)
        # 只读项逐项说明只能在部署环境改；可编辑项只有真来自部署环境的才带说明。
        readonly = [
            item
            for item in settings.values()
            if not item["editable"] or item["restart_required"]
        ]
        editable_from_env = [
            item
            for item in settings.values()
            if item["editable"]
            and not item["restart_required"]
            and item["from_environment"]
        ]
        self.assertEqual(14, len(readonly))
        self.assertEqual(
            len(readonly),
            body.count('class="settings-field-source">只能在部署环境修改，'),
        )
        self.assertEqual(
            len(editable_from_env),
            body.count("值由部署环境设置，保存后由数据库接管")
            + body.count("已被在线配置覆盖，同名环境变量不再生效"),
        )
        # 既非只读、也没被部署环境设过的项，悬停层里只有说明和键名。
        plain = next(
            item
            for item in settings.values()
            if item["editable"]
            and not item["restart_required"]
            and not item["from_environment"]
        )
        plain_field = body[body.find(f'data-setting-key="{plain["key"]}"') :]
        plain_field = plain_field[: plain_field.find("</label>")]
        self.assertNotIn("settings-field-source", plain_field)

    def test_section_cards_render_next_to_their_own_section(self) -> None:
        """验证只读块紧贴它所服务的那一段，而不是统一堆在面板顶部。

        目录状态表与 `IMAGE_DIR`、时间段解析结果与「生效时间段与休息期」是同一件事的
        两半：先看清现状再改配置。块堆在面板顶部时，两者之间会隔着无关的分段。
        """
        _, client = self.logged_in_client()

        body = client.get("/admin/settings").get_data(as_text=True)

        def sequence(tab_id: str) -> list[str]:
            """按渲染顺序抽出该面板里的只读块标题与分段标题。"""
            start = body.find(f'id="settings-panel-{tab_id}"')
            panel = body[start : body.find("</section>", start)]
            return [
                (match.group(1) or "").strip()
                for match in re.finditer(r"<h3[^>]*>([^<]{1,40})", panel)
            ]

        self.assertEqual(
            [
                "新照片入库与分析闸门",
                "模型接口",
                "照片目录状态",
                "照片目录",
                "地点与城市推断",
            ],
            sequence("model"),
        )
        self.assertEqual(
            [
                "站点与功能开关",
                "展示页与轮播",
                "展示生效时间段（当前解析结果）",
                "生效时间段与休息期",
                "缩略图",
                "天气",
                "历史上的今天",
            ],
            sequence("display"),
        )
        # 设备下载地址与任何一段都不强相关，仍留在面板级。
        self.assertEqual(["设备下载地址", "每日选片", "渲染"], sequence("render"))

    def test_settings_info_blocks_are_flat_inside_panels(self) -> None:
        """验证只读信息块是面板的直接兄弟，一个面板最多一块，不存在嵌套两层框。"""
        _, client = self.logged_in_client()

        body = client.get("/admin/settings").get_data(as_text=True)

        # 仍需标题与说明的三处信息块保留 .settings-info-block，样式已改为纯分隔线。
        self.assertEqual(3, body.count('class="settings-info-block"'))
        for title in ("设备下载地址", "照片目录状态", "展示生效时间段"):
            self.assertIn(title, body)
        panel_starts = [
            match.start()
            for match in re.finditer(r'id="settings-panel-[a-z]+"', body)
        ]
        self.assertEqual(6, len(panel_starts))
        for start in panel_starts:
            panel = body[start : body.find("</section>", start)]
            self.assertLessEqual(
                panel.count('class="settings-info-block"'),
                1,
                "单个面板里最多一块只读信息块，多于一块说明出现了嵌套或重复",
            )

    def test_settings_form_round_trip_saves_across_all_tabs(self) -> None:
        """验证按页面渲染出的表单整体回提，可同时保存分布在不同标签的配置项。

        分类标签只做面板显隐，全部可编辑项始终位于同一个表单内，因此这里直接把渲染
        出来的控件原样回提，确认不会因为某个标签「没被点开」而漏掉字段或触发校验失败。
        """
        app, client = self.logged_in_client()
        configuration = app.extensions["inktime_services"]["configuration"]

        page = client.get("/admin/settings").get_data(as_text=True)
        form = dict(re.findall(r'name="([A-Za-z_]+)"[^>]*value="([^"]*)"', page))
        self.assertIn("expected_version", form)
        self.assertIn("csrf_token", form)
        # select 控件的当前值不在 value 属性里，需要从 selected 选项取。
        for key, options in re.findall(
            r'<select id="setting-([A-Z_]+)" name="[A-Z_]+">(.*?)</select>', page, re.S
        ):
            selected = re.search(r'value="([^"]*)" selected', options)
            self.assertIsNotNone(selected, f"{key} 必须有一个选中项")
            form[key] = selected.group(1)

        # 跨三个不同标签各改一项：模型与分析、展示与天气、上传与任务。
        form.update(
            {"TIMEOUT": "480", "DISPLAY_MIN_SCORE": "55.5", "JOB_MAX_ATTEMPTS": "2"}
        )
        before = configuration.list_admin_settings()
        before_version = before["version"]
        secret_before = next(
            item for item in before["settings"] if item["key"] == "API_KEY"
        )["configured"]

        response = client.post("/admin/settings", data=form, follow_redirects=False)

        self.assertIn(response.status_code, (302, 303))
        state = configuration.list_admin_settings()
        self.assertGreater(state["version"], before_version)
        values = {item["key"]: item.get("value") for item in state["settings"]}
        self.assertEqual(480, values["TIMEOUT"])
        self.assertEqual(55.5, values["DISPLAY_MIN_SCORE"])
        self.assertEqual(2, values["JOB_MAX_ATTEMPTS"])
        # 密钥控件回提的是空串，表示保持原值，不应被清空也不应被写入新值。
        secret_after = next(
            item for item in state["settings"] if item["key"] == "API_KEY"
        )["configured"]
        self.assertEqual(secret_before, secret_after)
