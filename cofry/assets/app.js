import {createSocial} from "./social.js";
import {createOwner} from "./owner.js";
import {createGeneration} from "./generation.js";

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
let refreshVersion = 0, activeView = "inicio", installPrompt, projectsData = [], submitting = false, paused = false;
const viewNames = {
  inicio:"Início", novo:"Estúdio de Cortes", projetos:"Projetos", agenda:"Agenda",
  contas:"Contas", geracao:"Imagens e Vídeos", notificacoes:"Notificações", configuracoes:"Configurações"
};
const menuMedia = matchMedia("(max-width: 880px)");
const projectIsActive = project => ["queued","running"].includes(project.status);
const social = createSocial({api, getProjects: () => projectsData});
const owner = createOwner({api, onPause(value) {
  paused = value;
  $("#generation-paused").hidden = !value;
  updateGenerate();
}});
const generation = createGeneration({api});
const names = new Map();

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

for (const wave of document.querySelectorAll("[data-wave]")) {
  for (let i=0;i<75;i++) {
    const bar = element("b");
    bar.style.setProperty("--h", `${12 + (Math.sin(i*1.75)**2 * 68 + Math.sin(i*.32)**2 * 17)}%`);
    wave.append(bar);
  }
}
for (const [value, name] of Object.entries(styles)) {
  for (const selector of ["#styles", "#edit-styles"]) $(selector).add(new Option(name, value));
}

async function api(path, options) {
  const response = await fetch("/studio" + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === "string" ? data.detail : "Não foi possível concluir a ação.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function updateMenu(open) {
  const mobile = menuMedia.matches;
  if (mobile) {
    document.body.classList.toggle("drawer-open", open);
    $("#nav-scrim").hidden = !open;
  } else {
    document.body.classList.remove("drawer-open");
    document.body.classList.toggle("sidebar-collapsed", !open);
    $("#nav-scrim").hidden = true;
  }
  $("#sidebar").inert = mobile && !open;
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
  $("#menu-toggle").setAttribute("aria-label", open ? "Recolher menu" : "Abrir menu");
}

$("#menu-toggle").addEventListener("click", () => {
  updateMenu($("#menu-toggle").getAttribute("aria-expanded") !== "true");
});
$("#nav-scrim").addEventListener("click", () => {
  updateMenu(false);
  $("#menu-toggle").focus();
});
menuMedia.addEventListener("change", event => updateMenu(!event.matches));
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && document.body.classList.contains("drawer-open")) {
    updateMenu(false);
    $("#menu-toggle").focus();
  }
});

function scrollToProject() {
  const [view, projectId] = location.hash.slice(1).split("/");
  if (view === "projetos" && projectId) document.getElementById(`project-${projectId}`)?.scrollIntoView();
}

