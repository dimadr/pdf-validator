"""Exact PDF meter ownership and delivery rechecks."""
from sqlalchemy import Text, func, select
from sqlalchemy.dialects.postgresql import ARRAY

from common.models import Object


def resolve_pdf_object(db, serials):
    """Resolve each full serial independently across all object cards."""
    if not isinstance(serials, (list, tuple)) or not serials or any(
        not isinstance(serial, str) or not serial for serial in serials
    ):
        return None, 'identity_missing'

    owners = {serial: [] for serial in serials}
    configured = func.regexp_split_to_array(
        func.btrim(Object.calculator_number, ' \t\r\n'), r'\s*,\s*', type_=ARRAY(Text),
    )
    for obj in db.scalars(select(Object).where(configured.overlap(list(owners)))):
        object_serials = {value.strip() for value in obj.calculator_number.split(',')}
        for serial in owners.keys() & object_serials:
            owners[serial].append(obj)

    if any(not matches for matches in owners.values()):
        return None, 'exact_object_not_found'
    if any(len(matches) > 1 for matches in owners.values()):
        return None, 'ambiguous_object_binding'

    objects = {matches[0].id: matches[0] for matches in owners.values()}
    if len(objects) != 1:
        return None, 'meters_belong_to_different_objects'
    return next(iter(objects.values())), None


def object_binding_matches(db, obj, revision):
    if obj is None:
        return False
    result = revision.validation_result or {}
    if 'object_meter_serials' not in result:
        return obj.calculator_number == revision.calculator_number
    resolved, _ = resolve_pdf_object(db, result['object_meter_serials'])
    return resolved is not None and resolved.id == obj.id
