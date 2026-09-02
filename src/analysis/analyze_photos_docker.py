#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
import base64
import hashlib
import json
import sqlite3
import os
import re
import subprocess
import time
from datetime import datetime, timezone
import requests
import io
from PIL import Image, ExifTags, ImageOps
import shutil

from src.configuration import like_prefix, parse_image_dirs
from src.database import connect_database
from src.migrations import migrate_database
from src.provider_fallback import ProviderHTTPError, sanitized_provider_error
from src.provider_options import apply_request_options

# 所有厂商统一走 requests 直接 POST，不再保留 OpenAI 软件开发工具包分支。
# 删掉它的三个理由：那个分支的进入条件是硬编码的 `"dashscope" in API_URL`，与「厂商档案」
# 这套抽象直接冲突——档案存在的意义就是不在代码里写死某一家；它构成一个隐藏的行为开关，
# 同一份代码装了库和没装库跑出两种结果，而这台机器上从未安装过该库，那段代码从未执行；
# 最要紧的是它不合并厂商特有请求参数，一旦有人装上库，`enable_thinking` 与
# `response_format` 的档案配置会静默失效，只表现为多花 token 和格式变化，不报任何错。

# 配置来源：.env 文件 + 环境变量（.env 为唯一配置源）
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


# =======================
# Docker 环境配置
# =======================

# 从环境变量读取配置
ROOT_DIR = Path(__file__).resolve().parent.parent.parent

# 加载 .env（已存在的环境变量优先，便于 BATCH_LIMIT=20 这类临时覆盖）
if load_dotenv:
    _env_file = ROOT_DIR / ".env"
    if _env_file.exists():
        load_dotenv(_env_file, override=False)

# 要扫描的图片目录（默认从环境变量读取，或使用 /photos）
# 支持分号分隔的多个目录；第一个是主目录，与 Web 与工作进程使用同一套解析规则。
try:
    IMAGE_DIRS = parse_image_dirs(
        os.environ.get("IMAGE_DIR", "/photos"), base_dir=ROOT_DIR
    )
except ValueError as _image_dir_error:
    raise SystemExit(f"IMAGE_DIR 配置无效: {_image_dir_error}") from _image_dir_error
IMAGE_DIR = IMAGE_DIRS[0]

# SQLite 数据库路径
DB_PATH = Path(os.environ.get("DB_PATH", "./photos.db")).expanduser()
if not DB_PATH.is_absolute():
    DB_PATH = (ROOT_DIR / DB_PATH).resolve()

# LM Studio/OpenAI 兼容接口
API_URL = os.environ.get("API_URL") or os.environ.get("LMSTUDIO_URL", "http://host.docker.internal:1234/v1/chat/completions")
API_BASE_URL = API_URL[:-len("/chat/completions")] if API_URL.rstrip("/").endswith("/chat/completions") else API_URL

# 模型名称
MODEL_NAME = os.environ.get("MODEL_NAME") or os.environ.get("LMSTUDIO_MODEL", "qwen3-vl-32b-instruct")

# API KEY
API_KEY = os.environ.get("API_KEY") or os.environ.get("LMSTUDIO_API_KEY", "")

# 每次处理多少张；None 为不限制
BATCH_LIMIT = os.environ.get("BATCH_LIMIT")
if BATCH_LIMIT:
    try:
        BATCH_LIMIT = int(BATCH_LIMIT)
    except:
        BATCH_LIMIT = None

# 请求超时时间（秒）
TIMEOUT = float(os.environ.get("TIMEOUT", 600))

# 发送给 VLM 之前，先把图片长边缩放到该值（像素）
VLM_MAX_LONG_EDGE = int(os.environ.get("VLM_MAX_LONG_EDGE", 2560))

# 中文城市索引：只读静态资源，随代码分发
CITY_INDEX_FILENAME = "world_cities_zh.csv"


def resolve_city_index_path(configured: str | Path | None = None) -> Path | None:
    """按显式配置、data 目录、随代码分发的 resources 顺序定位城市索引。

    这个文件曾经只放在 `data/` 下，而容器部署会把宿主目录挂到 `/app/data`，
    bind mount 遮蔽掉镜像里的同名目录，运行时就读不到——文件其实一直在镜像里，
    只是被盖住了。只读资源属于代码，因此主位置改到项目内 `resources/`，那里不受
    任何挂载影响。

    仍然优先看 `data/`，有两个实际理由：既有部署的文件已经在宿主 data 里，无需
    任何迁移动作；想换成自己的城市表时丢进 data 即可覆盖，不必改配置。

    Args:
        configured: 显式配置的路径，空值表示按默认顺序自动查找。

    Returns:
        第一个真实存在的候选路径；全部缺失时返回 None，由调用方降级处理。
    """
    candidates: list[Path] = []

    def append(path: Path) -> None:
        """按序登记候选并去重，避免显式配置与默认位置重复检查。"""
        resolved = path if path.is_absolute() else (ROOT_DIR / path).resolve()
        if resolved not in candidates:
            candidates.append(resolved)

    text = str(configured or "").strip()
    if text:
        append(Path(text).expanduser())
    append(Path("data") / CITY_INDEX_FILENAME)
    append(Path("resources") / CITY_INDEX_FILENAME)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


WORLD_CITIES_CSV = resolve_city_index_path(os.environ.get("WORLD_CITIES_CSV"))

# 城市搜索参数
CITY_GRID_DEG = float(os.environ.get("CITY_GRID_DEG", 1.0))
CITY_MAX_DISTANCE_KM = float(os.environ.get("CITY_MAX_DISTANCE_KM", 80.0))
HOME_LAT = float(os.environ.get("HOME_LAT", 22.543096))
HOME_LON = float(os.environ.get("HOME_LON", 114.057865))
HOME_RADIUS_KM = float(os.environ.get("HOME_RADIUS_KM", 60.0))


# =======================
# 工具函数
# =======================

# exiftool 是否可用：缺失时只降级 GPS/部分 EXIF，不中断流程
# 模块加载时即检测，避免依赖 require_exiftool() 的调用顺序
# （否则单独 import 本模块调用 read_exiftool_tags 会静默拿不到数据）
EXIFTOOL_AVAILABLE = shutil.which("exiftool") is not None

def require_exiftool() -> None:
    """检查 exiftool 是否可用
    
    该函数会检查系统中是否安装了 exiftool 工具，用于后续的 GPS/EXIF 读取。
    如果未找到 exiftool，会打印警告信息，但不会中断主流程。
    """
    global EXIFTOOL_AVAILABLE
    EXIFTOOL_AVAILABLE = shutil.which("exiftool") is not None
    if not EXIFTOOL_AVAILABLE:
        print(
            "[WARN] 未找到 exiftool，将跳过 exiftool 辅助的 GPS/EXIF 读取（不影响主流程）。\n"
        )

def encode_image_to_b64(path: Path) -> str:
    """读取图片并（可选）缩放长边后，重新编码为 JPEG，再转 base64。"""
    try:
        data = path.read_bytes()
    except Exception as e:
        raise RuntimeError(f"读取文件失败：{e}")

    # 尽量用 PIL 容错打开，然后统一转成干净的 JPEG bytes
    try:
        img = Image.open(io.BytesIO(data))
        # 处理 EXIF 旋转
        try:
            img = ImageOps.exif_transpose(img)  # type: ignore
        except Exception:
            pass

        # 统一色彩模式：JPEG 需要 RGB
        if img.mode in ("RGBA", "LA"):
            # 有透明通道时，用白底合成
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 可选缩放
        try:
            w, h = img.size
            long_edge = max(w, h)
            if VLM_MAX_LONG_EDGE and long_edge > VLM_MAX_LONG_EDGE:
                scale = float(VLM_MAX_LONG_EDGE) / float(long_edge)
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                img = img.resize((new_w, new_h), resample=Image.LANCZOS)
        except Exception:
            pass

        out = io.BytesIO()
        # quality 92 在观感和体积之间比较平衡；optimize 可能更慢但通常能降体积
        img.save(out, format="JPEG", quality=92, optimize=True)
        clean_bytes = out.getvalue()
        return base64.b64encode(clean_bytes).decode("utf-8")

    except Exception:
        # 兜底：如果 PIL 也打不开，就退回原始 bytes（让上游报错更直观）
        return base64.b64encode(data).decode("utf-8")


