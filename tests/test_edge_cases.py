"""Additional edge-case tests for UID element handling and service limits."""
import os
import tempfile
import unittest

import pydicom

from app import create_app
from app.dicom_engine import apply_mapping, load_and_plan, write_converted
from app.uidmapper import NON_REMAPPED_UI_KEYWORDS, derive_new_uid
from tests.dicom_factory import make_ct_file


class MultiValueUiTests(unittest.TestCase):
    def test_multivalued_ui_remapped_per_value(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "mv.dcm")
        make_ct_file(src)

        other_a, other_b = "1.9.9.1", "1.9.9.2"
        ds = pydicom.dcmread(src)
        # FailedSOPInstanceUIDList is a remappable multi-value UI tag.
        ds.FailedSOPInstanceUIDList = [other_a, other_b]
        # Blocked class-like multi-value UI must remain untouched.
        ds.SOPClassesSupported = [
            "1.2.840.10008.5.1.4.1.1.2",
            "1.2.840.10008.5.1.4.1.1.2.1",
        ]
        self.assertIn("SOPClassesSupported", NON_REMAPPED_UI_KEYWORDS)
        prepared = os.path.join(tmp, "mv_prepared.dcm")
        ds.save_as(prepared, write_like_original=False)

        ds2, plan = load_and_plan(
            prepared, "H9", ordinal=1, original_filename="mv.dcm"
        )
        self.assertIn(other_a, plan.mappings)
        self.assertIn(other_b, plan.mappings)
        apply_mapping(ds2, plan)

        self.assertEqual(
            list(ds2.FailedSOPInstanceUIDList),
            [derive_new_uid("H9", other_a), derive_new_uid("H9", other_b)],
        )
        self.assertEqual(
            list(ds2.SOPClassesSupported),
            ["1.2.840.10008.5.1.4.1.1.2", "1.2.840.10008.5.1.4.1.1.2.1"],
        )

        out = os.path.join(tmp, "mv_out.dcm")
        write_converted(ds2, out)
        reread = pydicom.dcmread(out)
        self.assertEqual(
            list(reread.FailedSOPInstanceUIDList),
            [derive_new_uid("H9", other_a), derive_new_uid("H9", other_b)],
        )

    def test_empty_ui_value_is_safe(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "empty.dcm")
        make_ct_file(src)
        ds, plan = load_and_plan(
            src, "H9", ordinal=1, original_filename="empty.dcm"
        )
        ds.FailedSOPInstanceUIDList = []
        apply_mapping(ds, plan)  # must not raise
        self.assertEqual(list(ds.FailedSOPInstanceUIDList), [])


class ServiceLimitTests(unittest.TestCase):
    def test_service_rejects_empty_file_list(self):
        from app.services import UploadError, process_batch

        data_dir = tempfile.mkdtemp()
        app = create_app(
            {
                "TESTING": True,
                "DICOM_DATA_DIR": data_dir,
                "STAGING_DIR": os.path.join(data_dir, "staging"),
                "PUBLISHED_DIR": os.path.join(data_dir, "published"),
                "DB_PATH": os.path.join(data_dir, "registry.db"),
            }
        )
        with app.app_context():
            with self.assertRaises(UploadError):
                process_batch(None, "H1", [])


if __name__ == "__main__":
    unittest.main()
