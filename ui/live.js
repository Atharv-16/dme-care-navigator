const statusEl = document.getElementById("status");
const titleEl = document.getElementById("title");
const hintEl = document.getElementById("hint");
const logEl = document.getElementById("log");
const answerBtn = document.getElementById("answer");
const interruptBtn = document.getElementById("interrupt");
const hangupBtn = document.getElementById("hangup");
const restartBtn = document.getElementById("restart");
const typed = document.getElementById("typed");
const typebox = document.getElementById("typebox");
const meter = document.getElementById("meter");
const meterFill = document.getElementById("meterFill");
const meterReadout = document.getElementById("meterReadout");
const turnState = document.getElementById("turnState");
const micPanel = document.querySelector(".mic-panel");
const activePartyName = document.getElementById("activePartyName");
const partyNodes = [...document.querySelectorAll("[data-party-node]")];
const analysisPanel = document.getElementById("analysisPanel");
const analysisTags = document.getElementById("analysisTags");

const WS_URL = `ws://${location.hostname}:8767`;
const INPUT_RATE = 16000;
const OUTPUT_RATE = 24000;

let ws = null;
let reconnectTimer = null;
let helloTimer = null;
let ringing = false;
let inCall = false;
let myRole = "clinic";
let lastLogged = "";

let micStream = null;
let audioCtx = null;
let micSource = null;
let captureNode = null;
let silentGain = null;
let micAnalyser = null;
let meterData = null;
let meterFrame = null;
let meterStartedAt = 0;
let sentResetTimer = null;
let suppressModelAudio = false;
let nextPlayTime = 0;
const activeSources = new Set();
const analysisEvents = new Set();

function setStatus(text) {
  statusEl.textContent = text;
}

function resetAnalysis() {
  analysisEvents.clear();
  analysisTags.replaceChildren();
  analysisPanel.hidden = true;
}

function showAnalysisWaiting() {
  resetAnalysis();
  analysisPanel.hidden = false;
  addAnalysisTag("waiting", "Waiting");
}

function addAnalysisTag(kind, text) {
  const key = `${kind}:${text}`;
  if (analysisEvents.has(key)) return;
  analysisEvents.add(key);
  if (kind !== "waiting") {
    analysisTags
      .querySelectorAll('[data-analysis-kind="waiting"]')
      .forEach((node) => node.remove());
  }
  const tag = document.createElement("span");
  tag.className = `analysis-tag ${kind}`;
  tag.dataset.analysisKind = kind;
  tag.textContent = text;
  analysisTags.append(tag);
}

function removeAnalysisTags(...kinds) {
  for (const kind of kinds) {
    analysisTags
      .querySelectorAll(`[data-analysis-kind="${kind}"]`)
      .forEach((node) => node.remove());
  }
}

function partyCategory(role) {
  if ((role || "").startsWith("supplier:")) return "supplier";
  if (role === "patient") return "eleanor";
  return role || "navigator";
}

function setActiveParty(role, title = "") {
  const category = partyCategory(role);
  partyNodes.forEach((node) => {
    node.classList.toggle("active", node.dataset.partyNode === category);
  });
  if (category === "navigator") {
    activePartyName.textContent = "Navigator is reviewing the call and deciding who is next";
  } else {
    activePartyName.textContent = `You are impersonating: ${title || label(role)}`;
  }
}

function label(speaker) {
  if (speaker === myRole || speaker === "you") return `You (${myRole})`;
  if (speaker === "eleanor") return "You (Eleanor)";
  if (speaker === "navigator") return "Navigator";
  return speaker;
}

function addLog(speaker, text) {
  const clean = (text || "").trim();
  if (!clean) return;
  const key = `${speaker}:${clean}`;
  if (key === lastLogged) return;
  lastLogged = key;
  const li = document.createElement("li");
  const strong = document.createElement("strong");
  strong.textContent = label(speaker);
  li.append(strong, ` ${clean}`);
  logEl.prepend(li);
}

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
  }
}

function connect() {
  clearTimeout(reconnectTimer);
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    setStatus("Connected. Waiting for the navigator to dial.");
    send({ type: "hello" });
    clearInterval(helloTimer);
    helloTimer = setInterval(() => {
      if (!inCall) send({ type: "hello" });
    }, 1000);
  };
  ws.onclose = () => {
    clearInterval(helloTimer);
    ringing = false;
    inCall = false;
    stopAudio();
    releaseMicrophone();
    setStatus("Disconnected. Reconnecting...");
    reconnectTimer = setTimeout(connect, 800);
  };
  ws.onerror = () => {
    setStatus("Cannot reach the live voice server.");
  };
  ws.onmessage = (event) => {
    onMessage(JSON.parse(event.data));
  };
}

