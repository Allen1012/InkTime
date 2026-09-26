"""后台照片筛选面板紧凑四列与日期弹层的静态回归测试。"""

from __future__ import annotations

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AdminFilterLayoutTestCase(unittest.TestCase):
    """锁定四等分布局、日期范围弹层和紧凑布尔筛选。"""

    @classmethod
    def setUpClass(cls) -> None:
        """读取筛选模板、后台样式和照片管理脚本。"""
        cls.template = (
            PROJECT_ROOT / "src/server/templates/admin/photos.html"
        ).read_text(encoding="utf-8")
        cls.stylesheet = (
            PROJECT_ROOT / "src/server/static/css/admin.css"
        ).read_text(encoding="utf-8")
        cls.script = (
            PROJECT_ROOT / "src/server/static/js/admin-photos.js"
        ).read_text(encoding="utf-8")

    def test_medium_container_uses_four_equal_columns_without_spans(self) -> None:
        """中等宽度的搜索与日期各占四分之一，不再跨两列。"""
        self.assertIn("@container (max-width: 1260px)", self.stylesheet)
        self.assertIn(
            "grid-template-columns: repeat(4, minmax(0, 1fr))",
            self.stylesheet,
        )
        self.assertNotIn(
            ".filter-search, .filter-date-range, .filter-check { grid-column: span 2; }",
            self.stylesheet,
        )

    def test_date_range_uses_compact_trigger_and_popover_panel(self) -> None:
        """筛选栏只显示日期摘要，起止日期放在可展开面板内。"""
        self.assertIn('class="date-range-popover"', self.template)
        self.assertIn('class="date-range-trigger"', self.template)
        self.assertIn('class="date-range-panel"', self.template)
        self.assertIn('data-date-range-start', self.template)
        self.assertIn('data-date-range-end', self.template)
        self.assertIn('data-date-range-clear', self.template)
        self.assertIn('type="submit">应用</button>', self.template)
        self.assertIn("position: absolute", self.stylesheet)
        self.assertIn("renderDateSummary", self.script)
        self.assertIn('event.key === "Escape"', self.script)

    def test_missing_date_filter_is_compact_and_grouped_with_actions(self) -> None:
        """单一布尔筛选应使用内容宽度胶囊，并与筛选按钮位于同一操作组。"""
        actions_start = self.template.index('<div class="filter-actions">')
        actions_end = self.template.index("</div>", actions_start)
        actions = self.template[actions_start:actions_end]
        self.assertIn('class="filter-check"', actions)
        self.assertIn("缺拍摄时间", actions)
        self.assertIn('type="submit">筛选</button>', actions)
        self.assertIn("width: max-content", self.stylesheet)
        self.assertIn(".filter-check:has(input:checked)", self.stylesheet)

    def test_narrow_containers_degrade_to_two_and_one_columns(self) -> None:
        """较窄内容区必须先降为两列，再在手机宽度降为单列。"""
        self.assertIn("@container (max-width: 900px)", self.stylesheet)
        self.assertIn(
            "grid-template-columns: repeat(2, minmax(0, 1fr))",
            self.stylesheet,
        )
        self.assertIn("@container (max-width: 620px)", self.stylesheet)
        self.assertIn(".filter-panel > * { grid-column: 1 / -1; }", self.stylesheet)
        self.assertIn(".date-range-fields { grid-template-columns: 1fr; }", self.stylesheet)


if __name__ == "__main__":
    unittest.main()
