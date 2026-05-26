// 클라이언트 스크립트 (DESIGN.md §3.2 / §7.6)
// - device_id 입력 칸은 입력 시 자동 대문자
// - 60초 폴링으로 active 알림 동기화
// - 장치 추가/삭제/토글, 토큰 등록, 일괄 다운로드 핸들러

document.addEventListener("DOMContentLoaded", () => {
  // 알림 폴링 + SmartThings 토큰 관련 알림 팝업 1회 (admin 접속 시)
  setInterval(pollAlerts, 60000);
  checkSmartThingsTokenPopup();

  // 장치 추가 폼 초기화 — device_type 의 첫 옵션에 맞춰 hub 드롭다운 채움.
  const typeSel = document.querySelector("#add-device-form select[name=device_type]");
  if (typeSel) onDeviceTypeChange(typeSel);

  // Device ID 입력의 hub 기반 case 자동 변환 (입력 시점):
  //   aqara       → 대문자
  //   smartthings → 소문자
  const idInput = document.querySelector("#add-device-form input[name=device_id_input]");
  if (idInput) {
    idInput.addEventListener("input", () => {
      const hubSel = idInput.form.querySelector("select[name=hub]");
      if (hubSel) applyDeviceIdCase(idInput, hubSel.value);
    });
  }

  // 편집 폼 초기화: 행마다 현재 device_type 의 지원 hub 목록으로 hub 드롭다운을 채우고,
  // 서버에 저장된 현재 hub 값을 selected 로 복원. 편집 행은 기본 숨김(display:none) 상태라
  // 사용자가 "편집" 버튼을 누른 뒤에야 화면에 노출되지만 초기 옵션은 미리 채워둔다.
  document.querySelectorAll(".device-edit-form").forEach((form) => {
    const typeSel = form.querySelector("select[name=device_type]");
    if (typeSel) onEditTypeChange(typeSel);
    const editIdInput = form.querySelector("input[name=device_id_input]");
    if (editIdInput) {
      editIdInput.addEventListener("input", () => {
        const hubSel = form.querySelector("select[name=hub]");
        if (hubSel) applyDeviceIdCase(editIdInput, hubSel.value);
      });
    }
  });
});

// SmartThings 토큰 무효/미등록 알림이 active 면 admin 에게 confirm 팝업 — DESIGN.md §15.2 .
// 세션당 코드별 1회만 표시 (sessionStorage), /admin/token 본인 페이지에서는 노출 안 함.
function checkSmartThingsTokenPopup() {
  // admin 이 아니면 작업 권한이 없으므로 스킵
  if (document.body.dataset.isAdmin !== "1") return;
  // 토큰 관리 페이지에서는 이미 그 자리에 있으므로 팝업 생략
  if (location.pathname.startsWith("/admin/token")) return;
  // 관련 알림 코드 (token 미등록 / 거부 / CLI 미설치)
  const codes = [
    "smartthings_token_invalid",
    "smartthings_token_missing",
    "smartthings_cli_missing",
  ];
  for (const code of codes) {
    const el = document.querySelector(`.alert[data-alert-code="${code}"]`);
    if (!el) continue;
    const dismissedKey = "st_popup_dismissed_" + code;
    if (sessionStorage.getItem(dismissedKey)) continue;
    sessionStorage.setItem(dismissedKey, "1");
    const msg = code === "smartthings_cli_missing"
      ? "⚠ smartthings CLI 바이너리가 서버 PATH 에 없습니다.\n\nSmartThings 데이터 수집이 중단됩니다.\nCLI 설치 + 서버 재시작이 필요합니다.\n\n토큰 관리 페이지로 이동하시겠습니까?"
      : "⚠ SmartThings 토큰이 무효하거나 미등록 상태입니다.\n\n데이터 수집이 중단됩니다.\nPAT 재등록이 필요합니다.\n\n/admin/token 페이지로 이동하시겠습니까?";
    if (confirm(msg)) {
      location.href = "/admin/token";
      return;   // 이동했으므로 추가 팝업 확인 불필요
    }
  }
}

