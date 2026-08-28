// freeweight.web.static.js.dashboard — the dashboard's own two conveniences.
//
// Both are conveniences and neither is required: the dashboard is fully usable, filterable and
// drillable with JavaScript disabled (ADR-0020, UI standards §13).
//
//   1. Submitting the filter bar when a select changes, so choosing a suite does not also require
//      finding the Apply button. The button stays in the markup and stays functional.
//   2. Remembering which panel the reader last opened, so returning to the dashboard returns them
//      to where they were rather than to the top.
(function () {
  "use strict";

  var PANEL_KEY = "freeweight-dashboard-panel";

  function wireFilterAutoSubmit() {
    var form = document.querySelector("form.filter-bar");
    if (!form) { return; }
    Array.prototype.forEach.call(form.querySelectorAll("select"), function (select) {
      select.addEventListener("change", function () { form.submit(); });
    });
  }

  function wirePanelMemory() {
    var headings = document.querySelectorAll("main h3[id]");
    if (!headings.length) { return; }
    var remembered = null;
    try { remembered = window.localStorage.getItem(PANEL_KEY); } catch (error) { /* private mode */ }
    // Only restores when the reader arrived without their own anchor: an explicit link to
    // #heatmap must win over what they looked at yesterday.
    if (remembered && !window.location.hash) {
      var target = document.getElementById(remembered);
      if (target) { target.scrollIntoView({ block: "start" }); }
    }
    Array.prototype.forEach.call(headings, function (heading) {
      heading.addEventListener("click", function () {
        try { window.localStorage.setItem(PANEL_KEY, heading.id); } catch (error) { /* ignore */ }
      });
    });
  }

  function start() {
    wireFilterAutoSubmit();
    wirePanelMemory();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
