"""管理员认证、照片管理页面与受保护写接口。"""

from __future__ import annotations

from typing import Any, Mapping

from flask import Blueprint, Response, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_user, logout_user

from ..auth import is_safe_next_target
from ..errors import ParameterError
from ..extensions import csrf, login_manager
from ..forms import LoginForm, PhotoEditForm


admin_page_blueprint = Blueprint("admin", __name__, url_prefix="/admin")
admin_api_blueprint = Blueprint("admin_api", __name__, url_prefix="/api/admin")
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _authentication_service() -> Any:
    """取得当前应用实例的管理员认证服务。"""
    return current_app.extensions["inktime_services"]["auth"]


def _admin_photo_service() -> Any:
    """取得当前应用实例的后台照片查询服务。"""
    return current_app.extensions["inktime_services"]["admin_photo"]


def _admin_photo_management_service() -> Any:
    """取得当前应用实例的后台照片写入与审计服务。"""
    return current_app.extensions["inktime_services"]["admin_photo_management"]


def _admin_job_service() -> Any:
    """取得当前应用实例的合并任务查询与管理服务。"""
    return current_app.extensions["inktime_services"]["admin_jobs"]


def _photo_job_service() -> Any:
    """取得阶段五照片分析任务服务。"""
    return current_app.extensions["inktime_services"]["photo_jobs"]


def _photo_lifecycle_service() -> Any:
    """取得回收站、永久删除与过期清理服务。"""
    return current_app.extensions["inktime_services"]["photo_lifecycle"]


def _upload_service() -> Any:
    """取得当前应用实例的安全上传服务。"""
    return current_app.extensions["inktime_services"]["uploads"]


def _positive_integer_argument(name: str, default: int) -> int:
    """读取正整数查询参数，格式错误时返回安全参数错误。"""
    raw_value = request.args.get(name)
    if raw_value in (None, ""):
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ParameterError(f"{name} 必须为正整数") from error
    if value < 1:
        raise ParameterError(f"{name} 必须为正整数")
    return value


def _admin_url(**updates: Any) -> str:
    """保留当前筛选参数并构造后台照片列表链接。"""
    parameters = request.args.to_dict()
    parameters.update({key: value for key, value in updates.items() if value is not None})
    return url_for("admin.photos", **parameters)


def _photo_form(photo: Mapping[str, Any]) -> PhotoEditForm:
    """把照片详情转换为编辑表单初值，不在模板中拼接日期格式。"""
    date_taken = str(photo.get("date_taken") or "")
    if date_taken:
        date_taken = date_taken.replace(":", "-", 2).replace(" ", "T", 1)
    return PhotoEditForm(
        data={
            "version": photo["version"],
            "caption": photo.get("description") or "",
            "side_caption": photo.get("side_caption") or "",
            "reason": photo.get("reason") or "",
            "exif_city": photo.get("location") or "",
            "category": photo.get("category") or "",
            "date_taken": date_taken,
            "analysis_status": photo.get("analysis_status") or "legacy",
        }
    )


def _edit_form_values(form: PhotoEditForm) -> dict[str, Any]:
    """提取页面允许编辑的字段，历史状态保持原值而不重新写入。"""
    values: dict[str, Any] = {
        "caption": form.caption.data,
        "side_caption": form.side_caption.data,
        "reason": form.reason.data,
        "exif_city": form.exif_city.data,
        "category": form.category.data,
        "date_taken": form.date_taken.data,
    }
    if form.analysis_status.data != "legacy":
        values["analysis_status"] = form.analysis_status.data
    return values


@admin_page_blueprint.before_request
def protect_admin_pages():
    """让当前及未来后台页面默认要求登录，并在写请求上校验跨站请求伪造令牌。"""
    if request.endpoint == "admin.login":
        if request.method in _MUTATING_METHODS:
            csrf.protect()
        return None
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if request.method in _MUTATING_METHODS:
        csrf.protect()
    return None


@admin_api_blueprint.before_request
def protect_admin_api():
    """让当前及未来后台接口默认先认证，再对写请求校验跨站请求伪造令牌。"""
    if not current_user.is_authenticated:
        return login_manager.unauthorized()
    if request.method in _MUTATING_METHODS:
        csrf.protect()
    return None


