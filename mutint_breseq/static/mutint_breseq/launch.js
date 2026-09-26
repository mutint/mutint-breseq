/* The Run breseq launcher: the name boxes, the options, the drop zone and the run list.
 *
 * **It was an inline `<script>` in `breseq/launch.html`** and grew past the point where that
 * was readable -- four hundred lines of behaviour inside a template that also holds the
 * markup. Nothing here needs the template engine: the two values it used to interpolate now
 * arrive through a `json_script` element, which is how every other page in the suite hands
 * data to its script.
 *
 * Loaded from `mutint_breseq/static/`, which is `AppDirectoriesFinder`'s dir and the one a
 * plugin gets for free -- `mutint_common/staticfiles/` is core's own and is named explicitly
 * in `STATICFILES_DIRS`.
 */
(function () {
    "use strict";

    var CONFIG = JSON.parse(document.getElementById("breseq-config").textContent);
    var EXPERIMENT_ID = CONFIG.experiment_id;
    var COMPONENT = CONFIG.component;
    // Slower than the Add page's 400ms, and deliberately: what is being watched here changes
    // on the scale of hours, not of samples-per-second. A run that has not moved in four
    // minutes is normal.
    var POLL_MS = 5000;

    var runsData = JSON.parse(
        document.getElementById("breseq-runs-data").textContent);
    var runsEl = document.getElementById("breseq-runs");
    var form = document.getElementById("breseq-form");
    var pollTimer = null;
    // Same guard as the Add page's: a poll already in flight when something else redraws must
    // not paint a stale list over the top of it.
    var pollGeneration = 0;

    function esc(text) {
        var div = document.createElement("div");
        div.textContent = text === null || text === undefined ? "" : String(text);
        return div.innerHTML;
    }

    function whenLocal(iso) {
        if (!iso) { return ""; }
        var when = new Date(iso);
        return isNaN(when.getTime()) ? "" : when.toLocaleString();
    }

    function elapsed(run) {
        var from = run.started_at || run.created_at;
        var to = run.finished_at;
        if (!from) { return ""; }
        var end = to ? new Date(to) : new Date();
        var seconds = Math.max(0, Math.round((end - new Date(from)) / 1000));
        // Whole units from the largest that applies down to seconds, every lower unit kept
        // even at zero -- "1h 0m 0s" rather than "1h" -- so a column of these lines up and a
        // run that took an hour reads differently from one that took an hour and a half.
        var hours = Math.floor(seconds / 3600);
        var minutes = Math.floor((seconds % 3600) / 60);
        var rest = seconds % 60;
        if (hours) { return hours + "h " + minutes + "m " + rest + "s"; }
        if (minutes) { return minutes + "m " + rest + "s"; }
        return rest + "s";
    }

    // The one thing the status column has to get right. A row that says "queued" is either a
    // job waiting its turn or a job nothing will ever pick up, and the second is by far this
    // feature's likeliest failure -- so where the queue can tell us, it does.
    function statusCell(run) {
        if (run.status === "imported") {
            return '<span class="label label-success">Imported</span>';
        }
        if (run.status === "failed") {
            return '<span class="label label-danger">Failed</span>';
        }
        if (run.status === "running") {
            return '<span class="label label-info">Running</span>';
        }
        var note = "";
        if (run.queue_status === "READY" || run.queue_status === "") {
            note = '<br><small style="color: #a94442;">waiting for a worker &mdash; ' +
                   'run <code>./mutint db_worker</code></small>';
        }
        return '<span class="label label-default">Queued</span>' + note;
    }

    // The job's own log page, for every run that has written anything -- including one that
    // is still running, which is the case the fold below cannot serve because `run.log` is
    // only filled in once the tools have finished.
    function logLink(run) {
        if (!run.log_url) { return ""; }
        return '<a href="' + esc(run.log_url) + '">log</a>';
    }

    function resultCell(run) {
        var bits = [];
        if (run.status === "imported") {
            if (run.sample_id) {
                bits.push('<a href="/mutations/breseq?experiment_id=' + EXPERIMENT_ID +
                          "&sample_id=" + run.sample_id + '">Mutations</a>');
            }
            if (run.report_url) {
                bits.push('<a href="' + run.report_url + '" target="_blank">' +
                          "breseq report</a>");
            }
        } else if (run.error) {
            bits.push('<span style="color: #a94442;">' + esc(run.error) + "</span>");
        }
        var log = logLink(run);
        if (log) { bits.push(log); }
        return bits.join(" &middot; ") || "&mdash;";
    }

    function detailRow(run) {
        var parts = [];
        if (run.population_sample) {
            parts.push("<b>Population sample</b> <code>-p</code>");
        }
        if (run.coverage_limit) {
            parts.push("<b>Coverage limit</b> " + esc(run.coverage_limit) + "-fold");
        }
        if (run.arguments) {
            parts.push("<b>Arguments</b> <code>" + esc(run.arguments) + "</code>");
        }
        if ((run.read_files || []).length) {
            parts.push("<b>Reads</b> " + esc(run.read_files.join(", ")) +
                       ((run.read_steps || []).length
                            ? " (" + esc(run.read_steps.join(", ")) + ")" : ""));
        }
        if ((run.accessions || []).length) {
            parts.push("<b>From the SRA</b> " + run.accessions.map(function (plan) {
                var runs = plan.runs || [];
                var detail = runs.length === 1 && runs[0] === plan.typed
                    ? "" : " (" + runs.length + " run" + (runs.length === 1 ? "" : "s") + ")";
                return esc(plan.typed) + esc(detail);
            }).join(", "));
        }
        if (!parts.length) { return ""; }
        return '<tr><td colspan="5" style="border-top: 0; padding-top: 0; color: #666;">' +
               '<small>' + parts.join(" &nbsp;&middot;&nbsp; ") + "</small></td></tr>";
    }

    // Things worth reading about a run that **worked**. `error` is for a failure and the fold
    // below only opens for one, so neither would show a sample that imported but not quite as
    // asked -- a pair split because its mates disagreed, say. Amber, not red: nothing went
    // wrong, but somebody should know.
    function noteRow(run) {
        var notes = run.notes || [];
        if (!notes.length) { return ""; }
        return '<tr><td colspan="5" style="border-top: 0; padding-top: 0;">' +
               notes.map(function (note) {
                   return '<div style="color: #8a6d3b;"><small>' + esc(note) + "</small></div>";
               }).join("") + "</td></tr>";
    }

    function logRow(run) {
        if (!run.log || run.status !== "failed") { return ""; }
        return '<tr><td colspan="5" style="border-top: 0; padding-top: 0;">' +
               '<details><summary style="cursor: pointer; color: #666;">' +
               "<small>run output</small></summary>" +
               '<pre style="max-height: 20em; overflow: auto; font-size: 11px;">' +
               esc(run.log) + "</pre></details></td></tr>";
    }

    function renderRuns(runs) {
        if (!runs.length) {
            runsEl.innerHTML = '<p style="color: #666;">Nothing has been run for this ' +
                               "experiment yet.</p>";
            return;
        }
        var html = ['<table class="table table-condensed">',
                    "<thead><tr><th>Sample</th><th>Status</th><th>Started</th>",
                    "<th>Took</th><th>Result</th></tr></thead><tbody>"];
        runs.forEach(function (run) {
            html.push("<tr>",
                      "<td>" + esc(run.sample_name) + "</td>",
                      "<td>" + statusCell(run) + "</td>",
                      "<td><small>" + esc(whenLocal(run.started_at || run.created_at)) +
                          "</small></td>",
                      "<td><small>" + esc(elapsed(run)) + "</small></td>",
                      "<td>" + resultCell(run) + "</td>",
                      "</tr>", detailRow(run), noteRow(run), logRow(run));
        });
        html.push("</tbody></table>");
        runsEl.innerHTML = html.join("");
    }

    function anyUnfinished(runs) {
        return runs.some(function (run) {
            return run.status === "queued" || run.status === "running";
        });
    }

    function stopPolling() {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        pollGeneration += 1;
    }

    // Polls only while something is actually in flight, and stops when nothing is. A page
    // left open on a finished experiment should cost nothing.
    function startPolling() {
        stopPolling();
        var mine = pollGeneration;

        function poll() {
            fetch("/breseq/runs?experiment_id=" + EXPERIMENT_ID)
                .then(function (resp) { return resp.ok ? resp.json() : null; })
                .then(function (body) {
                    if (mine !== pollGeneration) { return; }
                    if (body && body.runs) {
                        runsData = body.runs;
                        renderRuns(runsData);
                        if (!anyUnfinished(runsData)) { stopPolling(); return; }
                    }
                    pollTimer = setTimeout(poll, POLL_MS);
                })
                .catch(function () {
                    if (mine !== pollGeneration) { return; }
                    pollTimer = setTimeout(poll, POLL_MS);
                });
        }
        pollTimer = setTimeout(poll, POLL_MS);
    }

    function refresh(runs) {
        runsData = runs;
        renderRuns(runsData);
        if (anyUnfinished(runsData)) { startPolling(); } else { stopPolling(); }
    }

    renderRuns(runsData);
    if (anyUnfinished(runsData)) { startPolling(); }

    // Everything below is the form, which a reader without write access does not get.
    if (!form) { return; }

    var selected = [];
    // Set for the length of an upload. `mutintUpload` holds its own copy of the list, so a
    // file removed meanwhile would still go up and the page would lie about what is going;
    // Remove and Reset refuse while this is set, as the Import data page's do.
    var uploading = false;
    // A dropped metadata.csv, read client-side and posted with the preview and the launch
    // rather than uploaded with the reads: the server parses it with core's own reader
    // (`mutint_import.metadata`) and names the samples from it under Multiple samples.
    var metadataText = "";
    var metadataName = "";
    // The last preview's answer, keyed on everything it depends on, so a submit can ask for
    // a fresh one only when something changed. See `ensurePreview`.
    var lastPreview = null;
    var dropzone = document.getElementById("breseq-dropzone");
    var fileInput = document.getElementById("breseq-file-input");
    var fileListEl = document.getElementById("breseq-file-list");
    var submitBtn = document.getElementById("breseq-submit");
    var annotationHold = document.getElementById("breseq-annotation-hold");
    // Whether a reference annotator is still rewriting this experiment's annotation. The
    // panel under the tab strip -- core's, drawn by `{% import_tabs %}` on every import tab
    // page including this one -- owns the answer and announces it; this page only listens.
    //
    // **This is the most expensive thing that panel prevents.** A breseq run is hours, and one
    // launched against the reference as it stands right now will call each IS insertion as two
    // junctions -- which is exactly what running ISEScan first is for, and which re-annotating
    // the genome afterwards cannot turn back into a MOB. The run is not wrong so much as
    // wasted.
    var annotationBusy = false;
    var resetBtn = document.getElementById("breseq-reset");
    var nameInput = document.getElementById("breseq-sample-name");
    var populationInput = document.getElementById("breseq-population");
    var timePointInput = document.getElementById("breseq-time-point");
    var sampleInput = document.getElementById("breseq-sample");
    var identityNote = document.getElementById("breseq-identity-note");
    var argsInput = document.getElementById("breseq-arguments");

    // --- the four name boxes, kept in step both ways -----------------------------------
    //
    // Full Name is what a person recognises; the three parts are what is posted. Neither is
    // a second source of truth, because one is always derived from the other: typing a name
    // splits it, editing a part rebuilds it.
    //
    // `mutintSampleName` is the browser's copy of `mutint_import/sample_names.py`. It shows
    // what will happen and decides nothing -- the server composes the name from the three
    // parts and the importer parses it, both in Python.

    var syncing = false;
    var EXISTING = JSON.parse(
        document.getElementById("breseq-existing-samples").textContent);

    // A sample this run would land on top of, or null.
    //
    // **A collision supersedes rather than refuses.** The importer reuses the sample at a
    // coordinate and clears its mutation calls before writing the new ones, which is how a
    // corrected run replaces the one before it -- so this warns and never blocks. The two
    // keys are the importer's own: a placed name matches on the coordinate, a name carrying
    // none matches on the name it was imported under.
    function collision(population, timePoint, sample, name) {
        for (var i = 0; i < EXISTING.length; i += 1) {
            var row = EXISTING[i];
            if (population && timePoint) {
                if (row.population === population && row.time_point === timePoint
                        && row.sample === sample) {
                    return row;
                }
            } else if (name && row.source_name === name) {
                return row;
            }
        }
        return null;
    }

    // What the import will do with this name, said in the boxes rather than in prose. A name
    // carrying no coordinate is not an error: it is the unplaced case, and the sample lands
    // on `Unspecified` with no time point.
    function splitFromName() {
        if (syncing) { return; }
        syncing = true;
        var name = nameInput.value.trim();
        var parsed = window.mutintSampleName.parse(name);
        if (parsed) {
            populationInput.value = parsed.population;
            timePointInput.value = String(parsed.timePoint);
            sampleInput.value = parsed.sample;
        } else {
            populationInput.value = "";
            timePointInput.value = "";
            sampleInput.value = name;
        }
        noteOutcome(populationInput.value, timePointInput.value, sampleInput.value, name);
        syncing = false;
    }

    // The other direction. A name carries all three parts or none of them -- there is no
    // spelling for a population with no time point -- so clearing exactly one of the first
    // two says so rather than silently writing a name that means something else.
    function composeFromParts() {
        if (syncing) { return; }
        syncing = true;
        var population = populationInput.value.trim();
        var timePoint = timePointInput.value.trim();
        var sample = sampleInput.value.trim();

        if (!population && !timePoint) {
            nameInput.value = sample;
            noteOutcome("", "", sample, sample);
        } else if (population && timePoint) {
            nameInput.value = window.mutintSampleName.compose(population, timePoint, sample);
            noteOutcome(population, timePoint, sample, nameInput.value);
        } else {
            // No name can say this, so none is written: a population and a time point travel
            // together or not at all.
            note("A name carries a population and a time point together or neither. " +
                 "Fill the other in, or clear both to leave the sample unplaced.");
        }
        syncing = false;
    }

    function note(message, kind) {
        identityNote.textContent = message;
        // Amber for something to think about, grey for something merely worth knowing.
        identityNote.style.color = !message ? "" : (kind === "info" ? "#666" : "#8a6d3b");
    }

    // Said after every sync, and last, so a name that cannot be built is reported before a
    // name that would replace something.
    function noteOutcome(population, timePoint, sample, name) {
        var existing = collision(population, timePoint, sample, name);
        if (existing) {
            note("This replaces " + existing.label + ", which is already in this experiment: "
                 + "its mutations are cleared and rewritten by the import. Nothing else about "
                 + "the sample changes.");
            return;
        }
        note(population || timePoint
            ? ""
            : (sample ? "No population or time point, so this sample is filed under "
                        + "Unspecified with no time point." : ""),
            "info");
    }

    // Re-read on blur so the boxes show the *name's* reading rather than what was typed into
    // them: put an underscore in Population and the composed name has four fields and carries
    // no coordinate, which the boxes then say instead of pretending it worked.
    [populationInput, timePointInput, sampleInput].forEach(function (input) {
        input.addEventListener("input", composeFromParts);
        input.addEventListener("blur", splitFromName);
    });

    // --- the Input type menu ------------------------------------------------------------
    //
    // Two ways of saying what the samples are called, and the menu decides which: Single
    // sample composes a name from three boxes, Multiple samples derives one per read file
    // set (or takes them from a dropped metadata.csv). Every element carrying
    // `data-mode-show` lists the modes it belongs to, so the markup says where it appears
    // and this does not have to hold a list. The composed name is shown disabled: a
    // disabled input posts nothing and cannot be typed into, which is exactly "a preview".

    var modeInput = document.getElementById("breseq-input-mode");
    var nameHelp = document.getElementById("breseq-name-help");
    var prefs = window.mutintPreferences({
        authenticated: !!CONFIG.authenticated,
        url: CONFIG.preferences_url,
        embedded: CONFIG.preferences || {}
    });
    var MODE_KEY = "breseq.input_mode";
    // `parts` is Single sample and `read_names` is Multiple samples; the values are what
    // every stored preference holds, so only the labels changed when a third mode went.
    var MODES = ["parts", "read_names"];

    var NAME_HELP = {
        parts: "Built from the three boxes above. This is the sample's name in MutInt and " +
               "the directory breseq writes into."
    };

    function currentMode() {
        return MODES.indexOf(modeInput.value) === -1 ? "parts" : modeInput.value;
    }

    function syncInputMode() {
        var mode = currentMode();

        Array.prototype.forEach.call(
            document.querySelectorAll("[data-mode-show]"), function (el) {
                var modes = el.getAttribute("data-mode-show").split(/\s+/);
                el.hidden = modes.indexOf(mode) === -1;
            });

        // The composed name is read-only always: it is a preview of what the three boxes
        // make, kept in the markup as disabled and never enabled here.
        nameInput.disabled = true;
        [populationInput, timePointInput, sampleInput].forEach(function (input) {
            input.disabled = mode !== "parts";
        });
        nameHelp.textContent = NAME_HELP[mode] || "";

        if (mode === "parts") { composeFromParts(); }
        renderList();
    }

    modeInput.addEventListener("change", function () {
        prefs.set(MODE_KEY, currentMode());
        syncInputMode();
    });

    // `--no-paired-mapping` makes breseq treat every file as its own read set, which changes
    // how many samples a drop is. The preview would otherwise go on promising the pairing the
    // arguments box has just turned off.
    argsInput.addEventListener("change", function () {
        if (currentMode() === "read_names") { renderList(); }
    });
    // The ticked read steps, by name: trimming, and whatever other components registered.
    function checkedReadSteps() {
        return Array.prototype.map.call(
            document.querySelectorAll('#breseq-read-steps input[name="read_step"]:checked'),
            function (input) { return input.value; });
    }
    // Not `populationInput`, which is the Population *name* box above -- two different
    // meanings of the word, a few lines apart.
    var populationSampleInput = document.getElementById("breseq-population-sample");
    var coverageLimitInput = document.getElementById("breseq-coverage-limit");
    var limitCoverageInput = document.getElementById("breseq-limit-coverage");

    // The checkbox owns whether there is a limit; the box owns how much. Ticking it fills in
    // the middle of the recommended range rather than leaving an enabled empty box, which
    // would be a third state saying nothing. Unticking leaves the number where it is -- it is
    // visibly disabled, and not sent -- so changing your mind twice costs no typing.
    var DEFAULT_COVERAGE_LIMIT = "80";

    function syncCoverageLimit() {
        coverageLimitInput.disabled = !limitCoverageInput.checked;
        if (limitCoverageInput.checked) {
            if (!coverageLimitInput.value) {
                coverageLimitInput.value = DEFAULT_COVERAGE_LIMIT;
            }
            coverageLimitInput.focus();
            coverageLimitInput.select();
        }
    }

    limitCoverageInput.addEventListener("change", syncCoverageLimit);
    var errorEl = document.getElementById("breseq-error");
    var progressEl = document.getElementById("breseq-progress");
    var progressBar = document.getElementById("breseq-progress-bar");
    var progressText = document.getElementById("breseq-progress-text");
    var accessionsEl = document.getElementById("breseq-accessions");

    function accessionsText() {
        return accessionsEl ? accessionsEl.value.trim() : "";
    }

    function humanBytes(size) {
        if (size < 1024) { return size + " B"; }
        if (size < 1048576) { return (size / 1024).toFixed(0) + " KB"; }
        if (size < 1073741824) { return (size / 1048576).toFixed(1) + " MB"; }
        return (size / 1073741824).toFixed(2) + " GB";
    }

    // One row per staged file, each with its own Remove: reads are individual files, so
    // the unit somebody takes back is the file (the Import data page's is the drop, a breseq
    // folder being hundreds of files). The path rides in a data attribute rather than an
    // inline handler -- filenames are the person's own text -- and one delegated listener
    // below reads it back.
    function removeButton(path) {
        return "<button type=\"button\" class=\"btn btn-link btn-xs breseq-remove\"" +
               " data-path=\"" + esc(path) + "\"" + (uploading ? " disabled" : "") +
               ">Remove</button>";
    }

    function fileListHtml() {
        var items = selected.map(function (entry) {
            return "<li>" + esc(entry.path) + " <small style='color: #666;'>" +
                   humanBytes(entry.file.size) + "</small> " + removeButton(entry.path) + "</li>";
        });
        if (metadataText) {
            items.push("<li>" + esc(metadataName) +
                       " <small style='color: #666;'>sample names and coordinates" +
                       (currentMode() === "read_names"
                           ? "" : "; used under Multiple samples only") +
                       "</small> " + removeButton(metadataName) + "</li>");
        }
        return "<ul>" + items.join("") + "</ul>";
    }

    // A removal is a splice and a redraw: `renderList` already re-runs the preview where one
    // is drawn, and `previewGeneration` already discards a preview in flight for the longer
    // list. Refused mid-upload for the reason `uploading` gives.
    function removeEntry(path) {
        if (uploading) { return; }
        if (metadataText && path === metadataName) {
            metadataText = "";
            metadataName = "";
        }
        selected = selected.filter(function (entry) { return entry.path !== path; });
        renderList();
    }

    // Everything staged, the accessions with it: they are the run's input the way the files
    // are, and a successful launch already clears both together. The file input is reset so
    // that picking the same file again fires `change`, which a browser does not do for a
    // value it thinks is unchanged.
    function resetSelection() {
        if (uploading) { return; }
        selected = [];
        metadataText = "";
        metadataName = "";
        if (accessionsEl) { accessionsEl.value = ""; }
        fileInput.value = "";
        renderList();
    }

    // A poll already in flight when the drop changes must not paint a stale table over a newer
    // one -- the same guard the run list's poll uses, and for the same reason.
    var previewGeneration = 0;

    // What ENA said about an accession sample, under its files: where the name came from and
    // how much is about to be downloaded. A person is deciding whether this is the run they
    // meant before hours are spent on it, and the alias and title are what tell them.
    function accessionNote(sample) {
        if (!sample.accession) { return ""; }
        var bits = ["from " + esc(sample.accession)];
        if (sample.alias && sample.alias !== sample.name) {
            bits.push("alias " + esc(sample.alias));
        }
        if (sample.title) { bits.push(esc(sample.title)); }
        bits.push((sample.runs || []).length + " run" +
                  ((sample.runs || []).length === 1 ? "" : "s") + ", " +
                  humanBytes(sample.bytes || 0));
        return '<br><span style="color: #31708f;">' + bits.join(" &middot; ") + "</span>";
    }

    // The single-sample modes draw no table -- everything is one sample -- but an accession
    // is still worth a line saying what it resolved to, for the same reason as above.
    function accessionListHtml(samples) {
        var rows = samples.filter(function (sample) { return sample.accession; });
        if (!rows.length) { return ""; }
        return "<ul>" + rows.map(function (sample) {
            return "<li><b>" + esc(sample.accession) + "</b> <small style='color: #666;'>" +
                   esc(sample.files.join(", ")) + "</small>" + accessionNote(sample) +
                   "</li>";
        }).join("") + "</ul>";
    }

    function previewHtml(samples) {
        var head = '<table class="table table-condensed" style="margin-top: 1em;">' +
                   "<thead><tr><th>Sample</th><th>Population</th><th>Time point</th>" +
                   "<th>Files</th><th></th></tr></thead><tbody>";
        var rows = samples.map(function (sample) {
            // A name carrying no coordinate is not an error -- it is the unplaced case, and
            // saying so here is the whole reason this table exists.
            var placed = sample.placed
                ? "<td>" + esc(sample.population) + "</td><td>" + esc(sample.time_point) +
                  "</td>"
                : '<td colspan="2"><small style="color: #8a6d3b;">no population or time ' +
                  "point in the name &mdash; filed under Unspecified</small></td>";
            return "<tr><td><b>" + esc(sample.name) + "</b></td>" + placed +
                   '<td><small style="color: #666;">' + esc(sample.files.join(", ")) +
                   accessionNote(sample) + "</small></td><td>" +
                   (sample.replaces
                       ? '<small style="color: #8a6d3b;">replaces ' + esc(sample.replaces) +
                         "</small>"
                       : "") +
                   "</td></tr>";
        }).join("");
        return head + rows + "</tbody></table>";
    }

    // What the drop would be read as, asked of the server. **Deliberately not a copy of the
    // derivation in JavaScript**: the suite already carries one such copy, in
    // mutint_sample_names.js, and its own agreement test says what that costs -- it can only
    // check that the two specifications match, never that this implementation matches its own
    // table. That copy earns its place because a name box needs an answer per keystroke; a
    // file drop is a discrete event and can afford a round trip.
    // Everything the preview's answer depends on, as one string, so a cached answer is
    // known to be for the selection on screen and not for the one before it.
    function previewKey() {
        return JSON.stringify([currentMode(),
                               selected.map(function (entry) { return entry.path; }),
                               accessionsText(), argsInput.value, metadataText]);
    }

    // The preview's samples, from the cache when nothing changed since it was drawn, else
    // freshly asked. What the submit-time dialog reads: a launch decides on what the server
    // says the selection is, never on a table from an earlier state of it.
    function ensurePreview() {
        if (lastPreview && lastPreview.key === previewKey()) {
            return Promise.resolve(lastPreview.samples);
        }
        return renderPreview();
    }

    function renderPreview() {
        previewGeneration += 1;
        var mine = previewGeneration;
        var names = selected.map(function (entry) { return entry.path; });
        var typed = accessionsText();
        var readNames = currentMode() === "read_names";
        var key = previewKey();
        lastPreview = null;

        fileListEl.innerHTML = fileListHtml() +
            '<p style="color: #666; margin-top: 1em;">' +
            (typed ? "Asking ENA about the accessions…" : "Reading the names…") + "</p>";

        return mutintPostJson("/breseq/preview?experiment_id=" + EXPERIMENT_ID,
                              { names: names, accessions: typed, arguments: argsInput.value,
                                metadata: metadataText })
            .then(function (body) {
                if (mine !== previewGeneration) { return lastPreview ? lastPreview.samples : []; }
                var samples = body.samples || [];
                lastPreview = { key: key, samples: samples };
                if (!readNames) {
                    // One sample whatever was given; only the accessions need describing.
                    fileListEl.innerHTML = fileListHtml() + accessionListHtml(samples);
                    return samples;
                }
                var sources = [];
                if (names.length) {
                    sources.push(names.length + " file" + (names.length === 1 ? "" : "s"));
                }
                var fetched = samples.filter(function (s) { return s.accession; }).length;
                if (fetched) {
                    sources.push(fetched + " accession" + (fetched === 1 ? "" : "s"));
                }
                fileListEl.innerHTML =
                    "<p><b>" + samples.length + "</b> sample" +
                    (samples.length === 1 ? "" : "s") + " from " + sources.join(" and ") +
                    ":</p>" + previewHtml(samples);
                return samples;
            })
            .catch(function (err) {
                if (mine !== previewGeneration) { return []; }
                // The launch derives the names and resolves the accessions itself, so a
                // preview that could not be fetched costs the reader a description and not
                // the ability to launch -- except that an accession ENA refused here will be
                // refused there too, so the sentence is worth reading.
                fileListEl.innerHTML = fileListHtml() +
                    '<p style="color: #8a6d3b;">' +
                    (err.body && err.body.field === "accessions"
                        ? "" : "Could not work out what these files will be called: ") +
                    esc(err.message || String(err)) + "</p>";
                // Resolved, not rejected: a launch does not wait on the preview, and the
                // server refuses at launch whatever the preview would have refused.
                return [];
            });
    }

    function renderList() {
        var typed = accessionsText();
        submitBtn.disabled = annotationBusy || (!selected.length && !typed);
        if (annotationHold) {
            annotationHold.style.display = annotationBusy ? "" : "none";
        }
        if (resetBtn) {
            resetBtn.disabled = uploading || (!selected.length && !typed && !metadataText);
        }
        if (!selected.length && !typed) { fileListEl.innerHTML = fileListHtml(); return; }
        // The table is drawn whenever an accession is typed, whatever the mode: what an
        // accession resolved to is worth seeing before the download is committed to.
        if (currentMode() === "read_names" || typed) { renderPreview(); return; }
        previewGeneration += 1;   // any preview still in flight is for a mode we have left
        fileListEl.innerHTML = fileListHtml();
    }

    function isMetadata(path) {
        return path.split("/").pop().toLowerCase() === "metadata.csv";
    }

    function addEntries(entries) {
        var csv = null;
        entries.forEach(function (entry) {
            if (isMetadata(entry.path)) { csv = entry; return; }
            // A second drop adds to the first rather than replacing it, so a pair whose mates
            // live in different folders can be assembled in two gestures. Same path twice is
            // the same file.
            if (!selected.some(function (had) { return had.path === entry.path; })) {
                selected.push(entry);
            }
        });
        if (csv) {
            // Read here and posted as text with the preview and the launch: it is about the
            // reads rather than one of them, and the server has no reason to store it.
            var reader = new FileReader();
            reader.onload = function () {
                metadataText = String(reader.result || "");
                metadataName = csv.path;
                renderList();
            };
            reader.readAsText(csv.file);
        }
        renderList();
    }

    function setProgress(done, total, label) {
        progressEl.style.display = "block";
        var pct = total ? Math.floor((done / total) * 100) : 0;
        progressBar.style.width = pct + "%";
        progressBar.textContent = pct + "%";
        progressText.textContent = label;
    }

    function renderError(message) {
        errorEl.innerHTML = '<div class="alert alert-danger" style="margin-top: 1em;">' +
                            esc(message) + "</div>";
    }

    dropzone.addEventListener("click", function () { fileInput.click(); });
    fileInput.addEventListener("change", function () {
        addEntries(mutintFromFileList(fileInput.files));
        // So the same file can be picked again after being removed; see `resetSelection`.
        fileInput.value = "";
    });
    fileListEl.addEventListener("click", function (e) {
        var button = e.target.closest ? e.target.closest(".breseq-remove") : null;
        if (!button) { return; }
        e.preventDefault();
        removeEntry(button.getAttribute("data-path"));
    });
    if (resetBtn) { resetBtn.addEventListener("click", resetSelection); }
    ["dragenter", "dragover"].forEach(function (evt) {
        dropzone.addEventListener(evt, function (e) {
            e.preventDefault(); dropzone.style.background = "#eef6ff";
        });
    });
    ["dragleave", "drop"].forEach(function (evt) {
        dropzone.addEventListener(evt, function (e) {
            e.preventDefault(); dropzone.style.background = "#fafafa";
        });
    });
    dropzone.addEventListener("drop", function (e) {
        mutintCollectDropped(e.dataTransfer).then(addEntries);
    });

    if (accessionsEl) {
        // Typing enables the button at once; the preview waits for `change` -- leaving the
        // box -- because each preview is a round trip to ENA per accession, and a table that
        // redrew on every keystroke would ask about `S`, `SR`, `SRR`...
        accessionsEl.addEventListener("input", function () {
            submitBtn.disabled = annotationBusy || (!selected.length && !accessionsText());
            if (resetBtn) { resetBtn.disabled = uploading || submitBtn.disabled; }
        });
        accessionsEl.addEventListener("change", renderList);
    }

    document.addEventListener("mutint:annotation-status", function (event) {
        annotationBusy = !!(event.detail && event.detail.busy);
        renderList();
    });

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        if (annotationBusy) { return; }
        if (!selected.length && !accessionsText()) { return; }
        errorEl.innerHTML = "";
        // Only when a limit was asked for -- a disabled input is barred from constraint
        // validation and always reports itself valid, so the checkbox has to be asked first.
        //
        // The form is submitted through JS, so the browser's own validation never runs, and a
        // `type="number"` box holding something it will not parse reports an **empty** value.
        // Ticked-and-empty would therefore post as "every read", which is the opposite of
        // what the tick said. Asked before the upload, so a typo costs nothing at all.
        if (limitCoverageInput.checked && !coverageLimitInput.checkValidity()) {
            renderError("Limit the coverage to a number greater than zero, or deselect the " +
                        "box to use every read.");
            coverageLimitInput.focus();
            return;
        }
        submitBtn.disabled = true;
        // Under Single sample every read file, dropped or fetched, becomes one breseq run
        // and one sample. Accessions that resolve to several SRA samples are the case
        // somebody almost never means that way, so it is asked -- on a fresh preview, since
        // the one on screen may predate the last edit of the box. A preview that cannot be
        // fetched lets the launch go on: the server refuses whatever it would have.
        var confirmed = Promise.resolve(true);
        if (currentMode() === "parts" && accessionsText()) {
            confirmed = ensurePreview().then(function (samples) {
                var distinct = {};
                (samples || []).forEach(function (row) {
                    if (row.accession) { distinct[row.biosample || row.accession] = true; }
                });
                var count = Object.keys(distinct).length;
                if (count < 2) { return true; }
                return window.mutintConfirm(
                    "Run these reads as one sample?",
                    "The accessions resolve to " + count + " different SRA samples. Under " +
                    "Single sample every read file, dropped or fetched, is combined into " +
                    "one breseq run and one sample. Choose Multiple samples if each " +
                    "accession should be its own sample.",
                    "Run as one sample");
            });
        }
        confirmed.then(function (go) {
            if (!go) { submitBtn.disabled = false; return; }
            startUpload();
        });
    });

    function startUpload() {
        uploading = true;
        renderList();                     // greys every Remove and the Reset
        setProgress(0, 1, "Preparing…");

        // No session when nothing was dropped: a session exists to receive bytes, and a
        // launch whose reads are all fetched by accession has none. The server reads a blank
        // id as "no staged files" and the accessions as the rest.
        var upload = selected.length
            ? mutintUpload(selected, {
                experimentId: EXPERIMENT_ID,
                consumer: COMPONENT,
                onProgress: setProgress
            })
            : Promise.resolve("");

        upload.then(function (uploadId) {
            setProgress(1, 1, accessionsText() ? "Resolving the accessions and queueing the run…"
                                               : "Queueing the run…");
            return mutintPostJson("/breseq/launch?experiment_id=" + EXPERIMENT_ID, {
                upload_id: uploadId,
                accessions: accessionsText(),
                // **The mode decides what naming the server is being handed**, and the two
                // are different contracts rather than one with optional fields: `parts`
                // posts a coordinate for the server to compose, `read_names` posts nothing
                // about names -- the read files, and a metadata.csv if one was dropped, say.
                // Everything is sent every time and the server reads what its mode says to.
                input_mode: currentMode(),
                metadata: metadataText,
                population: populationInput.value,
                time_point: timePointInput.value,
                sample: sampleInput.value,
                arguments: argsInput.value,
                read_steps: checkedReadSteps(),
                population_sample: populationSampleInput.checked,
                // The number alone: unticked sends blank, which is already how the server
                // spells "every read". Nothing posts the checkbox itself, so the two can
                // never arrive disagreeing.
                coverage_limit: limitCoverageInput.checked ? coverageLimitInput.value : ""
            });
        }).then(function (body) {
            progressEl.style.display = "none";
            // **The files go, the form stays.** A batch is usually one population and one
            // time point with a different sample each time, so clearing those would make the
            // common case the one that costs the most typing. The Sample box is the thing to
            // change before the next drop, and it is left in view rather than emptied.
            selected = [];
            metadataText = "";
            metadataName = "";
            // The accessions go with the files, and for the same reason: they are the run's
            // input, and left in the box the next press would supersede the run just launched.
            if (accessionsEl) { accessionsEl.value = ""; }
            renderList();
            // A run already in flight for this sample was asked to stop -- its output was
            // about to be overwritten by this one. Said here because it happened on the
            // server and the run list shows it only once the worker has acted on the flag.
            if (body.superseded) {
                note("Stopped " + body.superseded + " run" +
                     (body.superseded === 1 ? "" : "s") +
                     " already under way for this sample.");
            }
            refresh(body.runs || []);
        }).catch(function (err) {
            progressEl.style.display = "none";
            renderError(err.message || String(err));
            if (err.body && err.body.field === "accessions" && accessionsEl) {
                accessionsEl.focus();
            }
        }).then(function () {
            uploading = false;
            renderList();
        });
    }

    // **Last, and it has to be.** `syncInputMode` calls `renderList`, which reads the file
    // list and the submit button -- `var`s assigned further down this function. Hoisting
    // declares them and does not assign them, so an init call placed beside the menu's own
    // wiring would run against `undefined` and throw before the page had drawn anything.
    // A stored mode the menu no longer offers -- the retired name mode -- falls back to
    // Single sample and is written back, so the row stops carrying a value nothing reads.
    var storedMode = prefs.get(MODE_KEY, "parts");
    if (MODES.indexOf(storedMode) === -1) {
        storedMode = "parts";
        prefs.set(MODE_KEY, storedMode);
    }
    modeInput.value = storedMode;
    syncInputMode();
}());
