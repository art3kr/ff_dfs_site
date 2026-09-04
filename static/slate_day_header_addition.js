/*
 * ADD THIS to static/slate.js — a small, self-contained addition to
 * hide day-header rows when the position filter leaves nothing visible
 * underneath them (e.g. filtering to "QB" on a day with no QBs playing).
 *
 * Call updateDayHeaderVisibility() as the LAST step inside your
 * existing filter button click handler, right after you've already
 * set each .player-row's display style for the new filter. It works
 * as a pure post-processing pass over whatever visibility state
 * already exists, so it doesn't need to know how your filter logic
 * itself decides which rows to show.
 */

function updateDayHeaderVisibility() {
    const allRows = Array.from(document.querySelectorAll("#slate-table tbody tr"));
    let currentHeader = null;
    let hasVisiblePlayerSinceHeader = false;

    function finalizePreviousHeader() {
        if (currentHeader) {
            currentHeader.style.display = hasVisiblePlayerSinceHeader ? "" : "none";
        }
    }

    allRows.forEach(function (row) {
        if (row.classList.contains("day-header-row")) {
            finalizePreviousHeader();
            currentHeader = row;
            hasVisiblePlayerSinceHeader = false;
        } else if (row.classList.contains("player-row")) {
            if (row.style.display !== "none") {
                hasVisiblePlayerSinceHeader = true;
            }
        }
    });
    finalizePreviousHeader();   // handle the last group
}

/*
 * Example of wiring it in — this is illustrative of the pattern, adjust
 * to match your actual existing click handler:
 *
 * document.querySelectorAll(".filter-btn").forEach(function (btn) {
 *     btn.addEventListener("click", function () {
 *         // ... your existing logic that sets each .player-row's
 *         //     display style based on the clicked filter ...
 *
 *         updateDayHeaderVisibility();   // <-- add this one line at the end
 *     });
 * });
 *
 * Also call updateDayHeaderVisibility() once on page load (outside any
 * click handler), in case the page ever renders with a non-"ALL"
 * filter pre-selected.
 */
