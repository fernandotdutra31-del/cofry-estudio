const $ = selector => document.querySelector(selector);
const TZ = "America/Sao_Paulo";
const platforms = {instagram:"Instagram", tiktok:"TikTok", youtube:"YouTube"};
const postStates = {
  scheduled:"Agendado", queued:"Na fila", publishing:"Publicando", pending:"Pendente",
  pending_approval:"Aguardando aprovação", submitting:"Enviando",
  draft:"Rascunho", partial:"Publicado parcialmente",
  published:"Publicado", failed:"Falha", cancelled:"Cancelado", canceled:"Cancelado"
};
const scheduledStates = new Set(["scheduled","queued","publishing","pending","pending_approval","submitting"]);
const editableStates = new Set(["scheduled","failed","draft"]);
const nonCancellableStates = new Set(["publishing","published","partial","cancelled","canceled","submitting"]);
const monthName = new Intl.DateTimeFormat("pt-BR", {month:"long", year:"numeric", timeZone:"UTC"});
const dayName = new Intl.DateTimeFormat("pt-BR", {dateStyle:"medium", timeZone:TZ});
const dayTime = new Intl.DateTimeFormat("pt-BR", {dateStyle:"short", timeStyle:"short", timeZone:TZ});
const dateParts = new Intl.DateTimeFormat("en-CA", {year:"numeric", month:"2-digit", day:"2-digit", timeZone:TZ});
const timePart = new Intl.DateTimeFormat("pt-BR", {hour:"2-digit", minute:"2-digit", hourCycle:"h23", timeZone:TZ});
const zoneOffset = new Intl.DateTimeFormat("en", {timeZone:TZ, timeZoneName:"longOffset"});

function node(tag, text, className) {
  const output = document.createElement(tag);
  if (text !== undefined) output.textContent = text;
  if (className) output.className = className;
  return output;
}

