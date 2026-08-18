# InkTime · 墨水屏回忆相框

<p align="left">
  <img src="esp32/InkTime.jpeg" width="80%">
</p>

InkTime 是一个「拉回你相册里的记忆」的墨水屏电子相框项目。

它不会随机展示照片，也不是简单地按时间轴播放，而是：

- 用 AI 理解每一张照片在拍什么
- 给照片按「值得回忆度」「美观度」打分
- 写一句灵光一现的旁白文案
- 每天从「历史上的今天」里选出**最值得被再次看到的照片**
- 推送到 ESP32 墨水屏上展示

---

## 项目整体结构

InkTime 分为四部分：

1. **照片分析（Python）**
   扫描相册 → 调用视觉模型 → 分类 / 评分 / 写文案 → 存入数据库

2. **图片渲染（Python）**
   从数据库里选出「历史上的今天」高分照片 → 渲染成 ESP32 可直接显示的 `.bin`

3. **Web 服务（Python / Flask）**
   对外提供 ESP32 下载接口、公开相册页面，以及需要登录的后台管理；
   配套一个独立的后台工作进程执行上传、分析、维护等持久化任务

4. **下载与展示（ESP32）**
   ESP32 定时从服务器拉取 `.bin` → 刷新墨水屏 → 深度休眠直至下次唤醒

## 功能总览

**公开部分**（无需登录）

- 照片墙、分类浏览、搜索
- 沉浸式展示页，可长时间轮播并抑制息屏；支持配置生效时间段（可按星期），夜间自动停在最后一张或显示休息提示，不消耗展示次数
- 展示页可选显示当前天气（默认关闭），数据源免注册免密钥，位置默认复用常驻地坐标，也可手动配置
- ESP32 下载接口

**后台管理**（需管理员登录，路径 `/admin`）

| 页面 | 能力 |
|------|------|
| `/admin` | 待处理事项（分析失败、任务失败、进行中任务、回收站，可点击直达）、照片概况（总数、近 7 天新增、平均评分、分类数）、分析进度（状态分布、拍摄时间与城市覆盖） |
| `/admin/photos` | 分页浏览、网格与表格切换、搜索、按分类与分析状态筛选、编辑单张、批量改分类或状态、批量移入回收站、扫描照片目录导入新照片 |
| `/admin/photos/upload` | 上传 JPEG / PNG / WebP / HEIC（含手机 HDR 的 MPO 与动态照片，取首帧），单文件默认上限 64 MiB，落盘前压到 5 MiB 以内；批次内逐文件独立校验，不合法的单独跳过并给出原因，其余照常入库 |
| `/admin/trash` | 回收站浏览、恢复、永久删除、按保留期批量清理 |
| `/admin/jobs` | 任务列表，含状态、进度、尝试次数、错误信息，可取消与重试；尝试次数用尽时给出照片详情页入口 |
| `/admin/photos/<id>` | 照片详情与编辑，可「重新分析全部」或「只重写文案」——失败重来与结果不满意都走这里 |
| `/admin/settings` | 在线修改配置并留存审计记录。分组可折叠，顶部展示各照片目录的角色、可用性与照片数。除数据库与输出目录、监听地址端口、会话与登录限流、`SECRET_KEY`、`DOWNLOAD_KEY` 外均可改，保存后无需重启；分析与渲染类从下一个任务生效。模型接口密钥只写不回显，留空表示保持原值 |

照片编辑使用版本号乐观锁，所有写操作都会记入审计日志。后台任务由独立工作进程执行，支持租约、超时恢复和最多三次重试。

容器镜像入口会在数据库文件不存在时自动执行首次迁移；管理员表为空时，可访问 `/admin/setup`，使用至少 24 个字符的一次性初始化令牌创建首个管理员。已有数据库在普通容器启动时只接受严格结构检查，不会自动升级；升级必须按备份、演练和显式迁移流程执行。

---

## 环境准备

### 1）Python

推荐 Python 3.10+。建议使用虚拟环境：

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2）安装 exiftool（可选）

InkTime 可以在不装 exiftool 的情况下运行，但不一定能完整获取 EXIF 中的 GPS 信息。

- macOS（Homebrew）：`brew install exiftool`
- Linux：`sudo apt-get install -y libimage-exiftool-perl`

### 3）配置 .env