@admin_page_blueprint.route("/login", methods=["GET", "POST"])
def login():
    """显示登录表单并建立受限的永久管理员会话。"""
    if current_user.is_authenticated:
        return redirect(url_for("admin.index"))

    form = LoginForm()
    if request.method == "GET":
        form.next.data = request.args.get("next", "")
        return render_template("admin/login.html", form=form)

    next_target = form.next.data
    form_is_valid = form.validate_on_submit()
    if form_is_valid:
        client_ip = request.remote_addr or "unknown"
        admin_user = _authentication_service().authenticate(
            form.username.data,
            form.password.data,
            client_ip,
        )
        if admin_user is not None:
            session.clear()
            login_user(admin_user, remember=False, fresh=True)
            session.permanent = True
            if is_safe_next_target(next_target):
                return redirect(next_target)
            return redirect(url_for("admin.index"))

    flash("登录失败，请检查凭据或稍后重试")
    form.password.data = ""
    form.next.data = next_target if is_safe_next_target(next_target) else ""
    return render_template("admin/login.html", form=form), 401


@admin_page_blueprint.post("/logout")
def logout():
    """销毁当前管理员会话；退出仅允许携带令牌的 POST 请求。"""
    logout_user()
    session.clear()
    return redirect(url_for("admin.login"))


@admin_page_blueprint.get("")
def index():
    """渲染可独立降级统计卡片的后台首页。"""
    return render_template("admin/index.html", statistics=_admin_photo_service().dashboard())


@admin_page_blueprint.get("/photos")
def photos():
    """渲染受白名单约束的照片分页列表和批量操作表单。"""
    result = _admin_photo_service().list_photos(
        page=_positive_integer_argument("page", 1),
        limit=_positive_integer_argument("limit", 24),
        query=request.args.get("query", ""),
        category=request.args.get("category", ""),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
        sort=request.args.get("sort", "latest"),
        view=request.args.get("view", "grid"),
    )
    result["urls"] = {
        "previous": _admin_url(page=result["page"] - 1) if result["page"] > 1 else None,
        "next": _admin_url(page=result["page"] + 1) if result["page"] < result["total_pages"] else None,
        "grid": _admin_url(view="grid", page=1),
        "table": _admin_url(view="table", page=1),
    }
    return render_template("admin/photos.html", result=result)


@admin_page_blueprint.post("/photos/batch")
def batch_photos():
    """处理页面批量编辑或移入回收站操作并展示逐项结果。"""
    items: list[dict[str, int]] = []
    for raw_item in request.form.getlist("selected"):
        try:
            photo_id, version = raw_item.split(":", 1)
            items.append({"id": int(photo_id), "version": int(version)})
        except (TypeError, ValueError) as error:
            raise ParameterError("批量照片选择格式无效") from error
    action = (
        "soft_delete"
        if request.form.get("batch_soft_delete") == "1"
        else request.form.get("action")
    )
    if action == "soft_delete":
        result = _photo_lifecycle_service().batch_soft_delete(
            items,
            int(current_user.id),
            current_user.username,
        )
    else:
        result = _admin_photo_management_service().batch_update(
            action,
            items,
            request.form.get("value"),
            current_user.id,
            current_user.username,
        )
    flash(
        f"批量操作完成：成功 {result['success_count']} 项，失败 {result['failure_count']} 项"
    )
    return redirect(url_for("admin.photos"))


@admin_page_blueprint.route("/photos/<int:photo_id>", methods=["GET", "POST"])
def photo_detail(photo_id: int):
    """展示照片详情，并用乐观锁提交受限字段编辑。"""
    photo = _admin_photo_service().detail(photo_id)
    if request.method == "GET":
        return render_template(
            "admin/photo_detail.html", photo=photo, form=_photo_form(photo)
        )

    form = PhotoEditForm()
    if not form.validate_on_submit():
        flash("照片字段校验失败，请检查输入")
        return render_template("admin/photo_detail.html", photo=photo, form=form), 400
    _admin_photo_management_service().update_photo(
        photo_id,
        form.version.data,
        _edit_form_values(form),
        current_user.id,
        current_user.username,
    )
    flash("照片信息已保存")
    return redirect(url_for("admin.photo_detail", photo_id=photo_id))


