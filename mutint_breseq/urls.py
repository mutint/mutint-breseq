from django.urls import re_path

import mutint_breseq.views


urlpatterns = [
    re_path(r'^$', mutint_breseq.views.breseq, name='breseq'),
    re_path(r'^runs$', mutint_breseq.views.runs, name='breseq_runs'),
    re_path(r'^launch$', mutint_breseq.views.launch, name='breseq_launch'),
    # What a drop would be read as, before any of it is uploaded. It exists so the page needs
    # no JavaScript copy of the derivation -- see `views.preview`.
    re_path(r'^preview$', mutint_breseq.views.preview, name='breseq_preview'),
    re_path(r'^run/(?P<pk>\d+)/delete$', mutint_breseq.views.run_delete,
            name='breseq_run_delete'),
    # The Evidence tab of the Curate page -- registered through core's curate_tab_registry
    # -- and its three writes, each `<what>` under it the way core's curate endpoints are.
    re_path(r'^evidence$', mutint_breseq.views.evidence, name='breseq_evidence'),
    re_path(r'^evidence/dismiss$', mutint_breseq.views.evidence_dismiss,
            name='breseq_evidence_dismiss'),
    re_path(r'^evidence/reopen$', mutint_breseq.views.evidence_reopen,
            name='breseq_evidence_reopen'),
    re_path(r'^evidence/resolve$', mutint_breseq.views.evidence_resolve,
            name='breseq_evidence_resolve'),
    # A curation check of one sample: the launcher, and the launch.
    re_path(r'^check$', mutint_breseq.views.check, name='breseq_check'),
    re_path(r'^check/launch$', mutint_breseq.views.check_launch, name='breseq_check_launch'),
    # There is no report route here any more. breseq's HTML is kept under the *sample* by
    # mutint-core's importer and served by its own sandboxed viewer at
    # /mutations/report/<sample_id>/ -- one home for it, and the right one, since a report
    # describes the sample that was produced rather than the run that produced it.
]