def ensure_table(conn: sqlite3.Connection) -> None:
    """确认照片表已由版本化迁移更新到当前最低结构。

    数据结构变更只能由 ``src.migrations`` 执行。本函数只做断言，避免分析脚本或
    元数据预览在没有迁移台账的情况下静默修改数据库。

    Args:
        conn: 已打开的 SQLite 数据库连接。

    Raises:
        RuntimeError: ``photo_scores`` 缺少当前代码必需的字段。
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(photo_scores)")}
    required_columns = {
        "id",
        "path",
        "date_source",
        "original_filename",
        "content_sha256",
        "analysis_status",
        "analysis_error",
        "is_deleted",
        "created_at",
        "updated_at",
        "version",
    }
    missing = required_columns - columns
    if missing:
        raise RuntimeError(f"photo_scores 缺少必要字段，请先执行数据库迁移: {sorted(missing)}")

# 旁白本身只要 8 到 24 个汉字，但输出上限**不能**按这个长度设：mimo-v2.5 这类模型会先
# 产出思考内容再给正文，配额不够就会在思考阶段耗尽，正文返回空或残缺。
# 这个值踩过两次坑：64 时正文直接为空；512 时带图片的请求思考更长，正文被截断成 '{'
# 这种残缺 JSON。2048 与评分调用取齐，正文很短，上限提高不会让它变长，只是给思考留够。
SIDE_CAPTION_MAX_TOKENS = 2048

# 模型偶尔把「没有内容」表达成这些字面量而不是空串。它们不是文案，必须挡掉，
# 否则数据库里会出现一条看起来合法的旁白。
# 厂商特有的额外请求体参数，由 photo_analyzer 按当前候选临时覆盖；直接运行本脚本时为空。
# 值为 None 表示删掉该参数——有的参数只能靠不发送来关闭，例如千问关掉思考后正文本身
# 就是干净一句话，`response_format` 反而让它输出 [{"caption": ...}]，而 OpenAI 兼容层
# 没有「结构化输出=关」这种取值。
PROVIDER_REQUEST_OPTIONS: dict[str, object] = {}

_CODE_FENCE_PATTERN = re.compile(
    r"^```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```$", re.DOTALL
)

_INVALID_CAPTION_TEXTS = frozenset(
    {"none", "null", "nil", "n/a", "na", "undefined", "无", "暂无", "无内容"}
)

# 用结构化输出约束旁白：只声明一个字段，模型就没有地方铺开思考过程。
# 实测（xiaomi/mimo-v2.5）默认调用消耗 292 token、其中 277 是思考；加上这个 schema
# 之后降到 105 token、思考 87，正文还从散文变成可直接解析的 JSON。
# 网关支持 response_format（见接口文档），但 reasoning_effort 标注为暂不支持，
# 也没有任何 enable_thinking 之类的开关，所以思考只能靠 schema 压缩、不能关闭。
SIDE_CAPTION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "SideCaption",
        "schema": {
            "type": "object",
            "title": "SideCaption",
            "properties": {
                "caption": {
                    "type": "string",
                    "title": "Caption",
                    "description": "一句 8 到 24 个汉字的中文旁白，不带引号",
                }
            },
            "required": ["caption"],
        },
    },
}


# 评分同样用结构化输出。它本来就在提示词里要求「严格只输出 JSON」，靠提示词约束的
# 代价是模型会先写一段思考再吐 JSON，解析全靠 json.loads 撞运气；换成 schema 之后
# 字段与类型由网关保证，思考 token 也被压下来。
# max_tokens 必须显式给：评分正文比旁白长（含 80~200 字画面描述），而思考型模型会先
# 花掉几百个 token 思考，不留预算就会把正文挤掉——旁白当年返回空正文就是这么来的。
ANALYSIS_MAX_TOKENS = 2048
ANALYSIS_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "PhotoAnalysis",
        "schema": {
            "type": "object",
            "title": "PhotoAnalysis",
            "properties": {
                "caption": {"type": "string", "title": "Caption", "description": "80 到 200 字的中文画面描述"},
                "type": {"type": "string", "title": "Type", "description": "照片类型，多个用 / 分隔"},
                "memory_score": {"type": "number", "title": "MemoryScore", "description": "值得回忆度 0 到 100，一位小数"},
                "beauty_score": {"type": "number", "title": "BeautyScore", "description": "美观程度 0 到 100，一位小数"},
                "reason": {"type": "string", "title": "Reason", "description": "不超过 60 字的中文评分理由"},
            },
            "required": ["caption", "type", "memory_score", "beauty_score", "reason"],
        },
    },
}

def _message_field(message, field: str):
    """从聊天补全的 message 取字段，同时兼容 SDK 对象与原始字典两种形态。"""
    if isinstance(message, dict):
        return message.get(field)
    return getattr(message, field, None)


def _clean_caption_text(value) -> str | None:
    """把模型返回的文本规范成旁白，判定不出有效内容时返回空值。

    刻意不做 `str(value)` 兜底。那会把 None 转成字符串 "None"——一个四字符的
    「合法」文案，能通过上层 `generate_narration()` 的空值校验，于是「模型没产出
    旁白」这个失败被伪装成正常结果写进数据库。调用方只有拿到 None 才能把本次
    生成判定为失败并触发重试。
    """
    if not isinstance(value, str):
        return None
    # 反引号一并剥掉：模型偶尔只吐出一个残缺的代码围栏（正文就是 ```），
    # 它不以 { 或 [ 开头，会一路走到这里被当成三字符的「合法」旁白写进库。
    # 正常旁白不会以反引号开头或结尾，因此这层剥离不会误伤真实文案。
    text = value.strip().strip("`").strip().strip(""""'""").strip()
    if not text or text.lower() in _INVALID_CAPTION_TEXTS:
        return None
    return text


def _strip_code_fence(text: str) -> str:
    """剥掉整段包裹内容的 Markdown 代码围栏，未包裹时原样返回。

    带 `response_format` 时部分模型仍会把 JSON 放进 ```json 围栏里。不剥的话
    整段围栏文本不以 { 开头，会被当成纯文本旁白原样入库。
    """
    match = _CODE_FENCE_PATTERN.match(text)
    return match.group(1).strip() if match else text


def _caption_from_payload(payload) -> str | None:
    """从结构化正文里取旁白，兼容对象与单元素数组两种形态。

    实测 `qwen3.8-max` 在 `json_schema` 下会把 schema 要求的对象再包一层数组
    （`[{"caption": "..."}]`），因此必须容忍这一层。元素多于一个时判失败而不是
    取第一个：那说明模型没有按「只输出一句」执行，结果不可信。
    """
    if isinstance(payload, list):
        if len(payload) != 1:
            return None
        payload = payload[0]
    if not isinstance(payload, dict):
        return None
    return _clean_caption_text(payload.get("caption"))


