"""Unassigned evidence: the rule, the import hook, the Evidence tab and its three writes.

Built on core's own breseq-folder fixture so the rows come out of a real import through
`register_sample_import_hook`, which is how a dropped folder and a launched run both reach
this table.
"""

import json
import os
import shutil
import tempfile

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from genomediff import GenomeDiff

from mutint_common import store
from mutint_common.curate_tab_registry import get_curate_tabs
from mutint_import import breseq_folder
from mutint_import.tests import breseq_fixture
from mutint_sample.models import Mutation, MutationCall, Sample

from mutint_breseq import evidence
from mutint_breseq.models import Evidence

#: Two mutations (the fixture's own), an assigned JC, a rejected JC, an ignored JC, an
#: unassigned JC and MC, a marginal RA and a UN. Exactly the MC and the last JC are
#: unassigned by breseq's rule.
GD_WITH_EVIDENCE = """#=GENOME_DIFF\t1.0
#=REFSEQ\ttest_ref
SNP\t1\t10\ttest_ref\t100\tA\tgene_name=thrA\tgene_product=aspartokinase\tfrequency=1
DEL\t2\t11,12\ttest_ref\t200\t5\tgene_name=thrB/thrC\tgene_product=x/y\tfrequency=1
RA\t10\t.\ttest_ref\t100\t0\tG\tA\tfrequency=1
JC\t11\t.\ttest_ref\t199\t-1\ttest_ref\t206\t1\t0\tcoverage=12
JC\t12\t.\ttest_ref\t60\t1\ttest_ref\t70\t-1\t0\treject=COVERAGE_EVENNESS_SKEW
JC\t13\t.\ttest_ref\t80\t1\ttest_ref\t90\t-1\t0\tignore=CONTIG_END
JC\t14\t.\ttest_ref\t120\t-1\ttest_ref\t140\t1\t0\tcoverage=8\tnew_junction_read_count=9\tpos_hash_score=4\tside_1_strand=-1\tside_2_strand=1
MC\t15\t.\ttest_ref\t130\t150\t0\t0\tleft_outside_cov=20\tright_outside_cov=18
RA\t16\t.\ttest_ref\t50\t0\tC\tT\tfrequency=0.2\tconsensus_reject=FREQUENCY_CUTOFF
UN\t17\t.\ttest_ref\t1\t5
"""


def _document(text):
    return GenomeDiff.read(iter(line for line in text.splitlines() if line.strip()))


class RuleTestCase(SimpleTestCase):

    def test_unassigned_is_breseqs_index_page_rule(self):
        found = [(r.type, r.id) for r in evidence.unassigned_records(_document(GD_WITH_EVIDENCE))]
        self.assertEqual([("JC", 14), ("MC", 15)], found)

    def test_a_rejected_item_marked_user_defined_stays(self):
        text = GD_WITH_EVIDENCE + "JC\t18\t.\ttest_ref\t20\t1\ttest_ref\t30\t-1\t0\treject=X\tuser_defined=1\n"
        self.assertIn(("JC", 18),
                      [(r.type, r.id) for r in evidence.unassigned_records(_document(text))])

    def test_coordinates_by_type(self):
        doc = _document(GD_WITH_EVIDENCE)
        by_id = {r.id: r for r in doc.evidence}
        self.assertEqual(("test_ref", 130, 150, "", None),
                         tuple(evidence.coordinates_of(by_id[15])))
        self.assertEqual(("test_ref", 120, None, "test_ref", 140),
                         tuple(evidence.coordinates_of(by_id[14])))
        self.assertEqual(("test_ref", 100, None, "", None),
                         tuple(evidence.coordinates_of(by_id[10])))
        deletion = [m for m in doc.mutations if m.type == "DEL"][0]
        self.assertEqual(("test_ref", 200, 204, "", None),
                         tuple(evidence.coordinates_of(deletion)))

    def test_original_coordinates_win_when_asked_for(self):
        text = ("#=GENOME_DIFF\t1.0\n"
                "MC\t1\t.\ttest_ref\t130\t150\t0\t0\toriginal_start=127\toriginal_end=147\n"
                "JC\t2\t.\ttest_ref\t5\t1\ttest_ref\t9\t-1\t0\toriginal_side_1_position=4\n")
        by_id = {r.id: r for r in _document(text).evidence}
        self.assertEqual((127, 147), evidence.coordinates_of(by_id[1], original=True)[1:3])
        self.assertEqual((130, 150), evidence.coordinates_of(by_id[1])[1:3])
        # Side 2 carried no original field: it falls back to its own.
        self.assertEqual(("test_ref", 4, None, "test_ref", 9),
                         tuple(evidence.coordinates_of(by_id[2], original=True)))

    def test_unreadable_bounds_answer_none(self):
        text = "#=GENOME_DIFF\t1.0\nMC\t1\t.\ttest_ref\tnope\t150\t0\t0\n"
        self.assertIsNone(evidence.coordinates_of(_document(text).evidence[0]))


