// freeweight.web.static.js.charts — hover detail for the server-rendered SVG charts.
//
// The charts are drawn on the server as inline SVG with a table of the same figures beside them
// (UI standards §5, §7: a chart is never the only representation of a critical figure). This file
// adds a tooltip on hover and keyboard focus, and nothing else. It draws nothing, fetches nothing,
// and re-themes nothing — every colour in the SVG is a design token, so the theme switch already
// re-themes the charts without any JavaScript at all.
(function () {
  "use strict";

  function describe(point, table, index) {
    var row = table && table.tBodies[0] ? table.tBodies[0].rows[index] : null;
    if (!row) { return null; }
    var cells = Array.prototype.slice.call(row.cells).map(function (cell) {
      return (cell.textContent || "").trim();
    });
    return cells.slice(1, 4).join(" · ");
  }

  function enhance() {
    var figures = document.querySelectorAll("figure.chart");
    Array.prototype.forEach.call(figures, function (figure) {
      var table = figure.querySelector("table");
      var points = figure.querySelectorAll("circle.point");
      Array.prototype.forEach.call(points, function (point, index) {
        var text = describe(point, table, index);
        if (!text) { return; }
        // A <title> child is the SVG-native tooltip: it is read by screen readers, shown on
        // hover by every browser, and needs no positioning code that could put it off-screen.
        var title = document.createElementNS("http://www.w3.org/2000/svg", "title");
        title.textContent = text;
        point.appendChild(title);
        point.setAttribute("tabindex", "0");
        point.setAttribute("role", "img");
        point.setAttribute("aria-label", text);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhance);
  } else {
    enhance();
  }
})();