async function onMessage(message) {
  switch (message.type) {
    case "incoming":
      resetAnalysis();
      ringing = true;
      myRole = message.role || myRole;
      setActiveParty(myRole, message.title);
      titleEl.textContent = `Incoming: ${message.title}`;
      hintEl.textContent =
        message.hint ||
        "The navigator is calling. Answer, then speak naturally.";
      setStatus("Ringing. Click Answer and allow the microphone.");
      answerBtn.disabled = false;
      hangupBtn.disabled = false;
      break;

    case "idle":
      if (ringing || inCall) return;
      setActiveParty("navigator");
      titleEl.textContent = "Waiting for a call";
      hintEl.textContent =
        message.hint || "Keep this tab open. The navigator will ring here.";
      setStatus("Connected. Waiting for the navigator to dial.");
      answerBtn.disabled = true;
      hangupBtn.disabled = true;
      break;

    case "in_call":
      resetAnalysis();
      ringing = false;
      inCall = true;
      myRole = message.role || myRole;
      setActiveParty(myRole, message.title);
      titleEl.textContent = message.title || "Live call";
      hintEl.textContent =
        "Talk naturally. Gemini handles pauses and interruptions automatically.";
      answerBtn.disabled = true;
      hangupBtn.disabled = false;
      interruptBtn.hidden = true;
      setStatus(`Live on ${message.model || "Gemini native audio"}. Start speaking.`);
      await startMicrophone();
      setTurnState("idle", "Listening");
      break;

    case "audio":
      if (suppressModelAudio) break;
      playPcm(message.b64);
      setStatus("Navigator is speaking. Talk over it to interrupt.");
      break;

    case "interrupted":
      stopAudio();
      setStatus("Navigator stopped. Keep speaking.");
      break;

    case "vad_state":
      if (message.state === "speaking") {
        suppressModelAudio = true;
        stopAudio();
        clearTimeout(sentResetTimer);
        setTurnState("speaking", "Speaking");
        setStatus("WebRTC VAD detected your voice.");
      } else if (message.state === "ended") {
        setTurnState("idle", "Sending");
        setStatus("WebRTC VAD detected the end of your speech.");
      }
      break;

    case "activity_ack":
      if (message.activity === "end") {
        suppressModelAudio = false;
        showTurnSent();
        setStatus("Your speech ended and was sent to Gemini.");
      }
      break;

    case "partial_transcript":
      if (message.text) {
        setStatus(`${label(message.speaker)}: ${message.text}`);
      }
      break;

    case "transcript":
      addLog(message.speaker, message.text);
      break;

    case "turn_complete":
      setStatus("Listening.");
      break;

    case "call_error":
      setStatus(`Call error: ${message.error}`);
      break;

    case "analysis_status": {
      analysisPanel.hidden = false;
      setActiveParty("navigator");
      titleEl.textContent = "Navigator analyzing the call";
      hintEl.textContent =
        "The transcript is being converted into verified memory and the next-call plan.";
      if (message.state === "sent") {
        removeAnalysisTags("waiting", "delay");
        addAnalysisTag("sent", "Sent");
        setStatus(`Analysis sent to ${message.model || "the LLM"}.`);
      } else if (message.state === "running") {
        removeAnalysisTags("waiting", "delay");
        addAnalysisTag("running", "Running");
        setStatus("Navigator analysis is running.");
      } else if (message.state === "retry") {
        const attempt = message.attempt || "?";
        const maximum = message.max_attempts || 8;
        const delay = message.delay_seconds || 10;
        removeAnalysisTags("running", "delay");
        addAnalysisTag("retry", `Retry ${attempt}/${maximum}`);
        addAnalysisTag("delay", `Delay ${delay}s`);
        setStatus(
          `Temporary analysis error (${message.error || "unknown"}). Retrying in ${delay}s.`,
        );
      } else if (message.state === "fallback") {
        removeAnalysisTags("running", "delay");
        addAnalysisTag("fallback", "Fallback model");
        setStatus(
          `${message.from_model || "Primary model"} failed with ${message.error || "an error"}. Switching to ${message.to_model || "the fallback model"}.`,
        );
      } else if (message.state === "received") {
        removeAnalysisTags("running", "delay");
        addAnalysisTag("received", "Received");
        setStatus("Analysis response received. Validating memory and the next call.");
      } else if (message.state === "failed") {
        removeAnalysisTags("running", "delay");
        addAnalysisTag("failed", "Failed");
        setStatus(`Analysis failed: ${message.error || "unknown error"}.`);
      }
      break;
    }

    case "ended":
      ringing = false;
      inCall = false;
      stopAudio();
      releaseMicrophone();
      setActiveParty("navigator");
      showAnalysisWaiting();
      titleEl.textContent = "Navigator deciding the next call";
      hintEl.textContent =
        "The call ended. The coordinator is updating memory and choosing who to call next.";
      setStatus(`Call ended (${message.reason}). Waiting for the next decision.`);
      answerBtn.disabled = true;
      hangupBtn.disabled = true;
      break;
  }
}

