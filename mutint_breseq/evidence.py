"""What breseq could not explain, kept per sample, and what a curation check leaves of it.

Two writers, one table. `replace_from_import` runs on every `.gd`-shaped import through
core's `register_sample_import_hook` and keeps the evidence breseq's index page lists as
*unassigned*. `replace_from_check` runs when a curation check finishes -- breseq re-run with
the sample's curated mutations applied -- and keeps both the residual mutations and the
unassigned evidence that run reported, in the **ancestor's** coordinates. Both delete before
they write, so the table is always one run's answer and never a union of several.

The rule for *unassigned* is breseq's own, from `output.cpp`'s index page: an item of one of
`UNASSIGNED_TYPES` whose id is in no mutation's evidence column (`filter_used_as_evidence`)
and that carries no `reject=` (`rejected_and_not_user_defined`), with `ignore=` entries
dropped as that page drops them. Marginal RA and JC -- the `marginal.html` lists -- are
deliberately not here: they are breseq's below-threshold calls, still readable in the stored
report, and a table of a thousand sub-cutoff SNPs per sample is not a to-do list. Nor is
`UN`, which breseq itself shows nowhere.

`suggested_mutation` is the other pure half: breseq's `mutation_predictor.cpp` rules for the
single-item cases, offered as a prefill for core's Add form and never written on their own.
"""

import logging
from collections import namedtuple

from django.db import transaction
from django.utils import timezone

from mutint_import.gd_import import _parse_document
from mutint_sample import storage as sample_storage

from mutint_breseq.models import COMPONENT, ORIGIN_CHECK, ORIGIN_IMPORT, Evidence

logger = logging.getLogger("mutint_breseq.evidence")

#: The evidence types breseq's index page lists when nothing accounts for them, in its order.
UNASSIGNED_TYPES = ("MC", "JC", "CN", "SC", "DP", "MP", "PD")

#: The record the sample carries about its last check, under this component's key.
CHECK_RECORD = "curation_check"

Coordinates = namedtuple("Coordinates", ["seq_id", "position", "end", "seq_id_2",
                                         "position_2"])

CheckSummary = namedtuple("CheckSummary", ["residual_mutations", "evidence",
                                           "carried_dismissals", "report_stored"])


def _plain(value):
    """A record value as a plain `int`/`float`/`str` -- `genomediff` hands back wrappers that
    remember their spelling, which JSON and the database do not want."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return str(value)


def record_of(record):
    """The record's attributes as a JSON-safe dict, plus its type and id."""
    data = {key: _plain(value) for key, value in dict(record.attributes).items()}
    data["type"] = record.type
    data["id"] = _plain(record.id)
    return data


def unassigned_records(document):
    """The evidence records of `document` breseq's index page would list as unassigned."""
    used = set()
    for mutation in document.mutations:
        for parent in (mutation.parent_ids or ()):
            used.add(int(parent))
    for record in document.evidence:
        if record.type not in UNASSIGNED_TYPES:
            continue
        if record.id is not None and int(record.id) in used:
            continue
        attributes = record.attributes
        if "reject" in attributes and "user_defined" not in attributes:
            continue
        if "ignore" in attributes and "user_defined" not in attributes:
            continue
        yield record


def _int(attributes, name):
    value = attributes.get(name)
    if value is None or value == "":
        return None
    return int(str(value))


def _coordinate(attributes, name, original):
    """`original_<name>` where asked for and present, else `<name>`."""
    if original:
        value = _int(attributes, "original_%s" % name)
        if value is not None:
            return value
    return _int(attributes, name)