```bash
cp .env.example .env
vi .env
```

必须配置：

| 配置项 | 说明 |
|--------|------|
| `IMAGE_DIR` | 照片库路径。支持多个目录，用分号分隔，第一个为主目录 |
| `API_URL` `MODEL_NAME` `API_KEY` | 视觉模型接口，使用 OpenAI 兼容协议（LM Studio 或云端服务均可） |
| `FONT_PATH` | 中文字体路径，留空会把中文渲染成豆腐块且不报错 |
| `SECRET_KEY` | 会话与 CSRF 签名密钥，生产环境必须为非空随机值 |

`.env` 是唯一配置源，各脚本会自行加载，不需要先 `source .env`。值含空格时要加引号，例如 `PROJECT_NAME="InkTime 相册"`。

生成 `SECRET_KEY`：

```bash
./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**关于 `DOWNLOAD_KEY`**：为防止照片隐私泄露，建议修改它，为 ESP32 下载路径加一个随机前缀作为密钥，并同步修改 `esp32/ink-display-7C-photo/ink-display-7C-photo.ino` 中的 `DAILY_PHOTO_PATH_PREFIX`。这不是加密，只是一个简单的路径口令。

`APP_ENV=production` 时会额外强制校验：`SECRET_KEY` 非空、`DOWNLOAD_KEY` 至少 24 个字符、`SESSION_COOKIE_SECURE` 为真。最后一项要求通过 HTTPS 访问，否则浏览器不会回传会话 cookie、后台无法登录；纯 HTTP 的内网环境请保持默认的 `development`。

### 4）初始化数据库（本地非容器运行）

本地虚拟环境运行不会自动建库或迁移，需要显式执行一次：

```bash
./venv/bin/python scripts/database_admin.py migrate --database data/photos.db
./venv/bin/python scripts/database_admin.py check-schema --database data/photos.db
```

已有数据库升级时，先备份再在副本上验证：

```bash
# 1) 通过 SQLite 备份应用程序编程接口（backup API）创建一致性备份，同时生成基线 JSON
./venv/bin/python scripts/database_admin.py backup --database data/photos.db --output-dir data/backups

# 2) 在备份副本上先跑一遍迁移与校验，确认无误
./venv/bin/python scripts/database_admin.py migrate --database data/backups/photos-<时间戳>.db
./venv/bin/python scripts/database_admin.py verify  --database data/backups/photos-<时间戳>.db \
    --baseline data/backups/photos-<时间戳>.baseline.json