async function ensureAudioContext() {
  if (!audioCtx || audioCtx.state === "closed") {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      latencyHint: "interactive",
    });
  }
  if (audioCtx.state === "suspended") await audioCtx.resume();
}

async function startMicrophone() {
  if (micStream) return;
  await ensureAudioContext();
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });

  micSource = audioCtx.createMediaStreamSource(micStream);
  captureNode = audioCtx.createScriptProcessor(2048, 1, 1);
  silentGain = audioCtx.createGain();
  silentGain.gain.value = 0;
  micAnalyser = audioCtx.createAnalyser();
  micAnalyser.fftSize = 1024;
  micAnalyser.smoothingTimeConstant = 0.25;
  meterData = new Float32Array(micAnalyser.fftSize);
  meterStartedAt = performance.now();

  captureNode.onaudioprocess = (event) => {
    if (!inCall) return;
    const input = event.inputBuffer.getChannelData(0);
    const pcm = downsampleToPcm16(input, audioCtx.sampleRate, INPUT_RATE);
    if (!pcm.byteLength) return;
    sendPcm(pcm);
  };

  micSource.connect(captureNode);
  micSource.connect(micAnalyser);
  captureNode.connect(silentGain);
  silentGain.connect(audioCtx.destination);
  micPanel.classList.add("capturing");
  updateMeter();
}

function releaseMicrophone() {
  clearTimeout(sentResetTimer);
  suppressModelAudio = false;
  if (meterFrame) {
    cancelAnimationFrame(meterFrame);
    meterFrame = null;
  }
  if (micAnalyser) {
    micAnalyser.disconnect();
    micAnalyser = null;
  }
  meterData = null;
  if (captureNode) {
    captureNode.onaudioprocess = null;
    captureNode.disconnect();
    captureNode = null;
  }
  if (micSource) {
    micSource.disconnect();
    micSource = null;
  }
  if (silentGain) {
    silentGain.disconnect();
    silentGain = null;
  }
  if (micStream) {
    micStream.getTracks().forEach((track) => track.stop());
    micStream = null;
  }
  micPanel.classList.remove("capturing", "no-signal");
  meterFill.style.width = "0%";
  meter.setAttribute("aria-valuenow", "0");
  meterReadout.textContent = "Waiting for microphone";
  setTurnState("idle", "Waiting");
}

function sendPcm(pcm) {
  send({
    type: "audio",
    mime: `audio/pcm;rate=${INPUT_RATE}`,
    b64: bytesToBase64(new Uint8Array(pcm.buffer)),
  });
}

function setTurnState(state, text) {
  turnState.className = `turn-state ${state}`;
  turnState.textContent = text;
}

function showTurnSent() {
  clearTimeout(sentResetTimer);
  setTurnState("sent", "Sent");
  sentResetTimer = setTimeout(() => {
    if (inCall) setTurnState("idle", "Listening");
  }, 900);
}

