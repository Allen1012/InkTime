-- 照片评分表
-- 存储照片的分析结果、评分和元数据
--
-- 注意：本文件仅作结构参考，正常流程不需要手动执行。
-- 实际建表由 src/analysis/analyze_photos_docker.py 的 ensure_table() 完成
-- （CREATE TABLE IF NOT EXISTS + 一系列 ALTER TABLE ADD COLUMN 补列），
-- 第一次运行分析会自动建库。手动建库容易与代码不同步，导致运行时
-- 报 no such column。修改结构时请同时改 ensure_table() 与本文件。
--
-- 列顺序与 ensure_table() 保持一致。
CREATE TABLE photo_scores (
            id                INTEGER PRIMARY KEY AUTOINCREMENT, -- 自增ID，WebUI 的 /api/photo/<id> 依赖它
            path              TEXT UNIQUE NOT NULL, -- 照片绝对路径，唯一约束，也是断点续跑的去重依据
            caption           TEXT, -- 照片内容描述
            type              TEXT, -- 照片类型，如人物/风景/美食等
            memory_score      REAL, -- 值得回忆度评分（0-100），选片主依据
            beauty_score      REAL, -- 美观程度评分（0-100）
            reason            TEXT, -- 评分理由
            width             INTEGER, -- 照片宽度（像素）
            height            INTEGER, -- 照片高度（像素）
            orientation       TEXT, -- 照片方向（landscape/portrait/square）
            used_at           TEXT, -- 上次被选中展示的时间（重新分析时不会被覆盖）
            exif_json         TEXT, -- 完整 EXIF JSON，render 与 server 解析拍摄日期/GPS 都依赖它
            raw_json          TEXT, -- VLM 原始返回，排错用
            exif_datetime     TEXT, -- 拍摄时间
            exif_make         TEXT, -- 相机制造商
            exif_model        TEXT, -- 相机型号
            exif_iso          INTEGER, -- ISO 感光度
            exif_exposure_time REAL, -- 曝光时间
            exif_f_number     REAL, -- 光圈值
            exif_focal_length REAL, -- 焦距
            exif_gps_lat      REAL, -- GPS 纬度
            exif_gps_lon      REAL, -- GPS 经度
            exif_gps_alt      REAL, -- GPS 海拔
            side_caption      TEXT, -- 一句话文案，渲染到墨水屏上
            exif_city         TEXT, -- 反查得到的中文城市名（离线，基于 data/world_cities_zh.csv）
            date_source       TEXT  -- exif_datetime 的来源：exif / xmp / filename / mtime / none
        );
