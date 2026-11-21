const $ = selector => document.querySelector(selector);
const path = "/generations";
const pendingStates = new Set(["queued","submitting","running"]);
const states = {
  queued:"Na fila", submitting:"Enviando ao provedor", running:"Gerando",
  completed:"Concluído", failed:"Falha", cancelled:"Cancelado"
};
const formatLabels = {"16:9":"Horizontal · 16:9", "9:16":"Vertical · 9:16", "1:1":"Quadrado · 1:1"};
const amounts = new Intl.NumberFormat("pt-BR",{style:"currency",currency:"USD",maximumFractionDigits:3});
const time = new Intl.DateTimeFormat("pt-BR",{dateStyle:"short",timeStyle:"short",timeZone:"America/Sao_Paulo"});

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function mediaURL(value) {
  try {
    const url = new URL(value,location.origin);
    if (url.username || url.password) return null;
    return url.protocol === "https:" || url.origin === location.origin ? url : null;
  } catch {
    return null;
  }
}

function showMedia(item, run) {
  if (!run.url) return;
  const url = mediaURL(run.url);
  if (!url) return;
  const visual = run.kind === "video" ? node("video") : node("img");
  visual.src = url.href;
  visual.style.cssText = "display:block;width:100%;max-height:17rem;object-fit:contain;border-radius:10px;background:#0b0614;margin:8px 0";
  if (run.kind === "video") {
    visual.controls = true;
    visual.playsInline = true;
    visual.preload = "none";
    visual.setAttribute("aria-label","Reproduzir vídeo gerado");
  } else {
    visual.loading = "lazy";
    visual.alt = run.mode === "demo" ? "Imagem de demonstração" : "Imagem gerada";
    visual.referrerPolicy = "no-referrer";
  }
  const link = node("a",url.origin === location.origin ? "Baixar arquivo" : "Abrir arquivo","action");
  link.href = url.href;
  if (url.origin === location.origin) link.download = "";
  else {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  item.append(visual,link);
}

function historyItem(run) {
  const item = node("li");
  const heading = node("div",undefined,"post-row");
  const state = node("span",states[run.status] || run.status,"status-chip");
  state.dataset.state = run.status;
  heading.append(node("strong",run.model),state);
  const date = new Date(run.created_at);
  const summary = [
    Number.isFinite(date.getTime()) ? time.format(date) : "",
    run.mode === "demo" ? "Demonstração" : `Estimativa: ${amounts.format(run.estimate_usd)}`
  ].filter(Boolean);
  item.append(heading,node("p",summary.join(" · "),"small"),node("p",run.prompt));
  if (run.error) item.append(node("p",run.error,"error"));
  showMedia(item,run);
  return item;
}

export function createGeneration({api}) {
  let enabled = false, state, version = 0, pending, timer, sending = false, kind = "imagem";
  const form = $("#generation-form");
  const modelField = $("#generation-model");
  const ratioField = $("#generation-ratio");
  const durationField = $("#generation-duration");
  const checked = $("#generation-confirm");
  const prompt = $("#generation-prompt");
  const submit = $("#generation-submit");

  function selectedModel() {
    return state?.models.find(model => model.id === modelField.value);
  }

  function setOptions(field, values, previous) {
    field.replaceChildren(...values.map(([label,value]) => new Option(label,String(value))));
    if (values.some(([,value]) => String(value) === previous)) field.value = previous;
    field.disabled = !values.length;
  }

  function updateSummary() {
    const model = selectedModel();
    const isDemo = state?.mode === "demo";
    const needsConfirmation = state?.mode === "real";
    const estimate = $("#generation-estimate");
    $("#generation-confirm-label").hidden = !needsConfirmation;
    checked.required = needsConfirmation;
    checked.disabled = !needsConfirmation;
    submit.disabled = sending || !model || !prompt.value.trim() || (needsConfirmation && !checked.checked);
    submit.textContent = sending ? "Enviando…" : (kind === "imagem" ? "Gerar imagem" : "Gerar vídeo");
    if (!state) {
      estimate.textContent = "Carregando catálogo…";
    } else if (!model) {
      estimate.textContent = "Nenhum modelo está disponível para esse formato.";
    } else if (isDemo) {
      estimate.textContent = "Custo da demonstração: US$ 0.";
    } else {
      const seconds = model.kind === "video" ? Number(durationField.value) : 1;
      estimate.textContent = `Custo estimado: ${amounts.format(model.usd * seconds)}. O valor final depende do provedor.`;
    }
  }

  function updateFormats() {
    checked.checked = false;
    const model = selectedModel();
    setOptions(ratioField,(model?.ratios || []).map(ratio => [formatLabels[ratio] || ratio,ratio]),ratioField.value);
    const durations = kind === "video" ? (model?.durations || []) : [];
    setOptions(durationField,durations.map(value => [`${value} s`,value]),durationField.value || "6");
    $("#generation-duration-label").hidden = kind !== "video";
    updateSummary();
  }

  function updateModel() {
    setOptions(modelField,(state?.models || []).filter(model => model.kind === kind).map(model => [model.label,model.id]),modelField.value);
    updateFormats();
  }

  function chooseType(type) {
    if (sending || kind === type) return;
    kind = type;
    $("#generation-image-tab").setAttribute("aria-pressed",String(kind === "imagem"));
    $("#generation-video-tab").setAttribute("aria-pressed",String(kind === "video"));
    $("#generation-error").textContent = "";
    updateModel();
  }

  function drawHistory() {
    const history = state.runs || [];
    const total = history.filter(run => run.mode === "real" && run.status === "completed")
      .reduce((sum,run) => sum + Number(run.estimate_usd || 0),0);
    const spend = $("#generation-spend");
    spend.hidden = state.mode !== "real" && !total;
    spend.textContent = `Total estimado no histórico de pedidos concluídos: ${amounts.format(total)}.`;
    $("#generation-history-state").textContent = history.length ? "" : "As suas gerações vão aparecer aqui.";
    $("#generation-history").replaceChildren(...history.map(historyItem));
  }

  function refresh() {
    clearTimeout(timer);
    if (!enabled) return Promise.resolve();
    if (pending) return pending;
    const current = version;
    const query = api(path).then(data => {
      if (!enabled || current !== version) return;
      const newCatalog = !state || state.mode !== data.mode || JSON.stringify(state.models) !== JSON.stringify(data.models);
      const newHistory = !state || JSON.stringify(state.runs) !== JSON.stringify(data.runs) || state.mode !== data.mode;
      state = data;
      $("#generation-demo").hidden = state.mode !== "demo";
      if (newCatalog) updateModel();
      if (newHistory) drawHistory();
    }).catch(error => {
      if (!enabled || current !== version) return;
      $("#generation-history-state").textContent = error.message;
    }).finally(() => {
      if (pending !== query) return;
      pending = undefined;
      if (enabled && state?.runs.some(run => pendingStates.has(run.status))) timer = setTimeout(refresh,4500);
    });
    pending = query;
    return query;
  }

  function setSession(config) {
    enabled = Boolean(config);
    version++;
    pending = undefined;
    state = undefined;
    sending = false;
    clearTimeout(timer);
    form.reset();
    $("#generation-history").replaceChildren();
    $("#generation-history-state").textContent = "Carregando histórico…";
    $("#generation-demo").hidden = true;
    $("#generation-error").textContent = "";
    updateModel();
  }

  function enter() {
    refresh();
  }

  $("#generation-image-tab").addEventListener("click",() => chooseType("imagem"));
  $("#generation-video-tab").addEventListener("click",() => chooseType("video"));
  $("#generation-refresh").addEventListener("click",refresh);
  modelField.addEventListener("change",updateFormats);
  for (const field of [ratioField,durationField]) field.addEventListener("change",() => {
    checked.checked = false;
    updateSummary();
  });
  checked.addEventListener("change",updateSummary);
  prompt.addEventListener("input",() => {
    checked.checked = false;
    updateSummary();
  });

  form.addEventListener("submit",async event => {
    event.preventDefault();
    const model = selectedModel();
    const description = prompt.value.trim();
    if (!enabled || !model || !description || sending || (state.mode === "real" && !checked.checked)) return;
    const current = version;
    const errorOutput = $("#generation-error");
    errorOutput.textContent = "";
    sending = true;
    updateSummary();
    try {
      await api(path,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          model_id:model.id, prompt:description, ratio:ratioField.value,
          duration:model.kind === "video" ? Number(durationField.value) : null,
          confirmed:state.mode === "real" && checked.checked,
        })
      });
      if (!enabled || version !== current) return;
      checked.checked = false;
      await pending;
      await refresh();
    } catch (error) {
      if (enabled && version === current) errorOutput.textContent = error.message;
    } finally {
      if (enabled && version === current) {
        sending = false;
        updateSummary();
      }
    }
  });

  return {setSession,enter};
}
