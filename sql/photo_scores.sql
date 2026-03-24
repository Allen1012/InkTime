-- 照片评分表
-- 存储照片的分析结果、评分和元数据
CREATE TABLE photo_scores (
            id                INTEGER PRIMARY KEY AUTOINCREMENT, -- 自增ID，作为主键
            path              TEXT UNIQUE, -- 照片文件路径，唯一约束
            caption           TEXT, -- 照片内容描述
            type              TEXT, -- 照片类型，如人物/风景/美食等
            memory_score      REAL, -- 值得回忆度评分（0-100）
            beauty_score      REAL, -- 美观程度评分（0-100）
            reason            TEXT, -- 评分理由
            width             INTEGER, -- 照片宽度（像素）
            height            INTEGER, -- 照片高度（像素）
            orientation       TEXT, -- 照片方向（landscape/portrait/square）
            used_at           TEXT, -- 上次使用时间
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
            side_caption      TEXT, -- 一句话文案
            exif_city         TEXT -- 拍摄城市
        );
