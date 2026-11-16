"""应收 SOP 清单（manifest）管理与到货核验。

质控流程
--------
1. 按 ``(医院代号, 原检查 UID)`` 登记应收 SOP Instance UID 清单
   （单院最多 ``MAX_MANIFESTS_PER_HOSPITAL`` 份，每份最多
   ``MAX_MANIFEST_INSTANCES`` 个实例）。清单可整份替换，持久化于 SQLite，
   重启后保留。
2. 核验（verify）在**同一次读事务快照**内取数，保证上传、清单修改与核验
   交错时结果只来自同一个已提交状态。判定规则：
   - 清单内实例须已入仓（存在已发布成品）且归属正确（成品的原检查 UID
     等于清单检查 UID，医院由数据范围天然隔离）；
   - 清单实例的嵌套 SOP 引用须能在同院"相应检查清单"内找到目标：目标已
     入仓、目标所属检查存在清单且目标列于其中；引用声明的 SOP 类须与目标
     实际 SOP 类一致；
   - 目标检查递归满足相同条件；引用成环本身不算缺件（访问集合防死循环）；
   - 成品文件须真实存在于 published/。
3. 全部满足时给出可供数（递归闭包内的去重实例数）并提供递归涉及转换件
   的 ZIP 下载；否则按 缺件 / 错属 / 类不符 / 成品缺失 四类逐条定位。

清单与核验结果不落缓存：每次请求实时重判，因此目标清单改变或新件入仓后，
引用方资格与可供数随之更新。清单替换与核验读取均为单事务，失败不改变已有
清单及成品。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import services
from .config import MAX_MANIFEST_INSTANCES, MAX_MANIFESTS_PER_HOSPITAL
from .uidmapper import is_legal_uid


class ManifestError(Exception):
    """客户端输入或限额问题 -> HTTP 400；已有清单与成品不受影响。"""


class ManifestNotFound(Exception):
    """医院或清单不存在 -> HTTP 404。"""


# Defect categories (kept in Chinese: they appear verbatim in the QC report).
CAT_MISSING = "缺件"        # 应收实例/引用目标未入仓，或目标不在相应清单内
CAT_WRONG_STUDY = "错属"    # 实例已入仓但归属检查与清单不符
CAT_CLASS = "类不符"        # 引用声明类与目标实际 SOP 类不一致
CAT_NO_PRODUCT = "成品缺失"  # 登记在册但 published/ 中的成品文件丢失


@dataclass
class Defect:
    category: str
    location: str
    message: str


@dataclass
class SupplyItem:
    sop_uid_old: str
    sop_uid_new: str
    study_uid_old: str
    batch_code: str
    stored_name: str
    original_filename: str
    file_sha256: str


@dataclass
class Verdict:
    hospital_code: str
    study_uid_old: str
    ok: bool
    defects: list[Defect]
    supply: list[SupplyItem]
    own_items: list[str] = field(default_factory=list)

    @property
    def supply_count(self) -> int:
        return len(self.supply)

    @property
    def supply_by_study(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.supply:
            counts[item.study_uid_old] = counts.get(item.study_uid_old, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# 清单录入 / 替换 / 删除
# ---------------------------------------------------------------------------

def parse_sop_uid_text(text: str) -> list[str]:
    """Split free-form textarea input into a deduplicated, ordered UID list."""
    tokens = re.split(r"[\s,;]+", text or "")
    uids: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        token = token.strip()
        if not token or token in seen:
            continue
        seen.add(token)
        uids.append(token)
    return uids


def replace_manifest(conn: sqlite3.Connection, hospital_code: str,
                     study_uid: str, sop_uids: list[str]) -> int:
    """Create or wholesale-replace one manifest atomically.

    All validation happens before/inside a single immediate transaction; any
    failure rolls back so the previously stored list is untouched.
    """
    if not is_legal_uid(study_uid):
        raise ManifestError(f"原检查 UID 不合法：{study_uid!r}")
    if not sop_uids:
        raise ManifestError("清单至少包含 1 个 SOP Instance UID。")
    if len(sop_uids) > MAX_MANIFEST_INSTANCES:
        raise ManifestError(
            f"单份清单最多 {MAX_MANIFEST_INSTANCES} 个实例，当前 {len(sop_uids)} 个。"
        )
    bad = [uid for uid in sop_uids if not is_legal_uid(uid)]
    if bad:
        shown = "、".join(bad[:3]) + (" …" if len(bad) > 3 else "")
        raise ManifestError(f"发现 {len(bad)} 个非法 SOP UID：{shown}")

    # The process-wide batch lock serializes this write with batch
    # publication; BEGIN IMMEDIATE then makes the limit check and the write
    # one atomic step against any other writer.
    with services._BATCH_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            hospital_id = services._get_or_create_hospital(conn, hospital_code)
            row = conn.execute(
                "SELECT id FROM manifest WHERE hospital_id = ? AND study_uid_old = ?",
                (hospital_id, study_uid),
            ).fetchone()
            if row is None:
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM manifest WHERE hospital_id = ?",
                    (hospital_id,),
                ).fetchone()["c"]
                if count >= MAX_MANIFESTS_PER_HOSPITAL:
                    raise ManifestError(
                        f"单院最多 {MAX_MANIFESTS_PER_HOSPITAL} 份清单，已达上限。"
                    )
                cur = conn.execute(
                    "INSERT INTO manifest(hospital_id, study_uid_old) VALUES (?, ?)",
                    (hospital_id, study_uid),
                )
                manifest_id = int(cur.lastrowid)
            else:
                manifest_id = row["id"]
                conn.execute(
                    "DELETE FROM manifest_item WHERE manifest_id = ?", (manifest_id,)
                )
                conn.execute(
                    "UPDATE manifest SET updated_at = ? WHERE id = ?",
                    (
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        manifest_id,
                    ),
                )
            for ordinal, uid in enumerate(sop_uids, start=1):
                conn.execute(
                    "INSERT INTO manifest_item(manifest_id, ordinal, sop_uid_old) "
                    "VALUES (?, ?, ?)",
                    (manifest_id, ordinal, uid),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return manifest_id


def delete_manifest(conn: sqlite3.Connection, hospital_code: str,
                    study_uid: str) -> bool:
    """Delete one manifest (and its items) atomically.  Returns False if absent."""
    with services._BATCH_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT m.id FROM manifest m JOIN hospital h ON h.id = m.hospital_id "
                "WHERE h.code = ? AND m.study_uid_old = ?",
                (hospital_code, study_uid),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            conn.execute(
                "DELETE FROM manifest_item WHERE manifest_id = ?", (row["id"],)
            )
            conn.execute("DELETE FROM manifest WHERE id = ?", (row["id"],))
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def list_manifests(conn: sqlite3.Connection, hospital_code: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.*, "
        "(SELECT COUNT(*) FROM manifest_item mi WHERE mi.manifest_id = m.id) "
        "AS item_count "
        "FROM manifest m JOIN hospital h ON h.id = m.hospital_id "
        "WHERE h.code = ? ORDER BY m.updated_at DESC, m.id DESC",
        (hospital_code,),
    ).fetchall()


def get_manifest(conn: sqlite3.Connection, hospital_code: str,
                 study_uid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT m.*, h.code AS hospital_code "
        "FROM manifest m JOIN hospital h ON h.id = m.hospital_id "
        "WHERE h.code = ? AND m.study_uid_old = ?",
        (hospital_code, study_uid),
    ).fetchone()


def list_manifest_items(conn: sqlite3.Connection, manifest_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM manifest_item WHERE manifest_id = ? ORDER BY ordinal",
        (manifest_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------

def verify_manifest(conn: sqlite3.Connection, hospital_code: str,
                    study_uid: str) -> Verdict:
    """Verify one manifest recursively against the archived products.

    Every database read happens inside a single snapshot transaction, so an
    interleaved upload or manifest edit cannot produce a torn verdict: the
    result reflects exactly one committed state.
    """
    conn.rollback()  # defensive: this path is read-only, drop any pending tx
    conn.execute("BEGIN")
    try:
        hospital = conn.execute(
            "SELECT * FROM hospital WHERE code = ?", (hospital_code,)
        ).fetchone()
        if hospital is None:
            raise ManifestNotFound(hospital_code)
        hospital_id = hospital["id"]

        manifest_rows = conn.execute(
            "SELECT id, study_uid_old FROM manifest WHERE hospital_id = ?",
            (hospital_id,),
        ).fetchall()
        id_to_study = {row["id"]: row["study_uid_old"] for row in manifest_rows}
        if study_uid not in id_to_study.values():
            raise ManifestNotFound(study_uid)

        item_rows = conn.execute(
            "SELECT mi.manifest_id, mi.sop_uid_old FROM manifest_item mi "
            "JOIN manifest m ON m.id = mi.manifest_id "
            "WHERE m.hospital_id = ? ORDER BY mi.ordinal",
            (hospital_id,),
        ).fetchall()
        # study_uid -> ordered expected SOP UIDs (whole hospital, one snapshot)
        manifests: dict[str, list[str]] = {s: [] for s in id_to_study.values()}
        for row in item_rows:
            manifests[id_to_study[row["manifest_id"]]].append(row["sop_uid_old"])

        product_rows = conn.execute(
            "SELECT bf.sop_uid_old, bf.sop_uid_new, bf.study_uid_old, "
            "bf.sop_class_uid, bf.stored_name, bf.original_filename, "
            "bf.file_sha256, b.batch_code "
            "FROM batch_file bf JOIN batch b ON b.id = bf.created_batch_id "
            "WHERE bf.hospital_id = ? AND b.status = 'published'",
            (hospital_id,),
        ).fetchall()
        products = {row["sop_uid_old"]: row for row in product_rows}

        ref_rows = conn.execute(
            "SELECT sop_uid_old, ref_sop_uid_old, ref_sop_class_uid "
            "FROM sop_reference WHERE hospital_id = ?",
            (hospital_id,),
        ).fetchall()
        references: dict[str, list[tuple[str, str | None]]] = {}
        for row in ref_rows:
            references.setdefault(row["sop_uid_old"], []).append(
                (row["ref_sop_uid_old"], row["ref_sop_class_uid"])
            )
    finally:
        conn.rollback()  # release the read snapshot

    defects: list[Defect] = []
    _seen_defects: set[tuple[str, str, str]] = set()

    def add_defect(category: str, location: str, message: str) -> None:
        key = (category, location, message)
        if key not in _seen_defects:
            _seen_defects.add(key)
            defects.append(Defect(category, location, message))

    supply: dict[str, SupplyItem] = {}
    visited_studies: set[str] = set()
    worklist = [study_uid]
    while worklist:
        study = worklist.pop()
        if study in visited_studies:
            continue
        visited_studies.add(study)
        members = manifests.get(study)
        if members is None:
            add_defect(
                CAT_MISSING,
                f"检查 {study}",
                "被引用的检查在同院没有对应应收清单",
            )
            continue
        for sop in members:
            product = products.get(sop)
            if product is None:
                add_defect(
                    CAT_MISSING,
                    f"清单[{study}] 实例 {sop}",
                    "应收实例未入仓",
                )
                continue
            if product["study_uid_old"] != study:
                add_defect(
                    CAT_WRONG_STUDY,
                    f"清单[{study}] 实例 {sop}",
                    f"实例实际归属检查 {product['study_uid_old'] or '<未登记>'}，"
                    f"与清单检查 {study} 不符",
                )
                continue
            supply[sop] = SupplyItem(
                sop_uid_old=sop,
                sop_uid_new=product["sop_uid_new"],
                study_uid_old=study,
                batch_code=product["batch_code"],
                stored_name=product["stored_name"],
                original_filename=product["original_filename"],
                file_sha256=product["file_sha256"],
            )
            for target, ref_class in references.get(sop, []):
                target_product = products.get(target)
                if target_product is None:
                    add_defect(
                        CAT_MISSING,
                        f"实例 {sop}（检查 {study}）的引用",
                        f"被引用实例 {target} 未入仓",
                    )
                    continue
                target_study = target_product["study_uid_old"]
                if target_study is None:
                    add_defect(
                        CAT_WRONG_STUDY,
                        f"实例 {sop} 引用的目标 {target}",
                        "目标实例缺少归属检查信息（同字节重传一次即可补登）",
                    )
                    continue
                target_members = manifests.get(target_study)
                if target_members is None:
                    add_defect(
                        CAT_MISSING,
                        f"实例 {sop} 引用的目标 {target}",
                        f"目标所属检查 {target_study} 在同院没有对应应收清单",
                    )
                    continue
                if target not in target_members:
                    add_defect(
                        CAT_MISSING,
                        f"实例 {sop} 引用的目标 {target}",
                        f"目标不在检查 {target_study} 的应收清单内",
                    )
                    continue
                actual_class = target_product["sop_class_uid"]
                if ref_class:
                    if actual_class is None:
                        add_defect(
                            CAT_CLASS,
                            f"实例 {sop} 引用的目标 {target}",
                            "目标成品未登记 SOP 类（同字节重传一次即可补登），"
                            "无法核对引用类",
                        )
                    elif ref_class != actual_class:
                        add_defect(
                            CAT_CLASS,
                            f"实例 {sop} 引用的目标 {target}",
                            f"引用声明类 {ref_class} 与目标实际类 {actual_class} 不一致",
                        )
                # 目标已入仓且在其检查清单内：目标检查须递归满足相同条件。
                # 成环时 visited_studies 终止展开，本身不算缺件。
                if target_study not in visited_studies:
                    worklist.append(target_study)

    # 成品文件须真实存在于 published/（DB 快照之外的不变文件，直接 stat）。
    for sop, item in supply.items():
        path = services.published_path(item.batch_code, item.stored_name)
        if not os.path.isfile(path):
            add_defect(
                CAT_NO_PRODUCT,
                f"实例 {sop}（检查 {item.study_uid_old}）",
                f"成品文件缺失：published/{item.batch_code}/{item.stored_name}",
            )

    ok = not defects
    return Verdict(
        hospital_code=hospital_code,
        study_uid_old=study_uid,
        ok=ok,
        defects=defects,
        supply=(
            sorted(supply.values(), key=lambda it: (it.study_uid_old, it.sop_uid_old))
            if ok
            else []
        ),
        own_items=list(manifests.get(study_uid, [])),
    )


# ---------------------------------------------------------------------------
# 供数下载
# ---------------------------------------------------------------------------

def build_supply_zip(verdict: Verdict) -> tempfile.SpooledTemporaryFile:
    """Pack every converted product in the verified recursive closure.

    The ZIP contains one ``<新 SOP UID>.dcm`` per instance plus a
    ``manifest.json`` documenting old->new UIDs, source batches and hashes.
    A spooled file keeps small archives in memory and rolls large ones to
    disk transparently.
    """
    if not verdict.ok:
        raise ManifestError("核验未通过，不能生成供数包。")
    spooled = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    with zipfile.ZipFile(spooled, "w", zipfile.ZIP_STORED) as archive:
        doc = {
            "hospital_code": verdict.hospital_code,
            "study_uid_old": verdict.study_uid_old,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "supply_count": len(verdict.supply),
            "items": [
                {
                    "sop_uid_old": item.sop_uid_old,
                    "sop_uid_new": item.sop_uid_new,
                    "study_uid_old": item.study_uid_old,
                    "batch_code": item.batch_code,
                    "original_filename": item.original_filename,
                    "file_sha256": item.file_sha256,
                }
                for item in verdict.supply
            ],
        }
        archive.writestr(
            "manifest.json", json.dumps(doc, ensure_ascii=False, indent=2)
        )
        for item in verdict.supply:
            path = services.published_path(item.batch_code, item.stored_name)
            archive.write(path, arcname=f"{item.sop_uid_new}.dcm")
    spooled.seek(0)
    return spooled
