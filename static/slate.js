/*
 * slate.js — position filter, column sort, and lineup builder
 * No dependencies. Auth state and existing lineup loaded from window.APP.
 */
(function () {
    "use strict";

    const CAP             = window.APP.salaryCap;
    const WEEK            = window.APP.week;
    const YEAR            = window.APP.year;
    const IS_AUTH         = window.APP.isAuthenticated;
    const EXISTING_LINEUP = window.APP.existingLineup; // array of player objects or null

    const SLOTS = {
        QB:   { eligible: ["QB"],            label: "QB"   },
        RB1:  { eligible: ["RB"],            label: "RB"   },
        RB2:  { eligible: ["RB"],            label: "RB"   },
        WR1:  { eligible: ["WR"],            label: "WR"   },
        WR2:  { eligible: ["WR"],            label: "WR"   },
        WR3:  { eligible: ["WR"],            label: "WR"   },
        TE:   { eligible: ["TE"],            label: "TE"   },
        FLEX: { eligible: ["RB", "WR", "TE"],label: "FLEX" },
        DST:  { eligible: ["DST","D","DEF"], label: "DST"  },
    };

    // lineup: slotKey -> player object or null
    const lineup = {};
    Object.keys(SLOTS).forEach(k => lineup[k] = null);

    // ------------------------------------------------------------------
    // DOM refs
    // ------------------------------------------------------------------
    const table      = document.getElementById("slate-table");
    const salaryUsed = document.getElementById("salary-used");
    const salaryRem  = document.getElementById("salary-remaining");
    const submitBtn  = document.getElementById("submit-btn");   // null if not logged in
    const submitMsg  = document.getElementById("submit-msg");

    if (!table) return;

    const tbody = table.querySelector("tbody");
    const rows  = Array.from(tbody.querySelectorAll("tr.player-row"));

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    function totalSalary() {
        return Object.values(lineup).reduce((s, p) => s + (p ? p.salary : 0), 0);
    }

    function playerInLineup(name) {
        return Object.values(lineup).some(p => p && p.name === name);
    }

    function findOpenSlot(pos) {
        const up = pos.toUpperCase();
        for (const [key, def] of Object.entries(SLOTS)) {
            if (key === "FLEX") continue;
            if (def.eligible.map(e => e.toUpperCase()).includes(up) && !lineup[key]) return key;
        }
        const flex = SLOTS["FLEX"];
        if (flex.eligible.map(e => e.toUpperCase()).includes(up) && !lineup["FLEX"]) return "FLEX";
        return null;
    }

    function updateSalaryDisplay() {
        const used = totalSalary();
        const rem  = CAP - used;
        salaryUsed.textContent = "$" + used.toLocaleString();
        salaryUsed.classList.toggle("over", used > CAP);
        salaryRem.textContent  = "(" + (rem >= 0
            ? rem.toLocaleString()
            : "-" + Math.abs(rem).toLocaleString()) + " left)";
    }

    function updateSubmitButton() {
        if (!submitBtn) return;
        const allFilled = Object.values(lineup).every(p => p !== null);
        const underCap  = totalSalary() <= CAP;
        submitBtn.disabled = !(allFilled && underCap);
    }

    function renderSlot(slotKey) {
        const el       = document.getElementById("slot-" + slotKey);
        const clearBtn = document.querySelector(".slot-clear[data-slot='" + slotKey + "']");
        const p        = lineup[slotKey];

        if (p) {
            el.classList.remove("empty");
            el.classList.add("filled");
            el.innerHTML = "<span class='slot-name'>" + p.name + "</span>"
                         + "<span class='slot-salary'>$" + p.salary.toLocaleString() + "</span>";
            if (clearBtn) clearBtn.classList.remove("hidden");
        } else {
            el.classList.remove("filled");
            el.classList.add("empty");
            const eligible = SLOTS[slotKey].eligible.join(" / ");
            el.innerHTML = "<span class='slot-empty-text'>" + eligible + "</span>";
            if (clearBtn) clearBtn.classList.add("hidden");
        }
    }

    function refreshRowStates() {
        const used = totalSalary();
        rows.forEach(function (row) {
            const name    = row.dataset.name;
            const pos     = row.dataset.position.toUpperCase();
            const salary  = parseInt(row.dataset.salary, 10) || 0;
            const inTeam  = playerInLineup(name);
            const hasSlot = findOpenSlot(pos) !== null;
            const fits    = (used + salary) <= CAP;

            row.classList.toggle("selected",   inTeam);
            row.classList.toggle("ineligible", !inTeam && (!hasSlot || !fits));
        });
    }

    // ------------------------------------------------------------------
    // Pre-load existing lineup (if user already submitted this week)
    // ------------------------------------------------------------------
    if (EXISTING_LINEUP && EXISTING_LINEUP.length === 9) {
        // Map canonical slot names back to our internal slot keys
        // Slots in order: QB, RB1, RB2, WR1, WR2, WR3, TE, FLEX, DST
        const slotSequence = ["QB","RB1","RB2","WR1","WR2","WR3","TE","FLEX","DST"];
        const slotMap      = { QB:"QB", RB:"RB", WR:"WR", TE:"TE", FLEX:"FLEX", DST:"DST" };

        // Track how many RB/WR slots filled
        const counts = {};
        EXISTING_LINEUP.forEach(function (p) {
            const base = p.slot; // QB / RB / WR / TE / FLEX / DST
            if (base === "RB" || base === "WR") {
                counts[base] = (counts[base] || 0) + 1;
                const key = base + counts[base]; // RB1, RB2, WR1, WR2, WR3
                lineup[key] = { name: p.name, position: p.position, salary: p.salary, slot: key };
            } else {
                lineup[base] = { name: p.name, position: p.position, salary: p.salary, slot: base };
            }
        });

        Object.keys(SLOTS).forEach(renderSlot);
        updateSalaryDisplay();
        refreshRowStates();
        updateSubmitButton();
    }

    // ------------------------------------------------------------------
    // Position filter buttons
    // ------------------------------------------------------------------
    document.querySelectorAll(".filter-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            const pos = btn.dataset.pos;
            rows.forEach(function (row) {
                const rp = row.dataset.position.toUpperCase();
                row.classList.toggle("hidden-row", pos !== "ALL" && rp !== pos);
            });
        });
    });

    // ------------------------------------------------------------------
    // Column sorting
    // ------------------------------------------------------------------
    let sortCol = null, sortAsc = true;

    table.querySelectorAll("thead th").forEach(function (th, i) {
        th.addEventListener("click", function () {
            sortAsc = sortCol === i ? !sortAsc : true;
            sortCol = i;
            table.querySelectorAll("thead th").forEach(h => h.classList.remove("sort-asc","sort-desc"));
            th.classList.add(sortAsc ? "sort-asc" : "sort-desc");

            rows.slice().sort(function (a, b) {
                const at = a.cells[i] ? a.cells[i].textContent.trim() : "";
                const bt = b.cells[i] ? b.cells[i].textContent.trim() : "";
                const an = parseFloat(at.replace(/[^0-9.\-]/g, ""));
                const bn = parseFloat(bt.replace(/[^0-9.\-]/g, ""));
                const r  = (!isNaN(an) && !isNaN(bn)) ? an - bn : at.localeCompare(bt);
                return sortAsc ? r : -r;
            }).forEach(r => tbody.appendChild(r));
        });
    });

    // ------------------------------------------------------------------
    // Click a player row → add / remove
    // ------------------------------------------------------------------
    rows.forEach(function (row) {
        row.addEventListener("click", function () {
            const name   = row.dataset.name;
            const pos    = row.dataset.position.toUpperCase();
            const salary = parseInt(row.dataset.salary, 10) || 0;
            const proj   = parseFloat(row.dataset.proj)     || 0;

            // Remove if already in lineup
            if (playerInLineup(name)) {
                for (const key of Object.keys(lineup)) {
                    if (lineup[key] && lineup[key].name === name) {
                        lineup[key] = null;
                        renderSlot(key);
                    }
                }
                updateSalaryDisplay();
                refreshRowStates();
                updateSubmitButton();
                return;
            }

            // If not logged in, clicking does nothing (submit btn is replaced by login link)
            // We still allow building a lineup visually — they just can't submit
            const slot = findOpenSlot(pos);
            if (!slot) return;
            if (totalSalary() + salary > CAP) return;

            lineup[slot] = { name, position: pos, salary, proj, slot };
            renderSlot(slot);
            updateSalaryDisplay();
            refreshRowStates();
            updateSubmitButton();
        });
    });

    // ------------------------------------------------------------------
    // Clear individual slot button
    // ------------------------------------------------------------------
    document.querySelectorAll(".slot-clear").forEach(function (btn) {
        btn.addEventListener("click", function (e) {
            e.stopPropagation();
            const key = btn.dataset.slot;
            lineup[key] = null;
            renderSlot(key);
            updateSalaryDisplay();
            refreshRowStates();
            updateSubmitButton();
        });
    });

    // ------------------------------------------------------------------
    // Submit lineup (logged-in users only)
    // ------------------------------------------------------------------
    if (submitBtn) {
        submitBtn.addEventListener("click", function () {
            // Map internal slot keys to canonical server slot names
            const slotMap = {
                QB:"QB", RB1:"RB", RB2:"RB",
                WR1:"WR", WR2:"WR", WR3:"WR",
                TE:"TE", FLEX:"FLEX", DST:"DST"
            };

            const players = Object.entries(lineup).map(([key, p]) => ({
                name:     p.name,
                position: p.position,
                salary:   p.salary,
                slot:     slotMap[key]
            }));

            submitBtn.disabled    = true;
            submitMsg.textContent = "Submitting…";
            submitMsg.className   = "submit-msg";

            fetch("/submit-lineup", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ week: WEEK, year: YEAR, players })
            })
            .then(r => r.json())
            .then(function (data) {
                if (data.ok) {
                    submitMsg.textContent = "✓ " + data.message;
                    submitMsg.className   = "submit-msg success";
                    // Update/show the submitted banner
                    let banner = document.getElementById("existing-banner");
                    if (!banner) {
                        banner = document.createElement("div");
                        banner.className = "existing-banner";
                        banner.id        = "existing-banner";
                        document.getElementById("roster-slots").before(banner);
                    }
                    banner.innerHTML = "<span class='existing-icon'>✓</span>"
                        + "<div><strong>Lineup submitted</strong>"
                        + "<span class='existing-time'>just now</span></div>";
                } else {
                    submitMsg.textContent = "✗ " + data.error;
                    submitMsg.className   = "submit-msg error";
                    submitBtn.disabled    = false;
                }
            })
            .catch(function () {
                submitMsg.textContent = "Network error — try again.";
                submitMsg.className   = "submit-msg error";
                submitBtn.disabled    = false;
            });
        });
    }

    // ------------------------------------------------------------------
    // Init
    // ------------------------------------------------------------------
    updateSalaryDisplay();
    updateSubmitButton();

}());
