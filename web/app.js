/* 漂移时钟校时控制台前端：原生 JS，1 秒轮询，无外部依赖。 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let state = null;
let activeTab = "candidate";

// ---------- 工具 ----------

function fmtTime(ms) {
  if (ms === null || ms === undefined) return "—";
  const d = new Date(ms);
  const p = (n, l = 2) => String(n).padStart(l, "0");
  return `${p(d.getUTCFullYear())}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
         `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}.${p(d.getUTCMilliseconds(), 3)}`;
}

function fmtClock(ms) {
  if (ms === null || ms === undefined) return "—";
  const d = new Date(ms);
  const p = (n, l = 2) => String(n).padStart(l, "0");
  return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}.${p(d.getUTCMilliseconds(), 3)}`;
}

function fmtSigned(ms) {
  if (ms === null || ms === undefined) return "—";
  const sign = ms >= 0 ? "+" : "";
  return `${sign}${ms.toFixed(2)} ms`;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function api(path, body) {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(path, opt);
  const data = await resp.json();
  if (!resp.ok || data.ok === false) {
    alert(`操作失败：${data.error || data.reason || resp.status}`);
    throw new Error("api error");
  }
  return data;
}

const STATE_LABELS = {
  calibrated: "校时点",
  interpolated: "插值",
  extrapolated: "外推",
  uncalibrated: "无校时",
};

// ---------- 渲染：顶部 ----------

function renderTop() {
  $("#serverClock").textContent = `参考时钟(UTC) ${fmtClock(state.server_time_ms)}`;
  const sel = $("#scenarioSelect");
  if (sel.options.length === 0) {
    // 场景列表首次填充
    api("/api/scenarios").then((d) => {
      sel.innerHTML = d.scenarios.map((s) =>
        `<option value="${s.name}">${esc(s.label)}</option>`).join("");
      sel.value = state.scenario;
    });
  } else {
    sel.value = state.scenario;
  }
}

// ---------- 渲染：设备卡片 ----------

function renderDevices() {
  const box = $("#deviceCards");
  box.innerHTML = state.devices.map((d) => `
    <div class="device-card">
      <h3>${esc(d.device_id)}
        <span class="pill ${d.running ? "running" : "paused"}">${d.running ? "上传中" : "已暂停"}</span>
        <span class="pill">boot #${d.boot_epoch}</span>
      </h3>
      <div class="device-meta">
        <span>当前漂移率 <b>${d.rate.toFixed(5)}</b></span>
        <span>本 boot 序号 <b>${d.seq}</b></span>
        <span>设备读数 <b>${fmtClock(d.device_time_ms)}</b></span>
        <span>上传间隔 <b>${d.interval_ms} ms</b></span>
      </div>
      <div class="device-actions">
        <label>漂移率
          <input type="number" step="0.001" value="${d.rate}" id="rate-${d.device_id}" style="min-width:90px" />
        </label>
        <button class="btn tiny" onclick="setRate('${d.device_id}')">应用漂移</button>
        <button class="btn tiny" onclick="toggleDevice('${d.device_id}', ${!d.running})">
          ${d.running ? "暂停设备" : "恢复设备"}
        </button>
        <button class="btn tiny warn" onclick="restartDevice('${d.device_id}')">重启设备(复用序号)</button>
      </div>
    </div>`).join("");

  // 校时表单的设备下拉
  $("#calDevice").innerHTML = state.devices.map((d) =>
    `<option value="${esc(d.device_id)}">${esc(d.device_id)}</option>`).join("");
  const dev = state.devices[0];
  if (dev) {
    $("#calBoot").innerHTML =
      `<option value="">当前 boot (#${dev.boot_epoch})</option>` +
      Array.from({ length: dev.boot_epoch }, (_, i) =>
        `<option value="${i + 1}">#${i + 1}</option>`).join("");
  }
}

window.setRate = async (deviceId) => {
  const rate = parseFloat($(`#rate-${deviceId}`).value);
  if (!(rate > 0)) return alert("漂移率必须是正数");
  await api("/api/devices/rate", { device_id: deviceId, rate });
  refresh();
};

window.toggleDevice = async (deviceId, run) => {
  await api(run ? "/api/devices/resume" : "/api/devices/pause", { device_id: deviceId });
  refresh();
};

window.restartDevice = async (deviceId) => {
  await api("/api/devices/restart", { device_id: deviceId });
  refresh();
};

// ---------- 渲染：校时记录 ----------

function renderCalibrations() {
  const cals = state.receiver.calibrations.slice().reverse();
  if (!cals.length) {
    $("#calList").innerHTML = `<div class="empty-note">暂无校时记录</div>`;
    return;
  }
  $("#calList").innerHTML = cals.map((c) => `
    <div class="cal-item ${c.is_backfill ? "backfill" : ""}">
      <span>${esc(c.id)}</span>
      <span>${esc(c.device_id)} #b${c.boot_epoch}</span>
      ${c.is_backfill ? '<span class="tag-backfill">迟到补校</span>' : "<span>即时</span>"}
      <span>设备 ${fmtClock(c.device_time_ms)}</span>
      <span>参考 ${fmtClock(c.ref_time_ms)}</span>
      <span title="到达接收服务的时刻">到达 ${fmtClock(c.received_at_ms)}</span>
    </div>`).join("");
}

$("#calNowBtn").onclick = async () => {
  await api("/api/calibrations", {
    device_id: $("#calDevice").value,
    boot_epoch: $("#calBoot").value ? parseInt($("#calBoot").value, 10) : null,
    device_time_ms: $("#calDeviceTime").value || null,
    ref_time_ms: $("#calRefTime").value || null,
    note: $("#calNote").value,
  });
  $("#calDeviceTime").value = ""; $("#calRefTime").value = ""; $("#calNote").value = "";
  refresh();
};

$("#calBackfillBtn").onclick = async () => {
  const devId = $("#calDevice").value;
  const dev = state.devices.find((d) => d.device_id === devId);
  // 向前补 20 秒的设备读数；参考时刻同样向前 20 秒
  const backMs = 20_000;
  await api("/api/calibrations", {
    device_id: devId,
    boot_epoch: $("#calBoot").value ? parseInt($("#calBoot").value, 10) : dev.boot_epoch,
    device_time_ms: dev.device_time_ms - backMs,
    ref_time_ms: state.server_time_ms - backMs,
    note: "手动补校",
  });
  refresh();
};

// ---------- 渲染：计算器 / 入库端 ----------

function renderCalculator() {
  const calc = state.calculator;
  const paused = calc.paused;
  $("#calcPauseBtn").disabled = paused;
  $("#calcResumeBtn").disabled = !paused;
  const pill = $("#calcState");
  pill.className = `state-pill ${paused ? "paused" : "running"}`;
  pill.textContent = paused ? "● 已暂停（触发排队中）" : "● 运行中";

  $("#jobList").innerHTML = calc.jobs.slice(0, 12).map((j) => `
    <div class="job-item ${j.status}">
      <span>作业 #${j.job_id}</span>
      <span>耗时 ${j.compute_delay_ms.toFixed(0)}ms</span>
      <span>水位 ${j.watermark}</span>
      <span>${j.status === "accepted" ? `→ v${j.version}` : j.status === "rejected" ? "已拒绝(旧结果)" : "计算中"}</span>
      ${j.status === "rejected" ? `<span style="color:var(--bad)">${esc(j.detail)}</span>` : ""}
    </div>`).join("") || `<div class="empty-note">尚无计算作业</div>`;
}

function renderPublisher() {
  const pub = state.publisher;
  $("#autoConfirm").checked = pub.auto_confirm;
  $("#publishState").innerHTML = `
    <div>当前发布版：<b>${pub.current_version === null ? "尚未发布" : "v" + pub.current_version}</b>
      （自动确认：${pub.auto_confirm ? "开" : "关"}）</div>
    <div style="margin-top:6px">历史发布：${
      pub.history.length
        ? pub.history.map((h) => `v${h.version}[${h.strategy === "local_recompute" ? "局部" : h.strategy === "mixed" ? "混合" : "重建"}]`).join(" → ")
        : "无"
    }</div>
    ${pub.last_reject ? `<div class="reject" style="margin-top:6px">栅栏拒绝：${esc(pub.last_reject.reason)}（尝试 v${pub.last_reject.attempted_version}，当前 v${pub.last_reject.current_version}）</div>` : ""}`;
}

$("#calcPauseBtn").onclick = async () => { await api("/api/calculator/pause", {}); refresh(); };
$("#calcResumeBtn").onclick = async () => { await api("/api/calculator/resume", {}); refresh(); };
$("#autoConfirm").onchange = async (e) => {
  await api("/api/publisher/auto", { enabled: e.target.checked }); refresh();
};
$("#confirmBtn").onclick = async () => {
  try { await api("/api/publisher/confirm", {}); } catch (_) { /* 弹窗已提示 */ }
  refresh();
};

