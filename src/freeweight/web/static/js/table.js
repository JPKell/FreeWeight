// freeweight.web.static.js.table — progressive enhancement for dense tables.
//
// ADR-0020: the page is complete before this file loads. Every table it touches is already
// rendered, already readable and already sortable by reloading with a different query — this adds
// client-side sorting and column visibility on top, and removes nothing if it never runs.
//
// Two rules from UI standards §5 are load-bearing here:
//
//   * **Sorting applies to the whole dataset, not the visible page.** A table that holds one page
//     of a longer result set therefore does *not* get a client-side sort control; it gets a link
//     that re-queries the server. `data-sortable` is set only where the server rendered every row
//     the query matched, and this file checks `data-complete` before wiring a header.
//   * **An unsupported value is not a small number.** Sorting puts em-dash cells last in both
//     directions rather than treating them as zero, because a machine that could not measure
//     something did not measure it as the worst result.
(function () {
  "use strict";

  var EM_DASH = "—";
  var STORAGE_PREFIX = "freeweight-columns:";

  function textOf(cell) {
    return (cell.textContent || "").trim();
  }

  function isUnsupported(cell) {
    return textOf(cell).indexOf(EM_DASH) === 0;
  }

  function numericValue(cell) {
    var parsed = parseFloat(textOf(cell).replace(/[^0-9eE+.-]/g, ""));
    return isNaN(parsed) ? null : parsed;
  }

  function compare(a, b, index, numeric) {
    var left = a.cells[index];
    var right = b.cells[index];
    if (!left || !right) { return 0; }
    var leftMissing = isUnsupported(left);
    var rightMissing = isUnsupported(right);
    // Missing sorts last in both directions: the caller flips the sign of the comparison for a
    // descending sort, so the missing cases are returned pre-flipped to survive it.
    if (leftMissing && rightMissing) { return 0; }
    if (leftMissing) { return Number.POSITIVE_INFINITY; }
    if (rightMissing) { return Number.NEGATIVE_INFINITY; }
    if (numeric) {
      var lv = numericValue(left);
      var rv = numericValue(right);
      if (lv === null && rv === null) { return 0; }
      if (lv === null) { return 1; }
      if (rv === null) { return -1; }
      return lv - rv;
    }
    return textOf(left).localeCompare(textOf(right));
  }

  function sortBy(table, index, ascending) {
    var body = table.tBodies[0];
    if (!body) { return; }
    var header = table.tHead.rows[0].cells[index];
    var numeric = header.classList.contains("numeric");
    var rows = Array.prototype.slice.call(body.rows);
    rows.sort(function (a, b) {
      var result = compare(a, b, index, numeric);
      if (result === Number.POSITIVE_INFINITY || result === Number.NEGATIVE_INFINITY) {
        return result === Number.POSITIVE_INFINITY ? 1 : -1;
      }
      return ascending ? result : -result;
    });
    rows.forEach(function (row) { body.appendChild(row); });
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (cell) {
      cell.removeAttribute("aria-sort");
    });
    header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
  }

  function wireSorting(table) {
    if (table.getAttribute("data-complete") !== "true") { return; }
    var head = table.tHead;
    if (!head || !head.rows.length) { return; }
    Array.prototype.forEach.call(head.rows[0].cells, function (cell, index) {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = cell.textContent.trim();
      button.setAttribute("aria-label", "Sort by " + cell.textContent.trim());
      var ascending = true;
      button.addEventListener("click", function () {
        sortBy(table, index, ascending);
        ascending = !ascending;
      });
      cell.textContent = "";
      cell.appendChild(button);
    });
  }

  function storageKey(table) {
    return STORAGE_PREFIX + (table.getAttribute("data-table") || "table");
  }

  function readHidden(table) {
    try {
      var raw = window.localStorage.getItem(storageKey(table));
      return raw ? JSON.parse(raw) : [];
    } catch (error) {
      return [];
    }
  }

  function writeHidden(table, hidden) {
    try {
      window.localStorage.setItem(storageKey(table), JSON.stringify(hidden));
    } catch (error) { /* a per-browser convenience, never required for correctness */ }
  }

  function applyHidden(table, hidden) {
    var head = table.tHead;
    if (!head || !head.rows.length) { return; }
    Array.prototype.forEach.call(head.rows[0].cells, function (cell, index) {
      var visible = hidden.indexOf(index) === -1;
      cell.hidden = !visible;
      Array.prototype.forEach.call(table.tBodies, function (body) {
        Array.prototype.forEach.call(body.rows, function (row) {
          if (row.cells[index]) { row.cells[index].hidden = !visible; }
        });
      });
    });
  }

  function wireColumnVisibility(table) {
    var head = table.tHead;
    if (!head || !head.rows.length || head.rows[0].cells.length < 6) { return; }
    var hidden = readHidden(table);
    var details = document.createElement("details");
    var summary = document.createElement("summary");
    summary.textContent = "Columns";
    details.appendChild(summary);
    Array.prototype.forEach.call(head.rows[0].cells, function (cell, index) {
      var id = storageKey(table) + ":" + index;
      var label = document.createElement("label");
      label.setAttribute("for", id);
      label.style.marginRight = "0.75rem";
      var box = document.createElement("input");
      box.type = "checkbox";
      box.id = id;
      box.checked = hidden.indexOf(index) === -1;
      box.addEventListener("change", function () {
        var position = hidden.indexOf(index);
        if (box.checked && position !== -1) { hidden.splice(position, 1); }
        if (!box.checked && position === -1) { hidden.push(index); }
        writeHidden(table, hidden);
        applyHidden(table, hidden);
      });
      label.appendChild(box);
      label.appendChild(document.createTextNode(" " + cell.textContent.trim()));
      details.appendChild(label);
    });
    table.parentNode.insertBefore(details, table);
    applyHidden(table, hidden);
  }

  function enhance() {
    var tables = document.querySelectorAll("table[data-table]");
    Array.prototype.forEach.call(tables, function (table) {
      wireColumnVisibility(table);
      if (table.getAttribute("data-sortable") === "true") { wireSorting(table); }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhance);
  } else {
    enhance();
  }
})();