class SuggestionTestCase(SimpleTestCase):

    def row(self, kind, position, end=None, seq_id_2="", position_2=None, record=None,
            **more):
        return Evidence(evidence_type=kind, seq_id="r", position=position, end=end,
                        seq_id_2=seq_id_2, position_2=position_2,
                        record=dict(record or {}, **more))

    def test_mc_is_a_deletion(self):
        self.assertEqual({"seq_id": "r", "position": 130, "__type": "DEL", "size": 21},
                         evidence.suggested_mutation(self.row("MC", 130, 150)))

    def test_facing_junction_sides_are_a_deletion(self):
        row = self.row("JC", 120, seq_id_2="r", position_2=140, side_1_strand=-1, side_2_strand=1)
        self.assertEqual({"seq_id": "r", "position": 121, "__type": "DEL", "size": 19},
                         evidence.suggested_mutation(row))

    def test_outward_junction_sides_are_an_amplification(self):
        row = self.row("JC", 140, seq_id_2="r", position_2=120, side_1_strand=1, side_2_strand=-1)
        self.assertEqual({"seq_id": "r", "position": 120, "__type": "AMP", "size": 21,
                          "new_copy_number": 2}, evidence.suggested_mutation(row))

    def test_a_junction_to_another_contig_is_position_only(self):
        row = self.row("JC", 120, seq_id_2="other", position_2=140, side_1_strand=-1,
                       side_2_strand=1)
        self.assertEqual({"seq_id": "r", "position": 120}, evidence.suggested_mutation(row))

    def test_cn_is_an_amp_or_a_del(self):
        self.assertEqual("AMP", evidence.suggested_mutation(
            self.row("CN", 10, 20, copy_number=3))["__type"])
        self.assertEqual("DEL", evidence.suggested_mutation(
            self.row("CN", 10, 20, copy_number=0))["__type"])
        self.assertNotIn("__type", evidence.suggested_mutation(
            self.row("CN", 10, 20, copy_number=1)))

    def test_a_residual_mutation_is_itself_at_its_original_position(self):
        row = self.row("SNP", 100, record={"type": "SNP", "id": 1, "seq_id": "r",
                                            "position": 103, "new_seq": "T",
                                            "original_position": 100})
        self.assertEqual({"seq_id": "r", "position": 100, "new_seq": "T", "__type": "SNP"},
                         evidence.suggested_mutation(row))