// ---------- 渲染：时间轴 ----------

function currentVersionData() {
  return activeTab === "candidate" ? state.candidate : state.published;
}

function renderTimeline() {
  const v = currentVersionData();
  const box = $("#boundaryBox");
  if (!v) {
    box.innerHTML = `<div class="empty-note">${activeTab === "candidate" ? "尚无候选版本" : "尚无已发布版本（关闭自动确认后需手动确认）"}</div>`;
    $("#timelineTable tbody").innerHTML = "";
    return;
  }
  box.innerHTML =
    `<div class="hint" style="margin:0 0 6px">版本 v${v.version} · 策略：${esc(v.strategy)} · ${esc(v.strategy_reason || "—")} · 重算 ${v.n_recomputed ?? v.recomputed_measurement_ids?.length ?? 0} 条</div>` +
    (v.boundaries.length
      ? v.boundaries.map((b) => `
          <div class="boundary ${b.kind}">
            <b>${b.kind === "overlap" ? "⚠ 区间重叠" : "⛔ 无校时段"}</b>
            <span>${esc(b.left_device)} 的 ${esc(b.left_id)} ↔ ${esc(b.right_device)} 的 ${esc(b.right_id)}</span>
            <span style="color:var(--muted)">${esc(b.reason)}</span>
          </div>`).join("")
      : `<div class="hint" style="margin:0;color:var(--ok)">✓ 相邻记录的不确定区间均不重叠，时间轴上没有不可排序边界</div>`);

  const tbody = $("#timelineTable tbody");
  tbody.innerHTML = v.records.map((r, idx) => {
    const shift = r.arrival_rank_in_timeline;
    const errClass = r.true_error_ms === null ? "" : r.true_error_ms >= 0 ? "err-pos" : "err-neg";
    return `
    <tr class="state-${r.calibration_state} ${r.crosses_midnight ? "midnight" : ""} ${shift !== 0 ? "order-shift" : ""}">
      <td>${idx + 1}</td>
      <td>${r.arrival_seq}</td>
      <td title="到达序名次 - 校正序名次；非 0 即说明到达顺序与真实先后不一致">${shift === null ? "—" : (shift > 0 ? "+" : "") + shift}</td>
      <td>${esc(r.device_id)} #b${r.boot_epoch}</td>
      <td>${r.seq}</td>
      <td>${fmtTime(r.device_time_ms)}</td>
      <td>${fmtTime(r.corrected_time_ms)}</td>
      <td>${r.uncertainty_ms === null ? "—" : r.uncertainty_ms.toFixed(2) + " ms"}</td>
      <td><span class="state-tag ${r.calibration_state}">${STATE_LABELS[r.calibration_state]}</span></td>
      <td>${r.based_on_cal_ids.join(",") || "—"}</td>
      <td class="${errClass}">${fmtSigned(r.true_error_ms)}</td>
      <td>${r.crosses_midnight ? "🌙 跨日" : ""}</td>
    </tr>`;
  }).join("");
}