function dateKey(date) {
  const parts = Object.fromEntries(dateParts.formatToParts(date).map(part => [part.type,part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function scheduledTime(day, time) {
  if (!day || !time) return null;
  const naive = new Date(`${day}T${time}:00Z`);
  if (!Number.isFinite(naive.getTime())) return null;
  const offset = zoneOffset.formatToParts(naive).find(part => part.type === "timeZoneName").value.replace("GMT", "") || "Z";
  return new Date(`${day}T${time}:00${offset}`);
}

function tomorrow() {
  const date = new Date(`${dateKey(new Date())}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return dateKey(date);
}

function normalized(text) {
  return String(text || "").normalize("NFD").replace(/\p{Diacritic}/gu,"").toLowerCase();
}

function chooseCut(projects, mode) {
  const result = [];
  for (const project of projects) {
    if (["queued","running"].includes(project.status)) continue;
    for (const cut of project.cortes) {
      result.push({project:project.job_id, id:cut.id, title:cut.titulo, captions:cut.legendas || {}});
    }
  }
  if (mode === "demo") result.push({
    project:"demo", id:"demo", title:"Corte de demonstração",
    captions: {instagram:"Este corte serve para testar a agenda do Cofry.", tiktok:"Corte de teste do Cofry.", youtube:"Teste de agendamento no Cofry."}
  });
  return result;
}

export function createSocial({api, getProjects}) {
  let enabled = false, state, pending, timer, currentView, generation = 0, selectedDay, editTarget, scheduleCuts = [], busy = false;
  let chosen = new Set(), sent = new Set(), keys = new Map(), captions = new Map();
  const [year, month] = dateKey(new Date()).split("-").map(Number);
  let visibleMonth = new Date(Date.UTC(year, month - 1, 1));
  const socialPath = "/social/posts";
  const dialog = $("#schedule-dialog");

  function accounts() {
    return state?.accounts ?? [];
  }

  function posts() {
    return state?.posts ?? [];
  }

  function activeAccounts() {
    return accounts().filter(account => account.active && Object.hasOwn(platforms,account.platform));
  }

  function drawAccounts() {
    const demo = state?.mode === "demo";
    $("#accounts-demo").hidden = !demo;
    $("#accounts-message").textContent = state ? "" : "Carregando contas…";
    if (!state) return;
    const cards = [];
    for (const [id, label] of Object.entries(platforms)) {
      const rows = accounts().filter(account => account.platform === id);
      for (const account of rows.length ? rows : [{name:"Conta não conectada",active:false}]) {
        const card = node("article",undefined,"card social-account");
        card.append(node("h2",label),node("p",account.name));
        card.append(node("span",account.active ? (demo ? "Conta de teste" : "Conectada") : "Indisponível","status-chip"));
        cards.push(card);
      }
    }
    $("#social-accounts").replaceChildren(...cards);
  }

  function drawCalendar() {
    $("#calendar-heading").textContent = monthName.format(visibleMonth);
    const year = visibleMonth.getUTCFullYear();
    const month = visibleMonth.getUTCMonth();
    const mondayOffset = (visibleMonth.getUTCDay() + 6) % 7;
    const last = new Date(Date.UTC(year,month + 1,0)).getUTCDate();
    const todayKey = dateKey(new Date());
    const postCount = new Map();
    for (const post of posts()) {
      if (["cancelled","canceled"].includes(post.status)) continue;
      const key = dateKey(new Date(post.scheduled_for));
      postCount.set(key,(postCount.get(key) || 0) + 1);
    }
    const days = ["Seg","Ter","Qua","Qui","Sex","Sáb","Dom"].map(label => node("span",label,"calendar-weekday"));
    for (let i=0;i<mondayOffset;i++) days.push(node("span"));
    for (let day=1;day<=last;day++) {
      const date = new Date(Date.UTC(year,month,day,12));
      const key = dateKey(date);
      const count = postCount.get(key);
      const button = node("button",String(day),count ? "has-posts" : undefined);
      button.type = "button";
      button.setAttribute("aria-pressed",String(selectedDay === key));
      button.setAttribute("aria-label",`${dayName.format(date)}${count ? `, ${count} ${count === 1 ? "publicação" : "publicações"}` : ""}`);
      if (key === todayKey) button.setAttribute("aria-current","date");
      button.addEventListener("click",() => {
        selectedDay = selectedDay === key ? undefined : key;
        drawCalendar();
        drawHistory();
        $("button[data-date='" + key + "']")?.focus({preventScroll:true});
      });
      button.dataset.date = key;
      days.push(button);
    }
    $("#calendar-grid").replaceChildren(...days);
    $("#calendar-date-filter").hidden = !selectedDay;
    if (selectedDay) $("#calendar-selected").textContent = dayName.format(new Date(`${selectedDay}T12:00:00Z`));
  }

  function drawPost(post) {
    const row = node("li");
    const head = node("div",undefined,"post-row");
    const status = node("span",postStates[post.status] || post.status,"status-chip");
    status.dataset.state = post.status;
    head.append(node("strong",platforms[post.platform] || post.platform),status);
    const caption = node("p",post.caption,"post-caption");
    row.append(head,node("p",dayTime.format(new Date(post.scheduled_for)), "small"),caption);
    const actions = node("div",undefined,"post-actions");
    if (editableStates.has(post.status)) {
      const edit = node("button","Editar / reagendar","quiet");
      edit.type = "button";
      edit.addEventListener("click",() => openSchedule(null,post));
      actions.append(edit);
    }
    if (!nonCancellableStates.has(post.status)) {
      const cancel = node("button","Cancelar","quiet");
      cancel.type = "button";
      cancel.addEventListener("click",async () => {
        if (!confirm("Cancelar este agendamento?")) return;
        cancel.disabled = true;
        try {
          await api(`${socialPath}/${encodeURIComponent(post.id)}`,{method:"DELETE"});
          await refresh();
        } catch (error) { $("#history-message").textContent = error.message; }
        finally { cancel.disabled = false; }
      });
      actions.append(cancel);
    }
    if (actions.children.length) row.append(actions);
    return row;
  }

  function drawHistory() {
    const all = posts();
    $("#post-total").textContent = String(all.length);
    $("#post-pending").textContent = String(all.filter(post => scheduledStates.has(post.status)).length);
    $("#post-done").textContent = String(all.filter(post => post.status === "published").length);
    $("#post-failed").textContent = String(all.filter(post => post.status === "failed").length);
    const query = normalized($("#post-search").value);
    const platform = $("#post-platform").value;
    const status = $("#post-status").value;
    const filtered = all.filter(post => {
      if (selectedDay && dateKey(new Date(post.scheduled_for)) !== selectedDay) return false;
      if (platform && post.platform !== platform) return false;
      if (status === "scheduled" ? !scheduledStates.has(post.status) : status && post.status !== status && !(status === "cancelled" && post.status === "canceled")) return false;
      return !query || normalized(post.caption).includes(query);
    }).sort((a,b) => Date.parse(b.scheduled_for) - Date.parse(a.scheduled_for));
    $("#post-list").replaceChildren(...filtered.map(drawPost));
    $("#history-message").textContent = state ? (state.warning || (filtered.length ? "" : "Nenhum agendamento neste filtro.")) : "Carregando agenda…";
  }

  function paint() {
    $("#agenda-demo").hidden = state?.mode !== "demo";
    drawAccounts();
    drawCalendar();
    drawHistory();
  }

  function refresh() {
    clearTimeout(timer);
    if (!enabled) return Promise.resolve();
    if (pending) return pending;
    const current = generation;
    const request = api("/social").then(data => {
      if (current !== generation || !enabled) return undefined;
      const changed = !state || JSON.stringify(state) !== JSON.stringify(data);
      state = data;
      if (changed) paint();
      return state;
    }).catch(error => {
      if (current !== generation || !enabled) return undefined;
      for (const target of ["#history-message","#accounts-message"]) $(target).textContent = error.message;
      return undefined;
    }).finally(() => {
      if (pending === request) {
        pending = undefined;
        if (enabled && currentView === "agenda") {
          const nextDemo = state?.mode === "demo" ? Math.min(Infinity,...posts()
            .filter(post => post.status === "scheduled")
            .map(post => Date.parse(post.scheduled_for) - Date.now())) : Infinity;
          const delay = Math.max(1000,Math.min(state?.mode === "demo" ? 15000 : 30000, nextDemo + 300));
          timer = setTimeout(refresh, delay);
        }
      }
    });
    pending = request;
    return request;
  }

  function session(available) {
    enabled = available;
    generation++;
    pending = undefined;
    clearTimeout(timer);
    if (!available) {
      state = undefined;
      $("#post-list").replaceChildren();
      $("#social-accounts").replaceChildren();
      if (dialog.open) dialog.close();
    }
  }

  function route(view) {
    currentView = view;
    clearTimeout(timer);
    if (enabled && ["contas","agenda"].includes(view)) refresh();
  }

  function selectedCut() {
    const selection = $("#schedule-cut").value;
    return scheduleCuts.find(cut => `${cut.project}/${cut.id}` === selection);
  }

  function selectedAccounts() {
    return activeAccounts().filter(account => chosen.has(account.id));
  }

  function selectedPlatforms() {
    return editTarget ? [editTarget.platform] : [...new Set(selectedAccounts().map(account => account.platform))];
  }

  function drawSummary() {
    const chosenDate = scheduledTime($("#schedule-day").value,$("#schedule-time").value);
    const count = editTarget ? 1 : selectedAccounts().length;
    $("#schedule-summary").textContent = chosenDate && count
      ? `${count} ${count === 1 ? "publicação" : "publicações"} em ${dayTime.format(chosenDate)} (Brasília).` : "";
    $("#schedule-submit").disabled = busy || !count || (!editTarget && !selectedCut()) || !chosenDate;
  }

  function removeConsentWhenIrrelevant(networks) {
    const needsConsent = !editTarget && networks.includes("tiktok");
    $("#tiktok-consent").hidden = !needsConsent;
    $("#schedule-consent").required = needsConsent;
    if (!needsConsent) $("#schedule-consent").checked = false;
  }

  function drawCaptions() {
    const networks = selectedPlatforms();
    const cut = selectedCut();
    $("#schedule-captions").replaceChildren(...networks.map(platform => {
      const label = node("label",`Legenda para ${platforms[platform] || platform}`);
      const field = node("textarea");
      field.rows = 2;
      field.maxLength = 2200;
      field.value = captions.get(platform) ?? (editTarget?.caption || cut?.captions[platform] || "");
      const count = node("span",`${field.value.length}/2200`,"small");
      field.addEventListener("input",() => {
        captions.set(platform,field.value);
        count.textContent = `${field.value.length}/2200`;
      });
      label.append(field,count);
      return label;
    }));
    removeConsentWhenIrrelevant(networks);
    drawSummary();
  }

  function drawScheduleAccounts() {
    $("#schedule-account-list").replaceChildren(...activeAccounts().map(account => {
      const label = node("label");
      const check = node("input");
      check.type = "checkbox";
      check.checked = chosen.has(account.id);
      check.addEventListener("change",() => {
        if (check.checked) chosen.add(account.id);
        else chosen.delete(account.id);
        drawCaptions();
      });
      label.append(check, document.createTextNode(`${platforms[account.platform]} · ${account.name}`));
      return label;
    }));
    if (!activeAccounts().length) $("#schedule-account-list").append(node("p","Conecte uma conta no Zernio para agendar.","small"));
    $("#schedule-accounts").hidden = Boolean(editTarget);
  }

  async function openSchedule(cut, post) {
    if (!state && !await refresh()) return;
    editTarget = post || undefined;
    chosen = new Set();
    sent = new Set();
    keys = new Map();
    captions = new Map();
    $("#schedule-error").textContent = "";
    $("#schedule-title").textContent = editTarget ? "Editar agendamento" : "Agendar corte";
    $("#schedule-submit").textContent = editTarget ? "Salvar alterações" : "Confirmar agendamento";
    $("#schedule-mode").hidden = state.mode !== "demo";
    $("#schedule-cut-label").hidden = Boolean(editTarget);
    $("#schedule-consent").checked = false;
    scheduleCuts = chooseCut(getProjects(),state.mode);
    $("#schedule-cut").disabled = Boolean(editTarget);
    $("#schedule-cut").replaceChildren(...scheduleCuts.map(entry => new Option(entry.title,`${entry.project}/${entry.id}`)));
    if (cut) $("#schedule-cut").value = `${cut.project}/${cut.id}`;
    const oldDate = editTarget ? new Date(editTarget.scheduled_for) : undefined;
    $("#schedule-day").value = oldDate ? dateKey(oldDate) : selectedDay || tomorrow();
    $("#schedule-day").min = dateKey(new Date());
    $("#schedule-time").value = oldDate ? timePart.format(oldDate) : "09:00";
    drawScheduleAccounts();
    drawCaptions();
    if (!dialog.open) dialog.showModal();
    if (!editTarget && !scheduleCuts.length) $("#schedule-error").textContent = "Gere um corte antes de agendar.";
  }

  function currentCaption(platform) {
    return captions.get(platform) ?? (editTarget?.caption || selectedCut()?.captions[platform] || "");
  }

  $("#schedule-new").addEventListener("click",() => openSchedule());
  $("#schedule-close").addEventListener("click",() => dialog.close());
  $("#schedule-cut").addEventListener("change",() => { captions.clear(); drawCaptions(); });
  for (const id of ["#schedule-day","#schedule-time"]) $(id).addEventListener("input",drawSummary);
  for (const button of $("#schedule-presets").querySelectorAll("button")) {
    button.addEventListener("click",() => { $("#schedule-time").value = button.dataset.time; drawSummary(); });
  }
  $("#schedule-form").addEventListener("submit",async event => {
    event.preventDefault();
    if (busy) return;
    const date = scheduledTime($("#schedule-day").value,$("#schedule-time").value);
    const cut = selectedCut();
    const recipients = selectedAccounts();
    const errorOutput = $("#schedule-error");
    errorOutput.textContent = "";
    if (!date || date.getTime() <= Date.now()) {
      errorOutput.textContent = "Escolha um dia e horário no futuro.";
      return;
    }
    if (!editTarget && (!cut || !recipients.length)) return;
    busy = true;
    drawSummary();
    try {
      if (editTarget) {
        await api(`${socialPath}/${encodeURIComponent(editTarget.id)}`,{
          method:"PATCH", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({scheduled_for:date.toISOString(),caption:currentCaption(editTarget.platform)})
        });
      } else {
        for (const account of recipients) {
          if (sent.has(account.id)) continue;
          if (!keys.has(account.id)) keys.set(account.id,crypto.randomUUID());
          await api(socialPath,{
            method:"POST", headers:{"Content-Type":"application/json"},
            body:JSON.stringify({
              project_id:cut.project,cut_id:cut.id,account_id:account.id,scheduled_for:date.toISOString(),
              caption:currentCaption(account.platform),consent:$("#schedule-consent").checked,
              idempotency_key:keys.get(account.id)
            })
          });
          sent.add(account.id);
        }
      }
      dialog.close();
      location.hash = "agenda";
      selectedDay = dateKey(date);
      const [year, month] = selectedDay.split("-").map(Number);
      visibleMonth = new Date(Date.UTC(year,month-1,1));
      await refresh();
    } catch (error) {
      errorOutput.textContent = sent.size ? `${sent.size} agendamento(s) concluído(s). ${error.message}` : error.message;
      if (sent.size) await refresh();
    } finally {
      busy = false;
      drawSummary();
    }
  });

  for (const [selector, delta] of [["#month-prev",-1],["#month-next",1]]) {
    $(selector).addEventListener("click",() => {
      visibleMonth = new Date(Date.UTC(visibleMonth.getUTCFullYear(),visibleMonth.getUTCMonth() + delta,1));
      selectedDay = undefined;
      drawCalendar();
      drawHistory();
    });
  }
  $("#clear-day").addEventListener("click",() => { selectedDay = undefined; drawCalendar(); drawHistory(); });
  for (const selector of ["#post-search","#post-platform","#post-status"]) $(selector).addEventListener("input",drawHistory);

  return {session,route,openSchedule};
}