function updateMeter() {
  if (!micAnalyser || !meterData || !micStream) return;
  micAnalyser.getFloatTimeDomainData(meterData);
  let sumSquares = 0;
  let peak = 0;
  for (const sample of meterData) {
    sumSquares += sample * sample;
    peak = Math.max(peak, Math.abs(sample));
  }
  const rms = Math.sqrt(sumSquares / meterData.length);
  const db = rms > 0 ? 20 * Math.log10(rms) : -100;
  const level = Math.max(0, Math.min(100, ((db + 72) / 72) * 100));
  meterFill.style.width = `${level}%`;
  meter.setAttribute("aria-valuenow", String(Math.round(level)));

  const elapsed = performance.now() - meterStartedAt;
  if (rms >= 0.006 || peak >= 0.025) {
    meterReadout.textContent = `Hearing you · ${Math.round(db)} dB`;
    micPanel.classList.remove("no-signal");
  } else if (rms >= 0.0015 || peak >= 0.008) {
    meterReadout.textContent = `Very quiet · ${Math.round(db)} dB`;
    micPanel.classList.remove("no-signal");
  } else if (elapsed > 2500) {
    meterReadout.textContent = "No clear voice detected";
    micPanel.classList.add("no-signal");
  } else {
    meterReadout.textContent = "Listening...";
  }
  meterFrame = requestAnimationFrame(updateMeter);
}

function downsampleToPcm16(input, sourceRate, targetRate) {
  if (targetRate > sourceRate) {
    throw new Error("Input sample rate is below 16 kHz.");
  }
  const ratio = sourceRate / targetRate;
  const outputLength = Math.max(1, Math.floor(input.length / ratio));
  const output = new Int16Array(outputLength);
  let sourceOffset = 0;

  for (let i = 0; i < outputLength; i++) {
    const nextOffset = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    let count = 0;
    for (; sourceOffset < nextOffset; sourceOffset++) {
      sum += input[sourceOffset];
      count++;
    }
    const sample = Math.max(-1, Math.min(1, count ? sum / count : 0));
    output[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return output;
}

async function playPcm(encoded) {
  if (!encoded) return;
  await ensureAudioContext();
  const bytes = base64ToBytes(encoded);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const sampleCount = Math.floor(bytes.byteLength / 2);
  const buffer = audioCtx.createBuffer(1, sampleCount, OUTPUT_RATE);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < sampleCount; i++) {
    channel[i] = view.getInt16(i * 2, true) / 32768;
  }

  const source = audioCtx.createBufferSource();
  source.buffer = buffer;
  source.connect(audioCtx.destination);
  activeSources.add(source);
  source.onended = () => activeSources.delete(source);

  const now = audioCtx.currentTime;
  if (nextPlayTime < now + 0.02) nextPlayTime = now + 0.02;
  source.start(nextPlayTime);
  nextPlayTime += buffer.duration;
}

function stopAudio() {
  for (const source of activeSources) {
    try {
      source.stop();
    } catch {
      // Source may have already ended.
    }
  }
  activeSources.clear();
  nextPlayTime = audioCtx ? audioCtx.currentTime : 0;
}

function bytesToBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function base64ToBytes(encoded) {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

answerBtn.onclick = async () => {
  try {
    await startMicrophone();
    send({ type: "answer" });
  } catch (error) {
    setStatus(`Microphone error: ${error.message}`);
  }
};

hangupBtn.onclick = () => {
  send({ type: "hangup" });
  inCall = false;
  stopAudio();
  releaseMicrophone();
  setActiveParty("navigator");
  titleEl.textContent = "Navigator deciding the next call";
  hintEl.textContent =
    "Updating coordinator memory. This page will ring again for the next party.";
  setStatus("Call ended. Waiting for the navigator's next decision.");
};

restartBtn.onclick = async () => {
  if (
    inCall &&
    !window.confirm("Restart now? The active call and current simulation run will end.")
  ) {
    return;
  }

  restartBtn.disabled = true;
  restartBtn.textContent = "Restarting…";
  setStatus("Restarting the navigator. This page will reconnect automatically.");
  stopAudio();
  releaseMicrophone();

  try {
    const response = await fetch("/api/restart", { method: "POST" });
    if (!response.ok) throw new Error(`server returned ${response.status}`);
  } catch (error) {
    setStatus(`Restart failed: ${error.message}`);
    restartBtn.disabled = false;
    restartBtn.textContent = "Restart navigator";
    return;
  }

  window.setTimeout(() => {
    restartBtn.disabled = false;
    restartBtn.textContent = "Restart navigator";
    if (!ws || ws.readyState === WebSocket.CLOSED) connect();
  }, 2500);
};

typebox.onsubmit = (event) => {
  event.preventDefault();
  const text = typed.value.trim();
  if (!text) return;
  typed.value = "";
  send({ type: "user_text", text });
};

interruptBtn.hidden = true;
setActiveParty("navigator");
connect();