def _extract_caption(message) -> str | None:
    """从正文里取旁白，优先按结构化输出解析，取不到就判失败。

    **刻意不读 `reasoning_content`。** 早先这里有一段「正文为空就回退到思考字段末行」
    的逻辑，建立在「可用文本藏在思考里」这个错误假设上。实测该模型的 `content` 一直
    是干净的一句旁白，思考单独走 `reasoning_content`；当年正文为空的真实原因是
    `max_tokens=64` 在思考阶段就被耗尽。那段回退没有救回任何数据，反而把思考文本的
    尾巴当成旁白写进了数据库（例如「最终我觉得"…"这个不错。它描述了」）。

    结论是：正文取不到就应该判失败并触发重试，不能去思考文本里捞。兜底伪装成功
    比直接失败更难排查。
    """
    raw = _message_field(message, "content")
    if not isinstance(raw, str) or not raw.strip():
        return None
    # 先剥 Markdown 代码围栏，再判断是不是结构化正文。顺序不能颠倒：带围栏的 JSON
    # 不以 { 开头，先判断就会漏进纯文本分支。
    text = _strip_code_fence(raw.strip())
    if not text:
        return None
    # 带 response_format 时正文是 {"caption": "..."}，也可能被再包一层数组。
    # 看起来像 JSON 就必须按 JSON 解析成功且取到 caption 才算有效，否则一律判失败——
    # 被 max_tokens 截断的响应长这样：'{' 或 '{"caption":"半句'，退化成纯文本会把
    # 这些残片当旁白写进库。`[` 同样要认：漏掉它会让整段 JSON 数组文本原样入库。
    # 两者都不是才按纯文本处理，保留未启用结构化输出的模型与历史行为。
    if text.startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return None
        return _caption_from_payload(payload)
    return _clean_caption_text(text)


# 生成一句话文案
def generate_side_caption(
    image_path: Path, image_b64: str | None = None
) -> str | None:
    """为照片生成一句旁白文案。

    Args:
        image_path: 图片文件路径。
        image_b64: 可选的已编码图片。调用方已经为同一张照片编码过时传入，
            可跳过重复的解码、缩放与重编码；省略时按原行为自行编码。

    Returns:
        去除引号与首尾空白的一句文案；读取失败或模型无有效输出时返回空值。
    """
    system_prompt = (
        "你是一位为「电子相框」撰写旁白短句的中文文案助手。\n"
        "你的目标不是描述画面，而是为画面补上一点'画外之意'。\n\n"
        "创作原则：\n"
        "1. 避免使用以下词语：世界、梦、时光、岁月、温柔、治愈、刚刚好、悄悄、慢慢 等（但不是绝对禁止）。\n"
        "2. 严禁使用如下句式：……里……着整个世界；……里……着整个夏天；……得像……（简单的比喻）; ……比……还……； ……得比……更……。\n"
        "3. 只基于图片中能确定的信息进行联想，不要虚构时间、人物关系、事件背景。\n"
        "4. 文案应自然、有趣，带一点幽默或者诗意，但请避免煽情、鸡汤。\n"
        "5. 不要复述画面内容本身，而是写'看完画面后，心里多出来的一句话'。\n"
        "6. 可以偏向以下风格之一：\n"
        "   - 日常中的微妙情绪\n"
        "   - 轻微自嘲或冷幽默\n"
        "   - 对时间、记忆、瞬间的含蓄感受\n"
        "   - 看似平淡但有余味的一句判断\n"
        "7. 避免小学生作文式的、套路式的模板化表达\n\n"
        "格式要求：\n"
        "1. 只输出一句中文短句，不要换行，不要引号，不要任何解释。\n"
        "2. 建议长度 8～24 个汉字，最多不超过 30 个汉字。\n"
        "3. 不要出现'这张照片''这一刻''那天'等指代照片本身的词。\n"
    )
    user_prompt = "请基于这张照片，生成一句符合规则的中文文案。"
    if image_b64 is None:
        try:
            img_b64 = encode_image_to_b64(image_path)
        except Exception:
            return None
    else:
        # 复用调用方已编码的结果：同一张照片在评分与文案两次调用间只解码、
        # 缩放、重编码一次。省下的是整轮 PIL 处理，高像素原图上这一步不便宜。
        img_b64 = image_b64

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            },
        ],
        "temperature": 0.7,
        "max_tokens": SIDE_CAPTION_MAX_TOKENS,
        "top_p": 0.9,
        "stream": False,
        "response_format": SIDE_CAPTION_RESPONSE_FORMAT,
    }
    apply_request_options(payload, PROVIDER_REQUEST_OPTIONS)

    try:
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=min(120, TIMEOUT))
    except Exception as error:
        sanitized = sanitized_provider_error(error)
        if sanitized is error:
            raise
        raise sanitized from error

    if not resp.ok:
        raise ProviderHTTPError(resp.status_code)

    try:
        data = resp.json()
        message = data["choices"][0]["message"]
    except Exception:
        print("[WARN] 旁白响应结构异常，无法读取 choices[0].message")
        return None

    caption = _extract_caption(message)
    if caption is None:
        # 正文与思考字段都取不到可用文本时留下线索：上层只会看到
        # narration_generation_failed，没有这行日志就无法判断是被截断还是没返回。
        finish_reason = None
        try:
            finish_reason = data["choices"][0].get("finish_reason")
        except Exception:
            pass
        print(
            "[WARN] 旁白为空，finish_reason="
            f"{finish_reason!r}，message 字段={sorted(message) if isinstance(message, dict) else type(message).__name__}"
        )
    return caption


def list_images(limit: int | None = None) -> list[Path]:
    """递归扫描图片目录，返回符合条件的图片文件列表
    
    Args:
        limit: 可选，限制返回的图片数量
        
    Returns:
        符合条件的图片文件路径列表
    """
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    files = []
    print("[INFO] 正在递归扫描图片目录，请稍候……")
    scanned = 0
    for image_dir in IMAGE_DIRS:
        if not image_dir.is_dir():
            print(f"[WARN] 照片目录不可用，已跳过：{image_dir}")
            continue
        print(f"[SCAN] 扫描目录：{image_dir}")
        for p in image_dir.rglob("*"):
            scanned += 1
            if scanned % 500 == 0:
                print(f"[SCAN] 已扫描文件数：{scanned} …")
            if p.is_file() and p.suffix.lower() in exts:
                if is_screenshot(p):
                    continue
                files.append(p)
    print(f"[INFO] 扫描完成，共发现 {len(files)} 张图片（文件总数 {scanned}）。")
    if limit is not None:
        files = files[:limit]
    return files


class _SkipMissingSync(Exception):
    """内部信号：本次不执行文件缺失同步。"""


def unavailable_image_dirs() -> list[Path]:
    """返回当前不可用的照片目录。

    多目录下只要有一个根不可用（例如网络存储未挂载），就不能执行文件缺失同步：
    该根下的照片不会出现在扫描清单里，会被整批误标为 `source_file_missing`，
    从此不再参与选片。
    """
    return [item for item in IMAGE_DIRS if not item.is_dir()]


def _image_dir_prefix_clause(alias: str = "path") -> tuple[str, list[str]]:
    """构造覆盖全部照片目录的 LIKE 前缀条件与参数。

    Args:
        alias: 参与匹配的列名。

    Returns:
        括号包裹的 SQL 条件与对应参数列表。
    """
    clause = " OR ".join(f"{alias} LIKE ? ESCAPE '\\'" for _ in IMAGE_DIRS)
    return f"({clause})", [like_prefix(item) for item in IMAGE_DIRS]

# 排除 Screenshot 图片
def is_screenshot(path: Path) -> bool:
    """判断是否为截图图片
    
    Args:
        path: 图片文件路径
        
    Returns:
        如果文件名包含 "screenshot"（不区分大小写），返回 True，否则返回 False
    """
    s = str(path)
    return "screenshot" in s.lower()


def filter_unscored(conn: sqlite3.Connection, paths: list[Path]) -> list[Path]:
    """过滤出尚未分析的图片路径
    
    Args:
        conn: SQLite 数据库连接
        paths: 待检查的图片文件路径列表
        
    Returns:
        尚未在数据库中分析过的图片路径列表
    """
    if not paths:
        return []

    cur = conn.cursor()
    placeholders = ",".join("?" for _ in paths)
    rows = cur.execute(
        f"SELECT path FROM photo_scores WHERE path IN ({placeholders}) "
        "AND is_deleted = 0 AND analysis_status IN ('legacy', 'succeeded')",
        [str(p) for p in paths],
    ).fetchall()
    already = {row[0] for row in rows}
    return [p for p in paths if str(p) not in already]