@admin_page_blueprint.get("/photos/<int:photo_id>/thumbnail")
def admin_photo_thumbnail(photo_id: int):
    """返回仅供已认证管理员查看的活动照片缩略图。"""
    content = _admin_photo_service().admin_thumbnail(photo_id)
    return Response(content.data, mimetype=content.mimetype)


@admin_page_blueprint.get("/photos/<int:photo_id>/full")
def admin_photo_full(photo_id: int):
    """返回仅供已认证管理员查看的活动照片原图。"""
    content = _admin_photo_service().admin_full_photo(photo_id)
    return send_file(content.path, mimetype=content.mimetype, as_attachment=False)


@admin_page_blueprint.get("/photos/upload")
def upload_photos_page():
    """渲染受认证保护的多文件上传页面。"""
    return render_template("admin/upload.html")


@admin_page_blueprint.get("/jobs")
def jobs():
    """渲染任务类型、状态、进度、重试、错误和关联照片。"""
    return render_template("admin/jobs.html", jobs=_admin_job_service().list_jobs())


@admin_page_blueprint.post("/jobs/<queue>/<int:job_id>/cancel")
def cancel_job(queue: str, job_id: int):
    """按明确队列处理页面任务取消。"""
    _admin_job_service().cancel(queue, job_id, int(current_user.id))
    flash("任务取消请求已提交")
    return redirect(url_for("admin.jobs"))


@admin_page_blueprint.post("/jobs/<int:job_id>/cancel")
def cancel_photo_job_legacy(job_id: int):
    """兼容阶段五页面路径并取消照片分析任务。"""
    _admin_job_service().cancel("photo", job_id, int(current_user.id))
    flash("任务取消请求已提交")
    return redirect(url_for("admin.jobs"))


@admin_page_blueprint.post("/jobs/<queue>/<int:job_id>/retry")
def retry_job(queue: str, job_id: int):
    """按明确队列处理页面任务重试。"""
    _admin_job_service().retry(queue, job_id, int(current_user.id))
    flash("任务已重新排队")
    return redirect(url_for("admin.jobs"))


@admin_page_blueprint.post("/jobs/<int:job_id>/retry")
def retry_photo_job_legacy(job_id: int):
    """兼容阶段五页面路径并重试照片分析任务。"""
    _admin_job_service().retry("photo", job_id, int(current_user.id))
    flash("任务已重新排队")
    return redirect(url_for("admin.jobs"))


@admin_api_blueprint.get("")
def status():
    """返回阶段六照片管理、回收站和维护任务能力状态。"""
    return jsonify(
        {
            "status": "ok",
            "data": {
                "phase": 6,
                "authentication": "implemented",
                "photo_editing": "implemented",
                "batch_operations": "implemented",
                "uploads": "implemented",
                "background_jobs": "implemented",
                "trash": "implemented",
                "restore": "implemented",
                "permanent_delete": "implemented",
                "artifact_blocking": "implemented",
                "username": current_user.username,
            },
        }
    )