def coordinates_of(record, original=False):
    """Where a record sits, as `Coordinates`, or None when its bounds cannot be read.

    By type: `start`/`end` for MC and CN (and UN); `side_1_*`/`side_2_*` for the junction
    shapes; `position` (plus `size`, where the type has one) for everything else, which is
    RA, SC, MP and every mutation type. With `original=True` the `original_*` spellings are
    read first, which is what a check's output carries; a record without them answers in
    its own frame.
    """
    attributes = record.attributes if hasattr(record, "attributes") else record
    kind = record.type if hasattr(record, "type") else attributes.get("type")
    try:
        if kind in ("MC", "CN", "UN"):
            seq_id = attributes.get("seq_id")
            start = _coordinate(attributes, "start", original)
            end = _coordinate(attributes, "end", original)
            if seq_id is None or start is None:
                return None
            return Coordinates(str(seq_id), start, end, "", None)
        if kind in ("JC", "DP", "PD"):
            seq_id = attributes.get("side_1_seq_id")
            position = _coordinate(attributes, "side_1_position", original)
            seq_id_2 = attributes.get("side_2_seq_id")
            position_2 = _coordinate(attributes, "side_2_position", original)
            if seq_id is None or position is None:
                return None
            return Coordinates(str(seq_id), position, None,
                               str(seq_id_2 or ""), position_2)
        seq_id = attributes.get("seq_id")
        position = _coordinate(attributes, "position", original)
        if seq_id is None or position is None:
            return None
        size = _int(attributes, "size")
        end = position + size - 1 if size else None
        return Coordinates(str(seq_id), position, end, "", None)
    except (TypeError, ValueError):
        return None


def _rows_for(sample, records, origin, original=False):
    rows = []
    for record in records:
        where = coordinates_of(record, original=original)
        if where is None:
            logger.warning("skipping %s evidence with unreadable bounds in sample %s",
                           record.type, sample.pk)
            continue
        data = record_of(record)
        if original and not any(key.startswith("original_") for key in data):
            # A check's output without the fields that make it one: positions are in the
            # mutated frame, and the row says so rather than pretending otherwise.
            data["frame"] = "mutated"
        rows.append(Evidence(
            sample=sample, evidence_type=record.type, seq_id=where.seq_id,
            position=where.position, end=where.end, seq_id_2=where.seq_id_2,
            position_2=where.position_2, source_id=_plain(record.id), origin=origin,
            record=data))
    return rows


def replace_from_import(sample, document):
    """Keep the unassigned evidence of a sample's own `.gd`; the import's rows, replaced.

    The function core's sample-import hook calls. A re-import supersedes the sample, so its
    evidence -- dismissals included -- goes with it; what survives a re-import is what the
    new file says.
    """
    rows = _rows_for(sample, unassigned_records(document), ORIGIN_IMPORT)
    with transaction.atomic():
        Evidence.objects.filter(sample=sample, origin=ORIGIN_IMPORT).delete()
        Evidence.objects.bulk_create(rows)
    return len(rows)


def replace_from_check(sample, output_gd_path, report_dir, run=None):
    """Replace the sample's evidence with what a curation check left, in original coordinates.

    Every mutation the check predicted is residual by construction -- the curated set was
    applied before the reads were aligned -- so each becomes a row beside the unassigned
    evidence. A row that matches a dismissed row of the previous set on type and position
    inherits the dismissal; nothing inherits `resolved_mutation`, since a check that still
    reports the evidence means the mutation did not explain it, which is the information the
    run exists to give.

    Takes no import lock. It creates no sample, population or mutation; it replaces one
    sample's evidence rows in one transaction and copies a directory, and nothing an importer
    holds the lock for races with that. The one overlap is a concurrent sample delete, which
    makes the copy fail best-effort and cascades the rows.
    """
    with open(output_gd_path, "rb") as handle:
        document = _parse_document(handle)

    residual = _rows_for(sample, list(document.mutations), ORIGIN_CHECK, original=True)
    unassigned = _rows_for(sample, unassigned_records(document), ORIGIN_CHECK, original=True)

    dismissed = {row.match_key(): row
                 for row in Evidence.objects.filter(sample=sample, dismissed_at__isnull=False)}
    carried = 0
    for row in residual + unassigned:
        earlier = dismissed.get(row.match_key())
        if earlier is not None:
            row.dismissed_at = earlier.dismissed_at
            row.dismissed_by_id = earlier.dismissed_by_id
            row.dismiss_note = earlier.dismiss_note
            carried += 1

    with transaction.atomic():
        Evidence.objects.filter(sample=sample).delete()
        Evidence.objects.bulk_create(residual + unassigned)

    stored = sample_storage.store_report(sample, report_dir, "check")
    sample.set_record(COMPONENT, CHECK_RECORD, {
        "at": timezone.now().isoformat(),
        "run": run.pk if run is not None else None,
        "residual_mutations": len(residual),
        "evidence": len(unassigned),
    }, save=False)
    sample.save(update_fields=["supplemental_data", "check_report_stored"])
    return CheckSummary(len(residual), len(unassigned), carried, stored)