class ImportedEvidenceTestCase(TestCase):
    """Rows out of a real import, and the tab over them."""

    def setUp(self):
        self.owner = User.objects.create(username="owner", email="o@e.com", is_active=True)
        self.client.force_login(self.owner)
        self.drop = tempfile.mkdtemp()
        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.drop, True)
        self.addCleanup(shutil.rmtree, self.store, True)
        patcher = override_settings(MUTINT_STORE_DIR=self.store)
        patcher.enable()
        self.addCleanup(patcher.disable)
        breseq_fixture.write_sample(self.drop, "s1", gd_text=GD_WITH_EVIDENCE)
        breseq_folder.import_breseq_folders(
            self.drop, project_name="P", experiment_name="e", owner_name="owner")
        self.sample = Sample.objects.get()
        self.experiment = self.sample.experiment
        self.experiment.project.user = self.owner
        self.experiment.project.save()

    def _page(self, **params):
        params.setdefault("experiment_id", self.experiment.id)
        params.setdefault("sample_id", self.sample.id)
        return self.client.get("/breseq/evidence", params)

    def _post(self, what, ids, **extra):
        body = {"experiment_id": self.experiment.id, "evidence_ids": json.dumps(ids)}
        body.update(extra)
        return self.client.post("/breseq/evidence/%s" % what, body)

    # --- the import hook ---------------------------------------------------------------

    def test_the_import_keeps_exactly_the_unassigned_items(self):
        rows = list(Evidence.objects.filter(sample=self.sample).order_by("source_id"))
        self.assertEqual([("JC", 14, 120, 140), ("MC", 15, 130, 150)],
                         [(r.evidence_type, r.source_id, r.position,
                           r.position_2 if r.is_junction else r.end) for r in rows])
        self.assertTrue(all(r.origin == "import" for r in rows))
        self.assertEqual("9", str(rows[0].record["new_junction_read_count"]))

    def test_a_reimport_replaces_the_rows(self):
        from django.utils import timezone
        row = Evidence.objects.get(evidence_type="MC")
        row.dismissed_at = timezone.now()
        row.save()
        breseq_folder.import_breseq_folders(
            self.drop, project_name="P", experiment_name="e", owner_name="owner")
        self.assertEqual(2, Evidence.objects.filter(sample=self.sample).count())
        self.assertFalse(Evidence.objects.get(evidence_type="MC").is_dismissed)

    def test_a_rename_reaches_both_sides_of_a_junction(self):
        evidence.rename_contigs(self.experiment.id, {"test_ref": "chrom"})
        row = Evidence.objects.get(evidence_type="JC")
        self.assertEqual(("chrom", "chrom"), (row.seq_id, row.seq_id_2))

    # --- the tab -----------------------------------------------------------------------

    def test_the_tab_is_in_curates_strip(self):
        labels = [tab["label"] for tab in get_curate_tabs(self.experiment.id)]
        self.assertEqual(["Edit", "Delete", "Add", "Copy", "Evidence", "History"], labels)

    def test_the_page_lists_the_open_rows_with_their_links(self):
        # The fixture folder carries no `output/`, so say the report is there.
        self.sample.report_stored = True
        self.sample.save(update_fields=["report_stored"])
        response = self._page()
        self.assertEqual(200, response.status_code)
        html = response.content.decode()
        self.assertIn("test_ref:130-150", html)
        self.assertIn("test_ref:120 (-) ↔ test_ref:140 (+)", html)
        # breseq's page for an MC is its coverage plot, `MC_PLOT_<id>`; a JC's is `JC_<id>`.
        self.assertIn("/mutations/report/%d/?page=evidence.html#MC_PLOT_15.html" % self.sample.id,
                      html)
        self.assertIn("/mutations/report/%d/?page=evidence.html#JC_14.html" % self.sample.id, html)
        self.assertIn("/mutations/browse?sample_id=%d&amp;locus=test_ref:130-150" % self.sample.id,
                      html)
        self.assertIn("Add mutation (DEL)", html)
        self.assertIn("Check curation", html)

    def test_the_page_wears_curates_strip_with_itself_active(self):
        html = self._page().content.decode()
        self.assertIn('<li class="active">\n            <a href="/breseq/evidence?experiment_id=%d">Evidence</a>'
                      % self.experiment.id, html)

    def test_a_reader_sees_the_rows_and_no_buttons(self):
        reader = User.objects.create(username="reader", email="r@e.com", is_active=True)
        from mutint_experiment.permissions import grant_project_access
        grant_project_access(self.experiment.project, reader, "read")
        self.client.force_login(reader)
        html = self._page().content.decode()
        self.assertIn("test_ref:130-150", html)
        self.assertNotIn("data-ev-dismiss", html)
        self.assertNotIn("Check curation", html)

    def test_the_status_filter(self):
        row = Evidence.objects.get(evidence_type="MC")
        self._post("dismiss", [row.pk], note="an artefact")
        self.assertNotIn("test_ref:130-150", self._page().content.decode())
        dismissed = self._page(status="dismissed").content.decode()
        self.assertIn("test_ref:130-150", dismissed)
        self.assertIn("an artefact", dismissed)

    # --- the writes --------------------------------------------------------------------

    def test_dismiss_and_reopen(self):
        row = Evidence.objects.get(evidence_type="MC")
        self.assertEqual(200, self._post("dismiss", [row.pk], note="why").status_code)
        row.refresh_from_db()
        self.assertEqual(("dismissed", "why", self.owner), (row.status, row.dismiss_note,
                                                            row.dismissed_by))
        self.assertEqual(200, self._post("reopen", [row.pk]).status_code)
        row.refresh_from_db()
        self.assertEqual("open", row.status)

    def test_resolving_with_a_mutation_the_sample_carries(self):
        row = Evidence.objects.get(evidence_type="MC")
        deletion = Mutation.objects.get(mutation_type="DEL")
        response = self._post("resolve", [row.pk], mutation_id=deletion.pk)
        self.assertEqual(200, response.status_code, response.content)
        row.refresh_from_db()
        self.assertEqual("resolved", row.status)

    def test_a_mutation_the_sample_does_not_carry_cannot_resolve(self):
        row = Evidence.objects.get(evidence_type="MC")
        deletion = Mutation.objects.get(mutation_type="DEL")
        MutationCall.objects.filter(sample=self.sample, mutation=deletion).delete()
        self.assertEqual(400, self._post("resolve", [row.pk], mutation_id=deletion.pk).status_code)

    def test_adding_through_core_then_resolving_and_deleting_reopens(self):
        row = Evidence.objects.get(evidence_type="MC")
        added = self.client.post("/curate/add/apply", {
            "experiment_id": self.experiment.id, "mutation_type": "DEL",
            "target_sample_ids": json.dumps([self.sample.id]),
            "seq_id": "test_ref", "position": 130, "size": 21}).json()
        self.assertEqual(200, self._post("resolve", [row.pk],
                                         mutation_id=added["mutation_id"]).status_code)
        row.refresh_from_db()
        self.assertEqual("resolved", row.status)

        # Delete the call on core's Delete tab: the row is open again, with no hook.
        call = MutationCall.objects.get(sample=self.sample, mutation_id=added["mutation_id"])
        self.client.post("/curate/delete/apply", {"experiment_id": self.experiment.id,
                                                  "call_ids": json.dumps([call.pk])})
        row.refresh_from_db()
        self.assertEqual("open", row.status)

        # Restore it from the history: resolved again.
        self.client.post("/curate/restore", {"experiment_id": self.experiment.id,
                                             "edit_set_id": added["edit_set_id"]})
        row.refresh_from_db()
        self.assertEqual("resolved", row.status)

    def test_a_reader_may_not_write(self):
        reader = User.objects.create(username="reader", email="r@e.com", is_active=True)
        from mutint_experiment.permissions import grant_project_access
        grant_project_access(self.experiment.project, reader, "read")
        self.client.force_login(reader)
        row = Evidence.objects.get(evidence_type="MC")
        self.assertEqual(403, self._post("dismiss", [row.pk]).status_code)

    def test_anonymous_may_not_write(self):
        self.client.logout()
        row = Evidence.objects.get(evidence_type="MC")
        self.assertEqual(403, self._post("dismiss", [row.pk]).status_code)

    def test_a_locked_experiment_refuses(self):
        from django.utils import timezone
        self.experiment.locked_at = timezone.now()
        self.experiment.locked_by = self.owner
        self.experiment.save()
        row = Evidence.objects.get(evidence_type="MC")
        self.assertEqual(403, self._post("dismiss", [row.pk]).status_code)

    def test_evidence_of_another_experiment_is_refused(self):
        other_drop = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other_drop, True)
        breseq_fixture.write_sample(other_drop, "s2", gd_text=GD_WITH_EVIDENCE)
        breseq_folder.import_breseq_folders(
            other_drop, project_name="P2", experiment_name="e2", owner_name="owner")
        foreign = Evidence.objects.exclude(sample=self.sample).first()
        self.assertEqual(404, self._post("dismiss", [foreign.pk]).status_code)


