"""End-to-end tests through the Flask test client and real SQLite/files."""
import io
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from app import create_app
from app.config import MAX_FILES_PER_BATCH
from tests.dicom_factory import make_ct_file, make_series


class GatewayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "data")
        self.upload_dir = os.path.join(self.tmp, "uploads")
        os.makedirs(self.data_dir)
        os.makedirs(self.upload_dir)
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
        self.series_paths = make_series(os.path.join(self.upload_dir, "s1"), count=3)
        self.hospital = "HOSP01"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def _upload(self, paths, hospital=None, filenames=None):
        data = {"hospital_code": hospital or self.hospital}
        filenames = filenames or [os.path.basename(p) for p in paths]
        data["dicom_files"] = [
            (io.BytesIO(open(p, "rb").read()), name)
            for p, name in zip(paths, filenames)
        ]
        return self.client.post(
            "/upload", data=data, content_type="multipart/form-data",
            follow_redirects=False,
        )

    def _batch_code(self, response):
        self.assertEqual(response.status_code, 302, response.data[:400])
        return response.headers["Location"].rstrip("/").split("/")[-1]

    # -- tests -------------------------------------------------------------

    def test_full_pipeline_publishes_batch_and_rewrites_uids(self):
        response = self._upload(self.series_paths)
        batch_code = self._batch_code(response)

        detail = self.client.get(f"/batches/{batch_code}")
        self.assertEqual(detail.status_code, 200)
        body = detail.get_data(as_text=True)
        # Three downloadable products.
        for i in range(1, 4):
            self.assertIn(f"/download/{batch_code}/{i}", body)

        # All three products exist and contain valid converted DICOM.
        import pydicom

        for i in range(1, 4):
            download = self.client.get(f"/download/{batch_code}/{i}")
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.mimetype, "application/dicom")
            ds = pydicom.dcmread(io.BytesIO(download.data))
            self.assertTrue(str(ds.SOPInstanceUID).startswith("2.25."))
            self.assertEqual(ds.Modality, "CT")

        # Mapping page shows old->new correspondence.
        mapping_page = self.client.get(f"/hospitals/{self.hospital}/mappings")
        self.assertEqual(mapping_page.status_code, 200)
        self.assertIn("2.25.", mapping_page.get_data(as_text=True))

    def test_study_series_shared_uids_get_single_mapping(self):
        self._upload(self.series_paths)
        import sqlite3

        conn = sqlite3.connect(os.path.join(self.data_dir, "registry.db"))
        try:
            rows = conn.execute(
                "SELECT m.old_uid, COUNT(DISTINCT m.new_uid) AS n "
                "FROM uid_mapping m JOIN hospital h ON h.id = m.hospital_id "
                "WHERE h.code = ? GROUP BY m.old_uid HAVING n > 1",
                (self.hospital,),
            ).fetchall()
            self.assertEqual(rows, [], "an old UID maps to more than one new UID")

            # The 3 files share study/series/for UID: distinct mapping count is
            # bounded by distinct old UIDs (3 SOP + study + series + for
            # + referenced SOP + referenced FoR + sync FoR).
            total = conn.execute(
                "SELECT COUNT(*) FROM uid_mapping m "
                "JOIN hospital h ON h.id = m.hospital_id WHERE h.code = ?",
                (self.hospital,),
            ).fetchone()[0]
            self.assertLessEqual(total, 9)
            kinds = {
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT uid_kind FROM uid_mapping"
                ).fetchall()
            }
            self.assertIn("study", kinds)
            self.assertIn("series", kinds)
            self.assertIn("instance", kinds)
            self.assertIn("frame_of_reference", kinds)
            self.assertIn("reference", kinds)
        finally:
            conn.close()

    def test_reupload_same_bytes_returns_original_products(self):
        first_code = self._batch_code(self._upload(self.series_paths))
        published = os.path.join(self.data_dir, "published", first_code)
        before = {name: os.path.getmtime(os.path.join(published, name))
                  for name in os.listdir(published)}

        second_code = self._batch_code(self._upload(self.series_paths))
        # Same product set: second batch reuses every batch_file row, and no
        # new product files appear on disk.
        self.assertEqual(sorted(os.listdir(published)), sorted(before))
        after = {name: os.path.getmtime(os.path.join(published, name))
                 for name in os.listdir(published)}
        self.assertEqual(before, after)

        import sqlite3

        conn = sqlite3.connect(os.path.join(self.data_dir, "registry.db"))
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM batch_file bf "
                "JOIN hospital h ON h.id = bf.hospital_id WHERE h.code = ?",
                (self.hospital,),
            ).fetchone()[0]
            self.assertEqual(rows, 3, "duplicate batch must not create new products")
            batches = conn.execute("SELECT COUNT(*) FROM batch").fetchone()[0]
            self.assertEqual(batches, 2, "but each upload is still registered")
        finally:
            conn.close()

    def test_conflicting_bytes_rejects_whole_batch(self):
        self._upload(self.series_paths)

        # Rebuild the first SOP with different pixel bytes, same SOP UID.
        import pydicom

        conflict_dir = os.path.join(self.upload_dir, "conflict")
        os.makedirs(conflict_dir, exist_ok=True)
        original = pydicom.dcmread(self.series_paths[0])
        conflict_path = os.path.join(conflict_dir, "ct_1.dcm")
        make_ct_file(
            conflict_path,
            sop_uid=str(original.SOPInstanceUID),
            series_uid=str(original.SeriesInstanceUID),
            study_uid=str(original.StudyInstanceUID),
            for_uid=str(original.FrameOfReferenceUID),
            pixel_seed=999,  # different pixels -> different bytes
        )
        response = self._upload([conflict_path])
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(response.headers["Location"])
        self.assertIn("整批冲突", follow.get_data(as_text=True))

        import sqlite3

        conn = sqlite3.connect(os.path.join(self.data_dir, "registry.db"))
        try:
            # No new product rows, no processing batches visible.
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM batch_file").fetchone()[0], 3
            )
            statuses = {
                r[0] for r in conn.execute("SELECT DISTINCT status FROM batch")
            }
            self.assertEqual(statuses, {"published"})
        finally:
            conn.close()
        # Staging must be clean after rejection.
        batch_staging = os.path.join(self.data_dir, "staging", "batches")
        self.assertEqual(os.listdir(batch_staging), [])

    def test_mapping_stable_across_batches_same_hospital(self):
        # Batch 1 establishes mappings for a study/series/for UID.
        first_resp = self._upload(self.series_paths)
        first_batch = self._batch_code(first_resp)
        import pydicom

        original = pydicom.dcmread(self.series_paths[0])
        study_uid = str(original.StudyInstanceUID)
        series_uid = str(original.SeriesInstanceUID)
        for_uid = str(original.FrameOfReferenceUID)

        # Batch 2: new SOPs but explicit references to batch-1 study/series.
        second_dir = os.path.join(self.upload_dir, "cross")
        os.makedirs(second_dir, exist_ok=True)
        second_path = os.path.join(second_dir, "ct_x.dcm")
        make_ct_file(
            second_path,
            study_uid=study_uid,
            series_uid=series_uid,
            for_uid=for_uid,
        )
        second_code = self._batch_code(self._upload([second_path]))
        self.assertNotEqual(first_batch, second_code)

        import sqlite3

        conn = sqlite3.connect(os.path.join(self.data_dir, "registry.db"))
        try:
            rows = conn.execute(
                "SELECT DISTINCT new_uid FROM uid_mapping m "
                "JOIN hospital h ON h.id = m.hospital_id "
                "WHERE h.code = ? AND m.old_uid IN (?, ?, ?)",
                (self.hospital, study_uid, series_uid, for_uid),
            ).fetchall()
        finally:
            conn.close()
        # Exactly one new UID per old UID, shared by both batches.
        self.assertEqual(len(rows), 3)

    def test_mapping_stable_across_restart(self):
        first_resp = self._upload(self.series_paths)
        first_batch = self._batch_code(first_resp)
        import sqlite3

        db = os.path.join(self.data_dir, "registry.db")
        conn = sqlite3.connect(db)
        before = conn.execute(
            "SELECT old_uid, new_uid FROM uid_mapping ORDER BY old_uid"
        ).fetchall()
        conn.close()

        # Simulate a service restart with a brand-new app instance.
        app2 = create_app(
            {
                "TESTING": True,
                "DICOM_DATA_DIR": self.data_dir,
                "STAGING_DIR": os.path.join(self.data_dir, "staging"),
                "PUBLISHED_DIR": os.path.join(self.data_dir, "published"),
                "DB_PATH": db,
            }
        )
        client2 = app2.test_client()
        second_series = make_series(
            os.path.join(self.upload_dir, "s2"), count=2
        )
        data = {
            "hospital_code": self.hospital,
            "dicom_files": [
                (io.BytesIO(open(p, "rb").read()), os.path.basename(p))
                for p in second_series
            ],
        }
        resp = client2.post(
            "/upload", data=data, content_type="multipart/form-data"
        )
        self.assertEqual(resp.status_code, 302)

        conn = sqlite3.connect(db)
        try:
            after = conn.execute(
                "SELECT old_uid, new_uid FROM uid_mapping ORDER BY old_uid"
            ).fetchall()
        finally:
            conn.close()
        for old, new in before:
            self.assertIn((old, new), after)

        # Previously published products still downloadable after restart.
        download = client2.get(f"/download/{first_batch}/1")
        self.assertEqual(download.status_code, 200)

    def test_hospital_isolation(self):
        self._upload(self.series_paths, hospital="HOSP-A")
        # Same files (same SOP UIDs) at a different hospital -> different new UIDs.
        resp_b = self._upload(self.series_paths, hospital="HOSP-B")
        self.assertEqual(resp_b.status_code, 302)

        import sqlite3

        conn = sqlite3.connect(os.path.join(self.data_dir, "registry.db"))
        try:
            # Take an old SOP UID uploaded to both hospitals and compare its
            # derived new UID in each hospital namespace.
            old_sop = conn.execute(
                "SELECT sop_uid_old FROM batch_file LIMIT 1"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT h.code, m.new_uid FROM uid_mapping m "
                "JOIN hospital h ON h.id = m.hospital_id "
                "WHERE m.old_uid = ? ORDER BY h.code",
                (old_sop,),
            ).fetchall()
        finally:
            conn.close()
        codes = {r[0] for r in rows}
        new_uids = {r[1] for r in rows}
        self.assertEqual(codes, {"HOSP-A", "HOSP-B"})
        self.assertEqual(len(new_uids), 2, "hospitals must not share new UIDs")

        # Two independent published batch directories, one per hospital.
        self.assertEqual(
            len(os.listdir(os.path.join(self.data_dir, "published"))), 2
        )

    def test_concurrent_uploads_never_duplicate_mapping(self):
        # Same hospital, disjoint SOPs, many concurrent batches.
        groups = []
        for g in range(6):
            groups.append(
                make_series(os.path.join(self.upload_dir, f"c{g}"), count=2)
            )

        def do_upload(paths):
            app = create_app(
                {
                    "TESTING": True,
                    "DICOM_DATA_DIR": self.data_dir,
                    "STAGING_DIR": os.path.join(self.data_dir, "staging"),
                    "PUBLISHED_DIR": os.path.join(self.data_dir, "published"),
                    "DB_PATH": os.path.join(self.data_dir, "registry.db"),
                }
            )
            client = app.test_client()
            data = {
                "hospital_code": self.hospital,
                "dicom_files": [
                    (io.BytesIO(open(p, "rb").read()), os.path.basename(p))
                    for p in paths
                ],
            }
            resp = client.post(
                "/upload", data=data, content_type="multipart/form-data"
            )
            return resp.status_code

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(do_upload, groups))
        self.assertTrue(all(s == 302 for s in statuses), statuses)

        import sqlite3

        conn = sqlite3.connect(os.path.join(self.data_dir, "registry.db"))
        try:
            dup_old = conn.execute(
                "SELECT hospital_id, old_uid, COUNT(*) c FROM uid_mapping "
                "GROUP BY hospital_id, old_uid HAVING c > 1"
            ).fetchall()
            dup_new = conn.execute(
                "SELECT new_uid, COUNT(*) c FROM uid_mapping GROUP BY new_uid HAVING c > 1"
            ).fetchall()
            self.assertEqual(dup_old, [])
            self.assertEqual(dup_new, [])
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM batch WHERE status != 'published'"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_too_many_files_rejected(self):
        paths = make_series(
            os.path.join(self.upload_dir, "big"), count=MAX_FILES_PER_BATCH + 1
        )
        response = self._upload(paths)
        follow = self.client.get(response.headers["Location"])
        self.assertIn("最多", follow.get_data(as_text=True))

    def test_invalid_hospital_code_rejected(self):
        response = self._upload(self.series_paths[:1], hospital="a")  # too short
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(response.headers["Location"])
        self.assertIn("医院代号不合法", follow.get_data(as_text=True))

    def test_staging_cleanup_after_simulated_crash(self):
        batch_staging = os.path.join(self.data_dir, "staging", "batches")
        crash_dir = os.path.join(batch_staging, "Bdeadbeef")
        os.makedirs(crash_dir)
        with open(os.path.join(crash_dir, "x.dcm"), "w") as fh:
            fh.write("partial")
        create_app(
            {
                "TESTING": True,
                "DICOM_DATA_DIR": self.data_dir,
                "STAGING_DIR": os.path.join(self.data_dir, "staging"),
                "PUBLISHED_DIR": os.path.join(self.data_dir, "published"),
                "DB_PATH": os.path.join(self.data_dir, "registry.db"),
            }
        )
        self.assertFalse(os.path.exists(crash_dir))

    def test_index_and_unknown_batch_404(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/batches/Bnope").status_code, 404)
        self.assertEqual(
            self.client.get("/hospitals/UNKNOWN/mappings").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
