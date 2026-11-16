"""Helpers for tests: generate synthetic uncompressed CT DICOM files.

The generated files are ordinary valid DICOM files (explicit/implicit VR little
endian, or big endian) with nested reference sequences, so they exercise the
same code paths as real hospital exports.
"""
from __future__ import annotations

import os

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    generate_uid,
)
from pydicom.sequence import Sequence

TS_BY_UID = {
    "1.2.840.10008.1.2": ImplicitVRLittleEndian,
    "1.2.840.10008.1.2.1": ExplicitVRLittleEndian,
    "1.2.840.10008.1.2.2": ExplicitVRBigEndian,
}


def make_ct_file(
    path: str,
    *,
    sop_uid: str | None = None,
    series_uid: str | None = None,
    study_uid: str | None = None,
    for_uid: str | None = None,
    referenced_sop_uid: str | None = None,
    referenced_for_uid: str | None = None,
    transfer_syntax: str = "1.2.840.10008.1.2.1",
    pixel_seed: int = 7,
    rows: int = 16,
    cols: int = 16,
) -> str:
    """Write one synthetic uncompressed CT image and return its path."""
    sop_uid = sop_uid or generate_uid()
    series_uid = series_uid or generate_uid()
    study_uid = study_uid or generate_uid()
    for_uid = for_uid or generate_uid()
    # A referenced UID that may not exist anywhere in the upload ("un-ingested
    # reference") - the service must still reserve a mapping for it.
    referenced_sop_uid = referenced_sop_uid or generate_uid()
    referenced_for_uid = referenced_for_uid or generate_uid()

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = TS_BY_UID[transfer_syntax]

    ds = Dataset()
    ds.file_meta = file_meta
    ds.preamble = b"\0" * 128

    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = "CT"
    ds.PatientID = "PAT-0001"
    ds.PatientName = "Test^Patient"
    ds.StudyID = "1"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.ContentDate = "20261116"
    ds.ContentTime = "120000"

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    rng = np.random.default_rng(pixel_seed)
    ds.PixelData = rng.integers(0, 4096, size=(rows, cols), dtype="<u2").tobytes()

    ds.FrameOfReferenceUID = for_uid

    # Nested reference sequences: ReferencedSOPSequence inside
    # ReferencedStudySequence-like structures, plus RT-study-style references.
    ref_instance = Dataset()
    ref_instance.ReferencedSOPClassUID = CTImageStorage  # class UID, must stay
    ref_instance.ReferencedSOPInstanceUID = referenced_sop_uid

    ref_series = Dataset()
    ref_series.SeriesInstanceUID = series_uid
    ref_series.ReferencedSOPSequence = Sequence([ref_instance])

    ref_study = Dataset()
    ref_study.StudyInstanceUID = study_uid
    ref_study.ReferencedSeriesSequence = Sequence([ref_series])
    ds.ReferencedStudySequence = Sequence([ref_study])

    for_item = Dataset()
    for_item.FrameOfReferenceUID = referenced_for_uid
    ds.ReferencedFrameOfReferenceSequence = Sequence([for_item])

    # Source/related FoR (not an entity present in the upload either).
    ds.SynchronizationFrameOfReferenceUID = referenced_for_uid

    # Class-like UIDs that must remain unchanged.
    ds.SOPClassesSupported = [CTImageStorage]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds.save_as(path, write_like_original=False)
    return path


def make_series(directory: str, count: int = 3, **kwargs) -> list[str]:
    """Create several CT files sharing one study/series/frame of reference."""
    study_uid = kwargs.pop("study_uid", generate_uid())
    series_uid = kwargs.pop("series_uid", generate_uid())
    for_uid = kwargs.pop("for_uid", generate_uid())
    referenced_sop_uid = kwargs.pop("referenced_sop_uid", generate_uid())
    referenced_for_uid = kwargs.pop("referenced_for_uid", generate_uid())
    paths = []
    for i in range(count):
        sop = kwargs.pop("sop_uid", None) or generate_uid()
        path = os.path.join(directory, f"ct_{i + 1}.dcm")
        make_ct_file(
            path,
            sop_uid=sop,
            study_uid=study_uid,
            series_uid=series_uid,
            for_uid=for_uid,
            referenced_sop_uid=referenced_sop_uid,
            referenced_for_uid=referenced_for_uid,
            pixel_seed=100 + i,
            **kwargs,
        )
        paths.append(path)
    return paths