def _file_sha256(path: Path) -> str:
    """流式计算照片内容摘要，避免一次把大文件读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mark_analysis_running(
    conn: sqlite3.Connection, path: Path, content_sha256: str
) -> None:
    """在模型调用前持久化运行状态，使中断和重试可观察。"""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO photo_scores
            (path, original_filename, content_sha256, analysis_status,
             analysis_error, created_at, updated_at)
        VALUES (?, ?, ?, 'running', NULL, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
            original_filename = COALESCE(photo_scores.original_filename, excluded.original_filename),
            content_sha256 = excluded.content_sha256,
            analysis_status = 'running',
            analysis_error = NULL,
            updated_at = excluded.updated_at,
            version = photo_scores.version + 1
        """,
        (str(path), path.name, content_sha256, timestamp, timestamp),
    )
    conn.commit()


def _mark_analysis_failed(conn: sqlite3.Connection, path: Path, error: Exception) -> None:
    """记录可重试的分析失败类型，不把异常正文或潜在敏感响应写入数据库。"""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE photo_scores SET analysis_status='failed', analysis_error=?, "
        "updated_at=?, version=version+1 WHERE path=? AND is_deleted=0",
        (type(error).__name__[:100], timestamp, str(path)),
    )
    conn.commit()


def _convert_gps_to_deg(value):
    """将 GPS 坐标从度分秒格式转换为十进制度数格式
    
    Args:
        value: GPS 坐标的度分秒格式值
        
    Returns:
        转换后的十进制度数，如果转换失败返回 None
    """
    try:
        d, m, s = value
        return float(d[0]) / float(d[1]) + float(m[0]) / float(m[1]) / 60.0 + float(s[0]) / float(s[1]) / 3600.0
    except Exception:
        return None


def read_gps_with_exiftool(path: Path):
    """使用 exiftool 读取图片的 GPS 信息
    
    Args:
        path: 图片文件路径
        
    Returns:
        包含 GPS 信息的字典，格式为 {"lat": 纬度, "lon": 经度, "alt": 海拔}，
        如果无法读取或 exiftool 不可用，返回 None
    """
    if not EXIFTOOL_AVAILABLE:
        return None
    try:
        result = subprocess.run(
            ["exiftool", "-n", "-json", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        # 没装 exiftool，则直接跳过
        return None
    except subprocess.CalledProcessError:
        return None

    try:
        data = json.loads(result.stdout)[0]
    except Exception:
        return None

    lat = data.get("GPSLatitude")
    lon = data.get("GPSLongitude")
    alt = data.get("GPSAltitude")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat),
        "lon": float(lon),
        "alt": float(alt) if alt is not None else None,
    }


def read_exif(path: Path) -> dict:
    """读取图片的 EXIF 信息
    
    Args:
        path: 图片文件路径
        
    Returns:
        包含 EXIF 信息的字典，包括：
        - width: 图片宽度
        - height: 图片高度
        - orientation: 图片方向（landscape/portrait/square）
        - datetime: 拍摄时间
        - make: 相机制造商
        - model: 相机型号
        - iso: ISO 感光度
        - exposure_time: 曝光时间
        - f_number: 光圈值
        - focal_length: 焦距
        - gps_lat: GPS 纬度
        - gps_lon: GPS 经度
        - gps_alt: GPS 海拔
    """
    info: dict = {}
    try:
        img = Image.open(path)
        try:
            width, height = img.size
            info["width"] = int(width)
            info["height"] = int(height)
            if width > height:
                info["orientation"] = "landscape"
            elif height > width:
                info["orientation"] = "portrait"
            else:
                info["orientation"] = "square"
        except Exception:
            pass
        exif_raw = img._getexif() or {}
    except Exception:
        return info

    exif = {}
    for tag_id, value in exif_raw.items():
        tag = ExifTags.TAGS.get(tag_id, tag_id)
        exif[tag] = value

    # 基本字段
    info["datetime"] = exif.get("DateTimeOriginal") or exif.get("DateTime")
    info["make"] = exif.get("Make")
    info["model"] = exif.get("Model")
    info["iso"] = exif.get("ISOSpeedRatings") or exif.get("PhotographicSensitivity")
    info["exposure_time"] = exif.get("ExposureTime")
    info["f_number"] = exif.get("FNumber")
    info["focal_length"] = exif.get("FocalLength")

    gps_info = exif.get("GPSInfo")
    lat = lon = None
    if isinstance(gps_info, dict):
        # GPSInfo 的 key 可能是数字，需要映射
        gps_tags = {}
        for k, v in gps_info.items():
            name = ExifTags.GPSTAGS.get(k, k)
            gps_tags[name] = v

        lat_ref = gps_tags.get("GPSLatitudeRef")
        lat_raw = gps_tags.get("GPSLatitude")
        lon_ref = gps_tags.get("GPSLongitudeRef")
        lon_raw = gps_tags.get("GPSLongitude")

        if lat_raw and lat_ref:
            lat = _convert_gps_to_deg(lat_raw)
            if lat is not None and lat_ref in ["S", "s"]:
                lat = -lat
        if lon_raw and lon_ref:
            lon = _convert_gps_to_deg(lon_raw)
            if lon is not None and lon_ref in ["W", "w"]:
                lon = -lon

    info["gps_lat"] = lat
    info["gps_lon"] = lon

    if info.get("gps_lat") is None or info.get("gps_lon") is None:
        gps = read_gps_with_exiftool(path)
        if gps is not None:
            info["gps_lat"] = gps["lat"]
            info["gps_lon"] = gps["lon"]
            if gps.get("alt") is not None:
                info["gps_alt"] = gps["alt"]

    return info


def read_exiftool_tags(path: Path, tags: list[str]) -> dict:
    """用 exiftool 读取指定标签，返回 {标签名: 值}。

    exiftool 能读到 PIL 读不到的 XMP / IPTC 段，PS 处理过的图常只剩这些。
    exiftool 不可用或出错时返回空 dict，不中断流程。
    """
    if not EXIFTOOL_AVAILABLE:
        return {}
    try:
        args = ["exiftool", "-n", "-json"]
        args += [f"-{t}" for t in tags]
        args.append(str(path))
        out = subprocess.run(args, capture_output=True, text=True, timeout=20)
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        data = json.loads(out.stdout)
        if isinstance(data, list) and data:
            return {k: v for k, v in data[0].items() if k != "SourceFile"}
    except Exception:
        pass
    return {}


def _normalize_datetime_str(value) -> str | None:
    """把各种日期写法统一成 EXIF 风格 'YYYY:MM:DD HH:MM:SS'。

    下游 extract_date_from_exif() 按这个格式解析，必须保持一致。
    """
    if value is None:
        return None
    # exiftool 对 HistoryWhen 等可重复标签返回数组，取第一个有效值
    if isinstance(value, (list, tuple)):
        for item in value:
            got = _normalize_datetime_str(item)
            if got:
                return got
        return None
    s = str(value).strip()
    if not s:
        return None
    # 去掉时区后缀，如 2019:12:24 00:56:04+08:00
    s = s.split("+")[0].split("Z")[0].strip()
    # 只取第一个日期时间（HistoryWhen 可能是逗号分隔的多个值）
    s = s.split(",")[0].strip()
    # 统一分隔符：2019-12-24 00:56:04 / 2019:12:24T00:56:04 → 2019:12:24 00:56:04
    s = s.replace("T", " ")
    parts = s.split()
    date_part = parts[0].replace("-", ":").replace("/", ":")
    time_part = parts[1] if len(parts) > 1 else "00:00:00"
    ymd = date_part.split(":")
    if len(ymd) < 3:
        return None
    try:
        y, m, d = int(ymd[0]), int(ymd[1]), int(ymd[2])
    except ValueError:
        return None
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}:{m:02d}:{d:02d} {time_part}"


