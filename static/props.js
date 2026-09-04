// static/props.js
// Pick-5 interaction for the Props page: clicking Over/Under on a
// prop card selects that pick for that prop (clicking the already-
// selected button deselects it); enforces exactly 5 selections total
// before Submit is enabled.

document.addEventListener("DOMContentLoaded", function () {
    if (!window.PROPS_APP || !window.PROPS_APP.isAuthenticated) return;

    const grid = document.getElementById("props-grid");
    if (!grid) return;

    const countEl = document.getElementById("props-count");
    const submitBtn = document.getElementById("props-submit-btn");
    const msgEl = document.getElementById("props-submit-msg");

    // selections: Map<propId, 'over'|'under'>
    const selections = new Map();

    // Pre-populate from any existing picks (server-rendered as "selected"
    // class already, so just seed the JS state to match)
    const existing = window.PROPS_APP.existingPicks || {};
    Object.keys(existing).forEach(function (propId) {
        selections.set(String(propId), existing[propId]);
    });

    function updateCountAndButton() {
        const count = selections.size;
        countEl.textContent = count + " / 5 selected";
        submitBtn.disabled = (count !== 5);
    }

    grid.addEventListener("click", function (e) {
        const btn = e.target.closest(".prop-pick-btn");
        if (!btn) return;

        const card = btn.closest(".prop-card");
        const propId = card.dataset.propId;
        const pick = btn.dataset.pick;   // 'over' or 'under'

        const currentlySelected = selections.get(propId);

        if (currentlySelected === pick) {
            // Clicking the already-selected button deselects it
            selections.delete(propId);
            card.querySelectorAll(".prop-pick-btn").forEach(b => b.classList.remove("selected"));
        } else {
            if (currentlySelected === undefined && selections.size >= 5) {
                msgEl.textContent = "You can only pick 5 props — deselect one first.";
                msgEl.className = "props-submit-error";
                return;
            }
            selections.set(propId, pick);
            card.querySelectorAll(".prop-pick-btn").forEach(function (b) {
                b.classList.toggle("selected", b.dataset.pick === pick);
            });
        }

        msgEl.textContent = "";
        updateCountAndButton();
    });

    submitBtn.addEventListener("click", function () {
        if (selections.size !== 5) return;

        const picks = Array.from(selections.entries()).map(function ([propId, pick]) {
            return { prop_bet_id: parseInt(propId, 10), pick: pick };
        });

        submitBtn.disabled = true;
        msgEl.textContent = "Submitting...";
        msgEl.className = "";

        fetch("/submit-props", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                year: window.PROPS_APP.year,
                week: window.PROPS_APP.week,
                picks: picks
            })
        })
            .then(function (r) { return r.json().then(data => ({ ok: r.ok, data: data })); })
            .then(function (result) {
                if (result.ok) {
                    msgEl.textContent = "Picks submitted!";
                    msgEl.className = "props-submit-success";
                } else {
                    msgEl.textContent = result.data.error || "Something went wrong.";
                    msgEl.className = "props-submit-error";
                }
                updateCountAndButton();
            })
            .catch(function () {
                msgEl.textContent = "Network error — please try again.";
                msgEl.className = "props-submit-error";
                updateCountAndButton();
            });
    });

    updateCountAndButton();
});
