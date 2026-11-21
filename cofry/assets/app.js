const $ = selector => document.querySelector(selector);
const styles = {
  branco:"Branco padrão", amarelo:"Destaque amarelo", neon_verde:"Neon verde", caixa:"Caixa",
  minimalista:"Minimalista", editorial:"Editorial", karaoke:"Karaokê", contorno:"Contorno",
  italico_caixa:"Itálico com caixa", laranja:"Laranja"
};
const stageNames = {
  fila:"Na fila", baixando:"Carregando vídeo", validando:"Validando vídeo", extraindo_audio:"Extraindo áudio",
  transcrevendo:"Transcrevendo", selecionando:"Selecionando trechos", renderizando:"Renderizando cortes",
  enviando:"Salvando arquivos", concluido:"Concluído", cancelado:"Cancelado"
};
let source = "file", selectedFile, settings, refreshTimer, signature = "", currentEdit, editingProject;
let refreshVersion = 0;
const names = new Map();

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

for (let i=0;i<75;i++) {
  const bar = element("b");
  bar.style.setProperty("--h", `${12 + (Math.sin(i*1.75)**2 * 68 + Math.sin(i*.32)**2 * 17)}%`);
  $("#wave").append(bar);
}
for (const [value, name] of Object.entries(styles)) {
  for (const selector of ["#styles", "#edit-styles"]) $(selector).add(new Option(name, value));
}

