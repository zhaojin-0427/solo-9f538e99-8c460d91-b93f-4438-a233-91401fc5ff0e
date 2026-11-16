"""HTTP routes: browser upload, batch inspection, mapping view, downloads."""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from werkzeug.utils import secure_filename

from . import services
from .config import HOSPITAL_CODE_REGEX, MAX_FILES_PER_BATCH
from .db import get_db
from .dicom_engine import DicomValidationError

bp = Blueprint("main", __name__)

_HOSPITAL_RE = re.compile(HOSPITAL_CODE_REGEX)


@bp.route("/")
def index():
    conn = get_db()
    return render_template(
        "index.html",
        hospitals=services.list_hospitals(conn),
        batches=services.list_batches(conn)[:20],
        max_files=MAX_FILES_PER_BATCH,
    )


@bp.route("/upload", methods=["POST"])
def upload():
    hospital_code = (request.form.get("hospital_code") or "").strip()
    if not _HOSPITAL_RE.match(hospital_code):
        flash("医院代号不合法：2-20 位字母、数字、下划线或短横线。", "error")
        return redirect(url_for("main.index"))

    incoming = request.files.getlist("dicom_files")
    incoming = [f for f in incoming if f and f.filename]
    if not incoming:
        flash("请至少选择一个 DICOM 文件。", "error")
        return redirect(url_for("main.index"))
    if len(incoming) > MAX_FILES_PER_BATCH:
        flash(f"每批最多上传 {MAX_FILES_PER_BATCH} 份文件，当前 {len(incoming)} 份。", "error")
        return redirect(url_for("main.index"))

    upload_root = os.path.join(current_app.config["STAGING_DIR"], "uploads")
    os.makedirs(upload_root, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix=f"up-{uuid.uuid4().hex[:8]}-", dir=upload_root)

    uploaded: list[services.UploadedFile] = []
    try:
        for index, file_storage in enumerate(incoming, start=1):
            original = file_storage.filename
            safe = secure_filename(original) or f"input_{index}.dcm"
            temp_path = os.path.join(work_dir, f"{index:02d}_{safe}")
            file_storage.save(temp_path)
            uploaded.append(
                services.UploadedFile(
                    original_filename=original, temp_path=temp_path
                )
            )

        conn = get_db()
        result = services.process_batch(conn, hospital_code, uploaded)
    except services.BatchConflictError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))
    except (services.UploadError, DicomValidationError) as exc:
        flash(f"批次被拒绝：{exc}", "error")
        return redirect(url_for("main.index"))
    except Exception:  # pragma: no cover - defensive, keeps uploads tidy
        current_app.logger.exception("batch processing failed")
        flash("服务器内部错误，该批次未发布。", "error")
        return redirect(url_for("main.index"))
    finally:
        # The work dir is request-scoped; the service only removes individual
        # files from it, so always drop the directory itself.
        shutil.rmtree(work_dir, ignore_errors=True)

    if result["reused_sops"]:
        flash(
            f"批次 {result['batch_code']} 已发布；其中 {len(result['reused_sops'])} "
            "个 SOP 为同字节重传，返回原成品。",
            "info",
        )
    else:
        flash(f"批次 {result['batch_code']} 已发布。", "success")
    return redirect(url_for("main.batch_detail", batch_code=result["batch_code"]))


@bp.route("/batches/<batch_code>")
def batch_detail(batch_code: str):
    conn = get_db()
    batch = services.get_batch(conn, batch_code)
    if batch is None:
        abort(404)
    items = services.list_batch_items(conn, batch["id"])
    mappings = services.list_mappings_for_batch(conn, batch["id"])
    return render_template(
        "batch.html",
        batch=batch,
        items=items,
        mappings=mappings,
    )


@bp.route("/hospitals/<hospital_code>/mappings")
def hospital_mappings(hospital_code: str):
    if not _HOSPITAL_RE.match(hospital_code):
        abort(404)
    conn = get_db()
    mappings = services.list_mappings_for_hospital(conn, hospital_code)
    if not mappings:
        # Distinguish unknown hospital from empty mapping table.
        hospital = conn.execute(
            "SELECT * FROM hospital WHERE code = ?", (hospital_code,)
        ).fetchone()
        if hospital is None:
            abort(404)
    batches = services.list_batches(conn, hospital_code)
    return render_template(
        "mappings.html",
        hospital_code=hospital_code,
        mappings=mappings,
        batches=batches,
    )


@bp.route("/download/<batch_code>/<int:ordinal>")
def download(batch_code: str, ordinal: int):
    conn = get_db()
    batch = services.get_batch(conn, batch_code)
    if batch is None:
        abort(404)
    items = services.list_batch_items(conn, batch["id"])
    item = next((row for row in items if row["ordinal"] == ordinal), None)
    if item is None:
        abort(404)
    path = services.published_path(batch_code, item["stored_name"])
    if not os.path.isfile(path):
        current_app.logger.error("published file missing on disk: %s", path)
        abort(404)
    return send_file(
        path,
        as_attachment=True,
        download_name=item["original_filename"],
        mimetype="application/dicom",
    )


@bp.errorhandler(413)
def too_large(_exc):
    flash("上传文件超过服务器允许的大小上限。", "error")
    return redirect(url_for("main.index"))
