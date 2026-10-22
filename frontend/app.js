const $ = (selector) => document.querySelector(selector);
const form = $("#analyze-form");
const formMessage = $("#form-message");
const previewSection = $("#preview-section");
const jobSection = $("#job-section");
let analysis = null;
let activeJob = null;
let pollTimer = null;
const uploads = new Map();

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
}
function duration(ms) {
  if (!ms) return "—";
  const seconds = Math.floor(ms / 1000);
  return Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
}
function artwork(url, name) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:" && parsed.hostname === "i.scdn.co") {
      return '<img class="track-art" loading="lazy" src="' + esc(parsed.href) + '" alt="">';
    }
  } catch (_) {}
  return '<span class="track-art track-art-empty" aria-hidden="true">♫</span>';
}
function setMessage(text, error = false) {
  formMessage.textContent = text;
  formMessage.classList.toggle("error", error);
}
function renderPreview(data) {
  analysis = data;
  previewSection.hidden = false;
  jobSection.hidden = true;
  $("#collection-kind").textContent = data.kind.toUpperCase();
  $("#collection-name").textContent = data.name;
  const totalSeconds = data.tracks.reduce((sum, track) => sum + (track.duration_ms || 0), 0) / 1000;
  $("#collection-summary").textContent = data.tracks.length + " tracks · " + Math.floor(totalSeconds / 60) + " min";
  const cover = $("#preview-art");
  if (data.artwork && artwork(data.artwork, data.name).includes("<img")) {
    cover.src = data.artwork;
    cover.alt = data.name + " artwork";
    cover.hidden = false;
  } else cover.hidden = true;

  $("#preview-tracks").innerHTML = data.tracks.map((track) =>
    '<label class="preview-track"><input class="track-check" type="checkbox" value="' + track.index + '" checked aria-label="Select ' + esc(track.title) + '">' +
    artwork(track.artwork, track.title) +
    '<span class="track-copy"><strong>' + esc(track.title) + '</strong><span>' + esc(track.artist) + '</span></span>' +
    '<span class="track-duration">' + duration(track.duration_ms) + '</span></label>'
  ).join("");
  $("#selection-count").textContent = data.tracks.length + " selected";
  updateSelection();
  previewSection.scrollIntoView({behavior: "smooth", block: "start"});
}
function updateSelection() {
  const selected = [...document.querySelectorAll(".track-check:checked")].length;
  $("#selection-count").textContent = selected + " selected";
  $("#start-job").disabled = !selected || !analysis;
  $("#select-all").textContent = selected ? "Deselect all" : "Select all";
}
document.addEventListener("change", (event) => {
  if (event.target.matches(".track-check")) updateSelection();
  if (event.target.id === "format-select") {
    $("#bitrate-setting").hidden = event.target.value !== "mp3";
  }
});
$("#select-all").addEventListener("click", () => {
  const checks = [...document.querySelectorAll(".track-check")];
  const shouldCheck = checks.some((check) => !check.checked);
  checks.forEach((check) => { check.checked = shouldCheck; });
  updateSelection();
});
$("#paste-button").addEventListener("click", async () => {
  try {
    const value = await navigator.clipboard.readText();
    if (value) { $("#spotify-url").value = value; $("#spotify-url").focus(); }
  } catch (_) {
    setMessage("Paste from your browser’s address bar or long-press the link field.");
  }
});
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("#analyze-button");
  button.disabled = true;
  setMessage("Loading Spotify metadata…");
  try {
    const response = await fetch("/api/analyze", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url: $("#spotify-url").value.trim()})
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Could not preview that Spotify link.");
    setMessage("");
    renderPreview(payload);
  } catch (error) {
    setMessage(error.message || "Could not reach the server.", true);
  } finally {
    button.disabled = false;
  }
});
$("#start-job").addEventListener("click", async () => {
  if (!analysis) return;
  const selected_indices = [...document.querySelectorAll(".track-check:checked")].map((item) => Number(item.value));
  const button = $("#start-job");
  button.disabled = true;
  button.textContent = "Preparing job…";
  try {
    const response = await fetch("/api/jobs", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        analysis_id: analysis.analysis_id, selected_indices,
        format: $("#format-select").value, bitrate: $("#bitrate-select").value
      })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Could not create this job.");
    activeJob = {id: payload.job.id, token: payload.token};
    localStorage.setItem("soundloom-job", JSON.stringify(activeJob));
    previewSection.hidden = true;
    jobSection.hidden = false;
    await refreshJob();
    jobSection.scrollIntoView({behavior: "smooth", block: "start"});
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(refreshJob, 1800);
  } catch (error) {
    setMessage(error.message, true);
    button.disabled = false;
    button.innerHTML = 'Prepare selected audio <span aria-hidden="true">→</span>';
  }
});
function apiHeaders() {
  return {"X-Job-Token": activeJob.token};
}
function renderJob(job) {
  $("#job-name").textContent = job.name;
  const counts = job.counts;
  $("#job-subtitle").textContent = job.total + " tracks · " + job.format.toUpperCase() +
    (job.format === "mp3" && job.bitrate ? " " + job.bitrate + " kbps" : "");
  $("#progress-number").textContent = Math.round(job.progress) + "%";
  $("#progress-bar").style.width = Math.max(0, Math.min(100, job.progress)) + "%";
  $("#progress-label").textContent = job.status === "completed" ? "Ready to download" :
    job.status === "partial" ? "Finished with some tracks needing attention" :
    job.status === "failed" ? "No tracks finished successfully" :
    job.status === "cancelled" ? "Job cancelled" :
    counts.processing ? "Converting audio" :
    counts.queued ? "Waiting for an encoder" : "Add audio to start processing";
  $("#progress-detail").textContent = counts.completed + " ready · " + counts.failed + " failed · " + counts.awaiting_upload + " waiting for audio";
  $("#active-detail").textContent = counts.processing ? counts.processing + " encoding" : "";
  const actions = [];
  if (job.zip_url) actions.push('<a class="button button-primary" href="' + esc(job.zip_url) + '">Download playlist ZIP <span aria-hidden="true">↓</span></a>');
  if (counts.failed) actions.push('<button class="button button-quiet" id="retry-button" type="button">Retry failed tracks</button>');
  $("#job-actions").innerHTML = actions.join("");
  $("#retry-button")?.addEventListener("click", retryFailed);
  $("#cancel-job").disabled = !job.active;
  $("#cancel-job").textContent = job.active ? "Cancel job" : "Job finished";
  $("#job-tracks").innerHTML = job.tracks.map((track) => {
    const uploading = uploads.get(track.id);
    const state = uploading ? "Uploading " + uploading + "%" :
      track.status === "awaiting_upload" ? "Add audio file" :
      track.status === "uploading" ? "Upload continuing on server" :
      track.status === "processing" ? "Converting " + Math.round(track.progress) + "%" :
      track.status === "queued" ? "Queued" :
      track.status === "completed" ? (track.lossless_from_source ? "Ready · lossless source" :
        track.source_bitrate ? "Ready · source " + Math.round(track.source_bitrate / 1000) + " kbps" : "Ready") :
      track.status === "duplicate" ? "Duplicate track" :
      track.status === "failed" ? "Needs attention" : track.status;
    const canUpload = track.status === "awaiting_upload" || track.status === "failed";
    const uploadControl = canUpload ?
      '<label class="button button-quiet upload-control" for="file-' + track.id + '">Add audio<input id="file-' + track.id + '" class="audio-file" type="file" accept="audio/*,.flac,.opus,.ogg" data-id="' + track.id + '" hidden></label>' : "";
    const cancelControl = ["queued", "processing", "awaiting_upload"].includes(track.status) ?
      '<button class="icon-button cancel-track" data-id="' + track.id + '" type="button" aria-label="Cancel ' + esc(track.title) + '">×</button>' : "";
    const download = track.download_url ? '<a class="icon-button download-track" href="' + esc(track.download_url) + '" aria-label="Download ' + esc(track.title) + '">↓</a>' : "";
    return '<div class="job-track">' + artwork(null, track.title) +
      '<span class="track-copy"><strong>' + String(track.position).padStart(2, "0") + ' · ' + esc(track.title) +
      '</strong><span>' + esc(track.artist) + '</span>' +
      (track.error ? '<span class="track-error">' + esc(track.error) + '</span>' : "") +
      '</span><span class="track-state">' + esc(state) + '</span>' + uploadControl + download + cancelControl + '</div>';
  }).join("");
  $("#job-tracks").querySelectorAll(".audio-file").forEach((input) => input.addEventListener("change", uploadTrack));
  $("#job-tracks").querySelectorAll(".cancel-track").forEach((button) => button.addEventListener("click", () => cancelTrack(button.dataset.id)));
  if (!job.active && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
async function refreshJob() {
  if (!activeJob) return;
  try {
    const response = await fetch("/api/jobs/" + encodeURIComponent(activeJob.id), {headers: apiHeaders()});
    if (response.status === 404) throw new Error("This job link is no longer valid.");
    const job = await response.json();
    if (!response.ok) throw new Error(job.detail || "Unable to read job status.");
    renderJob(job);
  } catch (error) {
    if (pollTimer) clearInterval(pollTimer);
    $("#job-subtitle").textContent = error.message;
  }
}
function uploadTrack(event) {
  const file = event.target.files?.[0];
  const id = event.target.dataset.id;
  if (!file || !activeJob) return;
  const data = new FormData();
  data.append("file", file, file.name);
  const xhr = new XMLHttpRequest();
  xhr.open("PUT", "/api/jobs/" + encodeURIComponent(activeJob.id) + "/tracks/" + encodeURIComponent(id) + "/audio");
  xhr.setRequestHeader("X-Job-Token", activeJob.token);
  uploads.set(id, 0);
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      uploads.set(id, Math.round(100 * e.loaded / e.total));
      const status = $("#job-tracks").querySelector('[data-id="' + CSS.escape(id) + '"]');
      if (status) status.textContent = "Uploading " + uploads.get(id) + "%";
    }
  };
  xhr.onload = () => {
    uploads.delete(id);
    if (xhr.status < 200 || xhr.status >= 300) {
      try { alert(JSON.parse(xhr.responseText).detail || "Upload failed."); }
      catch (_) { alert("Upload failed. Please try another file."); }
    }
    refreshJob();
  };
  xhr.onerror = () => { uploads.delete(id); alert("Upload interrupted. You can retry this track."); refreshJob(); };
  xhr.send(data);
  renderUploadStatus(id);
}
function renderUploadStatus(id) {
  refreshJob();
}
async function retryFailed() {
  await fetch("/api/jobs/" + encodeURIComponent(activeJob.id) + "/retry", {method: "POST", headers: apiHeaders()});
  refreshJob();
  if (!pollTimer) pollTimer = setInterval(refreshJob, 1800);
}
async function cancelTrack(id) {
  await fetch("/api/jobs/" + encodeURIComponent(activeJob.id) + "/tracks/" + encodeURIComponent(id), {method: "DELETE", headers: apiHeaders()});
  refreshJob();
}
$("#cancel-job").addEventListener("click", async () => {
  if (!activeJob || !confirm("Cancel unfinished tracks in this job?")) return;
  await fetch("/api/jobs/" + encodeURIComponent(activeJob.id), {method: "DELETE", headers: apiHeaders()});
  refreshJob();
});
$("#new-job").addEventListener("click", () => {
  if (pollTimer) clearInterval(pollTimer);
  activeJob = null;
  localStorage.removeItem("soundloom-job");
  analysis = null;
  jobSection.hidden = true;
  previewSection.hidden = true;
  form.reset();
  setMessage("");
  window.scrollTo({top: 0, behavior: "smooth"});
});
async function refreshAuth() {
  try {
    const response = await fetch("/api/auth/status");
    const status = await response.json();
    $("#connect-button").hidden = !status.configured || status.connected;
    $("#logout-button").hidden = !status.connected;
    $("#auth-state").hidden = !status.connected;
    $("#auth-state").textContent = status.connected ? "Spotify connected" : "";
  } catch (_) {}
}
$("#logout-button").addEventListener("click", async () => {
  await fetch("/api/auth/logout", {method: "POST"});
  refreshAuth();
});
async function restoreJob() {
  try {
    const saved = JSON.parse(localStorage.getItem("soundloom-job") || "null");
    if (!saved?.id || !saved?.token) return;
    activeJob = saved;
    const response = await fetch("/api/jobs/" + encodeURIComponent(saved.id), {headers: apiHeaders()});
    if (!response.ok) { localStorage.removeItem("soundloom-job"); activeJob = null; return; }
    const job = await response.json();
    jobSection.hidden = false;
    renderJob(job);
    if (job.active) pollTimer = setInterval(refreshJob, 1800);
  } catch (_) {
    localStorage.removeItem("soundloom-job");
    activeJob = null;
  }
}
refreshAuth();
restoreJob();
if ("serviceWorker" in navigator) window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
