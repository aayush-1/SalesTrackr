"""Read legacy manufacturing names without writing them into new datasets."""

from copy import deepcopy

LEGACY_COLLECTIONS = {"suppliers": "partners"}
LEGACY_FILES = {"suppliers": "partners.json", "assignments": "partner-links.json"}
LEGACY_FIELDS = {
    "partnerId": "supplierId",
    "partnerName": "supplierName",
    "currentPartnerId": "currentSupplierId",
    "currentPartnerName": "currentSupplierName",
    "reference": "partNumber",
    "productReference": "productPartNumber",
}


def canonical_fields(value):
    if isinstance(value, list):
        return [canonical_fields(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        name = LEGACY_FIELDS.get(key, key)
        if name in result:
            raise ValueError("Conflicting legacy and current manufacturing fields.")
        result[name] = canonical_fields(item)
    return result


def canonical_data(data):
    result = {}
    collections = {old: new for new, old in LEGACY_COLLECTIONS.items()}
    for kind, rows in data.items():
        name = collections.get(kind, kind)
        if name in result:
            raise ValueError("Conflicting legacy and current manufacturing collections.")
        result[name] = (
            {key: canonical_fields(row) for key, row in rows.items()}
            if kind not in ("settings", "productFolders") and isinstance(rows, dict)
            else deepcopy(rows)
        )
    return result


def canonical_counts(counts):
    names = {old: new for new, old in LEGACY_COLLECTIONS.items()}
    result = {}
    for key, count in counts.items():
        name = names.get(key, key)
        if name in result:
            raise ValueError("Conflicting legacy and current manufacturing counts.")
        result[name] = count
    return result