async function api(path, options) {
  const response = await fetch("/studio" + path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "Não foi possível concluir a ação.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function showSession(config) {
  settings = config;
  $("#login").hidden = Boolean(config);
  $("#workspace").hidden = !config;
  $("#logout").hidden = !config;
  clearTimeout(refreshTimer);
  refreshVersion++;
  if (!config) return;
  $("#limits").textContent = `Ou clique para escolher · até ${config.max_mb} MiB · mínimo 2 min`;
  $("#link-help").textContent = config.youtube
    ? "Link direto, Google Drive, Dropbox ou YouTube. Use vídeos próprios ou autorizados."
    : "Links públicos de arquivo, Google Drive e Dropbox.";
  $("#mode").textContent = config.modo === "ia"
    ? "Seleção e ganchos gerados com IA."
    : "Seleção local pela transcrição. O gancho usa uma frase do vídeo.";
  signature = "";
  refresh();
}

$("#login").addEventListener("submit", async event => {
  event.preventDefault();
  $("#login-error").textContent = "";
  const field = $("#key");
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    await api("/session", {method:"POST",headers:{"X-API-Key":field.value}});
    field.value = "";
    showSession(await api("/session"));
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally { button.disabled = false; }
});
$("#logout").addEventListener("click", async () => {
  try { await api("/session", {method:"DELETE"}); showSession(null); }
  catch (error) { $("#projects-message").textContent = error.message; }
});
api("/session").then(showSession).catch(error => {
  if (error.status !== 401) $("#login-error").textContent = "O servidor do estúdio não respondeu.";
});
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/studio/assets/sw.js", {scope:"/studio"}).catch(() => {});

function setSource(value) {
  source = value;
  $("#drop").hidden = value !== "file";
  $("#url-panel").hidden = value !== "url";
  $("#file-tab").setAttribute("aria-pressed", value === "file");
  $("#url-tab").setAttribute("aria-pressed", value === "url");
}
$("#file-tab").addEventListener("click", () => setSource("file"));
$("#url-tab").addEventListener("click", () => setSource("url"));
function choose(file) {
  selectedFile = file;
  $("#filename").textContent = file?.name || "Arraste seu vídeo aqui";
  $("#job-error").textContent = "";
}
$("#file").addEventListener("change", event => choose(event.target.files[0]));
$("#drop").addEventListener("keydown", event => {
  if (["Enter"," "].includes(event.key)) { event.preventDefault(); $("#file").click(); }
});
for (const eventName of ["dragenter","dragover"]) $("#drop").addEventListener(eventName, event => {
  event.preventDefault();
  $("#drop").classList.add("drag");
});
$("#drop").addEventListener("dragleave", () => $("#drop").classList.remove("drag"));
$("#drop").addEventListener("drop", event => {
  event.preventDefault();
  $("#drop").classList.remove("drag");
  choose(event.dataTransfer.files[0]);
});

function options(form) {
  const fields = form.elements;
  const params = {};
  for (const key of ["quantidade","duracao_min","duracao_pref","duracao_max","trecho_inicio","trecho_fim"]) {
    params[key] = fields[key].value === "" ? (key === "trecho_fim" ? null : 0) : Number(fields[key].value);
  }
  for (const key of ["formato","foco","enquadramento","estilo_legenda","posicao_legenda","idioma"]) {
    params[key] = fields[key].value;
  }
  for (const key of ["gancho","seguir_rosto","manchete"]) params[key] = fields[key].checked;
  for (const key of ["palavras_chave","evitar"]) params[key] = fields[key].value.split(",").map(value => value.trim()).filter(Boolean);
  return params;
}

function upload(optionsValue, file) {
  return new Promise((resolve,reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST","/studio/projects");
    xhr.setRequestHeader("Content-Type","application/octet-stream");
    xhr.setRequestHeader("X-Studio-Options",optionsValue);
    xhr.responseType = "json";
    xhr.upload.onprogress = event => {
      $("#upload-status").textContent = event.lengthComputable ? `Enviando vídeo · ${Math.round(event.loaded / event.total * 100)}%` : "Enviando vídeo…";
    };
    xhr.onload = () => {
      if (xhr.status === 202) resolve(xhr.response);
      else reject(new Error(xhr.response?.detail || "O envio do vídeo falhou."));
    };
    xhr.onerror = () => reject(new Error("A conexão caiu durante o envio."));
    xhr.send(file);
  });
}

$("#job").addEventListener("submit", async event => {
  event.preventDefault();
  const errorText = $("#job-error");
  errorText.textContent = "";
  const button = $("#generate");
  button.disabled = true;
  try {
    const params = options(event.currentTarget);
    if (!(params.duracao_min <= params.duracao_pref && params.duracao_pref <= params.duracao_max)) {
      throw new Error("A duração ideal deve ficar entre a mínima e a máxima.");
    }
    // HTTP headers are ASCII in browsers. Escape accents while preserving the JSON values.
    const encoded = JSON.stringify(params).replace(/[^\x00-\x7F]/g, c => "\\u" + c.charCodeAt(0).toString(16).padStart(4,"0"));
    let job;
    if (source === "url") {
      const url = $("#url").value.trim();
      if (!url.startsWith("https://")) throw new Error("Informe um link HTTPS.");
      job = await api("/projects", {
        method:"POST",headers:{"Content-Type":"application/json","X-Studio-Options":encoded},body:JSON.stringify({url})
      });
      names.set(job.job_id, new URL(url).hostname);
    } else {
      if (!selectedFile) throw new Error("Escolha um vídeo para enviar.");
      if (selectedFile.size > settings.max_mb * 1024 * 1024) throw new Error(`O limite é ${settings.max_mb} MiB.`);
      job = await upload(encoded, selectedFile);
      names.set(job.job_id, selectedFile.name);
    }
    refresh(true);
    $("#projetos").scrollIntoView({behavior:"smooth"});
  } catch (error) {
    errorText.textContent = error.message;
  } finally {
    button.disabled = false;
    $("#upload-status").textContent = "";
  }
});

function clipCard(project, cut) {
  const card = element("article",undefined,"clip");
  const video = element("video");
  video.controls = true;
  video.preload = "none";
  video.playsInline = true;
  video.poster = cut.url_capa;
  video.src = cut.url_mp4;
  const details = element("details");
  const select = element("select");
  select.setAttribute("aria-label","Rede social");
  for (const [key, label] of [["instagram","Instagram"],["tiktok","TikTok"],["youtube","YouTube Shorts"]]) {
    select.add(new Option(label,key));
  }
  const caption = element("p", cut.legendas.instagram, "small");
  caption.dataset.caption = "";
  select.onchange = () => { caption.textContent = cut.legendas[select.value]; };
  const copy = element("button","Copiar legenda","action");
  copy.onclick = async () => {
    try { await navigator.clipboard.writeText(caption.textContent); copy.textContent = "Copiada"; }
    catch { copy.textContent = "Selecione o texto para copiar"; }
  };
  details.append(element("summary","Legendas para postar"),select,caption,copy);
  const actions = element("div",undefined,"actions");
  for (const [label,url] of [["Baixar MP4",cut.url_mp4],["Abrir capa",cut.url_capa]]) {
    const link = element("a",label,"action");
    link.href = label === "Baixar MP4" && url.startsWith("/studio/") ? url + "?download=true" : url;
    link.target = "_blank";
    link.rel = "noopener";
    actions.append(link);
  }
  const edit = element("button","Editar","action");
  edit.disabled = ["queued","running"].includes(project.status);
  edit.onclick = () => openEdit(project, cut);
  actions.append(edit);
  card.append(video, element("h4",cut.titulo), element("p",cut.gancho,"small"),
              element("p",`${cut.duracao.toFixed(1)} s no original · Nota ${cut.nota}/100`,"small"), actions, details);
  return card;
}

function projectCard(project) {
  const card = element("article",undefined,"card project");
  const header = element("header");
  const title = names.get(project.job_id) || project.cortes[0]?.titulo || `Projeto ${project.job_id.slice(0,8)}`;
  const remove = element("button",["queued","running"].includes(project.status) ? "Cancelar" : "Excluir","quiet");
  remove.onclick = async () => {
    if (!confirm("Cancelar ou excluir este projeto e seus arquivos?")) return;
    try { await api(`/projects/${project.job_id}`,{method:"DELETE"}); refresh(true); }
    catch (error) { $("#projects-message").textContent = error.message; }
  };
  header.append(element("h3", title),remove);
  const status = element("p", project.status === "failed" ? "Falha no processamento" :
    `${stageNames[project.etapa] || project.etapa} · ${project.progresso}%`, "status");
  card.append(header,status);
  if (["queued","running"].includes(project.status)) {
    const progress = element("progress");
    progress.max = 100;
    progress.value = project.progresso;
    progress.setAttribute("aria-label","Progresso do processamento");
    card.append(progress);
  }
  if (project.erro) card.append(element("p",project.erro,"error"));
  if (project.cortes.length) {
    const clips = element("div",undefined,"clip-grid");
    clips.append(...project.cortes.map(cut => clipCard(project,cut)));
    card.append(clips);
  }
  return card;
}

async function refresh(force=false) {
  const version = ++refreshVersion;
  clearTimeout(refreshTimer);
  try {
    const data = await api("/projects");
    if (version !== refreshVersion || !settings) return;
    const nextSignature = JSON.stringify(data);
    if (signature !== nextSignature || force) {
      $("#projects-list").replaceChildren(...data.map(projectCard));
      signature = nextSignature;
    }
    $("#projects-message").textContent = data.length ? "" : "Os cortes que você gerar aparecem aqui.";
    if (data.some(project => ["queued","running"].includes(project.status))) refreshTimer = setTimeout(refresh,1300);
  } catch (error) {
    if (version !== refreshVersion) return;
    if (error.status === 401) showSession(null);
    else {
      $("#projects-message").textContent = "Sem conexão com o estúdio. Tentando novamente.";
      refreshTimer = setTimeout(refresh,3500);
    }
  }
}

function openEdit(project,cut) {
  editingProject = project.job_id;
  currentEdit = cut.id;
  const fields = $("#edit-form").elements;
  fields.inicio.value = Number(cut.inicio.toFixed(3));
  fields.fim.value = Number(cut.fim.toFixed(3));
  fields.gancho.value = cut.gancho;
  fields.manchete.value = "";
  fields.blocos.value = "";
  fields.estilo.value = $("#styles").value;
  $("#edit-error").textContent = "";
  $("#edit").showModal();
}
$("#close-edit").onclick = () => $("#edit").close();
$("#edit-form").addEventListener("submit", async event => {
  event.preventDefault();
  const fields = event.currentTarget.elements;
  const button = event.currentTarget.querySelector(".primary");
  button.disabled = true;
  $("#edit-error").textContent = "";
  try {
    const blocos = fields.blocos.value.split("\n").filter(line => line.trim()).map(line => {
      const [a,b,...text] = line.split("|");
      if (!a?.trim() || !b?.trim() || !Number.isFinite(Number(a)) || !Number.isFinite(Number(b)) || !text.join("|").trim()) {
        throw new Error("Use o formato início | fim | texto para cada bloco.");
      }
      return {inicio:Number(a),fim:Number(b),texto:text.join("|").trim()};
    });
    const body = {
      inicio:Number(fields.inicio.value),fim:Number(fields.fim.value),estilo_legenda:fields.estilo.value,
      gancho:fields.gancho.value.trim() || false,manchete:fields.manchete.value.trim() || false
    };
    if (blocos.length) body.legenda = {blocos,estilo:fields.estilo.value};
    await api(`/projects/${editingProject}/cuts/${currentEdit}`,{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)
    });
    $("#edit").close();
    refresh(true);
  } catch (error) {
    $("#edit-error").textContent = error.message;
  } finally { button.disabled = false; }
});
