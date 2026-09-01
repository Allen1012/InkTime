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
| `/admin/settings` | 在线修改配置并留存审计记录。配置按「模型与分析、展示与天气、渲染与设备、上传与任务、系统与安全」五个标签分类，另有一个只读的「配置审计」标签，可跨标签搜索配置项，保存栏吸底且一次提交全部可编辑项。模型接口地址、模型名与密钥同段展示。仅认证管理员可查看并复制包含当前下载密钥的完整设备 `latest.bin` 地址；页面还展示各照片目录状态。`IMAGE_DIR` 可改，但只能选择容器已挂载目录。除数据库与输出目录、监听地址端口、会话与登录限流、`SECRET_KEY`、`DOWNLOAD_KEY` 外均可改，保存后无需重启；分析与渲染类从下一个任务生效。模型接口密钥只写不回显，留空表示保持原值 |

照片编辑使用版本号乐观锁，所有写操作都会记入审计日志。后台任务由独立工作进程执行，支持租约、超时恢复和最多三次重试。

容器镜像入口会在数据库文件不存在时自动执行首次迁移；管理员表为空时可访问 `/admin/setup`：未配置 `INITIAL_SETUP_TOKEN` 时，可信家庭局域网内首位完成设置的人可创建管理员；配置令牌后仍严格校验。`SECRET_KEY` 与 `DOWNLOAD_KEY` 缺省时分别持久化到数据库同目录的隐藏文件。已有数据库在普通容器启动时只接受严格结构检查，不会自动升级；升级必须按备份、演练和显式迁移流程执行，或显式开启 `AUTO_MIGRATE_ON_START` 让容器入口在强制备份后自动补齐缺失迁移。

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

### 3）可选高级配置

家庭局域网基础 Docker Compose 不需要复制 `.env`；直接按后文命令启动即可。以下配置仅用于本地脚本、模型接入、外部照片目录或公网/生产覆盖：

```bash
cp .env.example .env
vi .env
```

常用高级项：

