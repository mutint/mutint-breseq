from django.apps import AppConfig


class BreseqConfig(AppConfig):
    name = 'mutint_breseq'

    def ready(self):
        from django.urls import re_path, include
        from mutint_common.about_registry import register_about_section
        from mutint_common.import_tab_registry import register_import_tab
        from mutint_common.plugin_registry import register_plugin_urlpatterns
        from mutint_breseq.version import __version__

        register_plugin_urlpatterns([
            re_path(r'^breseq/', include('mutint_breseq.urls')),
        ])
        # A tab on the Import data page rather than a sidebar entry: running breseq is one
        # more way of getting a sample into an experiment, and it belongs beside the others.
        # The tab is this plugin's own page, which wears the same strip -- a type tab could
        # not carry the sample name and command line the launcher needs (see below). By
        # url_name: the registry skips a tab whose name will not reverse, so a half-installed
        # plugin cannot leave a dead tab.
        register_import_tab('run_breseq', 'Run breseq', url_name='breseq')
        register_about_section(self, name='mutint-breseq', version=__version__,
                               template='about/sections/mutint_breseq.html')

        # Nothing else is registered, and each absence is a decision:
        #
        # **No rebuilder.** This plugin derives nothing from the mutations -- it *makes* them,
        # once, and hands them to mutint-core's importer, which asks for every registered
        # rebuild itself through `run_post_processing`. A `BreseqRun` row is a record of what
        # happened, and a record of the past does not go stale.
        #
        # **No export handler.** It adds no mutation type; what it produces is ordinary
        # samples, exported by core's `mut` like any other.
        #
        # **No import handler**, which is the one worth explaining because it looks like the
        # obvious way to build this. A FASTQ drop needs a sample name and a command line, and
        # `handle(experiment, staged_root, paths, user)` can carry neither -- so registering
        # would put a tab on the Import data page that cannot carry what the tab needs.
        # The upload machinery is still core's: see `mutint_import.staging`, which exists for
        # exactly this and was added with this plugin.
        #
        # **No example dataset.** One would have to ship read files and run breseq to
        # demonstrate anything, which is minutes of CPU inside `./mutint test`.
