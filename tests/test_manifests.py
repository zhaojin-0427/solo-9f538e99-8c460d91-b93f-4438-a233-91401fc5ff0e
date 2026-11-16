"""Tests for the QC expected-SOP manifest feature (清单维护/核验/供数)."""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import zipfile

import pydicom
from pydicom.uid import CTImageStorage, MRImageStorage, generate_uid

from app import create_app
from app import manifests as manifest_svc
from app.config import MAX_MANIFEST_INSTANCES, MAX_MANIFESTS_PER_HOSPITAL
from app.uidmapper import derive_new_uid
from tests.dicom_factory import make_ct_file


class ManifestTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "data")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.data_dir)
        os.makedirs(self.work)
        self.app = create_app(
            {
                "TESTING": True,
                "DICOM_DATA_DIR": self.data_dir,
                "STAGING_DIR": os.path.join(self.data_dir, "staging"),
                "PUBLISHED_DIR": os.path.join(self.data_dir, "published"),
                "DB_PATH": os.path.join(self.data_dir, "registry.db"),
            }
        )
        self.client = self.app.test_client()
        self.hospital = "HOSP01"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def _db(self):
        conn = sqlite3.connect(
            os.path.join(self.data_dir, "registry.db"), timeout=30
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _make_file(self, name, study_uid, sop_uid=None, referenced_sop_uid=None,
                   referenced_sop_class_uid=CTImageStorage, seed=11):
        sop_uid = sop_uid or generate_uid()
        path = os.path.join(self.work, name)
        make_ct_file(
            path,
            sop_uid=sop_uid,
            study_uid=study_uid,
            referenced_sop_uid=referenced_sop_uid or sop_uid,  # default: self-ref
            referenced_sop_class_uid=referenced_sop_class_uid,
            pixel_seed=seed,
        )
        return path, sop_uid

    def _upload(self, paths, hospital=None):
        files = []
        for p in paths:
            with open(p, "rb") as fh:
                files.append((io.BytesIO(fh.read()), os.path.basename(p)))
        data = {"hospital_code": hospital or self.hospital, "dicom_files": files}
        response = self.client.post(
            "/upload", data=data, content_type="multipart/form-data"
        )
        self.assertEqual(response.status_code, 302, response.data[:400])
        return response.headers["Location"].rstrip("/").split("/")[-1]

    def _save(self, hospital, study_uid, sop_uids):
        return self.client.post(
            f"/hospitals/{hospital}/manifests",
            data={"study_uid": study_uid, "sop_uids": "\n".join(sop_uids)},
        )

    def _verify(self, hospital, study_uid):
        conn = self._db()
        try:
            # Service layer reads paths from current_app, like the routes do.
            with self.app.app_context():
                return manifest_svc.verify_manifest(conn, hospital, study_uid)
        finally:
            conn.close()

    def _defect_text(self, verdict):
        return " | ".join(
            f"{d.category}:{d.location}:{d.message}" for d in verdict.defects
        )


class ManifestVerifyTests(ManifestTestBase):
    def test_verify_ok_and_download_recursive_supply(self):
        study_a, study_b = generate_uid(), generate_uid()
        sop_a = generate_uid()
        fb, sop_b = self._make_file("b.dcm", study_b)
        # a references b (cross-study); b references itself (1-cycle).
        fa, _ = self._make_file("a.dcm", study_a, sop_uid=sop_a,
                                referenced_sop_uid=sop_b)
        self._upload([fa])
        self._upload([fb])
        self.assertEqual(self._save(self.hospital, study_a, [sop_a]).status_code, 302)
        self.assertEqual(self._save(self.hospital, study_b, [sop_b]).status_code, 302)

        verdict = self._verify(self.hospital, study_a)
        self.assertTrue(verdict.ok, self._defect_text(verdict))
        self.assertEqual(verdict.supply_count, 2)
        self.assertEqual(
            {item.sop_uid_old for item in verdict.supply}, {sop_a, sop_b}
        )

        page = self.client.get(f"/hospitals/{self.hospital}/manifests/{study_a}")
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("可供 2 份", body)

        resp = self.client.get(
            f"/hospitals/{self.hospital}/manifests/{study_a}/download"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        archive = zipfile.ZipFile(io.BytesIO(resp.data))
        names = archive.namelist()
        dcm_names = [n for n in names if n.endswith(".dcm")]
        self.assertEqual(len(dcm_names), 2)
        self.assertIn("manifest.json", names)

        doc = json.loads(archive.read("manifest.json"))
        self.assertEqual(doc["hospital_code"], self.hospital)
        self.assertEqual(doc["study_uid_old"], study_a)
        self.assertEqual(doc["supply_count"], 2)
        self.assertEqual({i["sop_uid_old"] for i in doc["items"]}, {sop_a, sop_b})

        # Entries are the converted products: new UID names, original pixels.
        new_a = derive_new_uid(self.hospital, sop_a)
        self.assertIn(f"{new_a}.dcm", dcm_names)
        converted = pydicom.dcmread(io.BytesIO(archive.read(f"{new_a}.dcm")))
        self.assertEqual(str(converted.SOPInstanceUID), new_a)
        original = pydicom.dcmread(fa)
        self.assertEqual(bytes(converted.PixelData), bytes(original.PixelData))
        # Releases the spooled temp archive via the on-close hook.
        resp.close()

    def test_missing_instance_reported(self):
        study = generate_uid()
        path, sop = self._make_file("m1.dcm", study)
        self._upload([path])
        missing = generate_uid()
        self._save(self.hospital, study, [sop, missing])

        verdict = self._verify(self.hospital, study)
        self.assertFalse(verdict.ok)
        self.assertIn(manifest_svc.CAT_MISSING, self._defect_text(verdict))
        self.assertIn(missing, self._defect_text(verdict))

        page = self.client.get(f"/hospitals/{self.hospital}/manifests/{study}")
        self.assertIn("缺件", page.get_data(as_text=True))
        # Download is refused while defects exist.
        resp = self.client.get(f"/hospitals/{self.hospital}/manifests/{study}/download")
        self.assertEqual(resp.status_code, 302)

    def test_wrong_attribution_reported(self):
        real_study, listed_study = generate_uid(), generate_uid()
        path, sop = self._make_file("w1.dcm", real_study)
        self._upload([path])
        self._save(self.hospital, listed_study, [sop])

        verdict = self._verify(self.hospital, listed_study)
        self.assertFalse(verdict.ok)
        text = self._defect_text(verdict)
        self.assertIn(manifest_svc.CAT_WRONG_STUDY, text)
        self.assertIn(real_study, text)

    def test_reference_target_not_archived(self):
        study = generate_uid()
        ghost = generate_uid()
        path, sop = self._make_file("r1.dcm", study, referenced_sop_uid=ghost)
        self._upload([path])
        self._save(self.hospital, study, [sop])

        verdict = self._verify(self.hospital, study)
        self.assertFalse(verdict.ok)
        text = self._defect_text(verdict)
        self.assertIn(manifest_svc.CAT_MISSING, text)
        self.assertIn(ghost, text)

    def test_reference_target_study_without_manifest(self):
        study_a, study_b = generate_uid(), generate_uid()
        fb, sop_b = self._make_file("tb.dcm", study_b)
        fa, sop_a = self._make_file("ta.dcm", study_a, referenced_sop_uid=sop_b)
        self._upload([fa])
        self._upload([fb])
        self._save(self.hospital, study_a, [sop_a])
        # No manifest for study_b.

        verdict = self._verify(self.hospital, study_a)
        self.assertFalse(verdict.ok)
        text = self._defect_text(verdict)
        self.assertIn("没有对应应收清单", text)
        self.assertIn(study_b, text)

    def test_reference_target_not_listed_in_its_manifest(self):
        study_a, study_b = generate_uid(), generate_uid()
        fb, sop_b = self._make_file("ub.dcm", study_b)
        other, sop_other = self._make_file("uo.dcm", study_b)
        fa, sop_a = self._make_file("ua.dcm", study_a, referenced_sop_uid=sop_b)
        self._upload([fa])
        self._upload([fb, other])
        self._save(self.hospital, study_a, [sop_a])
        self._save(self.hospital, study_b, [sop_other])  # b not listed

        verdict = self._verify(self.hospital, study_a)
        self.assertFalse(verdict.ok)
        text = self._defect_text(verdict)
        self.assertIn("不在检查", text)
        self.assertIn(sop_b, text)

    def test_reference_class_mismatch_reported(self):
        study_a, study_b = generate_uid(), generate_uid()
        fb, sop_b = self._make_file("cb.dcm", study_b)
        # a declares its reference to b as MR Image Storage; b is really CT.
        fa, sop_a = self._make_file(
            "ca.dcm", study_a, referenced_sop_uid=sop_b,
            referenced_sop_class_uid=MRImageStorage,
        )
        self._upload([fa])
        self._upload([fb])
        self._save(self.hospital, study_a, [sop_a])
        self._save(self.hospital, study_b, [sop_b])

        verdict = self._verify(self.hospital, study_a)
        self.assertFalse(verdict.ok)
        text = self._defect_text(verdict)
        self.assertIn(manifest_svc.CAT_CLASS, text)
        self.assertIn(str(MRImageStorage), text)
        self.assertIn(str(CTImageStorage), text)

    def test_missing_product_file_reported(self):
        study = generate_uid()
        path, sop = self._make_file("p1.dcm", study)
        batch_code = self._upload([path])
        self._save(self.hospital, study, [sop])
        self.assertTrue(self._verify(self.hospital, study).ok)

        os.unlink(os.path.join(self.data_dir, "published", batch_code, f"{sop}.dcm"))
        verdict = self._verify(self.hospital, study)
        self.assertFalse(verdict.ok)
        self.assertIn(manifest_svc.CAT_NO_PRODUCT, self._defect_text(verdict))

    def test_reference_cycle_is_not_a_defect(self):
        # Same-study cycle a<->b plus a cross-study cycle c<->d.
        study_ab, study_cd = generate_uid(), generate_uid()
        sop_a, sop_b = generate_uid(), generate_uid()
        sop_c, sop_d = generate_uid(), generate_uid()
        fa, _ = self._make_file("ca.dcm", study_ab, sop_uid=sop_a,
                                referenced_sop_uid=sop_b)
        fb, _ = self._make_file("cb.dcm", study_ab, sop_uid=sop_b,
                                referenced_sop_uid=sop_a)
        fc, _ = self._make_file("cc.dcm", study_cd, sop_uid=sop_c,
                                referenced_sop_uid=sop_d)
        fd, _ = self._make_file("cd.dcm", study_cd, sop_uid=sop_d,
                                referenced_sop_uid=sop_c)
        self._upload([fa, fb])
        self._upload([fc, fd])
        self._save(self.hospital, study_ab, [sop_a, sop_b])
        self._save(self.hospital, study_cd, [sop_c, sop_d])

        verdict = self._verify(self.hospital, study_ab)
        self.assertTrue(verdict.ok, self._defect_text(verdict))
        # Only the same-study cycle is reachable from study_ab's manifest.
        self.assertEqual({i.sop_uid_old for i in verdict.supply}, {sop_a, sop_b})

        verdict_cd = self._verify(self.hospital, study_cd)
        self.assertTrue(verdict_cd.ok, self._defect_text(verdict_cd))
        self.assertEqual(verdict_cd.supply_count, 2)

    def test_cross_study_recursive_closure(self):
        # a (study A) -> b (study B) -> c (study C): all three supplied.
        study_a, study_b, study_c = generate_uid(), generate_uid(), generate_uid()
        sop_c_path, sop_c = self._make_file("xc.dcm", study_c)
        fb, sop_b = self._make_file("xb.dcm", study_b, referenced_sop_uid=sop_c)
        fa, sop_a = self._make_file("xa.dcm", study_a, referenced_sop_uid=sop_b)
        self._upload([fa])
        self._upload([fb])
        self._upload([sop_c_path])
        self._save(self.hospital, study_a, [sop_a])
        self._save(self.hospital, study_b, [sop_b])
        self._save(self.hospital, study_c, [sop_c])

        verdict = self._verify(self.hospital, study_a)
        self.assertTrue(verdict.ok, self._defect_text(verdict))
        self.assertEqual(verdict.supply_count, 3)
        self.assertEqual(
            verdict.supply_by_study, {study_a: 1, study_b: 1, study_c: 1}
        )

    def test_reevaluation_after_target_list_and_upload_changes(self):
        study_a, study_b = generate_uid(), generate_uid()
        fb, sop_b = self._make_file("eb.dcm", study_b)
        fa, sop_a = self._make_file("ea.dcm", study_a, referenced_sop_uid=sop_b)
        self._upload([fa])
        self._save(self.hospital, study_a, [sop_a])

        # 1) target not archived yet.
        self.assertFalse(self._verify(self.hospital, study_a).ok)
        # 2) target archived but its study has no manifest.
        self._upload([fb])
        v2 = self._verify(self.hospital, study_a)
        self.assertFalse(v2.ok)
        self.assertIn("没有对应应收清单", self._defect_text(v2))
        # 3) target manifest created -> now ok.
        self._save(self.hospital, study_b, [sop_b])
        v3 = self._verify(self.hospital, study_a)
        self.assertTrue(v3.ok, self._defect_text(v3))
        self.assertEqual(v3.supply_count, 2)
        # 4) target manifest replaced without b -> referrer fails again.
        other, sop_other = self._make_file("eo.dcm", study_b)
        self._upload([other])
        self._save(self.hospital, study_b, [sop_other])
        v4 = self._verify(self.hospital, study_a)
        self.assertFalse(v4.ok)
        self.assertIn("不在检查", self._defect_text(v4))

    def test_hospital_isolation_of_manifests(self):
        study = generate_uid()
        path, sop = self._make_file("iso.dcm", study)
        self._upload([path], hospital="HOSP-A")
        # Same SOP listed at another hospital: not archived there -> 缺件.
        self._save("HOSP-B", study, [sop])
        verdict = self._verify("HOSP-B", study)
        self.assertFalse(verdict.ok)
        self.assertIn(manifest_svc.CAT_MISSING, self._defect_text(verdict))
        # The owning hospital verifies fine.
        self._save("HOSP-A", study, [sop])
        self.assertTrue(self._verify("HOSP-A", study).ok)


class ManifestCrudTests(ManifestTestBase):
    def test_replace_manifest_wholesale(self):
        study = generate_uid()
        f1, sop1 = self._make_file("r1.dcm", study)
        f2, sop2 = self._make_file("r2.dcm", study, seed=12)
        self._upload([f1, f2])
        self._save(self.hospital, study, [sop1])
        self.assertEqual(self._verify(self.hospital, study).own_items, [sop1])

        self._save(self.hospital, study, [sop2])
        verdict = self._verify(self.hospital, study)
        self.assertEqual(verdict.own_items, [sop2])
        self.assertTrue(verdict.ok, self._defect_text(verdict))
        self.assertEqual(verdict.supply_count, 1)

    def test_failed_replace_keeps_existing_list(self):
        study = generate_uid()
        f1, sop1 = self._make_file("k1.dcm", study)
        self._upload([f1])
        self._save(self.hospital, study, [sop1])

        # Invalid UID inside the replacement -> whole save rejected.
        resp = self._save(self.hospital, study, [generate_uid(), "not a uid!"])
        self.assertEqual(resp.status_code, 302)
        follow = self.client.get(resp.headers["Location"])
        self.assertIn("清单未保存", follow.get_data(as_text=True))
        self.assertEqual(self._verify(self.hospital, study).own_items, [sop1])

        # Over-limit replacement rejected as well, old list still intact.
        too_many = [generate_uid() for _ in range(MAX_MANIFEST_INSTANCES + 1)]
        resp = self._save(self.hospital, study, too_many)
        follow = self.client.get(resp.headers["Location"])
        self.assertIn("清单未保存", follow.get_data(as_text=True))
        self.assertEqual(self._verify(self.hospital, study).own_items, [sop1])

    def test_manifest_limits(self):
        conn = self._db()
        try:
            # 20 manifests per hospital are accepted, the 21st is rejected.
            for _ in range(MAX_MANIFESTS_PER_HOSPITAL):
                manifest_svc.replace_manifest(
                    conn, self.hospital, generate_uid(), [generate_uid()]
                )
            with self.assertRaises(manifest_svc.ManifestError):
                manifest_svc.replace_manifest(
                    conn, self.hospital, generate_uid(), [generate_uid()]
                )
            rows = manifest_svc.list_manifests(conn, self.hospital)
            self.assertEqual(len(rows), MAX_MANIFESTS_PER_HOSPITAL)

            # Exactly 100 instances per manifest are accepted.
            study = generate_uid()
            manifest_svc.replace_manifest(
                conn, "HOSP-LIM", study,
                [generate_uid() for _ in range(MAX_MANIFEST_INSTANCES)],
            )
            with self.assertRaises(manifest_svc.ManifestError):
                manifest_svc.replace_manifest(
                    conn, "HOSP-LIM", generate_uid(),
                    [generate_uid() for _ in range(MAX_MANIFEST_INSTANCES + 1)],
                )
            # Empty list and illegal UIDs are rejected.
            with self.assertRaises(manifest_svc.ManifestError):
                manifest_svc.replace_manifest(conn, "HOSP-LIM", generate_uid(), [])
            with self.assertRaises(manifest_svc.ManifestError):
                manifest_svc.replace_manifest(
                    conn, "HOSP-LIM", generate_uid(), ["1.2.03"]
                )
            with self.assertRaises(manifest_svc.ManifestError):
                manifest_svc.replace_manifest(
                    conn, "HOSP-LIM", "bad study uid", [generate_uid()]
                )
        finally:
            conn.close()

    def test_parse_dedupes_and_tolerates_separators(self):
        uids = manifest_svc.parse_sop_uid_text(
            "1.2.3, 1.2.4\n1.2.3;1.2.5\r\n  1.2.4 "
        )
        self.assertEqual(uids, ["1.2.3", "1.2.4", "1.2.5"])

    def test_manifest_persists_across_restart(self):
        study = generate_uid()
        path, sop = self._make_file("persist.dcm", study)
        self._upload([path])
        self._save(self.hospital, study, [sop])

        app2 = create_app(
            {
                "TESTING": True,
                "DICOM_DATA_DIR": self.data_dir,
                "STAGING_DIR": os.path.join(self.data_dir, "staging"),
                "PUBLISHED_DIR": os.path.join(self.data_dir, "published"),
                "DB_PATH": os.path.join(self.data_dir, "registry.db"),
            }
        )
        client2 = app2.test_client()
        page = client2.get(f"/hospitals/{self.hospital}/manifests/{study}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("可供 1 份", page.get_data(as_text=True))

    def test_delete_manifest(self):
        study = generate_uid()
        path, sop = self._make_file("del.dcm", study)
        self._upload([path])
        self._save(self.hospital, study, [sop])
        resp = self.client.post(
            f"/hospitals/{self.hospital}/manifests/{study}/delete"
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            self.client.get(f"/hospitals/{self.hospital}/manifests/{study}").status_code,
            404,
        )

    def test_pages_and_entry_points(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        # Entry form redirect.
        resp = self.client.get("/manifests?hospital_code=HOSP01")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/hospitals/HOSP01/manifests", resp.headers["Location"])
        resp = self.client.get("/manifests?hospital_code=x")
        self.assertEqual(resp.status_code, 302)  # back to index with flash
        # Hospital page renders even before any upload/manifest.
        self.assertEqual(self.client.get("/hospitals/NEWHOSP/manifests").status_code, 200)
        # Unknown manifest -> 404; bad hospital code -> 404.
        self.assertEqual(
            self.client.get(f"/hospitals/HOSP01/manifests/{generate_uid()}").status_code,
            404,
        )
        self.assertEqual(self.client.get("/hospitals/!/manifests").status_code, 404)


class ManifestConcurrencyTests(ManifestTestBase):
    def test_interleaved_verify_replace_upload_is_snapshot_consistent(self):
        study = generate_uid()
        fa, sop_a = self._make_file("sa.dcm", study)
        fb, sop_b = self._make_file("sb.dcm", study, seed=21)
        self._upload([fa, fb])
        self._save(self.hospital, study, [sop_a])

        stop = threading.Event()
        errors: list[BaseException] = []
        verdicts: list[manifest_svc.Verdict] = []

        def writer():
            conn = self._db()
            try:
                i = 0
                while not stop.is_set():
                    i += 1
                    uids = [sop_a] if i % 2 else [sop_a, sop_b]
                    manifest_svc.replace_manifest(conn, self.hospital, study, uids)
            except BaseException as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)
            finally:
                conn.close()

        def reader():
            conn = self._db()
            try:
                with self.app.app_context():
                    while not stop.is_set():
                        verdicts.append(
                            manifest_svc.verify_manifest(conn, self.hospital, study)
                        )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        def uploader():
            client = self.app.test_client()
            for k in range(3):
                if stop.is_set():
                    break
                path = os.path.join(self.work, f"extra{k}.dcm")
                make_ct_file(path)
                with open(path, "rb") as fh:
                    payload = fh.read()
                resp = client.post(
                    "/upload",
                    data={
                        "hospital_code": self.hospital,
                        "dicom_files": [(io.BytesIO(payload), f"extra{k}.dcm")],
                    },
                    content_type="multipart/form-data",
                )
                if resp.status_code != 302:
                    errors.append(AssertionError(f"upload failed: {resp.status_code}"))

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=uploader),
        ]
        for thread in threads:
            thread.start()
        stop.wait(1.0)
        stop.set()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertTrue(verdicts, "reader never produced a verdict")
        for verdict in verdicts:
            # Every verdict reflects exactly one committed manifest state:
            # either [a] or [a, b], never a torn mixture.
            self.assertTrue(verdict.ok, self._defect_text(verdict))
            self.assertIn(verdict.supply_count, (1, 2))
            self.assertIn(
                sorted(verdict.own_items), [[sop_a], sorted([sop_a, sop_b])]
            )


class ManifestBackfillTests(ManifestTestBase):
    def test_identical_reupload_backfills_attribution_and_references(self):
        study = generate_uid()
        target_study = generate_uid()
        ft, sop_t = self._make_file("bt.dcm", target_study)
        fa, sop_a = self._make_file("ba.dcm", study, referenced_sop_uid=sop_t)
        self._upload([fa])
        self._upload([ft])

        # Simulate rows archived by an older version: no attribution, no refs.
        conn = self._db()
        conn.execute(
            "UPDATE batch_file SET study_uid_old = NULL, series_uid_old = NULL, "
            "sop_class_uid = NULL"
        )
        conn.execute("DELETE FROM sop_reference")
        conn.commit()
        conn.close()

        # Same-bytes re-upload reuses the products and backfills the metadata.
        self._upload([fa])
        self._upload([ft])

        conn = self._db()
        try:
            row = conn.execute(
                "SELECT study_uid_old, sop_class_uid FROM batch_file "
                "WHERE sop_uid_old = ?",
                (sop_a,),
            ).fetchone()
            self.assertEqual(row["study_uid_old"], study)
            self.assertEqual(row["sop_class_uid"], str(CTImageStorage))
            refs = conn.execute(
                "SELECT ref_sop_uid_old, ref_sop_class_uid FROM sop_reference "
                "WHERE sop_uid_old = ?",
                (sop_a,),
            ).fetchall()
            self.assertEqual(
                [(r["ref_sop_uid_old"], r["ref_sop_class_uid"]) for r in refs],
                [(sop_t, str(CTImageStorage))],
            )
        finally:
            conn.close()

        self._save(self.hospital, study, [sop_a])
        self._save(self.hospital, target_study, [sop_t])
        verdict = self._verify(self.hospital, study)
        self.assertTrue(verdict.ok, self._defect_text(verdict))
        self.assertEqual(verdict.supply_count, 2)


if __name__ == "__main__":
    unittest.main()