@admin_api_blueprint.patch("/photos/<int:photo_id>")
def update_photo_api(photo_id: int):
    """按 JSON 中的预期版本更新单张照片并返回新版本。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    values = dict(payload)
    version = values.pop("version", None)
    result = _admin_photo_management_service().update_photo(
        photo_id,
        version,
        values,
        current_user.id,
        current_user.username,
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/photos/batch")
def batch_photos_api():
    """批量设置分类或分析状态并返回逐项成功与失败结果。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    result = _admin_photo_management_service().batch_update(
        payload.get("action"),
        payload.get("items"),
        payload.get("value"),
        current_user.id,
        current_user.username,
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/photos/upload")
def upload_photos():
    """整批校验并原子保存最多十张图片，再创建 pending 分析任务。"""
    try:
        result = _upload_service().upload(request.files.getlist("photos"), int(current_user.id))
    except ValueError as error:
        raise ParameterError(str(error)) from error
    if request.accept_mimetypes.accept_html and not request.is_json:
        counts = result["counts"]
        flash(f"上传完成：接收 {counts['accepted']} 张，重复 {counts['duplicate']} 张")
        return redirect(url_for("admin.jobs"))
    return jsonify({"status": "ok", "data": result}), 201


@admin_api_blueprint.post("/jobs/backfill-content-hash")
def enqueue_content_hash_backfill():
    """安全创建低优先级历史最终文件摘要回填任务，重复任务自动跳过。"""
    payload = request.get_json(silent=True) or {}
    limit = payload.get("limit", 1000) if isinstance(payload, dict) else 1000
    try:
        result = _photo_job_service().enqueue_hash_backfill(int(current_user.id), int(limit))
    except (TypeError, ValueError) as error:
        raise ParameterError("limit 必须是整数") from error
    return jsonify({"status": "ok", "data": result}), 202


@admin_api_blueprint.get("/jobs")
def list_jobs_api():
    """返回后台任务列表。"""
    return jsonify({"status": "ok", "data": _admin_job_service().list_jobs()})


@admin_api_blueprint.post("/jobs/<queue>/<int:job_id>/cancel")
def cancel_job_api(queue: str, job_id: int):
    """按明确队列取消等待任务或请求运行任务协作取消。"""
    state = _admin_job_service().cancel(queue, job_id, int(current_user.id))
    return jsonify({"status": "ok", "data": {"state": state}})


@admin_api_blueprint.post("/jobs/<int:job_id>/cancel")
def cancel_photo_job_api_legacy(job_id: int):
    """兼容阶段五接口路径并取消照片分析任务。"""
    state = _admin_job_service().cancel("photo", job_id, int(current_user.id))
    return jsonify({"status": "ok", "data": {"state": state}})


@admin_api_blueprint.post("/jobs/<queue>/<int:job_id>/retry")
def retry_job_api(queue: str, job_id: int):
    """按明确队列重新排队合法终态任务。"""
    result = _admin_job_service().retry(queue, job_id, int(current_user.id))
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/jobs/<int:job_id>/retry")
def retry_photo_job_api_legacy(job_id: int):
    """兼容阶段五接口路径并重试照片分析任务。"""
    result = _admin_job_service().retry("photo", job_id, int(current_user.id))
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/photos/<int:photo_id>/reanalyze")
def reanalyze_photo_api(photo_id: int):
    """为单张照片排队完整重新分析，不清空旧业务字段。"""
    result = _photo_job_service().enqueue_analysis([photo_id], int(current_user.id))[0]
    return jsonify({"status": "ok", "data": result}), 202


@admin_api_blueprint.post("/photos/reanalyze")
def reanalyze_photos_api():
    """为最多一百张照片批量排队重新分析。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("photo_ids"), list):
        raise ParameterError("请求体必须包含 photo_ids 数组")
    result = _photo_job_service().enqueue_analysis(payload["photo_ids"], int(current_user.id))
    return jsonify({"status": "ok", "data": result}), 202


@admin_api_blueprint.post("/photos/<int:photo_id>/regenerate-narration")
def regenerate_narration_api(photo_id: int):
    """为单张照片排队重新生成旁白，失败时保留旧旁白。"""
    result = _photo_job_service().enqueue_narration(photo_id, int(current_user.id))
    return jsonify({"status": "ok", "data": result}), 202


@admin_page_blueprint.post("/photos/<int:photo_id>/trash")
def soft_delete_photo(photo_id: int):
    """把活动照片安全移入回收站并触发显示产物重渲染。"""
    _photo_lifecycle_service().soft_delete(
        photo_id,
        request.form.get("expected_version"),
        int(current_user.id),
        current_user.username,
    )
    flash("照片已移入回收站，旧显示产物已屏蔽并等待重渲染")
    return redirect(url_for("admin.trash"))


@admin_page_blueprint.get("/trash")
def trash():
    """渲染包含删除快照和操作入口的回收站分页页面。"""
    result = _photo_lifecycle_service().list_trash(
        _positive_integer_argument("page", 1),
        _positive_integer_argument("limit", 24),
    )
    result["previous_url"] = (
        url_for("admin.trash", page=result["page"] - 1, limit=result["limit"])
        if result["page"] > 1
        else None
    )
    result["next_url"] = (
        url_for("admin.trash", page=result["page"] + 1, limit=result["limit"])
        if result["page"] < result["total_pages"]
        else None
    )
    return render_template(
        "admin/trash.html",
        result=result,
        retention_days=_photo_lifecycle_service().retention_days,
    )


@admin_page_blueprint.post("/trash/<int:photo_id>/restore")
def restore_trash_photo(photo_id: int):
    """把回收站文件不覆盖地恢复至删除前位置。"""
    _photo_lifecycle_service().restore(
        photo_id,
        request.form.get("expected_version"),
        int(current_user.id),
        current_user.username,
    )
    flash("照片已恢复，显示产物已屏蔽并等待重渲染")
    return redirect(url_for("admin.trash"))


@admin_page_blueprint.route("/trash/<int:photo_id>/purge", methods=["GET", "POST"])
def confirm_purge_photo(photo_id: int):
    """使用独立确认页面和预期版本永久删除回收站照片。"""
    photo = _photo_lifecycle_service().get_trash_photo(photo_id)
    if request.method == "GET":
        return render_template("admin/purge_confirm.html", photo=photo)
    _photo_lifecycle_service().purge(
        photo_id,
        request.form.get("expected_version"),
        int(current_user.id),
        current_user.username,
        request.form.get("confirmation"),
    )
    flash("照片已永久删除")
    return redirect(url_for("admin.trash"))


@admin_page_blueprint.get("/trash/cleanup-preview")
def trash_cleanup_preview():
    """只读预览默认保留期限之前的回收站照片。"""
    preview = _photo_lifecycle_service().cleanup_preview(limit=100)
    return render_template("admin/trash_cleanup.html", preview=preview)


@admin_page_blueprint.post("/trash/cleanup")
def enqueue_trash_cleanup():
    """按明确截止时间和批量大小排队过期回收站清理。"""
    try:
        batch_size = int(request.form.get("batch_size", "100"))
    except (TypeError, ValueError) as error:
        raise ParameterError("batch_size 必须是整数") from error
    result = _photo_lifecycle_service().enqueue_cleanup(
        int(current_user.id),
        current_user.username,
        cutoff=request.form.get("cutoff") or None,
        batch_size=batch_size,
    )
    flash(f"清理任务已排队：维护任务 #{result['id']}")
    return redirect(url_for("admin.jobs"))


@admin_api_blueprint.get("/trash")
def list_trash_api():
    """返回受认证保护的回收站分页数据。"""
    result = _photo_lifecycle_service().list_trash(
        _positive_integer_argument("page", 1),
        _positive_integer_argument("limit", 24),
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.delete("/photos/<int:photo_id>")
@admin_api_blueprint.post("/photos/<int:photo_id>/trash")
def soft_delete_photo_api(photo_id: int):
    """按预期版本安全软删除活动照片。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    result = _photo_lifecycle_service().soft_delete(
        photo_id,
        payload.get("expected_version"),
        int(current_user.id),
        current_user.username,
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/photos/<int:photo_id>/restore")
@admin_api_blueprint.post("/trash/<int:photo_id>/restore")
def restore_trash_photo_api(photo_id: int):
    """按预期版本安全恢复回收站照片。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    result = _photo_lifecycle_service().restore(
        photo_id,
        payload.get("expected_version"),
        int(current_user.id),
        current_user.username,
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.delete("/trash/<int:photo_id>")
@admin_api_blueprint.post("/trash/<int:photo_id>/purge")
def purge_trash_photo_api(photo_id: int):
    """使用确认文本和预期版本永久删除回收站照片。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    result = _photo_lifecycle_service().purge(
        photo_id,
        payload.get("expected_version"),
        int(current_user.id),
        current_user.username,
        payload.get("confirmation"),
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.get("/trash/cleanup-preview")
def trash_cleanup_preview_api():
    """只读返回达到保留期限的稳定编号清理预览。"""
    result = _photo_lifecycle_service().cleanup_preview(
        cutoff=request.args.get("cutoff") or None,
        limit=_positive_integer_argument("limit", 100),
    )
    return jsonify({"status": "ok", "data": result})


@admin_api_blueprint.post("/trash/cleanup")
def enqueue_trash_cleanup_api():
    """排队独立过期回收站维护任务。"""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        raise ParameterError("请求体必须是 JSON 对象")
    try:
        batch_size = int(payload.get("batch_size", 100))
    except (TypeError, ValueError) as error:
        raise ParameterError("batch_size 必须是整数") from error
    result = _photo_lifecycle_service().enqueue_cleanup(
        int(current_user.id),
        current_user.username,
        cutoff=payload.get("cutoff"),
        batch_size=batch_size,
    )
    return jsonify({"status": "ok", "data": result}), 202
