"""Unit tests for deterministic UID derivation and remap eligibility."""
import unittest

from app.uidmapper import (
    NON_REMAPPED_UI_KEYWORDS,
    classify_uid,
    derive_new_uid,
    is_legal_uid,
    is_remappable_ui,
)
from pydicom.datadict import tag_for_keyword


class UidMapperTests(unittest.TestCase):
    def test_derived_uid_is_legal(self):
        new_uid = derive_new_uid("HOSP-A", "1.2.840.10008.1.2.3.4.5")
        self.assertTrue(is_legal_uid(new_uid), new_uid)
        self.assertLessEqual(len(new_uid), 64)
        self.assertTrue(new_uid.startswith("2.25."))

    def test_deterministic_same_input(self):
        args = ("HOSP-A", "1.2.3.4.5")
        self.assertEqual(derive_new_uid(*args), derive_new_uid(*args))

    def test_hospital_isolation(self):
        old = "1.2.3.4.5"
        self.assertNotEqual(
            derive_new_uid("HOSP-A", old), derive_new_uid("HOSP-B", old)
        )

    def test_distinct_old_uids_distinct_new(self):
        a = derive_new_uid("H", "1.2.3.4.5")
        b = derive_new_uid("H", "1.2.3.4.6")
        self.assertNotEqual(a, b)

    def test_legal_uid_validator_rejects_bad(self):
        self.assertFalse(is_legal_uid(""))
        self.assertFalse(is_legal_uid("1.2."))
        self.assertFalse(is_legal_uid("1..2"))
        self.assertFalse(is_legal_uid("1.02.3"))  # leading zero
        self.assertFalse(is_legal_uid("a.b.c"))
        self.assertFalse(is_legal_uid("1." + "1" * 65))
        self.assertTrue(is_legal_uid("1.2.840.10008.1.2.1"))
        self.assertTrue(is_legal_uid("2.25.123456789012345678901234567890123456789"))

    def test_transfer_syntax_and_class_uids_never_remapped(self):
        for keyword in [
            "TransferSyntaxUID",
            "SOPClassUID",
            "MediaStorageSOPClassUID",
            "ReferencedSOPClassUID",
            "ImplementationClassUID",
        ]:
            self.assertIn(keyword, NON_REMAPPED_UI_KEYWORDS)
            tag = tag_for_keyword(keyword)
            self.assertIsNotNone(tag)
            self.assertFalse(is_remappable_ui(tag, keyword))

    def test_entity_uids_are_remappable(self):
        for keyword in [
            "StudyInstanceUID",
            "SeriesInstanceUID",
            "SOPInstanceUID",
            "MediaStorageSOPInstanceUID",
            "FrameOfReferenceUID",
            "ReferencedSOPInstanceUID",
            "ReferencedFrameOfReferenceUID",
        ]:
            tag = tag_for_keyword(keyword)
            self.assertTrue(is_remappable_ui(tag, keyword), keyword)

    def test_unknown_tag_not_remapped(self):
        # (0x0009, 0x1001) is a private creator range tag -> must be skipped.
        self.assertFalse(is_remappable_ui(0x00091001, None))

    def test_classification(self):
        self.assertEqual(classify_uid("StudyInstanceUID"), "study")
        self.assertEqual(classify_uid("SeriesInstanceUID"), "series")
        self.assertEqual(classify_uid("SOPInstanceUID"), "instance")
        self.assertEqual(
            classify_uid("FrameOfReferenceUID"), "frame_of_reference"
        )
        self.assertEqual(
            classify_uid("ReferencedSOPInstanceUID"), "reference"
        )


if __name__ == "__main__":
    unittest.main()
