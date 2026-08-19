/*
 * slate.js — column sorting for the player table
 * No dependencies. Click a column header to sort; click again to reverse.
 */

(function () {
    "use strict";

    const table = document.getElementById("slate-table");
    if (!table) return;

    const headers = table.querySelectorAll("thead th");
    let sortCol = null;
    let sortAsc = true;

    headers.forEach(function (th, colIndex) {
        th.addEventListener("click", function () {
            if (sortCol === colIndex) {
                sortAsc = !sortAsc;
            } else {
                sortCol = colIndex;
                sortAsc = true;
            }

            // Update header classes
            headers.forEach(function (h) {
                h.classList.remove("sort-asc", "sort-desc");
            });
            th.classList.add(sortAsc ? "sort-asc" : "sort-desc");

            sortTable(colIndex, sortAsc);
        });
    });

    function sortTable(colIndex, ascending) {
        const tbody = table.querySelector("tbody");
        const rows  = Array.from(tbody.querySelectorAll("tr"));

        rows.sort(function (a, b) {
            const aText = a.cells[colIndex] ? a.cells[colIndex].textContent.trim() : "";
            const bText = b.cells[colIndex] ? b.cells[colIndex].textContent.trim() : "";

            // Strip non-numeric characters for numeric columns ($, ,, %)
            const aNum = parseFloat(aText.replace(/[^0-9.\-]/g, ""));
            const bNum = parseFloat(bText.replace(/[^0-9.\-]/g, ""));

            let result;
            if (!isNaN(aNum) && !isNaN(bNum)) {
                result = aNum - bNum;
            } else {
                result = aText.localeCompare(bText);
            }

            return ascending ? result : -result;
        });

        rows.forEach(function (row) {
            tbody.appendChild(row);
        });
    }
}());
