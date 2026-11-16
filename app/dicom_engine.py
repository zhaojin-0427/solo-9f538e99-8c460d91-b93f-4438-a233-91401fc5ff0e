"""DICOM reading, validation and UID rewriting built on pydicom."""
from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset, FileMetaDataset

from .config import UNCOMPRESSED_TRANSFER_SYNTAXES
from .uidmapper import classify_uid, derive_new_uid, is_remappable_ui


class DicomValidationError(ValueError):
    """Raised when an uploaded file is not an acceptable uncompressed CT."""


@dataclass
class FilePlan:
    """Everything learned about one input file before it is converted."""

    ordinal: int
    original_filename: str
    sha256: str
    sop_uid: str
    series_uid: str
    study_uid: str
    for_uid: str | None
    transfer_syntax_uid: str
    sop_class_uid: str
    # old_uid -> (new_uid, kind); insertion order preserved for reporting
    mappings: dict[str, tuple[str, str]] = field(default_factory=dict)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _keyword_of(element: DataElement) -> str | None:
    keyword = getattr(element, "keyword", None)
    return keyword or None


def _collect_ui_elements(dataset: Dataset) -> list[DataElement]:
    """Collect every remappable UI element, including nested sequences.

    The file meta dataset is walked separately because ``Dataset.walk`` does
    not descend into ``file_meta``.
    """
    found: list[DataElement] = []

    def visit(_ds: Dataset, element: DataElement) -> None:
        if element.VR != "UI":
            return
        keyword = _keyword_of(element)
        if is_remappable_ui(element.tag, keyword):
            found.append(element)

    file_meta = getattr(dataset, "file_meta", None)
    if isinstance(file_meta, FileMetaDataset):
        file_meta.walk(visit)
    dataset.walk(visit)
    return found


def _is_multi(value) -> bool:
    """Whether a UI value is sequence-valued (pydicom MultiValue included)."""
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _ui_values(element: DataElement) -> list[str]:
    value = element.value
    if value is None or value == "":
        return []
    # pydicom exposes multi-valued elements as MultiValue, a MutableSequence
    # subclass (not a ``list`` subclass).
    if _is_multi(value):
        return [v for v in value if v not in (None, "")]
    return [value]


def load_and_plan(path: str | Path, hospital_code: str, ordinal: int,
                  original_filename: str, sha256: str | None = None) -> tuple[Dataset, FilePlan]:
    """Read a DICOM file, validate it and compute its UID mapping plan."""
    ds = pydicom.dcmread(str(path), force=False)

    file_meta = getattr(ds, "file_meta", None)
    if not isinstance(file_meta, FileMetaDataset) or not getattr(file_meta, "TransferSyntaxUID", None):
        raise DicomValidationError(
            f"{original_filename}: missing DICOM file meta / Transfer Syntax UID"
        )

    transfer_syntax_uid = str(file_meta.TransferSyntaxUID)
    if transfer_syntax_uid not in UNCOMPRESSED_TRANSFER_SYNTAXES:
        raise DicomValidationError(
            f"{original_filename}: compressed or unsupported transfer syntax "
            f"{transfer_syntax_uid}; only uncompressed CT is accepted"
        )

    if getattr(ds, "Modality", None) != "CT":
        raise DicomValidationError(
            f"{original_filename}: Modality is {getattr(ds, 'Modality', '<missing>')!r}, expected 'CT'"
        )

    for required in ("SOPInstanceUID", "SeriesInstanceUID", "StudyInstanceUID"):
        if not getattr(ds, required, None):
            raise DicomValidationError(f"{original_filename}: missing required UID {required}")

    if "PixelData" not in ds:
        raise DicomValidationError(f"{original_filename}: no PixelData element (not a CT image)")

    sop_class_uid = str(ds.SOPClassUID)

    plan = FilePlan(
        ordinal=ordinal,
        original_filename=original_filename,
        sha256=sha256 or sha256_file(path),
        sop_uid=str(ds.SOPInstanceUID),
        series_uid=str(ds.SeriesInstanceUID),
        study_uid=str(ds.StudyInstanceUID),
        for_uid=str(ds.FrameOfReferenceUID) if getattr(ds, "FrameOfReferenceUID", None) else None,
        transfer_syntax_uid=transfer_syntax_uid,
        sop_class_uid=sop_class_uid,
    )

    for element in _collect_ui_elements(ds):
        kind = classify_uid(_keyword_of(element))
        for old_uid in _ui_values(element):
            if old_uid not in plan.mappings:
                plan.mappings[old_uid] = (derive_new_uid(hospital_code, old_uid), kind)

    return ds, plan


def apply_mapping(ds: Dataset, plan: FilePlan) -> None:
    """Rewrite every remappable UI value in place using the plan."""
    for element in _collect_ui_elements(ds):
        values = _ui_values(element)
        if not values:
            continue
        if _is_multi(element.value):
            element.value = [
                plan.mappings[v][0] if v in plan.mappings else v
                for v in element.value
            ]
        else:
            old = element.value
            if old in plan.mappings:
                element.value = plan.mappings[old][0]

    # Keep file meta consistent even if the source file lacked the element.
    file_meta = ds.file_meta
    if not getattr(file_meta, "MediaStorageSOPInstanceUID", None):
        file_meta.MediaStorageSOPInstanceUID = plan.mappings[plan.sop_uid][0]
    if not getattr(file_meta, "MediaStorageSOPClassUID", None):
        file_meta.MediaStorageSOPClassUID = plan.sop_class_uid


def write_converted(ds: Dataset, out_path: str | Path) -> None:
    """Write the converted dataset preserving pixels and transfer syntax."""
    # write_like_original=False -> always emit a proper preamble/file meta;
    # pydicom encodes according to the dataset's own transfer syntax, so the
    # PixelData bytes are carried through untouched.
    ds.save_as(str(out_path), write_like_original=False)


def verify_converted(original_path: str | Path, converted_path: str | Path) -> None:
    """Assert the conversion kept pixels, transfer syntax and SOP class."""
    src = pydicom.dcmread(str(original_path))
    dst = pydicom.dcmread(str(converted_path))

    if bytes(src.PixelData) != bytes(dst.PixelData):
        raise RuntimeError("pixel data bytes changed during conversion")
    if str(src.file_meta.TransferSyntaxUID) != str(dst.file_meta.TransferSyntaxUID):
        raise RuntimeError("transfer syntax UID changed during conversion")
    if str(src.SOPClassUID) != str(dst.SOPClassUID):
        raise RuntimeError("SOP class UID changed during conversion")
    if str(src.file_meta.MediaStorageSOPClassUID) != str(dst.file_meta.MediaStorageSOPClassUID):
        raise RuntimeError("media storage SOP class UID changed during conversion")