async function pollAlerts() {
  try {
    const r = await fetch("/api/alerts");
    if (!r.ok) return;
    const { alerts } = await r.json();
    const banner = document.getElementById("alert-banner");
    if (!banner) return;
    const existing = new Set(
      Array.from(banner.querySelectorAll(".alert")).map((e) => Number(e.dataset.alertId))
    );
    const incoming = new Set(alerts.map((a) => a.id));
    // 사라진 것 제거
    banner.querySelectorAll(".alert").forEach((e) => {
      if (!incoming.has(Number(e.dataset.alertId))) e.remove();
    });
    // 새로 추가된 것
    let stAlertAdded = false;
    alerts.forEach((a) => {
      if (existing.has(a.id)) return;
      const div = document.createElement("div");
      div.className = `alert alert-${a.level}`;
      div.dataset.alertId = a.id;
      div.dataset.alertCode = a.code;   // 팝업 트리거가 코드로 매칭
      div.innerHTML = `<span><strong>[${a.code}]</strong> ${escapeHtml(a.message)}</span>`;
      banner.appendChild(div);
      if (a.code && a.code.startsWith("smartthings_")) stAlertAdded = true;
    });
    // 폴링 중 SmartThings 관련 알림이 새로 떴으면 팝업도 다시 한 번 확인 (세션당 1회 보장)
    if (stAlertAdded) checkSmartThingsTokenPopup();
  } catch (e) {
    console.warn("alert poll failed", e);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function dismissAlert(id) {
  const r = await fetch(`/api/alerts/${id}/resolve`, { method: "POST" });
  if (r.ok) {
    const el = document.querySelector(`.alert[data-alert-id="${id}"]`);
    if (el) el.remove();
  }
}

// ─── 장치 ───

// device_type 변경 시: 지원 hub 옵션을 동적으로 채우고 placeholder 갱신.
// 같은 device_type 이 여러 hub 를 지원할 수 있으므로 (DESIGN.md §15) 사용자가 선택 가능.
function onDeviceTypeChange(sel) {
  const opt = sel.options[sel.selectedIndex];
  if (!opt) return;
  const hubs = (opt.getAttribute("data-hubs") || "").split(",").filter(Boolean);
  const hubSel = sel.form.querySelector("select[name=hub]");
  if (!hubSel) return;
  // 기존 옵션 비우고 다시 채움
  hubSel.innerHTML = "";
  hubs.forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = v === "aqara" ? "Aqara (Open API)" : (v === "smartthings" ? "SmartThings (PAT)" : v);
    hubSel.appendChild(o);
  });
  // disabled 처리 시 폼 제출에서 hub 가 누락되므로 잠그지 않는다 (단일 hub 인 경우 자동 선택만).
  onHubChange(hubSel);
}

// hub 변경 시: placeholder + 기존 Device ID 입력값의 case 를 즉시 보정.
//   aqara       → 대문자 (UI 표시), 서버는 lumi.<소문자> 로 정규화
//   smartthings → 소문자 (UI 표시), 서버는 원본 그대로
function onHubChange(hubSel) {
  if (!hubSel) return;
  const hub = hubSel.value;
  const idInput = hubSel.form.querySelector("input[name=device_id_input]");
  if (!idInput) return;
  if (hub === "smartthings") {
    idInput.placeholder = "예: 3e7b675d14dfa559dae13000 또는 0a59334e-c81f-4081-a501-f09048b9cca9";
  } else {
    idInput.placeholder = "예: 4CF8CDF3C752EDB";
  }
  applyDeviceIdCase(idInput, hub);
}

// hub 에 맞춰 Device ID 입력값 case 보정 (커서 위치 보존).
function applyDeviceIdCase(idInput, hub) {
  const pos = idInput.selectionStart;
  const newVal = (hub === "smartthings") ? idInput.value.toLowerCase() : idInput.value.toUpperCase();
  if (newVal !== idInput.value) {
    idInput.value = newVal;
    if (pos !== null) {
      try { idInput.setSelectionRange(pos, pos); } catch (e) { /* type=date 등 일부 input 은 무시 */ }
    }
  }
}

async function submitAddDevice(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const payload = {
    device_type: fd.get("device_type"),
    hub: fd.get("hub"),
    device_id_input: fd.get("device_id_input"),
    install_location: fd.get("install_location") || null,
    install_date: fd.get("install_date") || null,
    alias: fd.get("alias") || null,
  };
  const r = await fetch("/api/devices", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert("등록 실패: " + (err.detail || r.status));
    return false;
  }
  location.reload();
  return false;
}