def datetime_from_filename(path: Path) -> str | None:
    """从文件名猜拍摄时间。

    支持两类常见命名：
    - 含 8 位日期：IMG_20221023_141428.jpg、20221023-xxx.jpg
    - 含 10/13 位 Unix 时间戳：FIMO_1647600645102.jpg（部分相机 App 用毫秒时间戳）
    """
    name = path.stem

    # 10/13 位时间戳（先试，避免被 8 位日期规则误截）
    for m in re.finditer(r"(?<!\d)(\d{13}|\d{10})(?!\d)", name):
        raw = m.group(1)
        ts = int(raw) / 1000.0 if len(raw) == 13 else float(raw)
        if 946684800 <= ts <= 4102444800:  # 2000-01-01 ~ 2100-01-01
            return time.strftime("%Y:%m:%d %H:%M:%S", time.localtime(ts))

    # 8 位日期 YYYYMMDD，可选紧随 6 位时间
    for m in re.finditer(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?:[_\-]?(\d{2})(\d{2})(\d{2}))?(?!\d)", name):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            continue
        hh = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        if hh > 23 or mi > 59 or ss > 59:
            hh = mi = ss = 0
        return f"{y:04d}:{mo:02d}:{d:02d} {hh:02d}:{mi:02d}:{ss:02d}"

    return None


# XMP / IPTC 中可能残留的时间标签，按可信度排序
_FALLBACK_DATE_TAGS = [
    "DateTimeOriginal",   # 极少数情况 PIL 读不到但 exiftool 能读到
    "CreateDate",
    "DateCreated",
    "SubSecCreateDate",
    "MetadataDate",       # PS 编辑时间
    "HistoryWhen",        # PS 编辑历史
    "ModifyDate",
]


def resolve_datetime(
    path: Path, exif_datetime, *, original_filename: str | None = None
) -> tuple[str | None, str]:
    """确定照片的展示用日期，返回 (日期字符串, 来源标记)。

    三级兜底，全部失败时返回空日期。**不使用文件修改时间**：上传落盘会把 mtime
    刷成上传时刻，拷贝移动也会改写它，把它当拍摄时间会让雪景照显示成八月。宁可
    留空——留空的照片不进选片候选池，不会以错误日期出现在「历史上的今天」。

    文件名一级会同时看原始上传名与磁盘名。上传照片的磁盘名是随机十六进制串
    （防重名与路径穿越），日期线索只存在于原始名里，只看磁盘名等于白找。

    Args:
        path: 照片在磁盘上的路径。
        exif_datetime: 已从 EXIF 读到的拍摄时间，可为空。
        original_filename: 上传时的原始文件名；扫描入库的照片没有这一项。

    来源标记含义：
        exif     — EXIF 拍摄时间，语义准确
        xmp      — XMP/IPTC 残留时间，PS 处理过的图多为「编辑时间」而非拍摄时间
        filename — 从原始名或磁盘名解析
        none     — 全部失败，日期留空
    """
    # 1) EXIF 拍摄时间
    norm = _normalize_datetime_str(exif_datetime)
    if norm:
        return norm, "exif"

    # 2) XMP / IPTC 残留时间（含 PS 编辑时间）
    tags = read_exiftool_tags(path, _FALLBACK_DATE_TAGS)
    for t in _FALLBACK_DATE_TAGS:
        norm = _normalize_datetime_str(tags.get(t))
        if norm:
            return norm, "xmp"

    # 3) 文件名：原始上传名优先，其次磁盘名
    for candidate in (original_filename, path.name):
        if not candidate:
            continue
        norm = datetime_from_filename(Path(candidate))
        if norm:
            return norm, "filename"

    return None, "none"


