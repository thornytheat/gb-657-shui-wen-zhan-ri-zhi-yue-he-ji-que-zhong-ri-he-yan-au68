// 月量更正：导入暂存、发布/重新定基、版本切换。所有请求走同源接口。
function el(id) {
  return document.getElementById(id);
}

function notice(message, isError = false) {
  const box = el("notice");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("is-error", Boolean(isError));
  box.hidden = false;
}

async function request(url, options) {
  const response = await fetch(url, options || {});
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = { error: "E_BAD_RESPONSE", message: "响应不是 JSON" };
  }
  if (!response.ok) {
    const code = payload && payload.error ? payload.error : response.status;
    const detail = payload && payload.message ? payload.message : "请求失败";
    const extra = payload && payload.detail ? " " + JSON.stringify(payload.detail) : "";
    throw new Error(code + " " + detail + extra);
  }
  return payload;
}

async function stageCorrection(button) {
  const reason = el("corr-reason").value.trim();
  if (!reason) {
    notice("更正理由必填", true);
    return;
  }
  const file = el("corr-file").files[0];
  const recordsText = el("corr-records").value.trim();
  const fd = new FormData();
  fd.append("reason", reason);

  try {
    let payload;
    if (file) {
      fd.append("file", file);
      payload = await request("/api/corrections/stage", { method: "POST", body: fd });
    } else if (recordsText) {
      let records;
      try {
        records = JSON.parse(recordsText);
      } catch (error) {
        notice("JSON 记录无法解析：" + error.message, true);
        return;
      }
      payload = await request("/api/corrections/stage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ records, reason }),
      });
    } else {
      notice("请选择 CSV 文件或粘贴 JSON 记录", true);
      return;
    }
    notice((payload.replayed ? "命中既有任务 " : "已暂存任务 ") + payload.run_id);
    window.setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    notice(String(error.message || error), true);
  } finally {
    button.disabled = false;
  }
}

async function publish(button) {
  const runId = button.dataset.run;
  button.disabled = true;
  try {
    const payload = await request(
      "/api/corrections/" + encodeURIComponent(runId) + "/publish",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    notice("已发布版本 " + payload.gen_id +
      (payload.resumed ? "（中断续跑）" : "") + "，月量行 " + payload.monthly_rows);
    window.setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    button.disabled = false;
    notice(String(error.message || error), true);
  }
}

async function rebase(button) {
  const runId = button.dataset.run;
  try {
    const payload = await request(
      "/api/corrections/" + encodeURIComponent(runId) + "/rebase",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    notice("已重新定基到 " + (payload.base_gen || "空基准") + "，请再次发布");
    window.setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    notice(String(error.message || error), true);
  }
}

async function switchGen(button) {
  const genId = button.dataset.gen;
  try {
    const payload = await request(
      "/api/corrections/generations/" + encodeURIComponent(genId) + "/switch",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    notice("当前版本：" + payload.current_gen + "（旧版 " + payload.previous_gen + " 仍可读）");
    window.setTimeout(() => window.location.reload(), 600);
  } catch (error) {
    notice(String(error.message || error), true);
  }
}

function bind() {
  const stageButton = el("corr-stage");
  if (stageButton) {
    stageButton.addEventListener("click", () => stageCorrection(stageButton));
  }
  document.querySelectorAll(".corr-publish").forEach((button) => {
    button.addEventListener("click", () => publish(button));
  });
  document.querySelectorAll(".corr-rebase").forEach((button) => {
    button.addEventListener("click", () => rebase(button));
  });
  const switchButton = el("corr-switch");
  if (switchButton) {
    switchButton.addEventListener("click", () => switchGen(switchButton));
  }
}

bind();
