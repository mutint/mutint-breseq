/* The Evidence tab's behaviour: the inline Add form (core's mutation form, prefilled from
   breseq's own suggestion), Resolve with an existing mutation, Dismiss with a note, Reopen.

   Every write is a `mutintPost` to a JSON endpoint and a reload: the page is a listing, and a
   listing redrawn from the server cannot drift from what was written. */
(function () {
    "use strict";

    var config = window.mutintEvidenceConfig;
    if (!config) { return; }
    var EXPERIMENT_ID = config.experimentId;
    var SAMPLE_ID = config.sampleId;
    var NEARBY = config.nearbyBases;

    var rows = {};
    JSON.parse(document.getElementById("evidence-rows").textContent).forEach(function (row) {
        rows[String(row.id)] = row;
    });
    var nearby = JSON.parse(document.getElementById("evidence-nearby").textContent);

    var formBox = document.getElementById("ev-form");
    var form = null;
    var formFor = null;

    function fail(err) {
        swal("", (err && err.message) || "That did not work.", "error");
    }

    // --- Add mutation: core's form, inline, prefilled -----------------------------------

    function openForm(id) {
        var row = rows[id];
        if (!row || !formBox) { return; }
        formFor = id;
        var initial = {};
        Object.keys(row.suggestion || {}).forEach(function (key) {
            initial[key] = row.suggestion[key];
        });
        // `mutintMutationForm` reads the inputs once, so the form is built per opening
        // rather than once per page: a second row's prefill has to reach the parked values.
        form = mutintMutationForm(initial);
        if (initial.__type) {
            document.getElementById("me-type").value = initial.__type;
            document.getElementById("me-type").dispatchEvent(new Event("change"));
            // The change handler repopulates from the parked values, which the form's own
            // constructor already seeded from `initial`.
        }
        document.getElementById("ev-form-locus").textContent = row.type + " at " + row.locus;
        formBox.style.display = "";
        formBox.scrollIntoView({behavior: "smooth", block: "start"});
    }

    function closeForm() {
        formBox.style.display = "none";
        formFor = null;
    }

    function addAndResolve() {
        if (!form || !formFor) { return; }
        form.clearFieldErrors();
        var payload = {
            experiment_id: EXPERIMENT_ID,
            mutation_type: form.type(),
            frequency: document.getElementById("me-f-frequency").value,
            target_sample_ids: JSON.stringify([parseInt(SAMPLE_ID, 10)])
        };
        var fields = form.fields();
        Object.keys(fields).forEach(function (name) { payload[name] = fields[name]; });

        // Core's Add, then this plugin's resolve against what it added -- two writes,
        // because the second needs the mutation id only the first can mint, and because the
        // Add tab's rules about what a mutation may be stay the Add tab's.
        window.mutintPost("/curate/add/apply", payload).then(function (body) {
            if (!body.mutation_id) { throw new Error("The mutation was not added."); }
            return window.mutintPost("/breseq/evidence/resolve", {
                experiment_id: EXPERIMENT_ID,
                evidence_ids: JSON.stringify([parseInt(formFor, 10)]),
                mutation_id: body.mutation_id
            });
        }).then(function () {
            window.location.reload();
        }).catch(function (err) {
            document.getElementById("me-error").textContent = err.message;
            form.showFieldErrors(err.body && err.body.errors);
        });
    }

    // --- Resolve with an existing mutation ----------------------------------------------

    function resolveWith(id) {
        var row = rows[id];
        if (!row) { return; }
        var position = parseInt((row.locus.match(/:(\d+)/) || [0, 0])[1], 10);
        var seqId = row.locus.split(":")[0];
        var candidates = nearby.filter(function (m) {
            return m.seq_id === seqId && Math.abs(m.position - position) <= NEARBY;
        });
        if (!candidates.length) {
            swal("", "No mutation of this sample lies within " + NEARBY +
                 " bp of this evidence. Add one instead.", "info");
            return;
        }
        var select = document.createElement("select");
        select.className = "form-control";
        candidates.forEach(function (m) {
            var option = document.createElement("option");
            option.value = m.id;
            option.textContent = m.label;
            select.appendChild(option);
        });
        swal({
            title: "Resolve with which mutation?",
            text: "The mutation of this sample that accounts for " + row.type + " at " +
                  row.locus + ".",
            content: select,
            buttons: ["Cancel", "Resolve"]
        }).then(function (go) {
            if (!go) { return; }
            return window.mutintPost("/breseq/evidence/resolve", {
                experiment_id: EXPERIMENT_ID,
                evidence_ids: JSON.stringify([parseInt(id, 10)]),
                mutation_id: select.value
            }).then(function () { window.location.reload(); });
        }).catch(fail);
    }

    // --- Dismiss, reopen -----------------------------------------------------------------

    function dismiss(id) {
        var row = rows[id];
        swal({
            title: "Dismiss this evidence?",
            text: row.type + " at " + row.locus + ". Say why, for whoever reads this next.",
            content: {element: "input", attributes: {placeholder: "e.g. repeat-region artefact"}},
            buttons: ["Cancel", "Dismiss"]
        }).then(function (note) {
            if (note === null) { return; }
            return window.mutintPost("/breseq/evidence/dismiss", {
                experiment_id: EXPERIMENT_ID,
                evidence_ids: JSON.stringify([parseInt(id, 10)]),
                note: note || ""
            }).then(function () { window.location.reload(); });
        }).catch(fail);
    }

    function reopen(id) {
        window.mutintPost("/breseq/evidence/reopen", {
            experiment_id: EXPERIMENT_ID,
            evidence_ids: JSON.stringify([parseInt(id, 10)])
        }).then(function () { window.location.reload(); }).catch(fail);
    }

    document.addEventListener("click", function (e) {
        var button = e.target.closest ? e.target.closest("button[data-ev-add], " +
            "button[data-ev-resolve], button[data-ev-dismiss], button[data-ev-reopen]") : null;
        if (!button) { return; }
        e.preventDefault();
        if (button.hasAttribute("data-ev-add")) { openForm(button.getAttribute("data-ev-add")); }
        else if (button.hasAttribute("data-ev-resolve")) { resolveWith(button.getAttribute("data-ev-resolve")); }
        else if (button.hasAttribute("data-ev-dismiss")) { dismiss(button.getAttribute("data-ev-dismiss")); }
        else if (button.hasAttribute("data-ev-reopen")) { reopen(button.getAttribute("data-ev-reopen")); }
    });

    if (formBox) {
        document.getElementById("ev-form-apply").addEventListener("click", addAndResolve);
        document.getElementById("ev-form-cancel").addEventListener("click", closeForm);
    }
}());