// SmartThings OAuth 연결은 리다이렉트 흐름(/admin/smartthings/oauth/start)이라 별도 JS 핸들러 없음.

// SmartThings PAT 저장 (stopgap, admin 전용) — /admin/token 페이지의 PAT 입력 폼.
// 성공/실패 결과는 페이지 redirect(쿼리 st_pat=...) 로 전달해 상단 배너로 명확히 표시.
async function submitSmartThingsPat(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const result = document.getElementById("st-pat-result");
  if (result) { result.textContent = "검증 중..."; result.className = "muted"; }
  const r = await fetch("/api/admin/smartthings_pat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pat: fd.get("pat") }),
  });
  if (r.ok) {
    const body = await r.json().catch(() => ({}));
    // 응답에 source 가 'pat-env' 면 환경변수가 우선됨을 명확히 안내.
    const n = body.device_count || 0;
    const key = (body.source === "pat-env") ? "overridden_by_env" : "saved";
    location.href = `/admin/token?st_pat=${key}&st_pat_n=${n}`;
  } else {
    const err = await r.json().catch(() => ({}));
    if (result) { result.textContent = "실패: " + (err.detail || r.status); result.className = "error"; }
  }
  return false;
}

// 저장된 PAT 파일 삭제 — OAuth 또는 환경변수 PAT 경로로 복귀.
async function clearSmartThingsPat(ev) {
  ev.preventDefault();
  if (!confirm("저장된 PAT 를 삭제하시겠습니까? OAuth 토큰이 있으면 그쪽으로 복귀합니다.")) return false;
  const r = await fetch("/api/admin/smartthings_pat", { method: "DELETE" });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert("삭제 실패: " + (err.detail || r.status));
    return false;
  }
  location.href = "/admin/token?st_pat=cleared";
  return false;
}

// 단일 device_history 행 삭제 (admin 전용). 무의미한 토글 이력 정리용.
async function deleteHistoryRow(historyId) {
  if (!confirm("이 변경 이력을 삭제하시겠습니까? (되돌릴 수 없음)")) return;
  const r = await fetch(`/api/devices/history/${historyId}`, { method: "DELETE" });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert("삭제 실패: " + (err.detail || r.status));
    return;
  }
  location.reload();
}

async function deleteDevice(id) {
  if (!confirm("이 장치를 삭제하시겠습니까? (Soft delete — 과거 CSV는 보존됨)")) return;
  const r = await fetch(`/api/devices/${id}`, { method: "DELETE" });
  if (!r.ok) { alert("삭제 실패: " + r.status); return; }
  location.reload();
}

