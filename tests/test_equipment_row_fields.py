"""
Equipment rows (TAG NO set, no ITEM NUMBER) get PART NUMBER = EQPT MODEL and
NEW DESCRIPTION = DESCRIPTION,EQPT MODEL,EQPT MAKE. Spare rows are untouched.
"""
from spir_dynamic.app.pipeline import _apply_equipment_row_fields
from spir_dynamic.extraction.output_schema import CI, row_from_dict

PART = CI["MANUFACTURER PART NUMBER"]
NEW_DESC = CI["NEW DESCRIPTION OF PARTS"]


def _equipment(**kw):
    base = {
        "tag_no": "P-101A",
        "desc": "CENTRIFUGAL PUMPS",
        "model": "OCVL-2X3X10H-1",
        "manufacturer": "SULZER PUMPS INDIA PVT. LTD.",
    }
    base.update(kw)
    return row_from_dict(base)


def test_equipment_row_gets_model_as_part_and_concatenated_new_desc():
    row = _equipment()
    _apply_equipment_row_fields([row])
    assert row[PART] == "OCVL-2X3X10H-1"
    assert row[NEW_DESC] == (
        "CENTRIFUGAL PUMPS,OCVL-2X3X10H-1,SULZER PUMPS INDIA PVT. LTD."
    )


def test_no_model_leaves_part_blank_and_skips_model_in_new_desc():
    row = _equipment(model=None)
    _apply_equipment_row_fields([row])
    assert row[PART] is None
    assert row[NEW_DESC] == "CENTRIFUGAL PUMPS,SULZER PUMPS INDIA PVT. LTD."


def test_all_parts_blank_leaves_new_desc_blank():
    row = _equipment(desc=None, model=None, manufacturer=None)
    _apply_equipment_row_fields([row])
    assert row[PART] is None
    assert row[NEW_DESC] is None


def test_spare_row_untouched():
    row = row_from_dict({
        "tag_no": "P-101A",
        "item_num": 1,
        "desc": "BEARING",
        "model": "OCVL-2X3X10H-1",
        "manufacturer": "SULZER",
        "mfr_part_no": None,
        "new_desc": None,
    })
    _apply_equipment_row_fields([row])
    assert row[PART] is None
    assert row[NEW_DESC] is None


def test_existing_values_not_overwritten():
    row = _equipment(mfr_part_no="EXISTING", new_desc="KEEP ME")
    _apply_equipment_row_fields([row])
    assert row[PART] == "EXISTING"
    assert row[NEW_DESC] == "KEEP ME"


def test_row_without_tag_ignored():
    row = _equipment(tag_no=None)
    _apply_equipment_row_fields([row])
    assert row[PART] is None
    assert row[NEW_DESC] is None