$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.remove("active"));
  t.classList.add("active");
  activeTab = t.dataset.tab;
  renderTimeline();
}));

// ---------- 渲染：原始记录 ----------

function renderRaw() {
  const rows = state.receiver.measurements.slice().reverse();
  $("#rawTable tbody").innerHTML = rows.map((m) => {
    const diff = m.arrival_time_ms - m.true_time_ms;
    return `
    <tr>
      <td>${m.arrival_seq}</td>
      <td>${esc(m.id)}</td>
      <td>${esc(m.device_id)} #b${m.boot_epoch}</td>
      <td>${m.seq}</td>
      <td>${fmtTime(m.device_time_ms)}</td>
      <td>${fmtTime(m.arrival_time_ms)}</td>
      <td>${fmtTime(m.true_time_ms)}</td>
      <td class="${diff >= 0 ? "err-neg" : "err-pos"}">+${diff.toFixed(1)} ms</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="empty-note">暂无原始记录</td></tr>`;
}

// ---------- 渲染：审计 ----------

function renderAudit() {
  $("#auditLog").innerHTML = state.audit.map((e) => `
    <div class="audit-item kind-${esc(e.kind)}">
      <span class="audit-kind">[${esc(e.kind)}]</span>${esc(e.msg)}
    </div>`).join("") || `<div class="empty-note">暂无审计事件</div>`;
}

// ---------- 轮询与绑定 ----------

async function refresh() {
  try {
    state = await (await fetch("/api/state")).json();
    renderTop();
    renderDevices();
    renderCalibrations();
    renderCalculator();
    renderPublisher();
    renderTimeline();
    renderRaw();
    renderAudit();
  } catch (err) {
    console.error("刷新失败", err);
  }
}

$("#resetBtn").onclick = async () => {
  const scenario = $("#scenarioSelect").value;
  if (!confirm(`确认重置整个系统？原始记录、全部校时与版本都会清空，并加载「${scenario}」场景。`)) return;
  await api("/api/reset", { scenario });
  refresh();
};
$("#scenarioSelect").onchange = async (e) => {
  if (!confirm(`切换到「${e.target.selectedOptions[0].textContent}」场景将重置全部数据，继续？`)) {
    e.target.value = state.scenario;
    return;
  }
  await api("/api/reset", { scenario: e.target.value });
  refresh();
};

refresh();
setInterval(refresh, 1000);