def normalize_type(raw) -> str:
    """把 VLM 返回的照片类型规范成 '/' 分隔。

    模型输出不稳定，见过 '风景/旅行'、'孩子, 旅行, 风景, 日常' 两种写法。
    server.py 按 '/' 拆标签，逗号写法会让整串变成一个畸形分类。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    # 统一各种分隔符为 /
    s = re.sub(r"[，,、|\\；;]+", "/", s)
    parts = [p.strip() for p in s.split("/")]
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "/".join(out)


def in_home(lat: float | None, lon: float | None) -> bool:
    """判断是否在“本地/常驻地”范围内。"""
    if lat is None or lon is None:
        return False
    try:
        d = haversine_km(float(lat), float(lon), float(HOME_LAT), float(HOME_LON))
        return d <= float(HOME_RADIUS_KM)
    except Exception:
        return False


def format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "00:00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


import csv
import math
from typing import Dict, List, Tuple, Optional

CityRecord = Tuple[float, float, str, str]  # (lat, lon, name_zh, name_en)

_CITY_CACHE_CITIES: List[CityRecord] | None = None
_CITY_CACHE_GRID: Dict[Tuple[int, int], List[int]] | None = None

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """使用 Haversine 公式计算两个经纬度点之间的距离
    
    Args:
        lat1: 第一个点的纬度
        lon1: 第一个点的经度
        lat2: 第二个点的纬度
        lon2: 第二个点的经度
        
    Returns:
        两点之间的距离，单位为公里
    """
    r = 6371.0  # 地球半径（公里）
    phi1 = math.radians(lat1)  # 转换为弧度
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)  # 纬度差
    dlambda = math.radians(lon2 - lon1)  # 经度差
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c

def grid_key(lat: float, lon: float) -> Tuple[int, int]:
    """根据经纬度计算网格键
    
    用于城市搜索时的网格索引，将地球表面按指定度数划分网格。
    
    Args:
        lat: 纬度
        lon: 经度
        
    Returns:
        网格坐标 (gx, gy)
    """
    gx = int(math.floor(lat / CITY_GRID_DEG))
    gy = int(math.floor(lon / CITY_GRID_DEG))
    return gx, gy

def load_world_cities(csv_path: Path | None) -> Tuple[List[CityRecord], Dict[Tuple[int, int], List[int]]]:
    """加载世界城市数据并构建网格索引

    缺失时返回空索引而不是终止进程。城市名只是锦上添花的元数据，不该有能力
    拖死整个分析流程——这里原来抛的是 `SystemExit`，它继承自 `BaseException`，
    普通的 `except Exception` 抓不住，于是一张带 GPS 的照片就能让工作进程退出。

    Args:
        csv_path: 城市数据 CSV 文件路径，None 表示所有候选位置都不存在

    Returns:
        包含两个元素的元组：
        1. 城市记录列表，每个记录格式为 (lat, lon, name_zh, name_en)
        2. 网格索引字典，键为网格坐标 (gx, gy)，值为城市在列表中的索引
    """
    if csv_path is None or not csv_path.exists():
        print(
            f"[WARN] 找不到城市索引文件（{csv_path or '未定位到任何候选路径'}），"
            f"本次不做城市反查；照片照常分析，城市字段留空"
        )
        return [], {}

    cities: List[CityRecord] = []
    grid_index: Dict[Tuple[int, int], List[int]] = {}

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                lat = float((row.get("lat") or "").strip())
                lon = float((row.get("lon") or "").strip())
            except Exception:
                continue
            name_en = (row.get("name_en") or "").strip()
            name_zh = (row.get("name_zh") or "").strip()
            cities.append((lat, lon, name_zh, name_en))

    for idx, (lat, lon, name_zh, name_en) in enumerate(cities):
        key = grid_key(lat, lon)
        grid_index.setdefault(key, []).append(idx)

    print(f"[INFO] 已加载中文城市库: {csv_path}")
    return cities, grid_index

def find_nearest_city(
    lat: float,
    lon: float,
    cities: List[CityRecord],
    grid_index: Dict[Tuple[int, int], List[int]],
    max_km: float = 80.0,
) -> str:
    """根据经纬度查找最近的城市
    
    Args:
        lat: 纬度
        lon: 经度
        cities: 城市记录列表
        grid_index: 网格索引字典
        max_km: 最大搜索距离（公里）
        
    Returns:
        最近城市的名称（中文优先，英文次之），如果没有找到符合条件的城市返回空字符串
    """
    if not cities:
        return ""

    gx, gy = grid_key(lat, lon)

    def collect_candidates(radius: int) -> List[int]:
        """收集指定半径内的城市候选
        
        Args:
            radius: 网格搜索半径
            
        Returns:
            城市索引列表
        """
        cand: List[int] = []
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                bucket = grid_index.get((gx + dx, gy + dy))
                if bucket:
                    cand.extend(bucket)
        return cand

    # 先在半径为1的网格内搜索
    candidates = collect_candidates(radius=1)
    if not candidates:
        # 如果没找到，扩大搜索半径到2
        candidates = collect_candidates(radius=2)
    if not candidates:
        return ""

    best_idx: Optional[int] = None
    best_dist = float("inf")

    # 计算每个候选城市与目标点的距离，找到最近的
    for idx in candidates:
        city_lat, city_lon, name_zh, name_en = cities[idx]
        d = haversine_km(lat, lon, city_lat, city_lon)
        if d < best_dist:
            best_dist = d
            best_idx = idx

    # 检查最近的城市是否在最大距离范围内
    if best_idx is None or best_dist > max_km:
        return ""

    _, _, name_zh, name_en = cities[best_idx]
    return name_zh or name_en or ""

def get_city_resolver():
    """获取城市解析器函数
    
    该函数会缓存城市数据，返回一个可以根据经纬度解析城市名称的函数。
    
    Returns:
        一个函数，接受纬度和经度作为参数，返回对应的城市名称
    """
    global _CITY_CACHE_CITIES, _CITY_CACHE_GRID
    if _CITY_CACHE_CITIES is None or _CITY_CACHE_GRID is None:
        _CITY_CACHE_CITIES, _CITY_CACHE_GRID = load_world_cities(WORLD_CITIES_CSV)

    def resolve(lat: float | None, lon: float | None) -> str:
        """根据经纬度解析城市名称
        
        Args:
            lat: 纬度
            lon: 经度
            
        Returns:
            城市名称，如果无法解析返回空字符串
        """
        if lat is None or lon is None:
            return ""
        return find_nearest_city(lat, lon, _CITY_CACHE_CITIES, _CITY_CACHE_GRID, max_km=CITY_MAX_DISTANCE_KM)

    return resolve


def call_vlm(image_path: Path, image_b64: str | None = None) -> dict:
    """调用视觉语言模型（VLM）分析图片
    
    Args:
        image_path: 图片文件路径
        image_b64: 可选的已编码图片。调用方已经为同一张照片编码过时传入，
            可跳过重复的解码、缩放与重编码；省略时按原行为自行编码。
        
    Returns:
        包含两个元素的元组：
        1. 模型分析结果字典，包含 caption、type、memory_score、beauty_score、reason 等字段
        2. 图片的 EXIF 信息字典
        
    Raises:
        RuntimeError: 当读取图片失败或模型请求失败时
    """
    if image_b64 is None:
        try:
            # 将图片编码为 base64 格式
            img_b64 = encode_image_to_b64(image_path)
        except Exception as e:
            raise RuntimeError(f"读取图片失败：{e}")
    else:
        img_b64 = image_b64

    # 读取图片的 EXIF 信息
    exif_info = read_exif(image_path)
    exif_json = json.dumps(exif_info, ensure_ascii=False, default=str)

    # 构建系统提示，指导模型如何分析图片
    system_prompt = (
        "你是一个\"个人相册照片评估助手\"，擅长理解真实照片的内容，并从回忆价值和美观角度打分。\n"
        "你会收到一张照片（以 base64 形式提供），你的任务是：\n"
        "1）用中文详细描述照片内容（80~200 字），\n"
        "2）判断照片的大致类型：人物/孩子/猫咪/家庭/旅行/风景/美食/宠物/日常/文档/杂物/其他，一张照片可以有不止一个类型。\n"
        "3）给出 0~100 的\"值得回忆度\" memory_score（精确到一位小数），\n"
        "4）给出 0~100 的\"美观程度\" beauty_score（精确到一位小数），\n"
        "5）用简短中文 reason 解释原因（不超过 40 字）。\n\n"

        "【值得回忆度（memory_score）评分方法】\n"
        "请先按照值得回忆的程度，先确定照片的'得分区间'，再进行精调：\n"
        "如何判定值得回忆度（memory_score）的得分区间：\n"
        "- 垃圾/随手拍/无意义记录：40.0 分以下（常见为 0~25；若还能勉强辨认但无故事，也不要超过 39.9）。\n"
        "- 稍微有点可回忆价值：以 65.0 分为中心（大多落在 58.1~70.3）。\n"
        "- 不错的回忆价值：以 75 分为中心（大多落在 68.7~82.4）。\n"
        "- 特别精彩、强烈值得珍藏：以 85 分为中心（大多落在 79.1~95.9；\n"
        "如何继续精调memory_score得分（若同时符合几条加分项，加分可叠加）：\n"
        "- 人物与关系：画面中含有面积较大的人脸，有人物互动，或属于合影 → 大幅提高评分；\n"
        "- 事件性：生日/聚会/仪式/舞台/明显事件 → 少许提高评分；\n"
        "- 稀缺性与不可复现：明显\"这一刻很难再来一次\" → 大幅提高评分；\n"
        "- 情绪强度：笑、哭、惊喜、拥抱、互动、氛围强 → 少许提高评分；\n"
        "- 信息密度：画面能讲清楚发生了什么 → 微微提高评分；\n"
        "- 优美风景：画面中含有壮丽的自然风光，或精美、有秩序感的构图 → 少许提高评分；\n"
        "- 旅行意义：异地、地标、旅途情景 → 少许提高评分。\n\n"

        "- 画质：画面不清晰、模糊、有残影、虚焦 → 微微降低评分。\n\n"

        "【重点照片的处理】\n"
        "如果画面中含有：孩子/猫咪/宠物题材，这些主题更容易产生高回忆价值，请直接以75分为中心，并大幅提高评分\"。\n"

        "【明显低价值图片的处理】\n"
        "对以下低价值图片，必须将 memory_score 压低到 0~25（最多不超过 39）。\n"
        "- 裸露、低俗、色情或违反公序良俗的图片。\n\n"

        "- 账单、收据、广告、随手拍的杂物、测试图片、屏幕截图等。\n\n"
        
        "【美观分（beauty_score）评分方法】\n"
        "美观分只评价视觉：构图、光线、清晰度、色彩、主体突出。\n"
        "不要被\"孩子/猫/旅行\"主题绑架美观分：主题不等于好看。\n"

        "请严格只输出 JSON，格式如下：\n"
        "{\n"
        "  \"caption\": \"……\",\n"
        "  \"type\": \"人物/家庭/旅行/…… 可以带多个type\",\n"
        "  \"memory_score\": 0.0-100.0 的数字, 精确到 1 位小数\n"
        "  \"beauty_score\": 0.0-100.0 的数字, 精确到 1 位小数\n"
        "  \"reason\": \"不超过 60 字的中文理由\"\n"
        "}\n"
        "不要输出任何多余文字，不要加注释。"
    )
       

    user_text = (
        "下面是照片的内容，请结合图像本身完成上述任务。\n"
    )

    # 构建请求头和请求体
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_b64}"
                        },
                    },
                ],
            },
        ],
        "temperature": 0.2,  # 降低随机性，使输出更稳定
        "stream": False,  # 非流式输出
        "max_tokens": ANALYSIS_MAX_TOKENS,
        "response_format": ANALYSIS_RESPONSE_FORMAT,
    }
    apply_request_options(payload, PROVIDER_REQUEST_OPTIONS)

    # 发送请求到 VLM API
    try:
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=TIMEOUT)
        if not resp.ok:
            raise ProviderHTTPError(resp.status_code)

        # 解析响应
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"].strip()
        except Exception:
            print("[DEBUG] 返回内容：", data)
            raise RuntimeError("解析失败：无法从 choices[0].message.content 读取内容")

        # 解析模型返回的 JSON 内容
        try:
            obj = json.loads(content)
        except Exception:
            print("[DEBUG] 非 JSON 输出：", content)
            raise RuntimeError("解析失败：模型未按 JSON 输出")

        return obj, exif_info
    except Exception as error:
        sanitized = sanitized_provider_error(error)
        if sanitized is error:
            raise
        raise sanitized from error


def main():
    """主函数，执行照片分析的完整流程
    
    流程包括：
    1. 扫描图片目录，获取图片文件列表
    2. 过滤掉截图图片
    3. 连接数据库，确保表结构存在
    4. 同步删除数据库中不存在于磁盘的文件记录
    5. 过滤出尚未分析的图片
    6. 调用 VLM 分析每张图片
    7. 生成一句话文案
    8. 提取 EXIF 信息和 GPS 数据
    9. 解析城市信息
    10. 更新回忆分（基于 GPS 信息）
    11. 将分析结果存入数据库
    12. 显示处理进度和统计信息
    """
    filelist_path = ROOT_DIR / "filelist.txt"

    # 扫描图片目录，获取图片文件列表
    print("[INFO] 正在扫描图片目录……")
    imgs = list_images()
    filelist_path.write_text("\n".join(str(p) for p in imgs), encoding="utf-8")
    print(f"[INFO] 已更新文件列表 filelist.txt，共 {len(imgs)} 个文件。")
    if not imgs:
        raise SystemExit(f"目录下没有图片文件: {IMAGE_DIR}")

    # 过滤掉截图图片
    imgs = [p for p in imgs if not is_screenshot(p)]
    if not imgs:
        raise SystemExit("[INFO] 所有图片都被 Screenshot 过滤规则排除了，没有可处理的图片。")

    # 数据结构变更必须先进入迁移台账，再打开分析连接。
    migrate_database(DB_PATH)
    conn = connect_database(DB_PATH)
    ensure_table(conn)
    city_resolver = get_city_resolver()

    # =======================
    # 同步删除：NAS/磁盘上已不存在的文件，也从数据库里删除
    # 只处理当前 IMAGE_DIR 前缀下的记录，避免误删其它历史路径。
    # 多目录下必须先扫描完全部目录再比对；任一目录不可用时整体跳过，否则该目录
    # 下的照片会被误标为文件缺失。
    # =======================
    prefix_clause, prefix_params = _image_dir_prefix_clause()
    missing_dirs = unavailable_image_dirs()
    if missing_dirs:
        print(
            "[WARN] 以下照片目录当前不可用，已跳过文件缺失同步，避免误标记："
            + "、".join(str(item) for item in missing_dirs)
        )

    try:
        if missing_dirs:
            raise _SkipMissingSync()
        # 用临时表避免 IN (...) 过长导致的 SQLite 参数上限问题
        conn.execute("DROP TABLE IF EXISTS _temp_existing_paths")
        conn.execute("CREATE TEMP TABLE _temp_existing_paths (path TEXT PRIMARY KEY)")

        # 批量插入当前扫描到的文件列表
        CHUNK = 2000
        total_files = len(imgs)
        inserted = 0
        for i in range(0, total_files, CHUNK):
            chunk = imgs[i : i + CHUNK]
            conn.executemany(
                "INSERT OR IGNORE INTO _temp_existing_paths(path) VALUES (?)",
                [(str(p),) for p in chunk],
            )
            inserted += len(chunk)
            if inserted % 10000 == 0:
                print(f"[CLEAN] 已写入存在文件清单：{inserted}/{total_files} …")

        # 标记：数据库里有记录，但磁盘上已不存在的文件。文件缺失不等同管理员软删除。
        cur_clean = conn.cursor()
        before_cnt = cur_clean.execute(
            f"SELECT COUNT(*) FROM photo_scores WHERE {prefix_clause} AND is_deleted=0",
            prefix_params,
        ).fetchone()[0]

        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur_clean.execute(
            f"""
            UPDATE photo_scores
            SET analysis_status = 'failed',
                analysis_error = 'source_file_missing',
                updated_at = ?,
                version = version + 1
            WHERE {prefix_clause}
              AND is_deleted = 0
              AND COALESCE(analysis_error, '') != 'source_file_missing'
              AND NOT EXISTS (
                    SELECT 1 FROM _temp_existing_paths t
                    WHERE t.path = photo_scores.path
              )
            """,
            [timestamp, *prefix_params],
        )
        marked_missing = cur_clean.rowcount if cur_clean.rowcount is not None else 0
        conn.commit()

        after_cnt = cur_clean.execute(
            f"SELECT COUNT(*) FROM photo_scores WHERE {prefix_clause} AND is_deleted=0",
            prefix_params,
        ).fetchone()[0]

        if marked_missing > 0:
            print(
                f"[CLEAN] 已标记 {marked_missing} 条文件缺失记录，未删除数据库行"
                f"（当前目录活动记录：{before_cnt} → {after_cnt}）。"
            )
        else:
            print("[CLEAN] 没有新增文件缺失记录。")

    except _SkipMissingSync:
        pass
    except Exception as e:
        # 清理失败不应影响主流程，但必须先回滚失败事务再继续读取。
        conn.rollback()
        print(f"[WARN] 同步清理数据库残留记录失败（已忽略，不影响主流程）：{e}")

    # 统计当前目录下已分析的照片数量
    cur_test = conn.cursor()
    counted = cur_test.execute(
        f"SELECT COUNT(*) FROM photo_scores WHERE {prefix_clause}",
        prefix_params,
    ).fetchone()[0]
    print(f"[INFO] 数据库中已有 {counted} 张已分析照片（仅统计当前配置的照片目录）。")

    # 过滤出尚未分析的图片
    target_paths = filter_unscored(conn, imgs)
    if not target_paths:
        print("[INFO] 所有图片都已经在 photo_scores 中有记录。")
        conn.close()
        return

    # 应用批次限制
    if BATCH_LIMIT is not None:
        target_paths = target_paths[:BATCH_LIMIT]

    # 计算进度条相关参数
    already_done = counted
    total = already_done + len(target_paths)
    print(f"[INFO] 本次准备处理 {len(target_paths)} 张图片（快照总数 {total}，已分析 {already_done}）。")

    cur = conn.cursor()
    start_time = time.time()

    # 处理每张图片
    for idx, path in enumerate(target_paths, start=1):
        t_photo_start = time.perf_counter()
        sep = "=" * 60
        print("\n" + sep)
        print(f"[{idx}/{len(target_paths)}] 处理: {path}")
        try:
            content_sha256 = _file_sha256(path)
            _mark_analysis_running(conn, path, content_sha256)
            from src.analysis.photo_analyzer import analyze_single_photo
            _row = conn.execute(
                "SELECT original_filename FROM photo_scores WHERE path=?", (str(path),)
            ).fetchone()
            analysis = analyze_single_photo(
                path,
                city_resolver=city_resolver,
                original_filename=(_row[0] if _row else None) or None,
            )
            result = {
                "caption": analysis["caption"],
                "type": analysis["type"],
                "memory_score": analysis["memory_score"],
                "beauty_score": analysis["beauty_score"],
                "reason": analysis["reason"],
            }
            exif_info = json.loads(analysis["exif_json"])
        except Exception as e:
            _mark_analysis_failed(conn, path, e)
            print(f"[WARN] 调用模型失败: {e}")
            continue
        t_after_vlm = time.perf_counter()
        vlm_cost = t_after_vlm - t_photo_start

        # 提取模型分析结果
        caption = str(result.get("caption", "")).strip()
        # 规范化类型：模型可能输出逗号分隔，统一成 '/'，否则 WebUI 分类页会出现畸形分类
        ptype = normalize_type(result.get("type"))
        try:
            memory_score = float(result.get("memory_score", 0.0))
        except Exception:
            memory_score = 0.0
        try:
            beauty_score = float(result.get("beauty_score", 0.0))
        except Exception:
            beauty_score = 0.0
        reason = str(result.get("reason", "")).strip()

        # 旁白已由共用单张分析服务生成；失败会让整张照片分析失败。
        side_caption = analysis["side_caption"]
        t_after_side = time.perf_counter()
        side_cost = t_after_side - t_after_vlm

        # 提取 EXIF 信息
        width = exif_info.get("width")
        height = exif_info.get("height")
        orientation = exif_info.get("orientation")
        exif_datetime = exif_info.get("datetime")
        exif_make = exif_info.get("make")
        exif_model = exif_info.get("model")

        # 日期兜底：按 EXIF → XMP(含 PS 编辑时间) → 文件名 三级补齐。
        # 不使用文件 mtime——上传落盘与拷贝移动都会改写它，当拍摄时间会得到
        # 完全错误的日期；取不到就留空，留空的照片不进选片候选池。
        # 原始名要一并参与：上传照片的磁盘名是随机串，日期线索只在原始名里。
        original_filename = None
        try:
            row = conn.execute(
                "SELECT original_filename FROM photo_scores WHERE path=?", (str(path),)
            ).fetchone()
            if row:
                original_filename = row[0] or None
        except Exception:
            original_filename = None
        exif_datetime, date_source = resolve_datetime(
            path, exif_datetime, original_filename=original_filename
        )
        # 同步写回 exif_info，render 从 exif_json 的 datetime 字段取日期
        exif_info["datetime"] = exif_datetime
        exif_info["date_source"] = date_source
        if date_source != "exif":
            print(f"  日期兜底：{exif_datetime}（来源 {date_source}）")

        # 辅助函数：转换值为整数
        def _to_int(v):
            try:
                if v is None:
                    return None
                return int(v)
            except Exception:
                return None

        # 辅助函数：转换值为浮点数
        def _to_float(v):
            try:
                if v is None:
                    return None
                return float(v)
            except Exception:
                return None

        # 提取并转换 EXIF 数值
        exif_iso = _to_int(exif_info.get("iso"))
        exif_exposure_time = _to_float(exif_info.get("exposure_time"))
        exif_f_number = _to_float(exif_info.get("f_number"))
        exif_focal_length = _to_float(exif_info.get("focal_length"))
        exif_gps_lat = _to_float(exif_info.get("gps_lat"))
        exif_gps_lon = _to_float(exif_info.get("gps_lon"))
        exif_gps_alt = _to_float(exif_info.get("gps_alt"))

        # 解析城市信息
        if exif_gps_lat is not None and exif_gps_lon is not None:
            exif_city = city_resolver(exif_gps_lat, exif_gps_lon)
        else:
            exif_city = ""

        # 异地加分已由共用单张分析服务统一完成，批量入口不重复计算。

        exif_json = json.dumps(exif_info, ensure_ascii=False, default=str)

        # 打印分析结果
        print(f"  类型    ：{ptype}")
        print(f"  回忆分  ：{memory_score:.1f}")
        print(f"  美观分  ：{beauty_score:.1f}")
        if side_caption:
            print(f"  一句话文案：{side_caption}")
        else:
            print("  一句话文案：(无)")
        print(f"  画面描述：{caption}")
        print(f"  理由    ：{reason}")

        # 将分析结果存入数据库。运行状态已先提交，成功写入会再次递增版本。
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # 用 UPSERT 而非 INSERT OR REPLACE：后者在 path 冲突时是「删旧行 + 插新行」，
        # 会导致 id 变化（WebUI 的 /api/photo/<id> 链接失效）。
        # UPSERT 原地更新，id 保持不变；used_at 不在更新列表里，因此自动保留。
        cur.execute(
            """
            INSERT INTO photo_scores
            (path, caption, type, memory_score, beauty_score, reason,
             width, height, orientation,
             exif_json, raw_json,
             exif_datetime, exif_make, exif_model,
             exif_iso, exif_exposure_time, exif_f_number, exif_focal_length,
             exif_gps_lat, exif_gps_lon, exif_gps_alt, side_caption, exif_city, date_source,
             original_filename, content_sha256, analysis_status, analysis_error,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, 'succeeded', NULL,
                    ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                caption            = excluded.caption,
                type               = excluded.type,
                memory_score       = excluded.memory_score,
                beauty_score       = excluded.beauty_score,
                reason             = excluded.reason,
                width              = excluded.width,
                height             = excluded.height,
                orientation        = excluded.orientation,
                exif_json          = excluded.exif_json,
                raw_json           = excluded.raw_json,
                exif_datetime      = excluded.exif_datetime,
                exif_make          = excluded.exif_make,
                exif_model         = excluded.exif_model,
                exif_iso           = excluded.exif_iso,
                exif_exposure_time = excluded.exif_exposure_time,
                exif_f_number      = excluded.exif_f_number,
                exif_focal_length  = excluded.exif_focal_length,
                exif_gps_lat       = excluded.exif_gps_lat,
                exif_gps_lon       = excluded.exif_gps_lon,
                exif_gps_alt       = excluded.exif_gps_alt,
                side_caption       = excluded.side_caption,
                exif_city          = excluded.exif_city,
                date_source        = excluded.date_source,
                original_filename  = COALESCE(photo_scores.original_filename, excluded.original_filename),
                content_sha256     = excluded.content_sha256,
                analysis_status    = 'succeeded',
                analysis_error     = NULL,
                created_at         = COALESCE(photo_scores.created_at, excluded.created_at),
                updated_at         = excluded.updated_at,
                version            = photo_scores.version + 1
            """,
            (
                str(path),
                caption,
                ptype,
                memory_score,
                beauty_score,
                reason,
                width,
                height,
                orientation,
                exif_json,
                None,
                exif_datetime,
                exif_make,
                exif_model,
                exif_iso,
                exif_exposure_time,
                exif_f_number,
                exif_focal_length,
                exif_gps_lat,
                exif_gps_lon,
                exif_gps_alt,
                side_caption,
                exif_city,
                date_source,
                path.name,
                content_sha256,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
        t_photo_end = time.perf_counter()
        total_cost = t_photo_end - t_photo_start

        # 显示进度条和预估时间
        processed_now = already_done + idx
        denom = total if total > 0 else 1
        progress = processed_now / denom
        # 夹紧，确保不会超过 100%
        if progress < 0:
            progress = 0.0
        if progress > 1:
            progress = 1.0

        bar_width = 30
        filled = int(bar_width * progress)
        bar = "█" * filled + "░" * (bar_width - filled)

        elapsed = time.time() - start_time
        avg_per = elapsed / idx if idx > 0 else 0
        remaining = max(total - processed_now, 0)
        eta = format_eta(remaining * avg_per) if avg_per > 0 else "00:00:00"

        print(f"[进度] {bar} {progress*100:5.1f}%  {processed_now}/{total}  本张耗时 {total_cost:4.1f}s  预计剩余 {eta} ")

    # 关闭数据库连接
    conn.close()
    print("\n[完成] 本批次处理完成。")


if __name__ == "__main__":
    require_exiftool()
    main()