# 3) 再迁移正式库，并用只读检查作为启动门禁
./venv/bin/python scripts/database_admin.py migrate      --database data/photos.db
./venv/bin/python scripts/database_admin.py check-schema --database data/photos.db
```

`verify` 会核对完整性、迁移版本与照片身份摘要；`identity_mismatches` 为空表示照片数据未被改动。

Docker 首次部署不要运行以上迁移命令，也不得预先创建零字节 `photos.db`；容器入口只会为不存在的数据库自动执行首次迁移。已有数据库仍需停止 Web 服务与后台工作进程的写入，按上述受控流程显式升级，普通容器启动不会自动升级。

### 5）创建管理员账号（本地非容器运行）

本地后台需要账号才能登录，密码至少 12 个字符：

```bash
./venv/bin/flask --app src.server.app create-admin
```

命令会交互询问用户名与密码（隐藏输入并二次确认），也可作为受控应急流程使用。Docker 首次部署不要运行 `create-admin`，应配置一次性初始化令牌并通过 `/admin/setup` 创建首个管理员，具体步骤见后文 Docker Compose 快速部署流程。

---

## 分析照片

分析前请确认 LM Studio（或你的云端视觉服务）已启动、`.env` 已配置。

```bash
./venv/bin/python src/analysis/analyze_photos_docker.py
```

也可以用封装脚本（自动加载 `.env` 并使用 venv 的 python）：

```bash
./scripts/run_analysis.sh
```

建议先小批量试跑，确认文案风格和评分标准符合预期后再全量：

```bash
BATCH_LIMIT=20 ./venv/bin/python src/analysis/analyze_photos_docker.py
```

视觉模型会为每张照片生成画面描述、照片类型、值得回忆度与画面美观度评分、一句话文案，结果存入 `data/photos.db`。

修改 `src/analysis/analyze_photos_docker.py` 中的提示词，可调整模型的评价标准和文案风格。

程序支持断点续跑，已处理过的照片不会重复分析，可以分几天跑完整个相册。

*请根据算力选择模型，作者使用的 qwen3-vl-30b 已能取得相当不错的文案。每张照片消耗 2 次 API 调用（一次评分描述、一次文案），用云端 API 时按张数 × 2 估算额度。*

> **关于没有拍摄时间的照片**（截图、微信保存、导出压缩过的图等）：它们照常展示，只是画面上不显示拍摄日期，也不参与「历史上的今天」的月日匹配，而是通过补足档进入当天画面。想让某张参与月日匹配，在后台照片详情页填写拍摄时间即可（来源记为 `manual`）；后台首页有「缺拍摄时间」入口可批量查看。分析完可以查一下有拍摄时间的比例：
>
> ```bash
> ./venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/photos.db').cursor();print(c.execute(\"SELECT COUNT(*) FROM photo_scores WHERE exif_datetime IS NOT NULL AND exif_datetime!=''\").fetchone(), c.execute('SELECT COUNT(*) FROM photo_scores').fetchone())"
> ```

## 渲染「历史上的今天」照片

7.3 寸四色屏：

```bash
./venv/bin/python src/render/render_daily_photo.py
```

13.3 寸六色屏：

```bash
./venv/bin/python src/render/render_daily_photo_133c.py
```

产物在 `data/output/`：`photo_{idx}.bin`、`latest.bin`，以及可用于肉眼检查效果的 `preview.png`。

## 启动 Web 服务

```bash
./venv/bin/python -m src.server.run_server
```

这是本地与生产共用的 Waitress 入口，不要用 Flask 开发服务器常驻。

后台的上传、重新分析、回收站清理等任务需要独立工作进程承接，另开一个进程运行：

```bash
./venv/bin/python -m src.analysis.run_worker
```

浏览器访问（端口取 `.env` 里的 `FLASK_PORT`，默认 5005）：

```
http://127.0.0.1:5005/
```

公开页面：`/`（照片墙）、`/category`（分类）、`/search`（搜索）、`/display`（沉浸式展示）。
后台入口：`/admin`。

`ENABLE_REVIEW_WEBUI` 与 `ENABLE_FILE_BROWSER` 只共同控制 `/files/`（渲染产物目录浏览），两者同时为真才开放；它们**不影响**照片墙、分类、搜索与展示页。这两项已可在后台配置页在线修改，改完立即生效。若要对外只保留 ESP32 下载接口，需要在反向代理上限制公开路由，或不对外暴露该端口。

### 访问控制

公开相册页面与 `GET /api/photos` 等公开接口无需登录。`/admin/*` 与 `/api/admin/*` 一律要求管理员登录，后台写请求同时受 CSRF 保护。

`FLASK_HOST=0.0.0.0` 时同网段设备可浏览公开照片与其中的 GPS 信息，请只在可信局域网内使用，或在反向代理上增加访问控制。公网部署建议加 HTTPS 与反代鉴权。

---

## 生产运行与定时任务

### systemd 三单元

| 单元 | 作用 |
|------|------|
| `deploy/inktime-schema.service` | 一次性只读结构门禁 |
| `deploy/inktime-server.service` | Web 常驻服务，`Requires/After` 结构门禁 |
| `deploy/inktime-worker.service` | 后台工作进程，`Requires/After` 结构门禁 |

按实际环境修改三个文件中的 `User`、`Group` 和路径后安装：

```bash
sudo cp deploy/inktime-schema.service deploy/inktime-server.service deploy/inktime-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now inktime-schema inktime-server inktime-worker
```

`inktime-server.service` 带 `Environment=APP_ENV=production`。若通过纯 HTTP 访问，会因强制 `SESSION_COOKIE_SECURE` 而无法登录后台，可用 drop-in 覆盖为 `development`：

```bash
sudo mkdir -p /etc/systemd/system/inktime-server.service.d
printf '[Service]\nEnvironment=APP_ENV=development\n' \
  | sudo tee /etc/systemd/system/inktime-server.service.d/local.conf
sudo systemctl daemon-reload && sudo systemctl restart inktime-server
```

日志用 `journalctl -u inktime-server -f` 查看。具体安全项、停止超时与验证命令见 [08-配置与部署](docs/knowledge/08-配置与部署.md)。

### Docker Compose 与离线镜像部署

`deploy/docker-compose.yml` 定义数据库结构门禁 `inktime-schema`、Web 服务 `inktime-server`、后台工作进程 `inktime-worker`，以及位于 `tools` profile 的三个一次性分析和渲染服务。所有服务共用正式 `deploy/Dockerfile`；数据库固定挂载到 `/app/data/photos.db`，渲染输出固定为 `/app/data/output`。

#### Docker Compose 快速部署

1. 复制配置并填写生产环境必需项：

```bash
cp .env.example .env
vi .env
```

至少设置以下内容：

```dotenv
APP_ENV=production
IMAGE_DIR=/srv/inktime/photos
SECRET_KEY=替换为随机会话密钥
DOWNLOAD_KEY=替换为至少 24 个字符的随机下载密钥
INITIAL_SETUP_TOKEN=替换为至少 24 个字符的一次性初始化令牌
SESSION_COOKIE_SECURE=True
```

`IMAGE_DIR` 必须是宿主机绝对路径。`inktime-server` 在 Docker Compose 中显式覆盖为 `APP_ENV=production`，`inktime-worker` 和一次性数据库结构门禁从同一份 `.env` 读取环境，因此 `.env` 也必须设置 `APP_ENV=production`。生产部署必须设置 `SESSION_COOKIE_SECURE=True` 并置于 HTTPS 之后；不能用生产配置搭配不安全会话 Cookie 启动。纯 HTTP 局域网临时验证请改用独立容器，并显式设置 `APP_ENV=development` 与 `SESSION_COOKIE_SECURE=False`。

2. 创建持久化目录和照片目录，但不要创建 `data/photos.db`；零字节数据库会被容器入口判定为异常：

```bash
mkdir -p data logs /srv/inktime/photos
```

3. 展开并人工检查最终配置：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml config
```

4. 构建镜像并启动数据库结构门禁、Web 服务和后台工作进程：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml build
docker compose --env-file .env -f deploy/docker-compose.yml up -d \
  inktime-schema inktime-server inktime-worker
```

数据库文件不存在时，镜像入口会自动执行首次迁移；已有数据库只做严格结构检查，不会自动升级。

5. 通过 `https://<部署地址>/admin/setup` 使用一次性初始化令牌创建首个管理员。确认新账号能够登录后，从 `.env` 删除 `INITIAL_SETUP_TOKEN`，并强制重建数据库结构门禁、Web 服务和后台工作进程，清除所有服务容器环境中的令牌：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d --force-recreate \
  inktime-schema inktime-server inktime-worker
```

6. 检查服务状态和日志：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml ps -a
docker compose --env-file .env -f deploy/docker-compose.yml logs --tail=200 \
  inktime-schema inktime-server inktime-worker
```

`inktime-schema` 成功完成后显示退出状态 0 属于正常现象。持久化数据库位于宿主 `data/`；升级或停止服务时不得执行 `docker compose down -v`，否则命名卷部署可能丢失数据。

#### 独立容器与离线镜像

使用 `docker run` 或导入离线镜像时，关键参数与 Docker Compose 保持一致：

- 将宿主持久化数据目录读写挂载到 `/app/data`，日志目录读写挂载到 `/app/logs`；
- 将每个照片目录分别挂载，且宿主与容器内使用完全相同的绝对路径；
- Web 容器保留镜像默认命令；
- 后台工作进程容器命令设为 `python -m src.analysis.run_worker`；
- 纯 HTTP 临时验证必须显式使用 `APP_ENV=development` 和 `SESSION_COOKIE_SECURE=False`，生产部署使用 HTTPS、`APP_ENV=production` 和 `SESSION_COOKIE_SECURE=True`。

Docker 首次部署不运行本地 `migrate` 或 `create-admin` 命令，也不预建 `photos.db`。完整的离线归档导入、独立 `docker run` 参数、多照片目录挂载和回滚步骤见 [08-配置与部署：Docker 与 Podman 离线镜像部署](docs/knowledge/08-配置与部署.md#docker-与-podman-离线镜像部署)。

#### 已有数据库升级

普通容器启动不会升级已有数据库。升级前必须停止 Web 服务和后台工作进程的写入，通过 SQLite backup API 创建一致性备份，在备份副本上演练迁移并校验基线；确认后再显式迁移正式数据库、执行结构检查，并使用新镜像和原有挂载重建容器。若需要回滚旧镜像，必须同时恢复与旧版本匹配的数据库备份。

#### 当前交付验证状态

当前交付镜像为 `localhost/inktime:9adc342-linux-amd64`，完整镜像编号为 `9f32c200591c45cd09259e0a342a15f077ff3da438c691d3946686e48e44d152`。Docker 归档为 `tmp/inktime-9adc342-linux-amd64.tar`，Secure Hash Algorithm 256-bit（SHA-256）摘要为 `a54332c6b41a9e9eec5f6edf4fcac5d60c58a7cb95b6d9955cdbf38f9bcce059`。

已在 Podman 环境完成镜像构建、归档内容检查、单容器首次管理员创建，以及使用同一数据卷重建容器后的登录验收。构建机没有安装 Docker，因此尚未验证 `docker load` 或目标 Docker 主机导入启动，也不能把 Podman 验收表述为 Docker 实机验收。

仍未验证：Docker Compose 六服务整组启动、后台工作进程对数据库结构门禁的真实等待、容器停止信号处理、损坏数据库拒绝启动、日志持久化、真实照片与渲染产物持久化、HTTPS、视觉语言模型（VLM）调用、自动扫描、渲染和 ESP32 下载。

### 自动扫描新照片

直接拷进 `IMAGE_DIR` 的照片不会自动入库，需要扫描才会出现在后台并进入选片候选池。两种触发方式：

- 后台照片管理页右上角的「扫描照片目录」按钮
- 命令行或定时任务：`./venv/bin/python -m src.analysis.run_scan`

扫描只负责发现新照片、登记为待分析并排队，实际分析由工作进程完成，因此需要 `run_worker` 处于运行状态。

```bash
chmod +x scripts/scan_library.sh
crontab -e
# 每天 03:30 扫描，早于渲染，新照片当天就有机会被选中
30 3 * * * /path/to/InkTime/scripts/scan_library.sh
```

日志见 `logs/scan.log`。脚本带目录锁，重复触发会跳过。

> **不要用 `analyze_photos_docker` 做定时分析**：它会把已排队的 `pending` 照片当作待处理对象，与工作进程重复分析同一张照片，白白消耗两倍模型额度。批量脚本适合首次全量导入，日常增量交给扫描加工作进程。

### 每日自动选片渲染

```bash
chmod +x scripts/daily_render.sh
crontab -e
# 每天 05:00 选片并渲染
0 5 * * * /path/to/InkTime/scripts/daily_render.sh
```

`scripts/daily_render.sh` 的项目路径按脚本自身位置自动推导，不需要手改。日志见 `logs/render.log`。

## 开发与测试

```bash
./venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

测试使用临时数据库并执行真实迁移，不会触碰 `data/photos.db`。

开发环境（`APP_ENV=development`）下模板改动刷新即生效；生产环境模板被缓存，改完需要重启服务。静态文件始终直接读盘。

## 文档

`docs/knowledge/` 下有分模块的详细文档：

| 文档 | 内容 |
|------|------|
| [01-项目概览](docs/knowledge/01-项目概览.md) | 定位、技术栈、目录结构 |
| [02-系统架构](docs/knowledge/02-系统架构.md) | 模块划分与数据流 |
| [03-照片分析模块](docs/knowledge/03-照片分析模块.md) | VLM 调用、评分规则、EXIF 提取 |
| [04-图片渲染模块](docs/knowledge/04-图片渲染模块.md) | 选片算法、画布布局、抖动与输出格式 |
| [05-Web服务器与前端](docs/knowledge/05-Web服务器与前端.md) | 路由、后台各阶段能力、前端实现 |
| [06-ESP32硬件模块](docs/knowledge/06-ESP32硬件模块.md) | 引脚、通信协议、低功耗与配网 |
| [07-数据库与存储](docs/knowledge/07-数据库与存储.md) | 表结构、迁移、索引 |
| [08-配置与部署](docs/knowledge/08-配置与部署.md) | 全部配置项、systemd 与 Docker 部署 |
| [09-开发指南](docs/knowledge/09-开发指南.md) | 常见问题与调试技巧 |

---

# ESP32 墨水屏硬件部分

## 硬件与引脚

#### 主控

本项目使用乐鑫 ESP32-S3-N8R8 模块。
也可以使用任何成品 ESP32 开发板制作。如使用其它开发板或模块，请注意选择带 PSRAM 的型号（需至少 384K PSRAM）。

#### 屏幕

本项目使用 7.3 寸四色墨水屏，型号为 EL073TS3（49-pin），使用 GxEPD2 库驱动（`GxEPD2_730c_GDEY073D46`）。
其它尺寸、型号请参照 GxEPD2 库中的硬件支持列表修改构造函数。

#### 墨水屏转接板

本项目使用 B 站「记得带马扎」制作的七色 EPD 墨水屏转接板（49-pin）。
市面上大部分 24-pin 墨水屏搭配 SPI 转接板亦可兼容。

#### 引脚定义

墨水屏使用 SPI 通信，默认引脚为：

- `PIN_EPD_BUSY = 14`
- `PIN_EPD_RST  = 13`
- `PIN_EPD_DC   = 12`
- `PIN_EPD_CS   = 11`
- `PIN_EPD_SCLK = 10`
- `PIN_EPD_DIN  = 9`

### 主板焊接

原理图、BOM 清单、制板文件均位于 `esp32/pcb` 文件夹。
原理图中的 H1 - H6 为测试焊盘引出，无需焊接真实器件：

- H1：UART 串口
- H2：USB
- H3：BOOT 引脚，烧录固件时需将该引脚短接到 GND 后上电
- H4：焊接至 EPD 墨水屏转接板
- H5：3.7V 电池焊盘
- H6：5V 输入测试焊盘

建议使用 UART 串口烧录固件。R2、R3、C5、C6 供 USB 使用，如无需要可留空不焊。

SW1：RESET 键，按下后重启设备，并从服务器拉取、显示图片一次。RESET 键可将设备从长休眠中唤醒。
SW2：WiFi 重置键，按住 SW2 再按下 SW1，ESP32 重启后会清空 NVS，以重新配置 WiFi 连接。
SW3 / SW4：备用 GPIO，以防未来需要添加功能，如无需要可留空不焊。

完整 PCB 板示例：

<p align="left">
  <img src="esp32/pcb/pcb.jpeg" width="80%">
</p>

## 编译与烧录

建议使用 Arduino IDE。

1. 安装 ESP32 Arduino Core
2. 选择开发板：ESP32-S3（必须开启 PSRAM）
3. 安装依赖库：`GxEPD2`
4. 打开并编译烧录 `ink-display-7C.ino`

### 自定义字体（可选）

把字体文件路径写进 `.env` 的 `FONT_PATH` 即可。
Ubuntu 可直接用系统自带的 `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`。
该项留空时中文会渲染成豆腐块且不报错。

## 首次配置

设备启动时会尝试从 NVS 读取已保存的 Wi-Fi 配置；若未配置或连接失败，会自动进入 AP 配置模式：

- 设备开启 AP 热点：`InkTime-xxxx`，默认密码 `12345678`
- 连接后用浏览器访问配置页面 `http://192.168.4.1/`
- 配置 Wi-Fi、服务器地址、定时更新时间并保存，设备会自动重启并进入正常工作流程

## 刷新与休眠

- 设备每天在配置的更新时间从服务器拉取一次当日图片并刷新墨水屏
- 成功刷新后进入 Deep Sleep，直到下一次被唤醒
- 若下载超时（默认 60s）也会进入长休眠，避免异常耗电
- 任何时候按下 RESET 键，会强制重启并立即拉取、刷新一次图片
- 长休眠待机电流 < 1mA，使用 2 节 18650 电池（5000mAh）约可实现半年续航

---

## 相关项目与许可

- ESP32 固件依赖 GxEPD2 © ZinggJM（GPL-3.0）：https://github.com/ZinggJM/GxEPD2
  如对外分发编译后的固件，请同时遵守 GPL-3.0。

- 后台界面图标取自 Lucide（ISC License），以内联 SVG 方式使用，无外部依赖：
  https://lucide.dev

- 项目中的离线中文城市名索引基于 GeoNames 数据制作：
  GeoNames © GeoNames contributors, CC BY 4.0
  https://www.geonames.org/
