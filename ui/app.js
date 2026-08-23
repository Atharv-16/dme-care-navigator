const state = {
  data: null,
  queue: [],
  index: 0,
  playing: false,
  utterance: null,
  speed: 1,
  gen: 0,
};

const $ = (id) => document.getElementById(id);
const player = $("player");

function flatten(data) {
  const queue = [];
  for (const scene of data.scenes) {
    scene.turns.forEach((turn, i) => {
      queue.push({ scene, turn, i });
    });
  }
  return queue;
}

function cardHtml(s, big) {
  const el = document.createElement("article");
  el.className = "party supplier-card" + (s.contacted ? "" : " idle") + (big ? " called-card" : "");
  el.dataset.party = `supplier:${s.id}`;
  el.innerHTML = `
      <span class="role">${s.contacted ? (s.outcome || "contacted") : "not called yet"}</span>
      <h2>${s.name}</h2>
      <p>${s.phone}</p>`;
  return el;
}

function renderSuppliers(data) {
  const called = $("called");
  const rest = $("suppliers");
  called.innerHTML = "";
  rest.innerHTML = "";
  (data.called || data.suppliers.filter((s) => s.contacted)).forEach((s) => {
    called.appendChild(cardHtml(s, true));
  });
  (data.suppliers || []).filter((s) => !s.contacted).forEach((s) => {
    rest.appendChild(cardHtml(s, false));
  });
}

function setSpeaking(party, onCallId) {
  document.querySelectorAll(".party").forEach((el) => {
    const id = el.dataset.party;
    el.classList.toggle("speaking", id === party);
    el.classList.toggle("on-call", Boolean(onCallId) && id === `supplier:${onCallId}`);
  });
}

function renderLog(active) {
  const log = $("log");
  log.innerHTML = "";
  state.queue.forEach((item, idx) => {
    const li = document.createElement("li");
    if (idx === active) li.className = "active";
    li.textContent = `${item.turn.label}: ${item.turn.text.slice(0, 140)}`;
    log.appendChild(li);
  });
  const current = log.querySelector(".active");
  if (current) current.scrollIntoView({ block: "nearest" });
}

function show(item) {
  const { scene, turn } = item;
  $("scene").textContent = scene.title + (scene.summary ? ` · ${scene.summary}` : "");
  $("caption").textContent = `${turn.label}: ${turn.text}`;
  setSpeaking(turn.party, turn.on_call || scene.supplier_id);
  renderLog(state.index);
}

function clearPlayerHandlers() {
  player.onended = null;
  player.onerror = null;
  player.onplaying = null;
  player.oncanplay = null;
  player.onloadeddata = null;
}

function unloadPlayer() {
  // Drop the current clip without load()-on-empty, which fires a delayed
  // error that can abort the next line (Prairie was long enough to show it).
  clearPlayerHandlers();
  player.pause();
  try {
    player.playbackRate = 1;
  } catch (_) {
    /* ignore */
  }
  player.removeAttribute("src");
}

function stopAudio() {
  state.gen += 1;
  unloadPlayer();
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  state.utterance = null;
}

function speakBrowser(text, gen) {
  return new Promise((resolve) => {
    if (!window.speechSynthesis || gen !== state.gen) return resolve();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = Math.min(2, 0.95 * state.speed + 0.1);
    state.utterance = u;
    u.onend = () => resolve();
    u.onerror = () => resolve();
    speechSynthesis.speak(u);
  });
}

function rateFor(party) {
  let rate = state.speed;
  const cap = party === "navigator" || party === "clinic" || party === "eleanor" || party === "medicare";
  if (cap && rate > 1) {
    rate = rate === 2 ? 1.35 : 1.2;
  }
  return rate;
}

function applyRate(party) {
  // Keep pitch for suppliers/clinic. Navigator GuyNeural + Chrome 2x stretch
  // turns to mush, so that voice stays slower.
  player.preservesPitch = true;
  if ("mozPreservesPitch" in player) player.mozPreservesPitch = true;
  if ("webkitPreservesPitch" in player) player.webkitPreservesPitch = true;
  try {
    player.playbackRate = rateFor(party);
  } catch (_) {
    /* ignore */
  }
}

function playFile(url, gen, party) {
  return new Promise((resolve) => {
    if (gen !== state.gen) return resolve();

    let settled = false;
    let started = false;
    let launching = false;

    const done = () => {
      if (settled) return;
      settled = true;
      clearPlayerHandlers();
      resolve();
    };

    unloadPlayer();
    if (gen !== state.gen) return done();

    player.onended = () => {
      if (gen !== state.gen) return done();
      if (!started) return;
      const dur = player.duration;
      if (Number.isFinite(dur) && dur > 0 && player.currentTime < dur - 0.4) {
        player.play().catch(() => done());
        return;
      }
      done();
    };
    player.onerror = () => done();

    const tryStart = () => {
      if (settled || launching || gen !== state.gen) return;
      launching = true;
      applyRate(party);
      player
        .play()
        .then(() => {
          started = true;
          applyRate(party);
        })
        .catch(() => done());
    };
    player.oncanplay = tryStart;
    player.onloadeddata = tryStart;
    player.src = url;
  });
}

async function playCurrent() {
  const gen = state.gen;
  const item = state.queue[state.index];
  if (!item || !state.playing || gen !== state.gen) return;
  show(item);
  $("pause").disabled = false;
  $("play").disabled = true;
  if (item.turn.audio) await playFile(item.turn.audio, gen, item.turn.party);
  else await speakBrowser(item.turn.text, gen);
  if (!state.playing || gen !== state.gen) return;
  if (state.index < state.queue.length - 1) {
    state.index += 1;
    await playCurrent();
  } else {
    state.playing = false;
    setSpeaking(null, null);
    $("play").disabled = false;
    $("pause").disabled = true;
    $("caption").textContent = "Recording finished.";
  }
}

function setSpeed(speed) {
  state.speed = speed;
  const item = state.queue[state.index];
  applyRate(item ? item.turn.party : null);
  document.querySelectorAll(".speed-btn").forEach((btn) => {
    btn.classList.toggle("active", Number(btn.dataset.speed) === speed);
  });
}

$("play").onclick = () => {
  if (!state.queue.length) return;
  stopAudio();
  state.playing = true;
  playCurrent();
};

$("pause").onclick = () => {
  state.playing = false;
  stopAudio();
  $("play").disabled = false;
  $("pause").disabled = true;
};

$("next").onclick = () => {
  const wasPlaying = state.playing;
  stopAudio();
  if (state.index < state.queue.length - 1) state.index += 1;
  show(state.queue[state.index]);
  if (wasPlaying) {
    state.playing = true;
    playCurrent();
  }
};

document.querySelectorAll(".speed-btn").forEach((btn) => {
  btn.onclick = () => setSpeed(Number(btn.dataset.speed));
});

fetch("/api/timeline")
  .then((r) => r.json())
  .then((data) => {
    state.data = data;
    state.queue = flatten(data);
    renderSuppliers(data);
    const s = data.stats || {};
    const rec = data.recording || {};
    $("stats").textContent = `${s.with_audio || 0}/${s.turns || 0} recorded mp3s`;
    if (rec.dialogue) {
      $("rec-note").textContent =
        `Yes: real Ollama talk (${rec.dialogue.split("(")[0].trim()}) spoken with edge-tts. Play uses those mp3s.`;
    }
    if (state.queue[0]) show(state.queue[0]);
  })
  .catch(() => {
    $("caption").textContent = "Could not load recording. Run python -m src --llm --voice first.";
  });
