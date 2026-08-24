/*
 * slate.js — column sorting, position filtering, and lineup builder
 * No dependencies. All state lives in the `lineup` object.
 */
(function () {
    "use strict";

    const CAP       = window.APP.salaryCap;
    const WEEK      = window.APP.week;
    const YEAR      = window.APP.year;

    // ------------------------------------------------------------------
    // Roster slot definitions
    // slot key -> { eligible positions[], display label }
    // ------------------------------------------------------------------
    const SLOTS = {
        QB:   { eligible: ["QB"],           label: "QB"   },
        RB1:  { eligible: ["RB"],           label: "RB"   },
        RB2:  { eligible: ["RB"],           label: "RB"   },
        WR1:  { eligible: ["WR"],           label: "WR"   },
        WR2:  { eligible: ["WR"],           label: "WR"   },
        WR3:  { eligible: ["WR"],           label: "WR"   },
        TE:   { eligible: ["TE"],           label: "TE"   },
        FLEX: { eligible: ["RB","WR","TE"], label: "FLEX" },
        DST:  { eligible: ["DST","D","DEF"],label: "DST"  },
    };

    // lineup: slotKey -> player object or null
    const lineup = {};
    Object.keys(SLOTS).forEach(k => lineup[k] = null);

    // ------------------------------------------------------------------
    // DOM refs
    // ------------------------------------------------------------------
    const table        = document.getElementById("slate-table");
    const salaryUsed   = document.getElementById("salary-used");
    const salaryRemain = document.getElementById("salary-remaining");
    const submitBtn    = document.getElementById("submit-btn");
    const submitMsg    = document.getElementById("submit-msg");
    const nameInput    = document.getElementById("submitter-name");

    if (!table) return; // no slate loaded

    const tbody  = table.querySelector("tbody");
    const rows   = Array.from(tbody.querySelectorAll("tr.player-row"));

    // ------------------------------------------------------------------
    // Position filter
    // ------------------------------------------------------------------
    document.querySelectorAll(".filter-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            const pos = btn.dataset.pos;
            rows.forEach(function (row) {
                const rowPos = row.dataset.position.toUpperCase();
                if (pos === "ALL" || rowPos === pos) {
                    row.classList.remove("hidden-row");
                } else {
                    row.classList.add("hidden-row");
                }
            });
        });
    });

    // ------------------------------------------------------------------
    // Column sorting
    // ------------------------------------------------------------------
    let sortCol = null;
    let sortAsc = true;

    table.querySelectorAll("thead th").forEach(function (th, colIndex) {
        th.addEventListener("click", function () {
            if (sortCol === colIndex) {
                sortAsc = !sortAsc;
            } else {
                sortCol = colIndex;
                sortAsc = true;
            }
            table.querySelectorAll("thead th").forEach(h => h.classList.remove("sort-asc", "sort-desc"));
            th.classList.add(sortAsc ? "sort-asc" : "sort-desc");

            const sorted = rows.slice().sort(function (a, b) {
                const aText = a.cells[colIndex] ? a.cells[colIndex].textContent.trim() : "";
                const bText = b.cells[colIndex] ? b.cells[colIndex].textContent.trim() : "";
                const aNum  = parseFloat(aText.replace(/[^0-9.\-]/g, ""));
                const bNum  = parseFloat(bText.replace(/[^0-9.\-]/g, ""));
                let result  = (!isNaN(aNum) && !isNaN(bNum))
                    ? aNum - bNum
                    : aText.localeCompare(bText);
                return sortAsc ? result : -result;
            });
            sorted.forEach(r => tbody.appendChild(r));
        });
    });

    // ------------------------------------------------------------------
    // Lineup builder — helpers
    // ------------------------------------------------------------------

    function totalSalary() {
        return Object.values(lineup).reduce(function (sum, p) {
            return sum + (p ? p.salary : 0);
        }, 0);
    }

    function playerInLineup(name) {
        return Object.values(lineup).some(p => p && p.name === name);
    }

    // Find the first empty slot that can accept `pos`
    function findOpenSlot(pos) {
        const posUp = pos.toUpperCase();
        // Try primary slots first (not FLEX)
        for (const [key, def] of Object.entries(SLOTS)) {
            if (key === "FLEX") continue;
            if (def.eligible.map(e => e.toUpperCase()).includes(posUp) && !lineup[key]) {
                return key;
            }
        }
        // Try FLEX
        const flexDef = SLOTS["FLEX"];
        if (flexDef.eligible.map(e => e.toUpperCase()).includes(posUp) && !lineup["FLEX"]) {
            return "FLEX";
        }
        return null;
    }

    function updateSalaryDisplay() {
        const used = totalSalary();
        const rem  = CAP - used;
        salaryUsed.textContent  = "$" + used.toLocaleString();
        salaryUsed.classList.toggle("over", used > CAP);
        salaryRemain.textContent = "(" + (rem >= 0 ? rem.toLocaleString() : "-" + Math.abs(rem).toLocaleString()) + " left)";
    }

    function updateSubmitButton() {
        const allFilled = Object.values(lineup).every(p => p !== null);
        const nameOk    = nameInput && nameInput.value.trim().length > 0;
        const underCap  = totalSalary() <= CAP;
        submitBtn.disabled = !(allFilled && nameOk && underCap);
    }

    // Render a filled slot
    function renderSlot(slotKey) {
        const el     = document.getElementById("slot-" + slotKey);
        const clearBtn = document.querySelector(".slot-clear[data-slot='" + slotKey + "']");
        const p      = lineup[slotKey];

        if (p) {
            el.classList.remove("empty");
            el.classList.add("filled");
            el.innerHTML = "<span class='slot-name'>" + p.name + "</span>"
                         + "<span class='slot-salary'>$" + p.salary.toLocaleString() + "</span>";
            if (clearBtn) clearBtn.classList.remove("hidden");
        } else {
            el.classList.remove("filled");
            el.classList.add("empty");
            const def = SLOTS[slotKey];
            el.innerHTML = "<span class='slot-empty-text'>"
                + (def.eligible.join(" / "))
                + "</span>";
            if (clearBtn) clearBtn.classList.add("hidden");
        }
    }

    // Mark rows as selected / ineligible
    function refreshRowStates() {
        const used = totalSalary();

        rows.forEach(function (row) {
            const name   = row.dataset.name;
            const pos    = row.dataset.position.toUpperCase();
            const salary = parseInt(row.dataset.salary, 10) || 0;

            const alreadyIn  = playerInLineup(name);
            const hasOpenSlot = findOpenSlot(pos) !== null;
            const wouldFit   = (used + salary) <= CAP;

            row.classList.toggle("selected",   alreadyIn);
            row.classList.toggle("ineligible", !alreadyIn && (!hasOpenSlot || !wouldFit));
        });
    }

    // ------------------------------------------------------------------
    // Click a player row → add to lineup
    // ------------------------------------------------------------------
    rows.forEach(function (row) {
        row.addEventListener("click", function () {
            const name   = row.dataset.name;
            const pos    = row.dataset.position.toUpperCase();
            const salary = parseInt(row.dataset.salary, 10) || 0;
            const proj   = parseFloat(row.dataset.proj) || 0;

            // If already in lineup, remove them
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

            // Find slot
            const slot = findOpenSlot(pos);
            if (!slot) return; // no open slot — row should be ineligible anyway

            // Check cap
            if (totalSalary() + salary > CAP) return;

            lineup[slot] = { name, position: pos, salary, proj, slot };
            renderSlot(slot);
            updateSalaryDisplay();
            refreshRowStates();
            updateSubmitButton();
        });
    });

    // ------------------------------------------------------------------
    // Clear individual slot
    // ------------------------------------------------------------------
    document.querySelectorAll(".slot-clear").forEach(function (btn) {
        btn.addEventListener("click", function (e) {
            e.stopPropagation();
            const slotKey = btn.dataset.slot;
            lineup[slotKey] = null;
            renderSlot(slotKey);
            updateSalaryDisplay();
            refreshRowStates();
            updateSubmitButton();
        });
    });

    // ------------------------------------------------------------------
    // Name input → re-check submit button
    // ------------------------------------------------------------------
    if (nameInput) {
        nameInput.addEventListener("input", updateSubmitButton);
    }

    // ------------------------------------------------------------------
    // Submit lineup
    // ------------------------------------------------------------------
    if (submitBtn) {
        submitBtn.addEventListener("click", function () {
            const submitter = nameInput.value.trim();
            if (!submitter) return;

            // Build payload — map slot keys to canonical slot names for the server
            const slotMap = {
                QB: "QB", RB1: "RB", RB2: "RB",
                WR1: "WR", WR2: "WR", WR3: "WR",
                TE: "TE", FLEX: "FLEX", DST: "DST"
            };

            const players = Object.entries(lineup).map(function ([key, p]) {
                return {
                    name:     p.name,
                    position: p.position,
                    salary:   p.salary,
                    slot:     slotMap[key]
                };
            });

            submitBtn.disabled = true;
            submitMsg.textContent = "Submitting…";
            submitMsg.className = "submit-msg";

            fetch("/submit-lineup", {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify({ submitter, week: WEEK, year: YEAR, players })
            })
            .then(r => r.json())
            .then(function (data) {
                if (data.ok) {
                    submitMsg.textContent = "✓ " + data.message;
                    submitMsg.className   = "submit-msg success";
                } else {
                    submitMsg.textContent = "✗ " + data.error;
                    submitMsg.className   = "submit-msg error";
                    submitBtn.disabled = false;
                }
            })
            .catch(function () {
                submitMsg.textContent = "Network error — try again.";
                submitMsg.className   = "submit-msg error";
                submitBtn.disabled = false;
            });
        });
    }

    // ------------------------------------------------------------------
    // Init
    // ------------------------------------------------------------------
    updateSalaryDisplay();
    updateSubmitButton();

}());
