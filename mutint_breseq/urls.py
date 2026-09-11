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
    # There is no report route here any more. breseq's HTML is kept under the *sample* by
    # mutint-core's importer and served by its own sandboxed viewer at
    # /mutations/report/<sample_id>/ -- one home for it, and the right one, since a report
    # describes the sample that was produced rather than the run that produced it.
]