async function toggleDevice(id, enable) {
  const r = await fetch(`/api/devices/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: enable }),
  });
  if (!r.ok) { alert("변경 실패: " + r.status); return; }
  location.reload();
}

// 설치 장소 드롭다운 필터 — 활성 장치 목록 (devices.html).
//   value === ""        : 전체 표시
//   value === "__none__": install_location 이 빈 행만
//   그 외               : 정확히 일치하는 행만
// 편집 폼 행(.device-edit-row)도 같은 install_location 을 가지므로 함께 토글.
// 단, 편집 폼은 사용자가 명시적으로 "편집" 버튼을 눌러야 보이므로 필터에 매치되더라도
// 기본 display=none 을 유지하기 위해 매치 안 될 때만 강제로 숨김 처리.
function filterDevicesByLocation(value) {
  let visible = 0;
  document.querySelectorAll("tr.device-row").forEach((tr) => {
    const loc = tr.dataset.installLocation || "";
    const match = (value === "")
      || (value === "__none__" ? loc === "" : loc === value);
    tr.style.display = match ? "" : "none";
    // 편집 폼 행은 부모 매치에 따라 강제 숨김 (펼친 상태에서 필터가 바뀌면 같이 사라짐).
    const editId = tr.id.replace("device-row-", "device-edit-");
    const editTr = document.getElementById(editId);
    if (editTr && !match) editTr.style.display = "none";
    if (match) visible++;
  });
  const counter = document.getElementById("active-visible-count");
  if (counter) counter.textContent = String(visible);
}

// 설치 장소 드롭다운 필터 — 데이터 현황 (data.html). devices 와 동일 로직.
function filterDataByLocation(value) {
  let visible = 0;
  document.querySelectorAll("tr.data-row").forEach((tr) => {
    const loc = tr.dataset.installLocation || "";
    const match = (value === "")
      || (value === "__none__" ? loc === "" : loc === value);
    tr.style.display = match ? "" : "none";
    if (match) visible++;
  });
  const counter = document.getElementById("data-visible-count");
  if (counter) counter.textContent = String(visible);
}

// 활성 디바이스 전체의 enabled 를 일괄 ON/OFF (DESIGN.md §7.4).
// 단일 요청으로 서버가 변경 대상만 골라 device_history 스냅샷과 함께 처리.
async function bulkToggleDevices(enable) {
  const label = enable ? "전체 ON" : "전체 OFF";
  if (!confirm(`활성 장치 전체를 ${label} 으로 변경하시겠습니까?`)) return;
  const status = document.getElementById("bulk-toggle-status");
  if (status) { status.textContent = "적용 중..."; status.className = "muted"; }
  const r = await fetch("/api/devices/bulk_enable", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: enable }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    if (status) { status.textContent = "실패: " + (err.detail || r.status); status.className = "error"; }
    return;
  }
  const body = await r.json();
  if (status) status.textContent = `완료 (${body.changed}/${body.total} 변경)`;
  location.reload();
}

// ─── 디바이스 편집 (DESIGN.md §7.5) ───
// 활성 행 아래 인라인 편집 행을 토글한다. 변경은 "적용" 버튼에서만 PATCH 로 전송.
// 등록자·등록일은 폼에서 제외돼 서버로도 가지 않는다.
function openDeviceEditor(id) {
  const row = document.getElementById("device-edit-" + id);
  if (!row) return;
  row.style.display = (row.style.display === "none" || !row.style.display) ? "" : "none";
}

function closeDeviceEditor(id) {
  const row = document.getElementById("device-edit-" + id);
  if (row) row.style.display = "none";
}

// 편집 폼의 device_type 변경 시: 지원 hub 옵션 갱신. 가능하면 현재 값(data-current-hub) 유지.
function onEditTypeChange(typeSel) {
  const form = typeSel.form;
  const opt = typeSel.options[typeSel.selectedIndex];
  if (!opt) return;
  const hubs = (opt.getAttribute("data-hubs") || "").split(",").filter(Boolean);
  const hubSel = form.querySelector("select[name=hub]");
  if (!hubSel) return;
  const wanted = form.dataset.currentHub || hubSel.value;
  hubSel.innerHTML = "";
  hubs.forEach((v) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = v === "aqara" ? "Aqara (Open API)" : (v === "smartthings" ? "SmartThings (PAT)" : v);
    if (v === wanted) o.selected = true;
    hubSel.appendChild(o);
  });
  // 현재 hub 가 새 device_type 에서 지원되지 않으면 첫 옵션이 자동 선택됨.
  const idInput = form.querySelector("input[name=device_id_input]");
  if (idInput) applyDeviceIdCase(idInput, hubSel.value);
}

// 편집 폼 제출: 변경된 필드만 PATCH 본문에 포함 (불필요한 이력 트리거 회피).
async function submitDeviceEdit(ev, id) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const statusEl = form.querySelector(".edit-status");
  const payload = {};
  // 식별 필드 — 값이 현재 행과 다를 때만 전송.
  const currentType = form.dataset.currentType;
  const currentHub = form.dataset.currentHub;
  const currentDevId = form.dataset.currentDeviceId;
  if (fd.get("device_type") !== currentType) payload.device_type = fd.get("device_type");
  if (fd.get("hub") !== currentHub) payload.hub = fd.get("hub");
  if (fd.get("device_id_input") !== currentDevId) payload.device_id_input = fd.get("device_id_input");
  // 메타 필드 — 항상 보내도 동일 값이면 이력에 의미 변화 없지만, 서버는 "변경된 필드가 있을 때만"
  // 이력 행을 INSERT 하므로 빈 PATCH 방지를 위해 그대로 전송한다.
  payload.alias = fd.get("alias") || null;
  payload.install_location = fd.get("install_location") || null;
  payload.install_date = fd.get("install_date") || null;
  payload.enabled = fd.get("enabled") === "1";
  // group_id: "" → null (해제)
  const g = fd.get("group_id");
  payload.group_id = (g === "" || g === null) ? null : Number(g);

  if (statusEl) { statusEl.textContent = "적용 중..."; statusEl.className = "muted edit-status"; }
  const r = await fetch(`/api/devices/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    if (statusEl) { statusEl.textContent = "실패: " + (err.detail || r.status); statusEl.className = "error edit-status"; }
    return false;
  }
  location.reload();
  return false;
}

