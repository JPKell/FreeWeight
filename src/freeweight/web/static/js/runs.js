// freeweight.web.static.js.runs — live-updates the run detail page over SSE.
//
// Connects to GET /api/v1/runs/{id}/events and applies each event to the status badge, the
// progress bar and the event log (templates/runs/detail.html).
//
// The page is already complete when this loads: the server rendered the run's status, tests and
// metrics, and it rendered the sequence of the last event it knew about into
// data-last-sequence. This connects with ?last_event_id=<that sequence>, so a page reloaded
// mid-run resumes exactly where the render left off — no gap, no duplicate. A dropped connection
// is handled by EventSource itself, which resends Last-Event-ID from the last frame's id; there
// is deliberately no reconnect logic here.
(function () {
  "use strict";

  // Every frame carries a named `event:` field, so EventSource's default "message" handler
  // never fires for any of them — each type has to be subscribed to by name. The list is the
  // server's own vocabulary (services/events.RUN_EVENT_TYPES); a type missing from it is simply
  // not rendered live, never silently swallowed as an unnamed message.
  var EVENT_TYPES = [
    "run.started", "run.progress", "run.completed", "run.failed", "run.cancelled",
    "run.interrupted", "run.degraded", "test.started", "test.progress", "test.completed",
    "test.skipped", "sample.started", "sample.completed", "sample.failed"
  ];

  var TERMINAL = {
    "run.completed": true,
    "run.failed": true,
    "run.cancelled": true,
    "run.interrupted": true
  };

  function setStatus(badge, status) {
    badge.textContent = status;
    badge.className = "status status-" + status;
  }

  function setProgress(bar, text, progress) {
    if (!progress || typeof progress.total !== "number" || progress.total <= 0) {
      return;
    }
    bar.max = progress.total;
    bar.value = progress.completed;
    text.textContent = progress.completed + " / " + progress.total + " samples";
  }

  function appendEvent(log, payload) {
    var item = document.createElement("li");
    var when = document.createElement("time");
    when.dateTime = payload.timestamp;
    when.textContent = payload.timestamp;
    var what = document.createElement("span");
    what.textContent = " " + payload.type + " — " + (payload.message || "");
    item.appendChild(when);
    item.appendChild(what);
    log.appendChild(item);
  }

  function connect(badge, bar, text, log) {
    var runId = badge.getAttribute("data-run-id");
    var last = badge.getAttribute("data-last-sequence") || "0";
    var url = "/api/v1/runs/" + encodeURIComponent(runId) +
      "/events?last_event_id=" + encodeURIComponent(last);
    var source = new EventSource(url);

    function handle(message) {
      var payload;
      try {
        payload = JSON.parse(message.data).payload;
      } catch (error) {
        return;
      }
      appendEvent(log, payload);
      setProgress(bar, text, payload.progress);
      if (payload.data && payload.data.status) {
        setStatus(badge, payload.data.status);
      }
      if (TERMINAL[payload.type]) {
        source.close();
        // The server-rendered tests and metrics tables were written before the run finished;
        // one reload replaces them with the final ones rather than this file duplicating the
        // template's rendering logic in JavaScript.
        window.location.reload();
      }
    }

    EVENT_TYPES.forEach(function (type) {
      source.addEventListener(type, handle);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var badge = document.getElementById("run-status");
    var bar = document.getElementById("run-progress");
    var text = document.getElementById("run-progress-text");
    var log = document.getElementById("run-events");
    if (!badge || !bar || !text || !log) {
      return;
    }
    if (badge.getAttribute("data-terminal") === "true") {
      text.textContent = "This run has finished.";
      return;
    }
    connect(badge, bar, text, log);
  });
})();
