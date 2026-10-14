/* The curation check launcher: one optional drop, one POST, and the list of this sample's
   checks. Deliberately small; the analysis launcher's script is four hundred lines about
   naming samples, none of which applies to a sample that already has a name. */
(function () {
    "use strict";

    var config = JSON.parse(document.getElementById("check-config").textContent);
    var EXPERIMENT_ID = config.experiment_id;
    var SAMPLE_ID = config.sample_id;
    var COMPONENT = config.component;
    var READS_SOURCE = config.reads_source;

    var runsEl = document.getElementById("breseq-runs");
    var form = document.getElementById("check-form");
    var selected = [];

    function esc(text) {
        return String(text == null ? "" : text).replace(/[&<>"]/g, function (c) {
            return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c];
        });
    }

    function whenLocal(iso) {
        if (!iso) { return ""; }
        return new Date(iso).toLocaleString();
    }

    function statusCell(run) {
        if (run.status === "checked") { return '<span class="label label-success">Checked</span>'; }
        if (run.status === "failed") { return '<span class="label label-danger">Failed</span>'; }
        if (run.status === "running") { return '<span class="label label-info">Running</span>'; }
        if (run.status === "cancelled") { return '<span class="label label-default">Cancelled</span>'; }
        var note = "";
        if (run.queue_status === "READY" || run.queue_status === "") {
            note = '<br><small style="color: #a94442;">waiting for a worker</small>';
        }
        return '<span class="label label-default">Queued</span>' + note;
    }

    function resultCell(run) {
        var bits = [];
        var summary = run.check_summary || {};
        if (run.status === "checked") {
            bits.push(esc(summary.residual_mutations) + " residual mutation(s), " +
                      esc(summary.evidence) + " item(s) of evidence");
            if (run.evidence_url) { bits.push('<a href="' + run.evidence_url + '">Evidence</a>'); }
            if (run.check_report_url) {
                bits.push('<a href="' + run.check_report_url + '" target="_blank">check report</a>');
            }
        } else if (run.error) {
            bits.push('<span style="color: #a94442;">' + esc(run.error) + "</span>");
        }
        if (run.log_url) { bits.push('<a href="' + esc(run.log_url) + '">log</a>'); }
        return bits.join(" &middot; ") || "&mdash;";
    }

    function renderRuns(runs) {
        if (!runs.length) {
            runsEl.innerHTML = '<p style="color: #666;">No check has been run for this sample yet.</p>';
            return;
        }
        var html = ['<table class="table table-condensed"><thead><tr><th>Started</th>',
                    "<th>Status</th><th>Reads</th><th>Result</th></tr></thead><tbody>"];
        runs.forEach(function (run) {
            var summary = run.check_summary || {};
            html.push("<tr><td><small>" + esc(whenLocal(run.started_at || run.created_at)) + "</small></td>",
                      "<td>" + statusCell(run) + "</td>",
                      "<td><small>" + esc(summary.reads_source || "") + "</small></td>",
                      "<td>" + resultCell(run) + "</td></tr>");
        });
        html.push("</tbody></table>");
        runsEl.innerHTML = html.join("");
    }

    function anyUnfinished(runs) {
        return runs.some(function (run) { return run.status === "queued" || run.status === "running"; });
    }

    var pollTimer = null;
    function poll() {
        fetch("/breseq/runs?experiment_id=" + EXPERIMENT_ID, {credentials: "same-origin"})
            .then(function (resp) { return resp.ok ? resp.json() : null; })
            .then(function (body) {
                if (!body) { return; }
                var mine = body.runs.filter(function (run) {
                    return run.kind === "check" && String(run.sample_id) === String(SAMPLE_ID);
                });
                renderRuns(mine);
                if (anyUnfinished(mine)) { pollTimer = setTimeout(poll, 5000); }
            })
            .catch(function () { pollTimer = setTimeout(poll, 15000); });
    }

    var initial = JSON.parse(document.getElementById("check-runs").textContent);
    renderRuns(initial);
    if (anyUnfinished(initial)) { pollTimer = setTimeout(poll, 5000); }

    if (!form) { return; }

    // --- the drop, only where the reads are not kept and not fetchable ------------------

    var dropzone = document.getElementById("breseq-dropzone");
    var fileInput = document.getElementById("breseq-file-input");
    var fileListEl = document.getElementById("breseq-file-list");
    var submitBtn = document.getElementById("check-submit");
    var errorEl = document.getElementById("check-error");
    var progressEl = document.getElementById("breseq-progress");
    var progressBar = document.getElementById("breseq-progress-bar");
    var progressText = document.getElementById("breseq-progress-text");

    function renderList() {
        if (!fileListEl) { return; }
        fileListEl.innerHTML = selected.length
            ? "<ul>" + selected.map(function (e) { return "<li>" + esc(e.path) + "</li>"; }).join("") + "</ul>"
            : "";
        submitBtn.disabled = READS_SOURCE === "drop" && !selected.length;
    }

    function addEntries(entries) {
        var seen = {};
        selected.forEach(function (e) { seen[e.path] = true; });
        entries.forEach(function (e) { if (!seen[e.path]) { selected.push(e); seen[e.path] = true; } });
        renderList();
    }

    if (dropzone) {
        dropzone.addEventListener("click", function () { fileInput.click(); });
        fileInput.addEventListener("change", function () {
            addEntries(mutintFromFileList(fileInput.files));
            fileInput.value = "";
        });
        ["dragenter", "dragover"].forEach(function (evt) {
            dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.style.background = "#eef6ff"; });
        });
        ["dragleave", "drop"].forEach(function (evt) {
            dropzone.addEventListener(evt, function (e) { e.preventDefault(); dropzone.style.background = "#fafafa"; });
        });
        dropzone.addEventListener("drop", function (e) { mutintCollectDropped(e.dataTransfer).then(addEntries); });
    }
    renderList();

    function setProgress(done, total, label) {
        progressEl.style.display = "";
        var pct = total ? Math.floor((done / total) * 100) : 0;
        progressBar.style.width = pct + "%";
        progressBar.textContent = pct + "%";
        progressText.textContent = label;
    }

    function checkedReadSteps() {
        return Array.prototype.map.call(
            document.querySelectorAll(".check-read-step:checked"),
            function (input) { return input.value; });
    }

    form.addEventListener("submit", function (e) {
        e.preventDefault();
        errorEl.innerHTML = "";
        submitBtn.disabled = true;
        var upload = selected.length
            ? mutintUpload(selected, {experimentId: EXPERIMENT_ID, consumer: COMPONENT, onProgress: setProgress})
            : Promise.resolve("");
        upload.then(function (uploadId) {
            setProgress(1, 1, "Queueing the check…");
            return mutintPostJson("/breseq/check/launch?experiment_id=" + EXPERIMENT_ID +
                                  "&sample_id=" + SAMPLE_ID, {
                upload_id: uploadId,
                reads_source: READS_SOURCE,
                arguments: document.getElementById("check-arguments").value,
                read_steps: checkedReadSteps(),
                population_sample: document.getElementById("check-population").checked,
                coverage_limit: document.getElementById("check-coverage-limit").value
            });
        }).then(function (body) {
            progressEl.style.display = "none";
            selected = [];
            renderList();
            renderRuns(body.runs || []);
            if (anyUnfinished(body.runs || [])) { pollTimer = setTimeout(poll, 5000); }
            submitBtn.disabled = false;
        }).catch(function (err) {
            progressEl.style.display = "none";
            errorEl.innerHTML = '<div class="alert alert-danger" style="margin-top: 1em;">' +
                                esc((err && err.message) || "The check could not be started.") + "</div>";
            submitBtn.disabled = false;
        });
    });
}());