class CheckIngestTestCase(ImportedEvidenceTestCase):
    """`replace_from_check` on a hand-written check output."""

    CHECK_GD = """#=GENOME_DIFF\t1.0
#=REFSEQ\ttest_ref
SNP\t1\t2\ttest_ref\t96\tT\tfrequency=1\toriginal_position=101
RA\t2\t.\ttest_ref\t96\t0\tA\tT\tfrequency=1\toriginal_position=101
MC\t3\t.\ttest_ref\t125\t145\t0\t0\toriginal_start=130\toriginal_end=150
JC\t4\t.\ttest_ref\t5\t1\ttest_ref\t9\t-1\t0\toriginal_side_1_position=5\toriginal_side_2_position=9\toriginal_side_1_position_offset=2
"""

    def _check(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        gd = os.path.join(work, "output.gd")
        with open(gd, "w") as handle:
            handle.write(self.CHECK_GD)
        report = os.path.join(work, "output")
        os.makedirs(report)
        with open(os.path.join(report, "index.html"), "w") as handle:
            handle.write("<html>check</html>")
        return evidence.replace_from_check(self.sample, gd, report)

    def test_residual_mutations_and_evidence_land_in_original_coordinates(self):
        summary = self._check()
        self.assertEqual((1, 2, 0, True), tuple(summary))
        rows = {(r.evidence_type): r for r in Evidence.objects.filter(sample=self.sample)}
        self.assertEqual({"SNP", "MC", "JC"}, set(rows))
        self.assertEqual(101, rows["SNP"].position)
        self.assertEqual((130, 150), (rows["MC"].position, rows["MC"].end))
        self.assertEqual(2, rows["JC"].record["original_side_1_position_offset"])
        self.assertTrue(all(r.origin == "check" for r in rows.values()))

    def test_a_dismissal_is_carried_forward_and_a_resolution_is_not(self):
        mc = Evidence.objects.get(evidence_type="MC")
        self._post("dismiss", [mc.pk], note="repeat")
        jc = Evidence.objects.get(evidence_type="JC")
        self._post("resolve", [jc.pk], mutation_id=Mutation.objects.get(mutation_type="DEL").pk)

        summary = self._check()

        self.assertEqual(1, summary.carried_dismissals)
        self.assertEqual("repeat", Evidence.objects.get(evidence_type="MC").dismiss_note)
        self.assertEqual("open", Evidence.objects.get(evidence_type="JC").status)

    def test_the_check_report_is_stored_and_served_as_the_samples_second_report(self):
        self._check()
        self.sample.refresh_from_db()
        self.assertTrue(self.sample.check_report_stored)
        self.assertTrue(os.path.isfile(
            os.path.join(store.sample_report_dir(self.sample.pk, "check"), "index.html")))
        self.assertEqual(200, self.client.get(
            "/mutations/report/%d/check/" % self.sample.pk).status_code)
        html = self._page().content.decode()
        self.assertIn("/mutations/report/%d/check/?page=evidence.html#MC_PLOT_3.html"
                      % self.sample.pk, html)
        # A residual mutation has no page of its own: its link is the check report's index.
        self.assertIn('href="/mutations/report/%d/check/" target="_blank"' % self.sample.pk, html)
        self.assertIn("As of the curation check", html)

    def test_output_without_original_fields_says_which_frame_it_is_in(self):
        self.CHECK_GD = "#=GENOME_DIFF\t1.0\nMC\t3\t.\ttest_ref\t125\t145\t0\t0\n"
        self._check()
        row = Evidence.objects.get(evidence_type="MC")
        self.assertEqual((125, "mutated"), (row.position, row.record.get("frame")))
