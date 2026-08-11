// v2-014 / V4 Phase 2: vanilla JS WebSocket-клиент для live-таймлайна.
function startDashboard(runId) {
  const eventsEl = document.getElementById("events");
  const wsStatus = document.getElementById("ws-status");
  const phaseEl = document.getElementById("run-phase");
  const stallEl = document.getElementById("run-stall");

  function refreshPhase() {
    fetch("/api/runs/" + runId)
      .then((r) => r.json())
      .then((data) => {
        if (!data || !data.run) return;
        if (phaseEl) phaseEl.textContent = data.run.phase || "—";
        if (stallEl) stallEl.textContent = data.run.stall_reason || "none";
      })
      .catch(() => {});
  }

  function addEventLine(data) {
    const line = document.createElement("div");
    line.className = "event-line" + (data.stream === "agent_events" ? " agent" : "");
    const typeLabel = data.stream === "agent_events" ? data.kind : data.type;
    line.innerHTML =
      '<span class="ev-id">#' + (data.id || "?") + '</span> ' +
      '<span class="ev-at">' + (data.at || "") + '</span> ' +
      '<span class="ev-type">' + typeLabel + '</span> ' +
      '<span class="ev-task">' + (data.task_id ? "task-" + data.task_id : "—") + '</span> ' +
      '<span class="ev-detail">' + JSON.stringify(data.detail || data.payload || {}) + '</span>';
    eventsEl.appendChild(line);
    eventsEl.scrollTop = eventsEl.scrollHeight;

    // Обновить статус задачи в таблице, если пришёл task_transition
    if (data.type === "task_transition" && data.detail && data.detail.to) {
      const statusEl = document.getElementById("status-" + data.task_id);
      if (statusEl) {
        statusEl.className = "status status-" + data.detail.to;
        statusEl.textContent = data.detail.to;
      }
    }
    if (
      data.type === "phase_changed" ||
      data.type === "budget_predicate_hit" ||
      data.type === "loop_stuck" ||
      data.type === "judge_approve_blocked"
    ) {
      refreshPhase();
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + location.host + "/ws/run/" + runId);
    ws.onopen = () => {
      wsStatus.textContent = "connected";
      wsStatus.className = "connected";
    };
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        addEventLine(data);
      } catch (e) {
        console.error("parse error", e);
      }
    };
    ws.onclose = () => {
      wsStatus.textContent = "disconnected, retrying in 2s…";
      wsStatus.className = "disconnected";
      setTimeout(connect, 2000);
    };
    ws.onerror = () => ws.close();
  }
  refreshPhase();
  connect();
}
