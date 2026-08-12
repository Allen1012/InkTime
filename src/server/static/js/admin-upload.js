"use strict";

/**
 * 后台上传页交互：拖拽收集文件、缩略图预览、逐项移除，以及逐张进度上传。
 *
 * 约定与限制：
 * - 拖拽是渐进增强，原生 input 保持可聚焦，键盘与点击方式始终可用；
 * - input.files 是唯一数据源，移除文件时通过 DataTransfer 重建，保证
 *   原生 required 校验与提交内容一致；
 * - 请求体必须显式 append。不能用 new FormData(form)，因为上传期间会禁用
 *   文件输入，而 FormData 会跳过 disabled 控件，导致一个文件都提交不上去；
 * - 上传进度只能通过 XMLHttpRequest.upload 获取，且单次请求只有整批字节数。
 *   multipart 按文件顺序推送，因此把整批已传字节按顺序分摊到各文件，即可
 *   得到每张照片的真实完成度；
 * - 文件名与服务端消息都来自不可信输入，一律用 textContent 写入。
 */
(function () {
    var form = document.getElementById("upload-form");
    if (!form) {
        return;
    }

    var input = document.getElementById("upload-input");
    var dropzone = document.getElementById("upload-dropzone");
    var list = document.getElementById("upload-list");
    var submit = form.querySelector(".upload-submit");
    var feedback = document.getElementById("upload-feedback");

    var maxFiles = parseInt(form.getAttribute("data-max-files"), 10) || 10;
    var maxBytes = parseInt(form.getAttribute("data-max-bytes"), 10) || 20 * 1024 * 1024;
    var jobsUrl = form.getAttribute("data-jobs-url") || "/admin/jobs";
    var allowedTypes = ["image/jpeg", "image/png", "image/webp"];
    var statusLabels = { accepted: "已接收", duplicate: "重复跳过", failed: "失败" };

    var previewUrls = [];
    var itemRefs = [];
    var uploadingFiles = [];
    var uploading = false;

    function formatSize(bytes) {
        if (bytes >= 1048576) {
            return (bytes / 1048576).toFixed(1) + " MiB";
        }
        return Math.max(1, Math.round(bytes / 1024)) + " KiB";
    }

    function setFeedback(message, kind) {
        feedback.textContent = "";
        feedback.className = "upload-feedback" + (kind ? " is-" + kind : "");
        feedback.hidden = !message;
        if (message) {
            feedback.appendChild(document.createTextNode(message));
        }
    }

    function appendJobsLink() {
        var link = document.createElement("a");
        link.href = jobsUrl;
        link.textContent = "查看任务列表";
        feedback.appendChild(document.createTextNode(" "));
        feedback.appendChild(link);
    }

    function releasePreviews() {
        previewUrls.forEach(function (url) {
            URL.revokeObjectURL(url);
        });
        previewUrls = [];
    }

    /** 用 DataTransfer 重建 input.files，使其与界面展示的集合保持一致。 */
    function commitFiles(files) {
        var transfer = new DataTransfer();
        files.forEach(function (file) {
            transfer.items.add(file);
        });
        input.files = transfer.files;
        renderList();
    }

    function currentFiles() {
        return Array.prototype.slice.call(input.files || []);
    }

    function removeAt(index) {
        var files = currentFiles();
        files.splice(index, 1);
        commitFiles(files);
    }

    function renderList() {
        releasePreviews();
        list.textContent = "";
        itemRefs = [];
        var files = currentFiles();
        if (!files.length) {
            return;
        }

        files.forEach(function (file, index) {
            var item = document.createElement("li");
            item.className = "upload-item";

            var thumbnail = document.createElement("span");
            thumbnail.className = "upload-thumb";
            if (allowedTypes.indexOf(file.type) !== -1) {
                var url = URL.createObjectURL(file);
                previewUrls.push(url);
                var image = document.createElement("img");
                image.src = url;
                image.alt = "";
                thumbnail.appendChild(image);
            }
            item.appendChild(thumbnail);

            var meta = document.createElement("span");
            meta.className = "upload-item-meta";

            var name = document.createElement("span");
            name.className = "upload-item-name";
            name.textContent = file.name;
            meta.appendChild(name);

            var detail = document.createElement("span");
            detail.className = "upload-item-detail";

            var size = document.createElement("span");
            size.className = "muted upload-item-size";
            size.textContent = formatSize(file.size);
            detail.appendChild(size);

            // 每张照片一条进度条，默认隐藏，提交后显示
            var track = document.createElement("span");
            track.className = "progress upload-item-track";
            track.setAttribute("role", "progressbar");
            track.setAttribute("aria-valuemin", "0");
            track.setAttribute("aria-valuemax", "100");
            track.setAttribute("aria-valuenow", "0");
            track.setAttribute("aria-label", file.name + " 上传进度");
            var fill = document.createElement("span");
            fill.className = "progress-bar";
            fill.style.width = "0%";
            track.appendChild(fill);

            var percent = document.createElement("span");
            percent.className = "progress-text upload-item-percent";
            percent.textContent = "0%";

            var status = document.createElement("span");
            status.className = "upload-item-status";
            status.hidden = true;

            detail.appendChild(track);
            detail.appendChild(percent);
            detail.appendChild(status);
            meta.appendChild(detail);
            item.appendChild(meta);

            var remove = document.createElement("button");
            remove.type = "button";
            remove.className = "upload-item-remove";
            remove.title = "移除 " + file.name;
            remove.setAttribute("aria-label", "移除 " + file.name);
            remove.textContent = "×";
            remove.addEventListener("click", function () {
                if (!uploading) {
                    removeAt(index);
                }
            });
            item.appendChild(remove);

            list.appendChild(item);
            itemRefs.push({
                root: item, track: track, fill: fill,
                percent: percent, status: status, remove: remove,
            });
            // 进度相关元素在提交前不占位
            track.hidden = true;
            percent.hidden = true;
        });
    }

    /** 客户端预检，服务端仍会完整校验整批文件。 */
    function validate(files) {
        if (!files.length) {
            return "请选择至少一张照片";
        }
        if (files.length > maxFiles) {
            return "每批最多 " + maxFiles + " 张，当前选择了 " + files.length + " 张";
        }
        for (var index = 0; index < files.length; index += 1) {
            var file = files[index];
            if (allowedTypes.indexOf(file.type) === -1) {
                return file.name + "：只支持 JPEG、PNG 和 WebP";
            }
            if (file.size > maxBytes) {
                return file.name + "：超过单张上限 " + formatSize(maxBytes);
            }
        }
        return "";
    }

    function setItemProgress(index, value) {
        var refs = itemRefs[index];
        if (!refs) {
            return;
        }
        var percent = Math.max(0, Math.min(100, Math.round(value)));
        refs.fill.style.width = percent + "%";
        refs.track.setAttribute("aria-valuenow", String(percent));
        refs.percent.textContent = percent + "%";
    }

    /**
     * 把整批已上传字节按文件顺序分摊到每张照片。
     * event.total 含 multipart 边界与字段开销，先折算回纯文件字节再分摊。
     */
    function distributeProgress(loaded, total) {
        var sumSizes = uploadingFiles.reduce(function (sum, file) {
            return sum + file.size;
        }, 0);
        var effective = total > 0 && sumSizes > 0 ? loaded * (sumSizes / total) : loaded;
        var offset = 0;
        uploadingFiles.forEach(function (file, index) {
            var done = Math.min(Math.max(effective - offset, 0), file.size);
            setItemProgress(index, file.size ? (done / file.size) * 100 : 100);
            offset += file.size;
        });
    }

    function showItemStatus(index, kind, message) {
        var refs = itemRefs[index];
        if (!refs) {
            return;
        }
        refs.track.hidden = true;
        refs.percent.hidden = true;
        refs.status.hidden = false;
        refs.status.className = "upload-item-status is-" + kind;
        // 失败项直接把服务端给出的原因写在文件名后面，不必去翻日志或猜
        refs.status.textContent = message
            ? (statusLabels[kind] || kind) + "：" + message
            : (statusLabels[kind] || kind);
        if (message) {
            refs.status.title = message;
        }
        refs.root.classList.add("is-" + kind);
    }

    function setUploading(active) {
        uploading = active;
        submit.disabled = active;
        input.disabled = active;
        dropzone.classList.toggle("is-disabled", active);
        itemRefs.forEach(function (refs, index) {
            refs.remove.disabled = active;
            if (active) {
                refs.track.hidden = false;
                refs.percent.hidden = false;
                refs.status.hidden = true;
                setItemProgress(index, 0);
            }
        });
    }

    function describeError(xhr) {
        try {
            var payload = JSON.parse(xhr.responseText);
            if (payload && payload.error && payload.error.message) {
                return payload.error.message;
            }
            if (payload && payload.message) {
                return payload.message;
            }
        } catch (error) {
            // 响应不是 JSON 时回退到状态码描述。
        }
        if (xhr.status === 413) {
            return "整批文件过大，已被服务器拒绝";
        }
        return "上传失败（HTTP " + xhr.status + "）";
    }

    function summarize(data) {
        var counts = (data && data.counts) || {};
        var accepted = counts.accepted || 0;
        var duplicate = counts.duplicate || 0;
        var failed = counts.failed || 0;
        var parts = ["成功接收 " + accepted + " 张"];
        if (duplicate) {
            parts.push("重复跳过 " + duplicate + " 张");
        }
        if (failed) {
            parts.push("失败 " + failed + " 张（见下方逐条说明）");
        }
        return parts.join("，") + (accepted ? "，已排队分析。" : "。");
    }

    /** 服务端返回与输入同序的逐项结果，按序标注并用文件名兜底校对。 */
    function applyResults(items) {
        if (!items || !items.length) {
            return;
        }
        items.forEach(function (result, index) {
            var target = index;
            var expected = uploadingFiles[index];
            if (expected && result.original_filename && result.original_filename !== expected.name) {
                for (var probe = 0; probe < uploadingFiles.length; probe += 1) {
                    if (uploadingFiles[probe].name === result.original_filename) {
                        target = probe;
                        break;
                    }
                }
            }
            showItemStatus(target, result.status || "accepted", result.message);
        });
    }

    dropzone.addEventListener("dragover", function (event) {
        event.preventDefault();
        if (!uploading) {
            dropzone.classList.add("is-dragover");
        }
    });

    ["dragleave", "dragend"].forEach(function (name) {
        dropzone.addEventListener(name, function () {
            dropzone.classList.remove("is-dragover");
        });
    });

    dropzone.addEventListener("drop", function (event) {
        event.preventDefault();
        dropzone.classList.remove("is-dragover");
        if (uploading || !event.dataTransfer) {
            return;
        }
        var dropped = Array.prototype.slice.call(event.dataTransfer.files || []);
        if (!dropped.length) {
            return;
        }
        var merged = currentFiles().concat(dropped);
        if (merged.length > maxFiles) {
            setFeedback("每批最多 " + maxFiles + " 张，多余的文件已忽略", "warning");
            merged = merged.slice(0, maxFiles);
        } else {
            setFeedback("");
        }
        commitFiles(merged);
    });

    input.addEventListener("change", function () {
        setFeedback("");
        renderList();
    });

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (uploading) {
            return;
        }
        var files = currentFiles();
        var problem = validate(files);
        if (problem) {
            setFeedback(problem, "error");
            return;
        }

        uploadingFiles = files;

        // 显式构建请求体，避免禁用文件输入后 FormData 跳过该控件
        var payload = new FormData();
        var csrfField = form.querySelector('input[name="csrf_token"]');
        if (csrfField) {
            payload.append("csrf_token", csrfField.value);
        }
        files.forEach(function (file) {
            payload.append("photos", file);
        });

        var xhr = new XMLHttpRequest();
        xhr.open("POST", form.getAttribute("action"), true);
        xhr.setRequestHeader("Accept", "application/json");
        xhr.upload.addEventListener("progress", function (event) {
            if (event.lengthComputable) {
                distributeProgress(event.loaded, event.total);
            }
        });
        xhr.addEventListener("load", function () {
            uploadingFiles.forEach(function (file, index) {
                setItemProgress(index, 100);
            });
            setUploading(false);
            if (xhr.status >= 200 && xhr.status < 300) {
                var body = {};
                try {
                    body = JSON.parse(xhr.responseText) || {};
                } catch (error) {
                    body = {};
                }
                var data = body.data || {};
                applyResults(data.items);
                setFeedback(summarize(data), "success");
                appendJobsLink();
                // 结果已逐项标注在卡片上，清空输入以便继续下一批
                commitFilesKeepingResults();
                return;
            }
            setFeedback(describeError(xhr), "error");
        });
        xhr.addEventListener("error", function () {
            setUploading(false);
            setFeedback("网络错误，上传未完成", "error");
        });
        xhr.addEventListener("abort", function () {
            setUploading(false);
            setFeedback("上传已取消", "warning");
        });

        setFeedback("");
        setUploading(true);
        xhr.send(payload);
    });

    /** 清空待上传集合但保留已标注结果的卡片，供用户核对本批结果。 */
    function commitFilesKeepingResults() {
        var transfer = new DataTransfer();
        input.files = transfer.files;
        itemRefs.forEach(function (refs) {
            refs.remove.disabled = true;
            refs.remove.hidden = true;
        });
        itemRefs = [];
    }

    renderList();
})();
