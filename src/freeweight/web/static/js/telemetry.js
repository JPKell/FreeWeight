// freeweight.web.static.js.telemetry — live-updates the telemetry bar over SSE.
//
// Connects to GET /api/v1/system/telemetry/stream and applies each "telemetry.sampled" event to
// the bar's fixed-width fields (partials/telemetry_bar.html). Reconnection on a dropped
// connection is the browser's own EventSource behaviour — no reconnect logic is written here.
(function () {
  "use strict";

  var UNSUPPORTED = "unsupported";
  var GB = 1024 * 1024 * 1024;

  function formatPercent(value) {
    return value === UNSUPPORTED ? "—" : String(Math.round(value));
  }

  function formatTemperature(value) {
    return value === UNSUPPORTED ? "—" : String(Math.round(value));
  }

  function formatPower(value) {
    return value === UNSUPPORTED ? "—" : String(Math.round(value));
  }

  function formatGigabytes(value) {
    return value === UNSUPPORTED ? "—" : (value / GB).toFixed(1);
  }

  function setField(bar, name, text, reason) {
    var el = bar.querySelector('[data-field="' + name + '"]');
    if (el === null) {
      return;
    }
    el.textContent = text;
    if (reason) {
      el.title = reason;
    } else {
      el.removeAttribute("title");
    }
  }

  function applySnapshot(bar, snapshot) {
    var reasons = snapshot.unavailable_reasons || {};
    setField(bar, "cpu_percent", formatPercent(snapshot.cpu_percent), reasons.cpu_percent);
    setField(
      bar,
      "cpu_temperature_c",
      formatTemperature(snapshot.cpu_temperature_c),
      reasons.cpu_temperature_c
    );
    setField(bar, "ram_used_bytes", formatGigabytes(snapshot.ram_used_bytes), reasons.ram_used_bytes);
    setField(
      bar,
      "ram_total_bytes",
      formatGigabytes(snapshot.ram_total_bytes),
      reasons.ram_total_bytes
    );

    var gpu = snapshot.gpus && snapshot.gpus.length > 0 ? snapshot.gpus[0] : null;
    var gpuReason = reasons.gpu || (gpu === null ? "no GPU detected" : undefined);
    if (gpu === null) {
      setField(bar, "gpu_utilization_percent", "—", gpuReason);
      setField(bar, "gpu_temperature_c", "—", gpuReason);
      setField(bar, "gpu_power_watts", "—", gpuReason);
      setField(bar, "gpu_vram_used_bytes", "—", gpuReason);
      setField(bar, "gpu_vram_total_bytes", "—", gpuReason);
      return;
    }
    setField(bar, "gpu_utilization_percent", formatPercent(gpu.utilization_percent));
    setField(bar, "gpu_temperature_c", formatTemperature(gpu.temperature_c));
    setField(bar, "gpu_power_watts", formatPower(gpu.power_watts));
    setField(bar, "gpu_vram_used_bytes", formatGigabytes(gpu.vram_used_bytes));
    setField(bar, "gpu_vram_total_bytes", formatGigabytes(gpu.vram_total_bytes));
  }

  function connect(bar) {
    var source = new EventSource("/api/v1/system/telemetry/stream");
    source.addEventListener("telemetry.sampled", function (event) {
      var envelope;
      try {
        envelope = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      var snapshot = envelope && envelope.payload && envelope.payload.data;
      if (snapshot) {
        applySnapshot(bar, snapshot);
      }
    });
    return source;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var bar = document.getElementById("telemetry-bar");
    if (bar === null || typeof EventSource === "undefined") {
      return;
    }
    connect(bar);
  });
})();