| 配置项 | 说明 |
|--------|------|
| `IMAGE_DIR` | 本地运行的照片库路径；容器外部目录还必须通过 Compose override 显式挂载 |
| `API_URL` `MODEL_NAME` `API_KEY` | 视觉模型接口，使用 OpenAI 兼容协议（LM Studio 或云端服务均可）。要同时保存多套模型配置见 [多套模型配置](#多套模型配置) |
| `FONT_PATH` | 中文字体路径，留空会把中文渲染成豆腐块且不报错 |
| `SECRET_KEY` `DOWNLOAD_KEY` | 可选显式覆盖；缺省时从数据库同目录的安全持久化文件读取或生成 |
| `INITIAL_SETUP_TOKEN` | 可选首次管理员令牌；公网或不可信网络建议配置 |

各脚本会在 `.env` 存在时自行加载，且进程环境变量优先，不需要先 `source .env`。值含空格时要加引号，例如 `PROJECT_NAME="InkTime 相册"`。

未显式配置时，应用在数据库结构门禁通过后分别创建或读取 `.inktime-secret-key` 与 `.inktime-download-key`。新文件权限为 `0600`；内容损坏、权限向组或其他用户开放、不是普通文件或无法安全读取时拒绝启动。显式环境变量优先于持久化文件。

`APP_ENV=production` 时仍强制要求安全配置有效：会话密钥非空、下载密钥至少 24 个字符且不是示例值、`SESSION_COOKIE_SECURE=True`。最后一项要求通过 HTTPS 访问，否则浏览器不会回传会话 Cookie、后台无法登录；纯 HTTP 的可信家庭局域网使用基础 Compose 的 `development`。

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

命令会交互询问用户名与密码（隐藏输入并二次确认），也可作为受控应急流程使用。Docker 首次部署不要运行 `create-admin`：基础家庭局域网模式直接访问 `/admin/setup`，首位访问者创建管理员；若已配置 `INITIAL_SETUP_TOKEN`，页面会要求提交匹配令牌。

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

## 多套模型配置

`.env` 里的 `API_URL`、`MODEL_NAME`、`API_KEY` 只能存一套，接了公司自建就没法同时留着千问。后台「模型厂商」页（`/admin/providers`）可以保存多套接入配置并按用途分开使用。

### 建档

每条档案是一个厂商：名称、接口地址、密钥，以及各自独立的超时与图片最长边（本地模型和云端服务的合理超时差得远，共用一个值不合适）。已经在 `.env` 里配好的那套可以点「导入当前配置为厂商」一键建档，不必手抄。

接口地址填 **OpenAI 兼容模式的基础地址**，通常以 `/v1` 结尾，例如 `https://dashscope.aliyuncs.com/compatible-mode/v1` 或 `http://127.0.0.1:1234/v1`。已经指向 `/chat/completions` 的完整端点也接受。不要填其他协议的路径（例如 Responses API 的 `/responses`），那会在测试时报 HTTP 404。

密钥在页面上只显示末四位，编辑时留空表示保持原值。每条档案都能点「测试连通性」，发一次最小请求验证地址与密钥是否可用——不测的话，填错只能等分析任务失败才发现，而那时已经排了一批任务。

### 一个厂商配多个模型

同一个厂商下可以填多个可选模型，实际调用只用其中「当前启用」的那一个。多模型之间**不会自动轮换、也不会在故障时自动切换**：各模型的授权额度不同，自动轮着调用会让额度以不可预期的方式被消耗，所以换模型是需要你明确决定的动作。

列表页的「启用模型」列有下拉可直接切换。编辑页把模型清单做成一组行，每行可以单独测试连通性，逐个验过再决定用哪个。

### 按用途分流与故障降级

建档本身**不会改变**任何行为——这是最容易误解的一点。档案显示「启用」只表示它允许被引用，真正决定谁干活的是配置管理页「模型与分析 → 用途路由」的三个配置项：

| 配置项 | 控制 |
|--------|------|
| `ANALYSIS_PROVIDER` | 照片评分与内容识别 |
| `NARRATION_PROVIDER` | 展示文案（旁白），留空时跟随分析 |
| `PANEL_PROVIDER` | 「历史上的今天」模型筛选，留空时跟随分析 |

值是厂商名称。三项都留空时分析仍走 `.env` 里那套兜底配置。厂商列表页会标注每条档案被哪些用途引用，未被引用的明确标「未被引用」。

一个用途可以填多个厂商，用分号分隔构成**降级候选链**，例如 `千问;公司`。主用厂商出现连接失败、超时、HTTP 429 或 HTTP 5xx 时自动切到下一个；但 HTTP 400、HTTP 401 这类其他 4xx，以及响应内容解析失败**不会**降级——那通常是密钥、参数或提示词的问题，换一家只是把同一笔钱花两遍。

后台任务在首次认领时把候选链固化进任务快照，因此排队时指向千问的任务执行时一定走千问，即使中间你把路由切成了别家。改完路由点「重试」可立即生效。实际发生的厂商切换会写入任务事件并记录警告日志，可在后台任务页复盘。

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

#### Docker Compose 家庭局域网零配置部署

基础 Compose 面向可信家庭局域网：默认 `APP_ENV=development`、普通 HTTP、`SESSION_COOKIE_SECURE=False`，无需复制 `.env`，命令也不带 `--env-file`。默认数据库、照片和渲染产物分别位于容器内 `/app/data/photos.db`、`/app/data/photos`、`/app/data/output`；宿主仓库的 `data/` 整体挂载到 `/app/data`，删除或重建容器后仍会保留这些数据。

正式镜像内置 GNU C Library 地址选择策略：双栈域名同时返回 IPv4 与 IPv6 地址时优先 IPv4，避免网络附加存储设备的 IPv6 路由不可用却仍返回 AAAA 记录时，模型请求先在坏链路上等待；只有 IPv6 地址的目标仍可正常使用 IPv6。该策略覆盖 Compose、独立容器和图形界面单容器，无需修改 Docker 全局 IPv6。若部署环境必须优先 IPv6，可切回旧版本镜像，或在重建容器时把自定义 `gai.conf` 只读挂载到 `/etc/gai.conf`。模型请求超时仍由后台 `TIMEOUT` 配置独立控制，思考型视觉模型建议保持默认 600 秒。

```bash
mkdir -p data logs

docker compose -f deploy/docker-compose.yml config
docker compose -f deploy/docker-compose.yml build
docker compose -f deploy/docker-compose.yml up -d \
  inktime-schema inktime-server inktime-worker
```

数据库文件不存在时，镜像入口会自动执行首次迁移；已有数据库只做严格结构检查，不会自动升级，也不要预先创建零字节 `data/photos.db`。服务启动后访问 `http://<局域网地址>:5005/admin/setup`：未配置 `INITIAL_SETUP_TOKEN` 时，局域网内首位完成设置的人可以创建管理员；配置后仍必须提交匹配令牌。请只在可信家庭局域网内使用缺省令牌模式，并立即完成首次设置。

`SECRET_KEY` 与 `DOWNLOAD_KEY` 未显式配置时，会分别持久化到数据库同目录的 `.inktime-secret-key` 与 `.inktime-download-key`，新文件权限为 `0600`。显式环境变量优先；已有文件权限不安全、内容损坏或不可安全读取时应用拒绝启动，不会静默重置。删除密钥文件会在下次启动生成新值，使既有登录会话失效并改变设备下载地址。

管理员登录后可在 `/admin/settings` 复制包含当前 `DOWNLOAD_KEY` 的完整 `latest.bin` 设备地址。该地址包含路径口令，不要公开分享。

检查服务状态和日志：

```bash
docker compose -f deploy/docker-compose.yml ps -a
docker compose -f deploy/docker-compose.yml logs --tail=200 \
  inktime-schema inktime-server inktime-worker
```

`inktime-schema` 成功完成后显示退出状态 0 属于正常现象。

#### 公网、生产与外部照片目录

基础 Compose 不适合公网或不可信网络。生产覆盖必须同时设置 `APP_ENV=production`、`SESSION_COOKIE_SECURE=True` 并置于 HTTPS 反向代理之后；可进一步显式设置随机 `SECRET_KEY`、至少 24 个字符的 `DOWNLOAD_KEY` 与 `INITIAL_SETUP_TOKEN`。只把 `APP_ENV` 改成 `production` 而保留基础 Compose 的不安全 Cookie 会被应用拒绝启动。

后台可以在线修改 `IMAGE_DIR`，但只能选择容器已经挂载、存在且可读的目录；在线配置不会新增容器挂载。外部照片库必须通过 Compose override 显式挂载到 Web 服务、后台工作进程和需要访问照片的工具服务，并让各容器使用一致的容器内绝对路径。宿主路径不要求与容器路径字面相同。

使用独立容器或离线镜像时同样应把宿主持久化目录挂载到 `/app/data`，完整流程见 [08-配置与部署：Docker 与 Podman 离线镜像部署](docs/knowledge/08-配置与部署.md#docker-与-podman-离线镜像部署)。

#### 已有数据库升级

普通容器启动不会升级已有数据库。升级前必须停止 Web 服务和后台工作进程的写入，通过 SQLite backup API 创建一致性备份，在备份副本上演练迁移并校验基线；确认后再显式迁移正式数据库、执行结构检查，并使用新镜像和原有挂载重建容器。若需要回滚旧镜像，必须同时恢复与旧版本匹配的数据库备份。

若不想每次有新迁移都手动执行升级命令，可以显式设置 `AUTO_MIGRATE_ON_START=true`：容器入口会在初始化锁内先只读探测缺失的迁移版本，强制创建一致性备份，只有备份成功才补齐迁移。它只覆盖「历史干净、仅落后若干版本」这一种情形，未知迁移版本、分叉历史、零字节或损坏的数据库仍然一律拒绝启动；结构已是最新时不备份也不写库，因此重启不会堆积备份文件。备份目录用 `DB_BACKUP_DIR` 指定，留空为数据库同级 `backups/`，必须位于持久化挂载内。

该开关需要在 `inktime-schema`、`inktime-server`、`inktime-worker` 上取一致的值（基础 Compose 已统一插值），`inktime-schema` 最先执行门禁，漏配它整组仍会启动失败。代价是回滚变重：自动升级后回退旧镜像必须同时恢复对应的数据库备份，否则旧程序会因未知迁移版本拒绝启动。生产与公网部署建议保持默认关闭。

#### 当前改动验证状态

本次家庭局域网零配置实现尚未完成新镜像构建、Docker Compose 整组启动或目标环境验收，因此这里不沿用旧版本的镜像编号、归档摘要和验收结论，也不把新实现表述为已经验证。历史离线交付记录保留在 [08-配置与部署](docs/knowledge/08-配置与部署.md#docker-与-podman-离线镜像部署)，仅用于追溯对应旧提交和旧产物。

### 自动扫描新照片

直接拷进 `IMAGE_DIR` 的照片不会自动入库，需要扫描才会出现在后台并进入选片候选池。两种触发方式：

- 后台照片管理页右上角的「扫描照片目录」按钮
- 命令行或定时任务：`./venv/bin/python -m src.analysis.run_scan`

扫描只负责发现新照片并登记入库，**不会自动排队分析**——一个几百张的目录自动全量分析会连续数小时按量计费。分析由哪个动作触发，取决于后台配置项「新照片默认收录状态」（`NEW_PHOTO_CURATION`）：

| 取值 | 新照片登记为 | 分析由什么触发 |
|------|--------------|----------------|
| `excluded`（默认） | 未收录 | 在后台把照片**改为已收录**，即自动排队分析 |
| `included` | 已收录 | 照片管理页的「放行分析」，按张数分批入队 |

后台上传不受此项影响：上传本身就是逐张的人工决定，一律按已收录登记并立即排队分析。把照片改回未收录会自动撤销它尚未执行的分析任务。

无论哪种方式，实际分析都由工作进程完成，因此需要 `run_worker` 处于运行状态。

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
