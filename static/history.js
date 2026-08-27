/*
 * history.js — year/week selector, column sorting, position filter
 * for the /history page. No dependencies.
 */
(function () {
    "use strict";

    // ------------------------------------------------------------------
    // Year/week selector
    // ------------------------------------------------------------------
    const yearSelect = document.getElementById("year-select");
    const weekSelect = document.getElementById("week-select");
    const form       = document.getElementById("history-selector");
    const weeksByYear = window.HISTORY_WEEKS_BY_YEAR || {};

    if (yearSelect && weekSelect && form) {
        // When year changes, repopulate the week dropdown with that
        // year's available weeks, then auto-submit.
        yearSelect.addEventListener("change", function () {
            const year  = yearSelect.value;
            const weeks = weeksByYear[year] || [];

            weekSelect.innerHTML = "";
            weeks.forEach(function (w) {
                const opt = document.createElement("option");
                opt.value = w;
                opt.textContent = "Week " + w;
                weekSelect.appendChild(opt);
            });

            form.submit();
        });

        weekSelect.addEventListener("change", function () {
            form.submit();
        });
    }

    // ------------------------------------------------------------------
    // Position filter
    // ------------------------------------------------------------------
    const table = document.getElementById("history-table");
    if (!table) return;

    const tbody = table.querySelector("tbody");
    const rows  = Array.from(tbody.querySelectorAll("tr"));

    document.querySelectorAll(".filter-btn").forEach(function (btn) {
        btn.addEventListener("click", function () {
            document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            const pos = btn.dataset.pos;
            rows.forEach(function (row) {
                const rowPos = row.className.replace("pos-", "").toUpperCase();
                row.style.display = (pos === "ALL" || rowPos === pos) ? "" : "none";
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
            table.querySelectorAll("thead th").forEach(h => h.classList.remove("sort-asc", "sort-desc"));
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

}());