function renderView() {
  const requested = location.hash.slice(1).split("/")[0] || "inicio";
  const view = Object.hasOwn(viewNames, requested) && (requested !== "notificacoes" || settings?.user?.owner)
    ? requested : "inicio";
  const changed = activeView !== view;
  activeView = view;
  for (const section of document.querySelectorAll("[data-panel]")) section.hidden = section.dataset.panel !== view;
  for (const link of document.querySelectorAll("[data-route]")) {
    if (link.dataset.route === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("#bar-location").textContent = settings ? viewNames[view] : "Estúdio";
  $("#account-menu").open = false;
  if (menuMedia.matches) updateMenu(false);
  if (changed) window.scrollTo({top:0, behavior:"instant"});
  if (view === "projetos" && location.hash.includes("/")) {
    $("#project-search").value = "";
    $("#project-filter").value = "";
    renderProjects();
  }
  requestAnimationFrame(scrollToProject);
  social.route(view);
  owner.route(view);
  if (view === "geracao" && settings) generation.enter();
}
window.addEventListener("hashchange",renderView);
for (const link of document.querySelectorAll("#sidebar [data-route]")) {
  link.addEventListener("click", () => {
    if (menuMedia.matches) updateMenu(false);
  });
}

function accountDetails(config) {
  const user = config?.user;
  const name = user?.name || "Proprietário";
  const email = user?.email || "";
  for (const output of document.querySelectorAll("[data-account-name]")) output.textContent = name;
  for (const output of document.querySelectorAll("[data-account-email]")) {
    output.textContent = email;
    output.hidden = !email;
  }
  $("#initials").textContent = name.trim().split(/\s+/).slice(0,2).map(part => part[0]).join("").toLocaleUpperCase("pt-BR");
  $("#auth-method").textContent = config?.strategy === "google" ? "Sessão do Google" : "Sessão pela chave do estúdio";
  $("#sidebar-credits").hidden = !config?.credits?.unlimited;
  $("#header-notifications").hidden = !user?.owner;
  for (const el of document.querySelectorAll("[data-owner-only]")) el.hidden = !user?.owner;
}

function showSession(config) {
  settings = config;
  const signedIn = Boolean(config);
  $("#entry").hidden = signedIn;
  document.body.classList.toggle("signed-in", signedIn);
  for (const selector of ["#workspace","#logout","#sidebar","#bar-actions","#menu-toggle","#bottom-nav"]) {
    $(selector).hidden = !signedIn;
  }
  updateMenu(signedIn && !menuMedia.matches);
  clearTimeout(refreshTimer);
  refreshVersion++;
  social.session(signedIn);
  generation.setSession(config);
  owner.session(signedIn && Boolean(config?.user?.owner));
  accountDetails(config);
  renderView();
  if (!config) {
    projectsData = [];
    $("#projects-list").replaceChildren();
    $("#recent-projects").replaceChildren();
    $("#job").reset();
    choose(undefined);
    setSource("file");
    $("#generate").disabled = true;
    return;
  }
  $("#limits").textContent = `Ou clique para escolher · até ${config.max_mb} MiB · mínimo 2 min`;
  $("#link-help").textContent = config.youtube
    ? "Link direto, Google Drive, Dropbox ou YouTube. Use vídeos próprios ou autorizados."
    : "Links públicos de arquivo, Google Drive e Dropbox.";
  $("#mode").textContent = config.modo === "ia"
    ? "Seleção e ganchos gerados com IA."
    : "Seleção local pela transcrição. O gancho usa uma frase do vídeo.";
  $("#demo-banner").hidden = config.modo !== "local" && config.demo !== true;
  $("#demo-description").textContent = "A seleção roda localmente, sem o provedor externo de IA.";
  signature = "";
  refresh();
}

const loginErrors = {
  google_unavailable:"O login com Google ainda não está configurado.",
  google_denied:"Esta conta Google não tem acesso ao estúdio.",
  google_invalid:"Não foi possível validar a sessão do Google.",
  google_retry:"Não foi possível entrar com Google. Tente novamente."
};
const loginURL = new URL(location.href);
const loginError = loginURL.searchParams.get("auth");
if (Object.hasOwn(loginErrors, loginError)) {
  $("#login-error").textContent = loginErrors[loginError];
  loginURL.searchParams.delete("auth");
  history.replaceState(null, "", loginURL);
}

api("/auth/options").then(options => {
  $("#google-login").hidden = !options.google;
  if (options.google) {
    $(".login").classList.add("login-with-google");
    $("#login-copy").textContent = "Use sua conta do Google ou a chave do estúdio.";
  }
}).catch(() => {});

window.addEventListener("beforeinstallprompt", event => {
  event.preventDefault();
  installPrompt = event;
  $("#install-button").hidden = false;
});
$("#install-button").addEventListener("click", async () => {
  if (!installPrompt) return;
  const prompt = installPrompt;
  installPrompt = null;
  $("#install-button").hidden = true;
  await prompt.prompt();
  if ((await prompt.userChoice).outcome === "accepted") {
    $("#install-help").textContent = "A instalação foi iniciada no seu dispositivo.";
  }
});
window.addEventListener("appinstalled", () => {
  $("#install-help").textContent = "Cofry está instalado neste dispositivo.";
  $("#install-button").hidden = true;
});
if (matchMedia("(display-mode: standalone)").matches || navigator.standalone) {
  $("#install-help").textContent = "Cofry está instalado neste dispositivo.";
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
    xhr.setRequestHeader("X-Studio-Title",encodeURIComponent(file.name.slice(0,160)));
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

const rights = $("#video-rights");
async function checkSourceLink(value) {
  let parsed;
  try { parsed = new URL(value); }
  catch { throw new Error("Informe um link HTTPS válido."); }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || (parsed.port && parsed.port !== "443")) {
    throw new Error("Informe um link HTTPS válido.");
  }
  const host = parsed.hostname.toLowerCase().replace(/\.$/,"");
  if ((host === "youtu.be" || host === "youtube.com" || host.endsWith(".youtube.com")) && !settings?.youtube) {
    const current = await api("/session");
    settings.youtube = current.youtube;
    if (current.youtube_status === "verificando") {
      throw new Error("O servidor está verificando o acesso ao YouTube. Aguarde alguns instantes e tente de novo.");
    }
    if (!current.youtube) {
      throw new Error(current.youtube_status === "indisponivel"
        ? "Este servidor não conseguiu acessar o YouTube. Você ainda pode colar um link do Drive, Dropbox ou MP4 direto."
        : "Para este link, envie o arquivo ou use Drive/Dropbox.");
    }
  }
  return parsed;
}
function updateGenerate() {
  $("#generate").disabled = submitting || paused || !rights.checked;
}
rights.addEventListener("change", updateGenerate);
$("#job").addEventListener("submit", async event => {
  event.preventDefault();
  const errorText = $("#job-error");
  if (submitting) return;
  if (paused) {
    errorText.textContent = "As gerações estão pausadas.";
    return;
  }
  errorText.textContent = "";
  const button = $("#generate");
  if (!rights.checked) {
    errorText.textContent = "Confirme que tem autorização para usar o vídeo.";
    return;
  }
  submitting = true;
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
      const target = await checkSourceLink(url);
      job = await api("/projects", {
        method:"POST",headers:{"Content-Type":"application/json","X-Studio-Options":encoded},body:JSON.stringify({url})
      });
      names.set(job.job_id, target.hostname);
    } else {
      if (!selectedFile) throw new Error("Escolha um vídeo para enviar.");
      if (selectedFile.size > settings.max_mb * 1024 * 1024) throw new Error(`O limite é ${settings.max_mb} MiB.`);
      job = await upload(encoded, selectedFile);
      names.set(job.job_id, selectedFile.name);
    }
    location.hash = `projetos/${job.job_id}`;
    refresh(true);
  } catch (error) {
    errorText.textContent = error.message;
  } finally {
    submitting = false;
    updateGenerate();
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
  const schedule = element("button","Agendar","action");
  schedule.disabled = projectIsActive(project);
  schedule.addEventListener("click", () => social.openSchedule({
    project:project.job_id, id:cut.id
  }));
  actions.append(edit,schedule);
  card.append(video, element("h4",cut.titulo), element("p",cut.gancho,"small"),
              element("p",`${cut.duracao.toFixed(1)} s no original · Nota ${cut.nota}/100`,"small"), actions, details);
  return card;
}

function projectTitle(project) {
  return project.title || names.get(project.job_id) || project.cortes[0]?.titulo || `Projeto ${project.job_id.slice(0,8)}`;
}

function projectSummary(project) {
  return project.status === "failed" ? "Falha no processamento" :
    projectIsActive(project) ? `${stageNames[project.etapa] || project.etapa} · ${project.progresso}%` :
    `${project.cortes.length} ${project.cortes.length === 1 ? "corte" : "cortes"}`;
}

function updateDashboard(projects) {
  const formatter = new Intl.NumberFormat("pt-BR");
  const clipCount = projects.reduce((total, project) => total + project.cortes.length, 0);
  $("#metric-projects").textContent = formatter.format(projects.length);
  $("#metric-clips").textContent = formatter.format(clipCount);
  $("#metric-active").textContent = formatter.format(projects.filter(projectIsActive).length);
  $("#recent-message").textContent = projects.length ? "" : "Seus projetos vão aparecer aqui depois do primeiro envio.";
  const recent = projects.slice(0,5).map(project => {
    const row = element("li");
    const link = element("a",undefined,"recent-link");
    link.href = `#projetos/${project.job_id}`;
    link.append(element("strong",projectTitle(project)), element("span",projectSummary(project),"small"));
    row.append(link);
    return row;
  });
  $("#recent-projects").replaceChildren(...recent);
}

function expiration(project) {
  const when = Date.parse(project.expires_at);
  if (!Number.isFinite(when)) return "";
  const days = Math.ceil((when - Date.now()) / 86_400_000);
  if (days < 0) return "Expirado";
  if (days === 0) return "Expira hoje";
  return `Expira em ${days} ${days === 1 ? "dia" : "dias"}`;
}

function projectCard(project) {
  const card = element("article",undefined,"card project");
  const header = element("header");
  card.id = `project-${project.job_id}`;
  const title = projectTitle(project);
  const remove = element("button",["queued","running"].includes(project.status) ? "Cancelar" : "Excluir","quiet");
  remove.onclick = async () => {
    if (!confirm("Cancelar ou excluir este projeto e seus arquivos?")) return;
    try { await api(`/projects/${project.job_id}`,{method:"DELETE"}); refresh(true); }
    catch (error) { $("#projects-message").textContent = error.message; }
  };
  const headline = element("div", undefined, "project-heading");
  const thumb = project.thumbnail || project.cortes[0]?.url_capa;
  if (thumb) {
    const image = element("img",undefined,"project-thumbnail");
    image.src = thumb;
    image.alt = "";
    image.loading = "lazy";
    headline.append(image);
  }
  const info = element("div");
  info.append(element("h3", title));
  const meta = [`${project.cortes.length} ${project.cortes.length === 1 ? "corte" : "cortes"}`,expiration(project)].filter(Boolean);
  info.append(element("p",meta.join(" · "),"small"));
  headline.append(info);
  const actions = element("div",undefined,"project-actions");
  const rename = element("button","Renomear","quiet");
  rename.type = "button";
  rename.onclick = async () => {
    const next = prompt("Novo nome do projeto",title)?.trim();
    if (!next || next === title) return;
    rename.disabled = true;
    try {
      await api(`/projects/${project.job_id}`,{method:"PATCH",headers:{"Content-Type":"application/json"},body:JSON.stringify({title:next})});
      await refresh(true);
    } catch (error) {
      $("#projects-message").textContent = error.message;
    } finally { rename.disabled = false; }
  };
  actions.append(rename,remove);
  header.append(headline,actions);
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
  if (project.status === "failed") {
    const retry = element("button","Tentar de novo","quiet");
    retry.type = "button";
    retry.onclick = async () => {
      retry.disabled = true;
      try {
        const started = await api(`/projects/${project.job_id}/retry`, {method:"POST"});
        location.hash = `projetos/${started.job_id}`;
        await refresh(true);
      } catch (error) { $("#projects-message").textContent = error.message; }
      finally { retry.disabled = false; }
    };
    card.append(retry);
  }
  if (project.cortes.length) {
    const clips = element("div",undefined,"clip-grid");
    clips.append(...project.cortes.map(cut => clipCard(project,cut)));
    card.append(clips);
  }
  return card;
}

function searchText(value) {
  return value.normalize("NFD").replace(/\p{Diacritic}/gu,"").toLowerCase();
}

function renderProjects() {
  const term = searchText($("#project-search").value.trim());
  const filter = $("#project-filter").value;
  const shown = projectsData.filter(project => {
    if (term && !searchText(projectTitle(project)).includes(term)) return false;
    if (filter === "active") return projectIsActive(project);
    if (filter === "ready") return project.cortes.length > 0;
    if (filter === "failed") return project.status === "failed";
    return true;
  });
  $("#projects-list").replaceChildren(...shown.map(projectCard));
  $("#projects-message").textContent = shown.length ? "" : (projectsData.length
    ? "Nenhum projeto encontrado com estes filtros."
    : "Os cortes que você gerar aparecem aqui.");
}
for (const selector of ["#project-search","#project-filter"]) $(selector).addEventListener("input",renderProjects);

async function refresh(force=false) {
  const version = ++refreshVersion;
  clearTimeout(refreshTimer);
  try {
    const data = await api("/projects");
    if (version !== refreshVersion || !settings) return;
    const nextSignature = JSON.stringify(data);
    if (signature !== nextSignature || force) {
      projectsData = data;
      renderProjects();
      updateDashboard(data);
      signature = nextSignature;
      if (force) requestAnimationFrame(scrollToProject);
    }
    if (data.some(projectIsActive)) refreshTimer = setTimeout(refresh,1300);
  } catch (error) {
    if (version !== refreshVersion) return;
    if (error.status === 401) showSession(null);
    else {
      $("#projects-message").textContent = "Sem conexão com o estúdio. Tentando novamente.";
      $("#recent-message").textContent = $("#projects-message").textContent;
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
