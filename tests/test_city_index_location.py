"""城市索引的定位顺序与缺失降级回归测试。

这个文件原来只放在 `data/` 下，而容器会把宿主目录挂到 `/app/data`，bind mount
遮蔽掉镜像里的同名目录，运行时读不到；更糟的是当时缺失会抛 `SystemExit`，一张
带 GPS 的照片就能让工作进程退出。这里同时锁住两件事：定位顺序，以及缺失时降级。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from src.analysis import analyze_photos_docker as legacy

ROOT_DIR = Path(__file__).resolve().parent.parent


class CityIndexLocationTestCase(unittest.TestCase):
    """验证 resolve_city_index_path 的候选顺序与去重。"""

    def test_bundled_copy_ships_with_the_code(self) -> None:
        """随代码分发的那份必须真实存在，且不在 data 目录下。

        它是只读静态资源，放在 resources/ 才不会被 data 挂载遮蔽。
        """
        bundled = ROOT_DIR / "resources" / "world_cities_zh.csv"
        self.assertTrue(bundled.is_file(), "resources 下必须有随代码分发的城市索引")
        self.assertFalse(
            (ROOT_DIR / "data" / "world_cities_zh.csv").exists(),
            "data 下不应再保留主副本，否则又回到被挂载遮蔽的老路",
        )

    def test_falls_back_to_bundled_when_data_copy_absent(self) -> None:
        """未配置且 data 下没有时，回退到随代码分发的那份。"""
        resolved = legacy.resolve_city_index_path(None)

        self.assertIsNotNone(resolved)
        self.assertEqual(ROOT_DIR / "resources" / "world_cities_zh.csv", resolved)

    def test_data_copy_takes_precedence(self) -> None:
        """data 下存在时优先用它：既有部署无需迁移，也便于换成自己的城市表。"""
        with patch.object(legacy, "ROOT_DIR", ROOT_DIR):
            data_copy = ROOT_DIR / "data" / "world_cities_zh.csv"
            self.addCleanup(lambda: data_copy.unlink(missing_ok=True))
            data_copy.parent.mkdir(parents=True, exist_ok=True)
            data_copy.write_text("lat,lon,name_zh,name_en\n", encoding="utf-8")

            resolved = legacy.resolve_city_index_path(None)

        self.assertEqual(data_copy.resolve(), resolved)

    def test_explicit_configuration_wins(self) -> None:
        """显式配置排在最前。"""
        with patch.object(legacy, "ROOT_DIR", ROOT_DIR):
            custom = ROOT_DIR / "resources" / "custom-cities-for-test.csv"
            self.addCleanup(lambda: custom.unlink(missing_ok=True))
            custom.write_text("lat,lon,name_zh,name_en\n", encoding="utf-8")

            resolved = legacy.resolve_city_index_path(str(custom))

        self.assertEqual(custom.resolve(), resolved)

    def test_stale_configuration_still_falls_back(self) -> None:
        """配置指向一个已不存在的路径时仍要回退，而不是直接放弃。

        既有部署的 .env 里普遍写着 ./data/world_cities_zh.csv；文件移走后如果
        不回退，这些部署会突然失去城市反查能力。
        """
        resolved = legacy.resolve_city_index_path("./data/world_cities_zh.csv")

        self.assertEqual(ROOT_DIR / "resources" / "world_cities_zh.csv", resolved)

    def test_returns_none_when_nothing_exists(self) -> None:
        """所有候选都不存在时返回 None，交给调用方降级。"""
        with patch.object(legacy, "ROOT_DIR", ROOT_DIR / "no-such-root"):
            self.assertIsNone(legacy.resolve_city_index_path(None))

    def test_ignore_rules_exclude_data_but_keep_resources(self) -> None:
        """镜像忽略规则必须排除整个 data/、同时放 resources/ 进镜像。

        资源移出 data/ 的意义全在这里：`data/` 整体排除后，镜像里不再有任何会被
        挂载遮蔽的内容；而 resources/ 一旦被误排除，城市索引就会在容器里彻底消失，
        比原来「被遮蔽」更难查。两个忽略文件要一致，Docker 与 Podman 各读一个。
        """
        for name in (".dockerignore", ".containerignore"):
            with self.subTest(name=name):
                rules = [
                    line.strip()
                    for line in (ROOT_DIR / name).read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
                self.assertIn("data/", rules, "整个 data/ 都不应进镜像")
                for rule in rules:
                    self.assertFalse(
                        rule.startswith("resources"),
                        f"{name} 不能排除 resources/：城市索引随代码分发靠它",
                    )


class CityIndexMissingDegradesTestCase(unittest.TestCase):
    """验证索引缺失不再终止进程。"""

    def test_missing_file_returns_empty_index(self) -> None:
        """缺失时返回空索引并告警，而不是抛 SystemExit。"""
        cities, grid = legacy.load_world_cities(Path("/definitely/not/here.csv"))

        self.assertEqual([], cities)
        self.assertEqual({}, grid)

    def test_none_path_returns_empty_index(self) -> None:
        """一个候选都没定位到时同样降级。"""
        cities, grid = legacy.load_world_cities(None)

        self.assertEqual([], cities)
        self.assertEqual({}, grid)

    def test_missing_index_does_not_raise_base_exception(self) -> None:
        """显式钉住不再抛 SystemExit。

        `SystemExit` 继承自 `BaseException`，调用链上的 `except Exception` 抓不住，
        所以它不是「一次分析失败」而是「整个进程退出」。这条断言防止有人图省事
        把硬失败改回来。
        """
        try:
            legacy.load_world_cities(Path("/definitely/not/here.csv"))
        except BaseException as error:  # noqa: BLE001 - 正是要确认什么都没抛
            self.fail(f"城市索引缺失不应抛异常，实际抛出 {type(error).__name__}")

    def test_resolver_works_without_index(self) -> None:
        """没有索引时城市反查返回空字符串，照片照常分析。"""
        with patch.object(legacy, "WORLD_CITIES_CSV", None), patch.object(
            legacy, "_CITY_CACHE_CITIES", None
        ), patch.object(legacy, "_CITY_CACHE_GRID", None):
            resolve = legacy.get_city_resolver()

            self.assertEqual("", resolve(22.54, 114.05))
            self.assertEqual("", resolve(None, None))


if __name__ == "__main__":
    unittest.main()
