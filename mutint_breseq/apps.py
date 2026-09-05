from django.apps import AppConfig


class BreseqConfig(AppConfig):
    name = 'mutint_breseq'

    def ready(self):
        from django.urls import re_path, include
        from aledb_common.about_registry import register_about_section
        from aledb_common.nav_registry import EXPERIMENT_SECTION, register_nav_item
        from aledb_common.plugin_registry import register_plugin_urlpatterns

        register_plugin_urlpatterns([
            re_path(r'^breseq/', include('mutint_breseq.urls')),
        ])
        # url_name rather than a literal path, as aledb-phylogeny and aledb-compare do:
        # nav_registry skips an entry whose name will not reverse, so a half-installed plugin
        # cannot leave a dead link in the sidebar.
        register_nav_item('Run breseq', url_name='breseq', section=EXPERIMENT_SECTION)
        register_about_section(self, name='mutint-breseq',
                               template='about/sections/mutint_breseq.html')

        # Nothing else is registered, and each absence is a decision:
        #
        # **No rebuilder.** This plugin derives nothing from the mutations -- it *makes* them,
        # once, and hands them to aledb-core's importer, which asks for every registered
        # rebuild itself through `run_post_processing`. A `BreseqRun` row is a record of what
        # happened, and a record of the past does not go stale.
        #
        # **No export handler.** It adds no mutation type; what it produces is ordinary
        # samples, exported by core's `mut` like any other.
        #
        # **No import handler**, which is the one worth explaining because it looks like the
        # obvious way to build this. A FASTQ drop needs a sample name and a command line, and
        # `handle(experiment, staged_root, paths, user)` can carry neither -- so registering
        # would put an entry in the Add page's dropdown that cannot carry what the entry needs.
        # The upload machinery is still core's: see `aledb_import.staging`, which exists for
        # exactly this and was added with this plugin.
        #
        # **No example dataset.** One would have to ship read files and run breseq to
        # demonstrate anything, which is minutes of CPU inside `./mutint test`.