def statuses_for(rows):
    """`{row.pk: status}` for many rows in two queries rather than one per row."""
    from mutint_sample.models import MutationCall

    wanted = {(row.sample_id, row.resolved_mutation_id)
              for row in rows if row.resolved_mutation_id is not None}
    present = set()
    if wanted:
        sample_ids = {sample_id for sample_id, _ in wanted}
        mutation_ids = {mutation_id for _, mutation_id in wanted}
        present = set(MutationCall.objects
                      .filter(sample_id__in=sample_ids, mutation_id__in=mutation_ids)
                      .values_list("sample_id", "mutation_id"))
    statuses = {}
    for row in rows:
        if row.is_dismissed:
            statuses[row.pk] = "dismissed"
        elif (row.sample_id, row.resolved_mutation_id) in present:
            statuses[row.pk] = "resolved"
        else:
            statuses[row.pk] = "open"
    return statuses


def suggested_mutation(row):
    """breseq's own single-item predictions, as a prefill for core's Add form.

    `mutation_predictor.cpp` turns one MC into a DEL, a JC whose sides face each other into a
    DEL or an AMP, and a CN into an AMP or a DEL; a residual mutation from a check is its own
    best description. Anything else -- the junction shapes that need a second item, a
    soft-clipped pile -- gets only its contig and position. Returns `{"__type": ..., fields}`
    or just the position fields, which is what `mutintMutationForm(initial)` takes.
    """
    record = row.record or {}
    kind = row.evidence_type
    fields = {"seq_id": row.seq_id, "position": row.position}

    if kind == "MC":
        if row.end is not None:
            fields.update(__type="DEL", size=row.end - row.position + 1)
        return fields
    if kind == "CN":
        copies = _int(record, "copy_number")
        if row.end is not None and copies is not None:
            if copies > 1:
                fields.update(__type="AMP", size=row.end - row.position + 1,
                              new_copy_number=copies)
            elif copies == 0:
                fields.update(__type="DEL", size=row.end - row.position + 1)
        return fields
    if kind == "JC":
        same = row.seq_id_2 == row.seq_id and row.position_2 is not None
        strand_1 = _int(record, "side_1_strand")
        strand_2 = _int(record, "side_2_strand")
        if same and strand_1 == -1 and strand_2 == 1 and row.position_2 > row.position + 1:
            fields.update(__type="DEL", position=row.position + 1,
                          size=row.position_2 - row.position - 1)
        elif same and strand_1 == 1 and strand_2 == -1 and row.position > row.position_2:
            fields.update(__type="AMP", position=row.position_2,
                          size=row.position - row.position_2 + 1, new_copy_number=2)
        return fields
    if len(kind) == 3 and record.get("type") == kind:
        # A residual mutation: its own record, at its original position. `original_*` keys
        # are breseq's bookkeeping, not fields of a mutation.
        from genomediff.schema import TYPE_SPECIFIC_FIELDS
        for name in TYPE_SPECIFIC_FIELDS.get(kind, ()):
            if name in record and name not in fields:
                fields[name] = record[name]
        fields["__type"] = kind
        if row.record.get("original_position") is not None:
            fields["position"] = row.position
        return fields
    return fields


def rename_contigs(experiment_id, renames):
    """Core's sequence-rename hook: the rows carry contig names on both sides of a junction."""
    from mutint_experiment import paths

    rows = Evidence.objects.filter(**{"sample__" + paths.to_experiment(): experiment_id})
    for old, new in renames.items():
        rows.filter(seq_id=old).update(seq_id=new)
        rows.filter(seq_id_2=old).update(seq_id_2=new)
