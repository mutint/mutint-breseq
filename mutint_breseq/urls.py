from django.urls import re_path

import mutint_breseq.views


urlpatterns = [
    re_path(r'^$', mutint_breseq.views.breseq, name='breseq'),
    re_path(r'^runs$', mutint_breseq.views.runs, name='breseq_runs'),
    re_path(r'^launch$', mutint_breseq.views.launch, name='breseq_launch'),
    re_path(r'^run/(?P<pk>\d+)/delete$', mutint_breseq.views.run_delete,
            name='breseq_run_delete'),
    # The one route taking a client-supplied path. `.*` rather than a narrower pattern because
    # breseq's report links to whatever it wrote -- evidence pages, PNGs, its own stylesheet --
    # and a pattern guessing at that list would 404 a page the report itself offers. The view
    # contains the path with a realpath check, which is the guard that actually holds.
    re_path(r'^run/(?P<pk>\d+)/report/(?P<path>.*)$', mutint_breseq.views.report,
            name='breseq_report'),
]
