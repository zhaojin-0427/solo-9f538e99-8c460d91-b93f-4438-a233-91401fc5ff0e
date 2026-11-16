"""Tests for the pydicom conversion engine."""
import os
import tempfile
import unittest

import pydicom

from app.dicom_engine import (
    DicomValidationError,
    apply_mapping,
    load_and_plan,
    verify_converted,
    write_converted,
)
from tests.dicom_factory import make_ct_file


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "in.dcm")
        make_ct_file(self.src)

    def test_plan_collects_nested_and_file_meta_uids(self):
        ds, plan = load_and_plan(self.src, "H1", ordinal=1, original_filename="in.dcm")
        keywords_found = set()
        for element in _all_ui_elements(ds):
            if getattr(element, "keyword", None):
                keywords_found.add(element.keyword)
        self.assertIn("MediaStorageSOPInstanceUID", keywords_found)
        self.assertIn("ReferencedSOPInstanceUID", keywords_found)
        self.assertIn("FrameOfReferenceUID", keywords_found)
        # One new UID per distinct old UID, all new UIDs unique.
        new_uids = [new for new, _kind in plan.mappings.values()]
        self.assertEqual(len(new_uids), len(set(new_uids)))

    def test_remap_changes_entity_uids_but_keeps_class_and_ts(self):
        ds, plan = load_and_plan(self.src, "H1", ordinal=1, original_filename="in.dcm")
        original_ts = str(ds.file_meta.TransferSyntaxUID)
        original_class = str(ds.SOPClassUID)
        original_pixels = bytes(ds.PixelData)

        apply_mapping(ds, plan)
        out = os.path.join(self.tmp, "out.dcm")
        write_converted(ds, out)
        verify_converted(self.src, out)

        converted = pydicom.dcmread(out)
        self.assertEqual(str(converted.file_meta.TransferSyntaxUID), original_ts)
        self.assertEqual(str(converted.SOPClassUID), original_class)
        self.assertEqual(
            str(converted.file_meta.MediaStorageSOPClassUID), original_class
        )
        self.assertEqual(bytes(converted.PixelData), original_pixels)

        # Entity UIDs changed to the planned new values.
        self.assertEqual(
            str(converted.SOPInstanceUID), plan.mappings[plan.sop_uid][0]
        )
        self.assertEqual(
            str(converted.SeriesInstanceUID), plan.mappings[plan.series_uid][0]
        )
        self.assertEqual(
            str(converted.StudyInstanceUID), plan.mappings[plan.study_uid][0]
        )
        self.assertEqual(
            str(converted.FrameOfReferenceUID), plan.mappings[plan.for_uid][0]
        )
        self.assertEqual(
            str(converted.file_meta.MediaStorageSOPInstanceUID),
            plan.mappings[plan.sop_uid][0],
        )

        # Nested references were rewritten consistently.
        nested_ref = converted.ReferencedStudySequence[0].ReferencedSeriesSequence[0]
        nested_sop = nested_ref.ReferencedSOPSequence[0]
        old_ref = _read_first(self.src, "ReferencedSOPInstanceUID")
        self.assertEqual(
            str(nested_sop.ReferencedSOPInstanceUID),
            plan.mappings[old_ref][0],
        )
        # Class UID nested alongside the reference stays unchanged.
        self.assertEqual(
            str(nested_sop.ReferencedSOPClassUID),
            str(pydicom.dcmread(self.src).SOPClassUID),
        )
        self.assertEqual(
            list(converted.SOPClassesSupported),
            list(pydicom.dcmread(self.src).SOPClassesSupported),
        )

    def test_uningested_reference_gets_reserved_mapping(self):
        ds, plan = load_and_plan(self.src, "H1", ordinal=1, original_filename="in.dcm")
        old_ref = _read_first(self.src, "ReferencedSOPInstanceUID")
        self.assertIn(old_ref, plan.mappings)
        self.assertTrue(plan.mappings[old_ref][0].startswith("2.25."))

    def test_implicit_little_endian_preserved(self):
        src = os.path.join(self.tmp, "implicit.dcm")
        make_ct_file(src, transfer_syntax="1.2.840.10008.1.2")
        ds, plan = load_and_plan(src, "H1", ordinal=1, original_filename="implicit.dcm")
        apply_mapping(ds, plan)
        out = os.path.join(self.tmp, "implicit_out.dcm")
        write_converted(ds, out)
        verify_converted(src, out)
        self.assertEqual(
            str(pydicom.dcmread(out).file_meta.TransferSyntaxUID),
            "1.2.840.10008.1.2",
        )

    def test_big_endian_preserved(self):
        src = os.path.join(self.tmp, "be.dcm")
        make_ct_file(src, transfer_syntax="1.2.840.10008.1.2.2")
        ds, plan = load_and_plan(src, "H1", ordinal=1, original_filename="be.dcm")
        apply_mapping(ds, plan)
        out = os.path.join(self.tmp, "be_out.dcm")
        write_converted(ds, out)
        verify_converted(src, out)
        self.assertEqual(
            str(pydicom.dcmread(out).file_meta.TransferSyntaxUID),
            "1.2.840.10008.1.2.2",
        )

    def test_non_ct_rejected(self):
        ds = pydicom.dcmread(self.src)
        ds.Modality = "MR"
        mr_path = os.path.join(self.tmp, "mr.dcm")
        ds.save_as(mr_path, write_like_original=False)
        with self.assertRaises(DicomValidationError):
            load_and_plan(mr_path, "H1", ordinal=1, original_filename="mr.dcm")

    def test_compressed_transfer_syntax_rejected(self):
        ds = pydicom.dcmread(self.src)
        ds.file_meta.TransferSyntaxUID = "1.2.840.10008.1.2.4.50"  # JPEG baseline
        # Raw little-endian pixels cannot be written under an encapsulated TS;
        # drop them so the file can be saved with a compressed TS declared.
        del ds.PixelData
        bad = os.path.join(self.tmp, "jpeg.dcm")
        ds.save_as(bad, write_like_original=False)
        with self.assertRaises(DicomValidationError):
            load_and_plan(bad, "H1", ordinal=1, original_filename="jpeg.dcm")


def _all_ui_elements(ds):
    elements = []

    def visit(_dataset, element):
        if element.VR == "UI":
            elements.append(element)

    ds.file_meta.walk(visit)
    ds.walk(visit)
    return elements


def _read_first(path, keyword):
    found = []

    def visit(_dataset, element):
        if getattr(element, "keyword", None) == keyword:
            found.append(str(element.value))

    pydicom.dcmread(path).walk(visit)
    return found[0]


if __name__ == "__main__":
    unittest.main()
