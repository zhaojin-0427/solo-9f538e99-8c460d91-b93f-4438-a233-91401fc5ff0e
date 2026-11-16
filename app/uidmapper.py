"""DICOM UID remapping rules.

Deterministic remapping
-----------------------
For a given hospital the new UID is derived solely from the hospital code and
the old UID (UUIDv5 -> ``2.25.<decimal UUID>``, the ITU-T / DICOM "root of last
resort" which needs no organisation prefix registration).  Therefore:

* the same hospital + same old UID always yields the same new UID, even across
  batches and after a service restart;
* different hospitals produce different mappings and never share UIDs;
* references that point at an entity not present in the upload are reserved in
  the same way as every other UID, so cross-batch references stay consistent.

Only entity/reference UIDs are rewritten.  SOP-class-like UIDs, transfer syntax
UIDs and implementation/creator UIDs are kept verbatim (see
``NON_REMAPPED_UI_KEYWORDS``).
"""
from __future__ import annotations

import uuid

import pydicom.datadict

# Fixed namespace so every deployment derives the same UIDs from the same
# (hospital code, old UID) pair.  The namespace UUID itself is arbitrary but
# must never change.
_MAPPING_NAMESPACE = uuid.UUID("6f3f1d8a-7c2b-4e9d-a1f5-9d4c8b6a2e10")

# UI-tag keywords whose value identifies a *class*, a transfer syntax or a
# coding/implementation authority rather than an entity instance.  These must
# not be remapped.
NON_REMAPPED_UI_KEYWORDS = frozenset(
    {
        "TransferSyntaxUID",
        "EncryptedContentTransferSyntaxUID",
        "FlowTransferSyntaxUID",
        "MACCalculationTransferSyntaxUID",
        "AvailableTransferSyntaxUID",
        "ReferencedTransferSyntaxUIDInFile",
        "StoredInstanceTransferSyntaxUID",
        "SOPClassUID",
        "MediaStorageSOPClassUID",
        "ReferencedSOPClassUID",
        "ReferencedSOPClassUIDInFile",
        "AffectedSOPClassUID",
        "RequestedSOPClassUID",
        "RTVCommunicationSOPClassUID",
        "OriginalSpecializedSOPClassUID",
        "RelatedGeneralSOPClassUID",
        "ReferencedRelatedGeneralSOPClassUIDInFile",
        "ManufacturerDeviceClassUID",
        "PertinentSOPClassesInSeries",
        "PertinentSOPClassesInStudy",
        "SOPClassesInStudy",
        "SOPClassesSupported",
        "ImplementationClassUID",
        "InstanceCreatorUID",
        "CreatorVersionUID",
        "ContextGroupExtensionCreatorUID",
        "TemplateExtensionCreatorUID",
        "PrivateInformationCreatorUID",
        "CodingSchemeUID",
        "MappingResourceUID",
    }
)

# Legal DICOM UID: digits separated by dots, each component 1..64 digits,
# whole string <= 64 chars, no leading zero inside a component (except "0").
_UID_MAX_LEN = 64


def derive_new_uid(hospital_code: str, old_uid: str) -> str:
    """Deterministically derive a legal new DICOM UID.

    ``2.25.<128-bit integer>`` is the DICOM-standard UID derived from a UUID;
    it is globally unique without registering an organisation root.
    """
    material = f"{hospital_code.strip()}\x1f{old_uid.strip()}"
    new_uid = "2.25." + str(uuid.uuid5(_MAPPING_NAMESPACE, material).int)
    if not is_legal_uid(new_uid):
        raise RuntimeError(f"Derived UID is not legal: {new_uid}")
    return new_uid


def is_legal_uid(uid: str) -> bool:
    """Validate against the DICOM UI value representation rules."""
    if not uid or len(uid) > _UID_MAX_LEN:
        return False
    components = uid.split(".")
    for component in components:
        if not component or len(component) > 64 or not component.isdigit():
            return False
        if len(component) > 1 and component[0] == "0":
            return False
    return True


def is_remappable_ui(tag: int, keyword: str | None) -> bool:
    """Whether a UI element carries an entity/reference UID to be rewritten."""
    if keyword is None:
        # Private / unknown tags are never rewritten.
        return False
    if keyword in NON_REMAPPED_UI_KEYWORDS:
        return False
    # Double check the dictionary really says VR == UI.
    try:
        vr = pydicom.datadict.dictionary_VR(tag)
    except KeyError:
        return False
    return vr == "UI"


def classify_uid(keyword: str | None) -> str:
    """Coarse classification stored with each mapping for the UI viewer."""
    if not keyword:
        return "other"
    if keyword in {"StudyInstanceUID"}:
        return "study"
    if keyword in {"SeriesInstanceUID"}:
        return "series"
    if keyword in {"SOPInstanceUID", "MediaStorageSOPInstanceUID"}:
        return "instance"
    if "FrameOfReference" in keyword or keyword in {"SourceFrameOfReferenceUID"}:
        return "frame_of_reference"
    if keyword.startswith("Referenced") or "Reference" in keyword:
        return "reference"
    return "other"