// ─── 그룹 (DISPLAY.md §4.8) ───
// drop-down value "" 는 "그룹 해제"를 의미. 명시적 null 로 PATCH 해야 서버가
// "필드 미제공" 과 구분해 group_id 컬럼을 비운다 (api.patch_device 의 model_fields_set 처리).
async function changeDeviceGroup(deviceId, value) {
  const groupId = value === "" ? null : Number(value);
  const r = await fetch(`/api/devices/${deviceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ group_id: groupId }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert("그룹 변경 실패: " + (err.detail || r.status));
    location.reload();  // UI를 서버 진실 값으로 되돌림
    return;
  }
  // 멤버 수 표시 갱신을 위해 페이지 리로드 (그룹 관리 표의 member_count 반영).
  location.reload();
}

async function submitAddGroup(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const payload = {
    name: fd.get("name"),
    device_type: fd.get("device_type") || null,
    description: fd.get("description") || null,
  };
  const r = await fetch("/api/groups", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert("그룹 추가 실패: " + (err.detail || r.status));
    return false;
  }
  location.reload();
  return false;
}

async function deleteGroup(id, name) {
  if (!confirm(`그룹 "${name}" 을(를) 삭제하시겠습니까?\n(멤버 디바이스의 그룹 소속이 자동 해제됩니다.)`)) return;
  const r = await fetch(`/api/groups/${id}`, { method: "DELETE" });
  if (!r.ok) { alert("그룹 삭제 실패: " + r.status); return; }
  location.reload();
}

// ─── 일괄 수동 수집 (DESIGN.md §7.4) ───
// 활성 장치 × 기간 모든 일자를 서버 백그라운드 스레드로 수집. 응답은 202 즉시,
// 진행은 /jobs 페이지에서 확인하라고 안내.
async function submitBulkCollect(ev) {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const from_ = fd.get("from");
  const to = fd.get("to");
  const status = document.getElementById("bulk-collect-status");
  if (!from_ || !to) {
    status.textContent = "시작일과 종료일을 모두 지정하세요.";
    status.className = "error";
    return false;
  }
  if (!confirm(`${from_} ~ ${to} 기간의 모든 활성 장치 데이터를 수집합니다. 계속할까요?`)) {
    return false;
  }
  status.textContent = "수집 요청 중...";
  status.className = "muted";
  const r = await fetch("/api/jobs/bulk_run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from: from_, to: to }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    status.textContent = "실패: " + (err.detail || r.status);
    status.className = "error";
    return false;
  }
  const body = await r.json();
  status.textContent =
    `백그라운드 수집 시작 (예상 ${body.estimated_jobs}개 작업, ${body.date_count}일 × ${body.device_count}장치). ` +
    `/jobs 에서 진행 확인.`;
  status.className = "muted";
  return false;
}

// ─── 토큰 ───
async function submitToken(ev) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const result = document.getElementById("token-result");
  result.textContent = "갱신 시도 중...";
  result.className = "muted";
  const r = await fetch("/api/admin/token/seed", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: fd.get("refresh_token") }),
  });
  if (r.ok) {
    result.textContent = "갱신 성공. 페이지를 새로고침합니다...";
    setTimeout(() => location.reload(), 1200);
  } else {
    const err = await r.json().catch(() => ({}));
    result.textContent = "실패: " + (err.detail || r.status);
    result.className = "error";
  }
  return false;
}

// ─── 일괄 다운로드 ───
function bundleDownload(ev, deviceId, bundleKey) {
  ev.preventDefault();
  const form = ev.target;
  const fd = new FormData(form);
  const url = `/api/data/${encodeURIComponent(deviceId)}/${encodeURIComponent(bundleKey)}/bundle`
            + `?from=${fd.get("from")}&to=${fd.get("to")}&format=${fd.get("format")}`;
  window.location.href = url;
  return false;
}
