const $ = selector => document.querySelector(selector);
const when = new Intl.DateTimeFormat("pt-BR",{
  dateStyle:"short", timeStyle:"short", timeZone:"America/Sao_Paulo"
});
const activityLabels = {
  cut:"Cortes", cuts:"Cortes", cortes:"Cortes", cuts_ready:"Cortes prontos", generation:"Geração",
  rerender:"Edição de corte", project:"Projeto", retry:"Nova tentativa", post:"Agendamento",
  schedule:"Agendamento", image:"Imagem", video:"Vídeo"
};

function node(type, text, className) {
  const output = document.createElement(type);
  if (text !== undefined) output.textContent = text;
  if (className) output.className = className;
  return output;
}

function happened(value) {
  const stamp = new Date(typeof value === "number" && value < 10_000_000_000 ? value * 1000 : value);
  return Number.isFinite(stamp.getTime()) ? when.format(stamp) : "";
}

function supportPush() {
  return isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function keyBytes(key) {
  const encoded = key.replace(/-/g,"+").replace(/_/g,"/");
  const padded = encoded.padEnd(Math.ceil(encoded.length / 4) * 4,"=");
  return Uint8Array.from(atob(padded), char => char.charCodeAt(0));
}

export function createOwner({api, onPause}) {
  let enabled = false, state, poll, pending, version = 0, subscribeBusy = false;

  function drawUnread(count) {
    const value = Math.max(0, Number(count) || 0);
    const badge = $("#notification-badge");
    badge.hidden = !value;
    badge.textContent = value > 99 ? "99+" : String(value);
    $("#header-notifications").setAttribute("aria-label", value ? `Notificações, ${value} não lidas` : "Notificações");
    $("#notifications-read").disabled = !value;
  }

  function drawNotifications() {
    const rows = state.notifications || [];
    $("#notifications-empty").textContent = rows.length ? "" : "Sua caixa de entrada está vazia.";
    $("#notifications-list").replaceChildren(...rows.map(message => {
      const item = node("li",undefined,message.read ? undefined : "unread");
      const time = node("p",happened(message.created_at),"small");
      item.append(node("h3",message.title),node("p",message.body),time);
      if (!message.read) item.setAttribute("aria-label","Notificação não lida");
      return item;
    }));
  }

  function drawSettings() {
    const pause = $("#pause-generations");
    pause.disabled = false;
    pause.setAttribute("aria-pressed",String(state.paused));
    pause.textContent = state.paused ? "Retomar gerações" : "Pausar todas as gerações";
    $("#pause-state").textContent = state.paused
      ? "As novas gerações estão pausadas."
      : "As gerações estão disponíveis.";
    onPause(Boolean(state.paused));
    const entries = state.ledger || [];
    $("#ledger-empty").textContent = entries.length ? "" : "As novas gerações e edições serão registradas aqui.";
    $("#ledger-list").replaceChildren(...entries.map(entry => {
      const item = node("li");
      const title = activityLabels[entry.kind] || "Atividade";
      item.append(node("strong",title));
      if (typeof entry.detail === "string") item.append(node("p",entry.detail));
      item.append(node("p",happened(entry.created_at),"small"));
      return item;
    }));
  }

  function drawPush() {
    const note = $("#push-state");
    const activate = $("#push-subscribe");
    const available = state.push?.available && state.push?.public_key;
    activate.hidden = !available || !supportPush() || Notification.permission === "denied";
    if (!available) {
      note.textContent = "O envio para aparelhos ainda não foi configurado no servidor.";
    } else if (!supportPush()) {
      note.textContent = "Abra em um navegador compatível. No iPhone, instale na tela de início para ativar.";
    } else if (Notification.permission === "denied") {
      note.textContent = "As notificações foram bloqueadas. Altere a permissão no navegador.";
    } else if (!activate.hidden && !subscribeBusy) {
      note.textContent = "Ative as notificações para receber avisos neste aparelho.";
    }
  }

  function paint(data) {
    state = data;
    drawUnread(data.unread);
    drawNotifications();
    drawSettings();
    drawPush();
  }

  function refresh() {
    clearTimeout(poll);
    if (!enabled) return Promise.resolve();
    if (pending) return pending;
    const attempt = version;
    const request = api("/owner").then(data => {
      if (enabled && attempt === version && (!state || JSON.stringify(state) !== JSON.stringify(data))) paint(data);
    }).catch(error => {
      if (!enabled || attempt !== version) return;
      $("#notifications-empty").textContent = error.message;
      $("#pause-state").textContent = error.message;
    }).finally(() => {
      if (pending === request) {
        pending = undefined;
        if (enabled) poll = setTimeout(refresh,20000);
      }
    });
    pending = request;
    return request;
  }

  function session(available) {
    enabled = available;
    version++;
    pending = undefined;
    clearTimeout(poll);
    if (enabled) {
      refresh();
    } else {
      state = undefined;
      drawUnread(0);
      onPause(false);
      $("#notifications-list").replaceChildren();
      $("#ledger-list").replaceChildren();
    }
  }

  function route(view) {
    if (enabled && ["notificacoes","configuracoes"].includes(view)) refresh();
  }

  async function mutated() {
    await pending;
    await refresh();
  }

  $("#notifications-read").addEventListener("click",async () => {
    const button = $("#notifications-read");
    button.disabled = true;
    try {
      await api("/owner/notifications/read",{method:"POST"});
      await mutated();
    } catch (error) {
      $("#notifications-empty").textContent = error.message;
      button.disabled = false;
    }
  });

  $("#pause-generations").addEventListener("click",async () => {
    if (!state) return;
    const button = $("#pause-generations");
    button.disabled = true;
    try {
      await api("/owner",{
        method:"PATCH",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({paused:!state.paused})
      });
      await mutated();
    } catch (error) {
      $("#pause-state").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  $("#owner-message").addEventListener("submit",async event => {
    event.preventDefault();
    const button = $("#notification-send");
    if (button.disabled) return;
    const response = $("#notification-response");
    const title = $("#notification-title").value.trim();
    const body = $("#notification-body").value.trim();
    if (!title || !body) {
      response.textContent = "Preencha o título e o texto.";
      return;
    }
    button.disabled = true;
    response.textContent = "";
    try {
      const result = await api("/owner/notifications",{
        method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title,body})
      });
      event.target.reset();
      response.textContent = typeof result?.sent === "number"
        ? `Aviso registrado. Aparelhos notificados: ${result.sent}.`
        : "O aviso foi registrado na caixa de entrada.";
      await mutated();
    } catch (error) {
      response.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  $("#push-subscribe").addEventListener("click",async () => {
    if (!state?.push?.available || !supportPush() || subscribeBusy) return;
    const button = $("#push-subscribe");
    const note = $("#push-state");
    subscribeBusy = true;
    button.disabled = true;
    try {
      if (await Notification.requestPermission() !== "granted") {
        drawPush();
        return;
      }
      const registration = await navigator.serviceWorker.getRegistration("/studio");
      if (!registration?.active) throw new Error("Reabra o estúdio e tente ativar as notificações novamente.");
      const subscription = await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({
        userVisibleOnly:true,applicationServerKey:keyBytes(state.push.public_key)
      });
      const {endpoint, keys} = subscription.toJSON();
      await api("/owner/push",{
        method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({endpoint,keys})
      });
      note.textContent = "Notificações ativadas neste aparelho.";
      button.hidden = true;
    } catch (error) {
      note.textContent = error.message || "Não foi possível ativar as notificações.";
    } finally {
      subscribeBusy = false;
      button.disabled = false;
    }
  });

  return {session,route,refresh};
}
