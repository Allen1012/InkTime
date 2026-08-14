"""拍摄时间兜底口径与存量修正的回归测试。

背景：兜底链的文件名一级原本只看磁盘路径，而上传照片的磁盘名是随机十六进制串，
日期线索只存在于 `original_filename` 里，于是全部掉到 mtime 兜底——上传落盘会把
mtime 刷成上传时刻，结果雪景照的拍摄时间显示成八月。

本用例固定两件事：文件名一级必须看原始名；mtime 不再充当拍摄时间。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

from src.analysis.analyze_photos_docker import resolve_datetime
from tests.support import TEST_TIMESTAMP, TemporaryDatabaseTestCase

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ResolveDatetimeTestCase(TemporaryDatabaseTestCase):
    """校验三级兜底的取值顺序与 mtime 的退场。"""

    def _touch(self, name: str) -> Path:
        """在临时图片目录里造一个占位文件。"""
        target = self.image_directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not-a-real-jpeg")
        return target

    def test_exif_wins_over_filename(self) -> None:
        """EXIF 存在时不看文件名，避免文件名里的日期覆盖准确值。"""
        path = self._touch("MVIMG_20260620_193658.jpg")

        resolved, source = resolve_datetime(path, "2019:12:24 00:56:20")

        self.assertEqual("2019:12:24 00:56:20", resolved)
        self.assertEqual("exif", source)

    def test_original_filename_used_when_disk_name_is_random(self) -> None:
        """上传照片的磁盘名是随机串，日期必须从原始名解析。"""
        path = self._touch(f"{uuid.uuid4().hex}.jpg")

        resolved, source = resolve_datetime(
            path, None, original_filename="MVIMG_20260620_193658.jpg"
        )

        self.assertEqual("2026:06:20 19:36:58", resolved)
        self.assertEqual("filename", source)

    def test_disk_name_used_when_no_original_filename(self) -> None:
        """扫描入库的照片没有原始名，磁盘名就是用户起的名字。"""
        path = self._touch("IMG_20221023_141428.jpg")

        resolved, source = resolve_datetime(path, None)

        self.assertEqual("2022:10:23 14:14:28", resolved)
        self.assertEqual("filename", source)

    def test_original_filename_takes_priority_over_disk_name(self) -> None:
        """两个名字都有日期时以原始名为准，它更接近拍摄现场。"""
        path = self._touch("IMG_20221023_141428.jpg")

        resolved, _source = resolve_datetime(
            path, None, original_filename="MVIMG_20260620_193658.jpg"
        )

        self.assertEqual("2026:06:20 19:36:58", resolved)

    def test_mtime_is_no_longer_a_date_source(self) -> None:
        """没有任何线索时留空，不能再拿文件修改时间冒充拍摄时间。"""
        path = self._touch("_DSC4058.jpg")

        resolved, source = resolve_datetime(path, None, original_filename="_DSC4058.jpg")

        self.assertIsNone(resolved)
        self.assertEqual("none", source)

    def test_blank_exif_falls_through_to_filename(self) -> None:
        """EXIF 为空字符串时继续兜底，不能当成有效值。"""
        path = self._touch(f"{uuid.uuid4().hex}.jpg")

        resolved, source = resolve_datetime(
            path, "   ", original_filename="IMG_20260510_133132.jpg"
        )

        self.assertEqual("2026:05:10 13:31:32", resolved)
        self.assertEqual("filename", source)


class FixMtimeDatesScriptTestCase(TemporaryDatabaseTestCase):
    """校验存量修正脚本的预览、写入与回收站覆盖。"""

    SCRIPT = PROJECT_ROOT / "scripts" / "fix_mtime_dates.py"

    def _insert_mtime_photo(
        self,
        *,
        original_filename: str | None,
        disk_name: str,
        current: str,
        is_deleted: int = 0,
        original_path: str | None = None,
    ) -> int:
        """写入一条 date_source='mtime' 的照片。"""
        path = self.image_directory / disk_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-a-real-jpeg")
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO photo_scores (path,original_filename,original_path,"
                "exif_datetime,date_source,exif_json,analysis_status,is_deleted,"
                "created_at,updated_at,version) "
                "VALUES (?,?,?,?,'mtime','{}','succeeded',?,?,?,1)",
                (
                    str(path),
                    original_filename,
                    original_path,
                    current,
                    is_deleted,
                    TEST_TIMESTAMP,
                    TEST_TIMESTAMP,
                ),
            )
            return int(cursor.lastrowid)

    def _run(self, *extra: str) -> str:
        """执行脚本并返回标准输出。"""
        result = subprocess.run(
            [sys.executable, str(self.SCRIPT), "--database", str(self.database_path), *extra],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def _read(self, photo_id: int) -> sqlite3.Row:
        """读取指定照片的日期字段。"""
        with self.database() as connection:
            return connection.execute(
                "SELECT exif_datetime,date_source FROM photo_scores WHERE id=?",
                (photo_id,),
            ).fetchone()

    def test_preview_does_not_touch_database(self) -> None:
        """默认只预览，数据库保持原样。"""
        photo_id = self._insert_mtime_photo(
            original_filename="MVIMG_20260620_193658.jpg",
            disk_name=f"{uuid.uuid4().hex}.jpg",
            current="2026:08:12 17:05:33",
        )

        output = self._run()

        self.assertIn("仅为预览", output)
        row = self._read(photo_id)
        self.assertEqual("2026:08:12 17:05:33", row["exif_datetime"])
        self.assertEqual("mtime", row["date_source"])

    def test_apply_fixes_date_from_original_filename(self) -> None:
        """能解析出日期的照片写入真实拍摄时间并记为 filename。"""
        photo_id = self._insert_mtime_photo(
            original_filename="MVIMG_20260620_193658.jpg",
            disk_name=f"{uuid.uuid4().hex}.jpg",
            current="2026:08:12 17:05:33",
        )

        self._run("--apply")

        row = self._read(photo_id)
        self.assertEqual("2026:06:20 19:36:58", row["exif_datetime"])
        self.assertEqual("filename", row["date_source"])

    def test_apply_clears_date_without_clue(self) -> None:
        """无线索的照片清空拍摄时间，不再以上传时间示人。"""
        photo_id = self._insert_mtime_photo(
            original_filename="_DSC4058.jpg",
            disk_name=f"{uuid.uuid4().hex}.jpg",
            current="2026:08:12 16:27:11",
        )

        self._run("--apply")

        row = self._read(photo_id)
        self.assertIsNone(row["exif_datetime"])
        self.assertEqual("none", row["date_source"])

    def test_apply_syncs_exif_json(self) -> None:
        """展示与渲染从 exif_json 取日期，只改列会造成两处不一致。"""
        photo_id = self._insert_mtime_photo(
            original_filename="IMG_20260510_133132.jpg",
            disk_name=f"{uuid.uuid4().hex}.jpg",
            current="2026:08:12 17:08:32",
        )

        self._run("--apply")

        with self.database() as connection:
            row = connection.execute(
                "SELECT json_extract(exif_json,'$.datetime') d,"
                "json_extract(exif_json,'$.date_source') s "
                "FROM photo_scores WHERE id=?",
                (photo_id,),
            ).fetchone()
        self.assertEqual("2026:05:10 13:31:32", row["d"])
        self.assertEqual("filename", row["s"])

    def test_apply_covers_trashed_photos(self) -> None:
        """回收站照片一并修正，否则恢复后错误日期跟着回来。"""
        original_path = str(self.image_directory / "MVIMG_20260607_124917.jpg")
        photo_id = self._insert_mtime_photo(
            original_filename=None,
            disk_name=".trash/abc123.jpg",
            current="2026:08:12 17:08:31",
            is_deleted=1,
            original_path=original_path,
        )

        self._run("--apply")

        row = self._read(photo_id)
        self.assertEqual("2026:06:07 12:49:17", row["exif_datetime"])
        self.assertEqual("filename", row["date_source"])

    def test_apply_leaves_other_sources_untouched(self) -> None:
        """只动 mtime 记录，EXIF 与手工填写的值不能被覆盖。"""
        with self.database() as connection:
            cursor = connection.execute(
                "INSERT INTO photo_scores (path,exif_datetime,date_source,"
                "analysis_status,is_deleted,created_at,updated_at,version) "
                "VALUES (?,'2019:12:24 00:56:20','manual','succeeded',0,?,?,1)",
                (
                    str(self.image_directory / "IMG_20260510_133132.jpg"),
                    TEST_TIMESTAMP,
                    TEST_TIMESTAMP,
                ),
            )
            manual_id = int(cursor.lastrowid)
        self._insert_mtime_photo(
            original_filename="MVIMG_20260620_193658.jpg",
            disk_name=f"{uuid.uuid4().hex}.jpg",
            current="2026:08:12 17:05:33",
        )

        self._run("--apply")

        row = self._read(manual_id)
        self.assertEqual("2019:12:24 00:56:20", row["exif_datetime"])
        self.assertEqual("manual", row["date_source"])
