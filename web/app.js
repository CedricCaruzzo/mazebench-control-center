const state = { runs: [], jobs: [], selected: null, detail: null, interactions: [], compactions: [], interactionsError: "", selectionSerial: 0, frame: 0, timer: null, asciiZoom: 1, capabilities: {}, worldRooms: [] };
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const number = (value) => Number(value || 0).toLocaleString();
const compact = (value) => Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(Number(value || 0));
const duration = (seconds) => { const n=Number(seconds); if(!Number.isFinite(n)) return "—"; if(n<60)return `${n.toFixed(1)}s`; return `${Math.floor(n/60)}m ${Math.round(n%60)}s`; };
const dateText = (value) => value ? new Date(value).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "Unknown time";
const shortRoom = (room) => String(room || "—").replace(/^level_/, "");
const latestJob = (kind, runId, jobs = state.jobs) => [...jobs].reverse().find(job => job.kind === kind && job.run_id === runId);
const jobStamp = (job) => job ? `${job.id}:${job.status}:${job.ended_at || ""}` : "";

async function api(path, options = {}) {
  const response = await fetch(path, { headers: {"Content-Type":"application/json"}, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function toast(message, error = false) {
  const el = $("#toast"); el.textContent = message; el.className = `toast is-visible${error ? " is-error" : ""}`;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.className = "toast", 3300);
}

async function refresh({quiet=false} = {}) {
  try {
    const previousReplay = latestJob("replay", state.selected);
    const [{runs, jobs}, health] = await Promise.all([api("/api/runs"), api("/api/health")]);
    state.runs = runs; state.jobs = jobs; state.capabilities = health.capabilities || {}; state.worldRooms = health.world_rooms || [];
    renderTrialOptions();
    const running = new Map(jobs.filter(job => ["queued","running","stopping"].includes(job.status)).map(job => [job.run_id, job]));
    state.runs.forEach(run => { if (running.has(run.id) && running.get(run.id).kind === "trial") run.status = running.get(run.id).status; });
    renderFleet(); renderRunList(); renderCapability();
    if (!state.selected && state.runs.length) await selectRun(state.runs[0].id, true);
    if (state.selected) {
      const selectedRun = state.runs.find(run => run.id === state.selected);
      const active = jobs.some(job => job.run_id === state.selected && ["queued","running","stopping"].includes(job.status));
      const replayChanged = jobStamp(previousReplay) !== jobStamp(latestJob("replay", state.selected, jobs));
      if (!document.fullscreenElement && (active || replayChanged || selectedRun?.status === "completed" && state.detail?.status !== "completed")) await selectRun(state.selected, true);
    }
    if (!quiet) toast("Run archive refreshed");
  } catch (error) { toast(error.message, true); }
}

function renderCapability() {
  const version=state.capabilities.mazebench_version||"?";
  const audited=state.capabilities.benchmark_contract_status==="passed";
  $("#replay-capability").textContent = state.capabilities.official_profile_ready&&audited ? `Official v${version} · contract audited` : `MazeBench contract mismatch (${version})`;
}

function renderTrialOptions() {
  const modelSelect=$("#model-profile"),observationSelect=$("#observation-mode"),contextSelect=$("#context-mode"),forkSelect=$("#fork-parent");
  if(modelSelect&&state.capabilities.model_profiles?.length){const selected=modelSelect.value;modelSelect.innerHTML=state.capabilities.model_profiles.map(profile=>`<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.label)}</option>`).join("");if([...modelSelect.options].some(option=>option.value===selected))modelSelect.value=selected}
  if(observationSelect&&state.capabilities.observation_modes?.length){const selected=observationSelect.value;observationSelect.innerHTML=state.capabilities.observation_modes.map(mode=>`<option value="${escapeHtml(mode.id)}">${escapeHtml(mode.label)}</option>`).join("");if([...observationSelect.options].some(option=>option.value===selected))observationSelect.value=selected}
  if(contextSelect&&state.capabilities.context_modes?.length){const selected=contextSelect.value;contextSelect.innerHTML=state.capabilities.context_modes.map(mode=>`<option value="${escapeHtml(mode.id)}">${escapeHtml(mode.label)}</option>`).join("");if([...contextSelect.options].some(option=>option.value===selected))contextSelect.value=selected;updateContextControls()}
  if(forkSelect){const selected=forkSelect.value,eligible=state.runs.filter(run=>!["running","queued","stopping"].includes(run.status)&&run.artifacts?.actions&&run.artifacts?.interactions&&Number(run.metrics?.summary?.actions||0)>0);forkSelect.innerHTML=`<option value="">Clean environment · no parent</option>`+eligible.map(run=>`<option value="${escapeHtml(run.id)}">${escapeHtml(run.id)} · ${number(run.metrics.summary.actions)} turns</option>`).join("");if([...forkSelect.options].some(option=>option.value===selected))forkSelect.value=selected;updateForkControls()}
  updateObservationControls();updateModelControls();
}

function updateModelControls(){const profile=state.capabilities.model_profiles?.find(item=>item.id===$("#model-profile")?.value),thinking=$("#thinking-toggle"),budget=$("#thinking-budget"),preserve=$("#preserve-thinking-toggle"),help=$("#model-help"),supported=profile?.thinking_contract==="qwen";if(thinking){thinking.disabled=!supported;if(!supported)thinking.checked=false}if(budget)budget.disabled=!supported;if(preserve){preserve.disabled=!supported;if(!supported)preserve.checked=false}if(help)help.textContent=profile?.managed?`${profile.provider||"Local"} service managed automatically · ${profile.token_count_mode||"provider"} token counting.`:`Externally managed ${profile?.provider||"OpenAI-compatible"} endpoint · ${profile?.token_count_mode||"estimated"} token counting.${supported?"":" Thinking controls unavailable."}`}

function updateContextControls(){const id=$("#context-mode")?.value||"generic-autocompact",observation=$("#observation-mode")?.value||"ascii",mode=state.capabilities.context_modes?.find(item=>item.id===id),external=id==="none",help=$("#context-help"),title=$("#context-callout-title"),copy=$("#context-callout-copy");if(help)help.textContent=mode?.description||"Complete raw traces remain on disk.";if(title)title.textContent=external?"Audited official engine · endpoint-managed context":"Audited official engine · generic harness compaction";if(copy)copy.textContent=external?"The Control Center forwards the normal growing conversation without summarizing it. The selected endpoint or upstream harness must handle any compaction or truncation; interactions are still journaled losslessly.":observation==="json"?"MazeBench supplies its official structured JSON observation with literal object-type names, visible-object coordinates, and the native JSON-mode instruction. The generic compactor retains a recent verbatim tail; ASCII character settings do not apply.":"MazeBench supplies the official perspective ASCII observation and matching character-mode instruction. The generic compactor retains a recent verbatim tail. Randomized mode keeps P and G fixed and records its seed."}

function officialSystemPrompt(){const prompts=state.capabilities.system_prompts||{},representation=$("#observation-mode")?.value||"ascii",identity=representation==="json"?"visible":$("#ascii-character-mode")?.value==="random"?"hidden":"visible";return prompts[`${representation}_${identity}`]||null}
function supportsExperimentControls(){return Boolean(state.capabilities.system_prompts&&state.capabilities.observation_modes&&Number(state.capabilities.representation_contract_version)>=2)}
function updateSystemPromptControls({reset=false}={}){const toggle=$("#unofficial-system-prompt"),editor=$("#system-prompt"),mode=$("#system-prompt-mode"),help=$("#system-prompt-help"),launch=$("#launch-button");if(!toggle||!editor)return;if(reset)toggle.checked=false;const unofficial=toggle.checked,prompt=officialSystemPrompt(),supported=supportsExperimentControls(),representation=$("#observation-mode")?.value==="json"?"JSON · literal names":$("#ascii-character-mode")?.value==="random"?`ASCII · random · seed ${$("#ascii-seed")?.value||"1"}`:"ASCII · default characters";if(!unofficial)editor.value=prompt||"Restart the Control Center to load the exact audited game-agent system prompt.";editor.readOnly=!unofficial||!supported;toggle.disabled=!supported;if(launch)launch.disabled=!supported;if(mode)mode.textContent=!supported?"restart required":`${unofficial?"unofficial · editable":"official · read-only"} · ${representation}`;if(help)help.textContent=!supported?"The page updated while an older Control Center backend is still running. Finish or stop its active trial, restart scripts/control-center.sh, then refresh.":unofficial?`Experimental ${representation} intervention: this exact edited text will be sent and the run will be labeled unofficial.`:`This is the exact ${representation} game-agent system-role text that will be sent and saved with the run.`}
function updateObservationControls(){const id=$("#observation-mode")?.value||"ascii",mode=state.capabilities.observation_modes?.find(item=>item.id===id),help=$("#observation-help"),characterField=$("#ascii-character-field"),characterMode=$("#ascii-character-mode"),seedField=$("#ascii-seed-field"),seed=$("#ascii-seed"),parent=state.runs.find(run=>run.id===$("#fork-parent")?.value),ascii=id==="ascii",random=characterMode?.value==="random";if(help)help.textContent=id==="json"?"Official structured JSON with literal object names and [x,y,elevation] coordinates. ASCII character settings are not used.":mode?.description||"Official perspective ASCII track.";if(characterField)characterField.hidden=!ascii;if(characterMode)characterMode.disabled=!ascii||Boolean(parent);if(seedField)seedField.hidden=!ascii||!random;if(seed)seed.disabled=!ascii||!random||Boolean(parent);updateSystemPromptControls();updateContextControls()}

function updateForkControls(){const parentId=$("#fork-parent")?.value||"",turn=$("#fork-turn"),field=$("#fork-turn-field"),level=$("[name='level']"),characterMode=$("#ascii-character-mode"),seed=$("#ascii-seed"),help=$("#fork-help"),parent=state.runs.find(run=>run.id===parentId),max=Number(parent?.metrics?.summary?.actions||0);if(turn){turn.disabled=!parent;turn.max=String(Math.max(1,max));if(parent&&(!Number(turn.value)||Number(turn.value)>max))turn.value=String(max);if(!parent)turn.value=""}if(field)field.classList.toggle("is-active",Boolean(parent));if(level){level.disabled=Boolean(parent);if(parent)level.value=parent.config?.level||""}if(parent&&characterMode)characterMode.value=parent.config?.ascii_character_mode||(parent.config?.hide_names?"random":"canonical");if(parent&&seed)seed.value=parent.config?.hide_names_seed||"1";if(help)help.textContent=parent?`Active-context branch from ${parent.id}. The exact latest compacted context and subsequent messages through the checkpoint are restored; model, reasoning, system prompt, and future compaction settings may change.`:"Optionally branch from a completed run. The game is replayed and verified before the model continues from the parent’s active context.";updateObservationControls()}

function renderFleet() {
  const complete = state.runs.filter(run => run.status === "completed");
  const primary=complete.filter(run=>run.lineage!=="fork"),forks=complete.length-primary.length;
  const traced = primary.filter(run => run.metrics.summary.actions > 0);
  const sums = primary.reduce((acc, run) => { const m=run.metrics.summary; acc.actions+=m.actions;acc.rooms=Math.max(acc.rooms,m.rooms_visited);acc.gems+=m.gems;acc.tokens+=m.total_tokens; return acc; }, {actions:0,rooms:0,gems:0,tokens:0});
  const novelty = traced.length ? traced.reduce((n,r)=>n+r.metrics.summary.novelty_rate,0)/traced.length : 0;
  $("#fleet-strip").innerHTML = [
    ["Completed trials", primary.length, `${forks} derived forks · ${state.runs.length} archived`], ["Actions recorded", compact(sums.actions), "independent trace events"],
    ["Furthest exploration", sums.rooms, "rooms in one run"], ["Gems collected", sums.gems, "across corpus"],
    ["Mean novelty", `${Math.round(novelty*100)}%`, `${compact(sums.tokens)} tokens`]
  ].map(([label,value,note]) => `<div class="fleet-stat"><small>${label}</small><strong>${value}</strong><em>${note}</em></div>`).join("");
}

function effectiveRunStatus(run) { return run.status || "incomplete"; }
function renderRunList() {
  const query = $("#run-search").value.toLowerCase();
  const runs = state.runs.filter(run => `${run.id} ${run.config?.model || ""}`.toLowerCase().includes(query));
  $("#run-count").textContent = runs.length;
  $("#run-list").innerHTML = runs.length ? runs.map(run => { const m=run.metrics.summary; const status=effectiveRunStatus(run); return `
    <button class="run-card${state.selected===run.id?" is-active":""}" data-run-id="${escapeHtml(run.id)}">
      <div class="run-card-head"><h3>${escapeHtml(run.id)}</h3><span class="badge ${escapeHtml(status)}">${escapeHtml(status)}</span></div>
      <div class="run-card-meta"><span>${escapeHtml(run.config?.model_label || run.config?.model || "local")}</span><span>${dateText(run.created_at)}</span></div>
      <div class="run-card-stats"><span><b>${number(m.actions)}</b><small>actions</small></span><span><b>${m.rooms_visited}</b><small>rooms</small></span><span><b>${Math.round(m.novelty_rate*100)}%</b><small>novelty</small></span></div>
    </button>`; }).join("") : `<div class="empty-state">No matching trials.</div>`;
  $$("[data-run-id]").forEach(button => button.onclick = () => selectRun(button.dataset.runId));
}

async function selectRun(runId, quiet=false) {
  const serial=++state.selectionSerial,oldChat=$("#reasoning-chat"),chatScroll=oldChat?{top:oldChat.scrollTop,pinned:oldChat.scrollHeight-oldChat.scrollTop-oldChat.clientHeight<36}:null;
  state.selected = runId; renderRunList();
  if (!quiet) $("#run-detail").innerHTML = `<div class="empty-detail"><p>Loading ${escapeHtml(runId)}…</p></div>`;
  try {
    const [detail,journal]=await Promise.all([
      api(`/api/runs/${encodeURIComponent(runId)}`),
      api(`/api/runs/${encodeURIComponent(runId)}/interactions`).catch(error=>({interactions:[],error:error.message}))
    ]);
    if(serial!==state.selectionSerial||runId!==state.selected)return;
    state.detail=detail;state.interactions=journal.interactions||[];state.compactions=journal.compactions||[];state.interactionsError=journal.error||"";state.frame=Math.max(0,state.detail.metrics.timeline.length-1);renderDetail();
    requestAnimationFrame(()=>{const chat=$("#reasoning-chat");if(!chat)return;if(!quiet||!chatScroll||chatScroll.pinned)chat.scrollTop=chat.scrollHeight;else chat.scrollTop=chatScroll.top});
  }
  catch (error) { toast(error.message, true); }
}

function metricCard(label, value, note="") { return `<div class="metric-card"><small>${label}</small><strong>${value}</strong><span>${note}</span></div>`; }
function renderDetail() {
  const run=state.detail, m=run.metrics.summary, timeline=run.metrics.timeline, running=["running","queued","stopping"].includes(run.status),jsonMode=run.config?.observation_mode==="json";
  const trialJob=latestJob("trial",run.id),livePhase=trialJob?.phase||"running_trial",replayJob = latestJob("replay", run.id), replayActive=["running","queued"].includes(replayJob?.status);
  $("#run-detail").innerHTML = `<div class="detail-wrap" id="analytics">
    <div class="detail-hero"><div><p class="eyebrow">RUN INSPECTOR</p><div class="detail-title-row"><h2 class="detail-title">${escapeHtml(run.id)}</h2><span class="badge ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span><span class="lineage ${escapeHtml(run.lineage)}">${run.lineage==="fork"?`fork · ${escapeHtml(run.fork?.parent_run_id||"parent")} @ ${number(run.fork?.turn)}`:run.lineage==="official"?`official ${escapeHtml(run.benchmark?.version||"")}`:run.lineage==="experimental"?"unofficial prompt":"legacy"}</span></div><p class="detail-sub">${escapeHtml(run.config?.model_label || run.config?.model || "local")} · ${escapeHtml(run.config?.observation_mode || "ascii").toUpperCase()} · ${escapeHtml(run.config?.profile || run.config?.system_prompt || "legacy profile")} · ${escapeHtml(run.config?.context_management?.mode || run.config?.context_mode || "full history")} · ${jsonMode?"literal JSON names":run.config?.hide_names===true?`random ASCII characters · seed ${escapeHtml(run.config?.hide_names_seed||"1")}`:run.config?.hide_names===false?"default ASCII characters":"character mode unknown"} · thinking ${run.config?.thinking?`on (${number(run.config.thinking_budget || 0)} tokens, ${run.config.preserve_thinking===false?"history dropped":"history preserved"})`:"off"} · ${dateText(run.created_at)} · ${escapeHtml(run.stop_condition || "no stop condition")}</p></div>
      <div class="detail-actions">${running ? `<button class="button button-danger" id="stop-run">Stop</button>` : `<button class="button button-danger" id="delete-run">Delete</button>`}<button class="button button-ghost" id="open-log">Run logs</button><button class="button button-ghost" id="open-interactions">Reasoning traces</button><button class="button button-primary" id="generate-replay"${replayActive?" disabled":""}>${replayActive?"Rendering…":run.artifacts.replay_video?"Regenerate 3D replay":"Generate 3D replay"}</button></div></div>
    <div class="summary-grid">
      ${metricCard("Rooms visited",m.rooms_visited,`${run.metrics.rooms.length} in trace`)}${metricCard("Gems",m.gems,"of 100 target")}${metricCard("Actions",number(m.actions),run.lineage==="fork"?`${number(m.branch_actions)} new · ${number(m.inherited_actions)} inherited`:`${m.valid_actions} valid`)}
      ${metricCard("State novelty",`${Math.round(m.novelty_rate*100)}%`,`${m.unique_states} unique states`)}${metricCard("Longest plateau",number(m.longest_plateau),"actions without novelty")}${metricCard("Token use",compact(m.total_tokens),`${compact(m.tokens_per_action || 0)} / action`)}
    </div>
    <div class="detail-grid">
      <section class="panel"><div class="panel-head"><div><p class="eyebrow">EXPLORATION SIGNAL</p><h3>Novelty over time</h3></div><div class="legend"><span><i class="acid"></i>rolling novelty</span></div></div><div class="panel-body"><canvas class="chart" id="novelty-chart"></canvas></div></section>
      <section class="panel"><div class="panel-head"><div><p class="eyebrow">PROGRESS</p><h3>Discovery curve</h3></div><div class="legend"><span><i class="cyan"></i>rooms</span><span><i class="orange"></i>gems</span></div></div><div class="panel-body"><canvas class="chart" id="progress-chart"></canvas></div></section>
    </div>
    <section class="panel reasoning-stream-panel"><div class="panel-head"><div><p class="eyebrow">MODEL TURN STREAM</p><h3>Reasoning → movement / action</h3></div><p>${running?(livePhase==="loading_model"?"loading model service":"live · follows newest"):"recorded journal"}</p></div><div class="reasoning-chat" id="reasoning-chat">${renderReasoningChat(state.interactions,state.compactions,timeline,running,state.interactionsError,livePhase)}</div></section>
    <section class="panel observation-panel"><div class="panel-head"><div><p class="eyebrow">SYNCHRONIZED OBSERVATION</p><h3>ASCII / 3D human replay</h3></div><p>model input · ${jsonMode?"JSON":"ASCII"} · ${running?"live trace":replayJob ? escapeHtml(replayJob.status) : "recorded trace"}</p></div>
      <div class="observation-grid">
        <div class="observation-pane model-observation"><div class="observation-pane-head"><div><small>HUMAN TRACE</small><b>${jsonMode?"MazeBench ASCII environment view":"Exact ASCII model observation"}</b></div><div class="observation-tools"><span id="model-input-caption">${jsonMode?"after action":"before action"}</span>${jsonMode?'<button id="open-raw-model-input" title="Open the exact JSON sent to the model">Raw JSON</button>':""}<button id="ascii-smaller" title="Zoom out">−</button><button id="ascii-fit" title="Reset observation zoom">100%</button><button id="ascii-larger" title="Zoom in">+</button></div></div><div class="ascii-perspective"><pre class="ascii-board" id="ascii-board"></pre></div></div>
        <div class="observation-pane human-observation"><div class="observation-pane-head"><div><small>HUMAN VIEW</small><b>Official 3D engine</b></div><span>isolated · camera only</span></div><div class="viewer-box">${renderHumanViewer(run,timeline)}</div></div>
      </div>
      <div class="replay-controls"><div class="transport"><button id="frame-start" title="First">|‹</button><button id="frame-prev" title="Previous">‹</button><button id="frame-play" title="Play">▶</button><button id="frame-next" title="Next">›</button></div><input class="scrubber" id="frame-slider" type="range" min="0" max="${Math.max(0,timeline.length-1)}" value="${state.frame}"><span class="turn-label" id="turn-label"></span></div>
      <div class="frame-meta observation-meta" id="frame-meta"></div>
    </section>
    <div class="spatial-grid">
      <section class="panel"><div class="panel-head"><div><p class="eyebrow">ROOM TRACE</p><h3>Within-room movement</h3></div><p id="path-caption">through selected decision</p></div><div class="panel-body"><canvas class="path-canvas" id="path-canvas"></canvas><div class="path-legend"><span><i class="path-line-key"></i>movement</span><span><i class="path-current-key"></i>current</span><span><i class="path-break-key">×</i>death / reset</span></div></div></section>
      <section class="panel"><div class="panel-head"><div><p class="eyebrow">ROOM COVERAGE</p><h3>World heatmap & allocation</h3></div><p>${run.metrics.rooms.length} rooms</p></div><div class="panel-body"><canvas class="world-map" id="world-map"></canvas><div class="rooms-list">${renderRooms(run.metrics.rooms)}</div></div></section>
      <section class="panel"><div class="panel-head"><div><p class="eyebrow">ACTION FEED</p><h3>Interaction trace</h3></div><p>${timeline.length} records</p></div><div class="action-feed">${renderActions(timeline)}</div></section>
    </div>
  </div>`;
  bindDetail(); requestAnimationFrame(() => { drawLineChart($("#novelty-chart"), [{color:"#c8f26b",values:timeline.map(x=>x.novelty_rolling)}],1); drawLineChart($("#progress-chart"), [{color:"#66d9cf",values:timeline.map(x=>x.rooms_visited)},{color:"#f3a85e",values:timeline.map(x=>x.gems)}]); drawWorldMap($("#world-map"),run.metrics.rooms); renderFrame(); });
}

function renderRooms(rooms) { const max=Math.max(1,...rooms.map(x=>x.actions)); return rooms.length ? rooms.map(room=>`<div class="room-row"><b>${escapeHtml(shortRoom(room.room))}</b><div class="room-bar"><i style="width:${room.actions/max*100}%"></i></div><span>${room.actions} act.</span></div>`).join("") : `<div class="empty-state">No action-level room data in this legacy run.</div>`; }
function renderActions(rows) { return rows.length ? [...rows].reverse().map(row=>`<div class="action-row" data-jump="${row.turn-1}"><span class="turn">#${row.turn}</span><b>${escapeHtml(row.command)}</b><span>${escapeHtml(shortRoom(row.room))} · ${row.x ?? "—"},${row.y ?? "—"}</span><i class="novel-dot ${row.novel_state?"yes":""}" title="${row.novel_state?"Novel state":"Revisit"}"></i></div>`).join("") : `<div class="empty-state">This older run did not retain actions.</div>`; }
function reasoningOutcome(row){if(!row)return"Waiting for engine result…";if(row.player_dead)return"Player died · respawned";if(row.room_changed)return`Entered room ${shortRoom(row.room)}`;if(row.action==="move")return row.moved===true?`Moved to ${row.x ?? "—"}, ${row.y ?? "—"}`:row.moved===false?"Movement blocked / unchanged":"Movement recorded";return row.novel_state?"Environment state changed":"No novel state"}
function normalizedCompactions(rows){const grouped=new Map();for(const row of rows||[]){const key=`${row.inherited_from?.run_id||"local"}:${row.attempt||row.compaction||row.started_at||grouped.size}`,old=grouped.get(key);if(!old)grouped.set(key,row);else if(old.status==="started"||row.status!=="started")grouped.set(key,{...old,...row,source_messages:row.source_messages||old.source_messages})}return [...grouped.values()]}
function compactionCall(row,interactions){const explicit=Number(row.before_call);if(Number.isFinite(explicit)&&explicit>0)return explicit;const stamp=String(row.ended_at||row.started_at||"");const next=interactions.find(item=>String(item.started_at||"")>=stamp);return Number(next?.call||Math.max(1,...interactions.map(item=>Number(item.call)||0)))}
function renderCompactionChat(row,call){const completed=row.status==="completed",failed=row.status==="failed",before=number(row.working_tokens_before),after=number(row.working_tokens_after),source=number(row.source_tokens),inherited=row.inherited_from?.run_id?` · inherited from ${escapeHtml(row.inherited_from.run_id)}`:"";return `<article class="compaction-chat ${escapeHtml(row.status||"started")}"><header><span>CONTEXT COMPACTION ${number(row.compaction)}</span><b>before turn ${number(call)}${inherited}</b></header><div><strong>${completed?`${before} → ${after} working tokens${row.recovery?` · ${escapeHtml(row.recovery)}`:""}`:failed?"Compaction failed":"Compaction in progress…"}</strong><small>${number(row.raw_messages_compacted)} messages summarized · ${source} source tokens · ${duration(Number(row.latency_ms||0)/1000)}</small>${row.summary?`<details><summary>Show generated continuation summary</summary><pre>${escapeHtml(row.summary)}</pre></details>`:""}${row.partial_summary?`<details><summary>Show rejected partial output</summary><pre>${escapeHtml(row.partial_summary)}</pre></details>`:""}${row.error?`<pre class="compaction-error">${escapeHtml(row.error.type)} · ${escapeHtml(row.error.message)}</pre>`:""}</div></article>`}
function renderReasoningChat(interactions,compactions,timeline,running,error="",phase="running_trial"){
  if(error)return`<div class="empty-state">Could not load the model journal: ${escapeHtml(error)}</div>`;
  const turns=new Map(timeline.map(row=>[Number(row.turn),row])),items=interactions.map(record=>({kind:"turn",order:Number(record.call)*2,record}));normalizedCompactions(compactions).forEach(record=>{const call=compactionCall(record,interactions);items.push({kind:"compaction",order:call*2-1,record,call})});items.sort((a,b)=>a.order-b.order);const entries=items.map(item=>{if(item.kind==="compaction")return renderCompactionChat(item.record,item.call);const record=item.record;
    const call=Number(record.call),row=turns.get(call),message=interactionMessage(record),reasoning=interactionReasoning(message),usage=record.response?.usage||{},latency=Number(record.latency_ms||0),command=message.content??row?.command??"No final command";
    return `<article class="reasoning-turn" id="reasoning-turn-${call}"><header><span>TURN ${number(call)}${record.inherited_from?` · inherited from ${escapeHtml(record.inherited_from.run_id)}`:""}</span><div>${latency?duration(latency/1000):"—"} · ${number(usage.completion_tokens||0)} output tokens</div></header><div class="reasoning-bubble"><small>MODEL REASONING</small><pre>${escapeHtml(reasoning||"No separate reasoning trace was returned for this turn.")}</pre></div><div class="reasoning-flow" aria-hidden="true"><i></i><span>then</span><i></i></div><button class="reasoning-result" type="button" ${row?`data-chat-turn="${row.turn}"`:"disabled"}><span class="reasoning-command"><small>ACTION</small><code>${escapeHtml(command)}</code></span><span class="reasoning-outcome"><small>ENVIRONMENT RESULT</small><b>${escapeHtml(reasoningOutcome(row))}</b><em>${row?`${escapeHtml(shortRoom(row.room))} · ${row.x ?? "—"}, ${row.y ?? "—"} · ${row.novel_state?"novel":"revisit"}`:"action journal pending"}</em></span><span class="reasoning-view">${row?"View state ›":""}</span></button>${record.error?`<div class="reasoning-call-error">${escapeHtml(record.error.type)} · ${escapeHtml(record.error.message)}</div>`:""}</article>`;
  }).join("");
  const next=Math.max(0,...interactions.map(row=>Number(row.call)||0),...timeline.map(row=>Number(row.turn)||0))+1,loading=phase==="loading_model",pending=running?`<article class="reasoning-pending"><span class="thinking-dots"><i></i><i></i><i></i></span><div><b>${loading?"Loading the selected local model service":`Turn ${next} · model is thinking`}</b><small>${loading?"The trial begins automatically when llama.cpp reports Qwen ready. Follow Run logs for load progress.":"The reasoning trace appears here when the local response completes."}</small></div></article>`:"";
  return entries||pending?entries+pending:`<div class="empty-state">No saved model-call journal exists for this run.</div>`;
}
function renderHumanViewer(run,timeline) { if(!timeline.length)return `<div class="video-cube">◇</div><b>No replayable actions</b><span>This run has no engine trace to reconstruct.</span>`;const turn=Number(timeline[Math.max(0,state.frame)]?.turn)||1,decision=turn+(run.config?.observation_mode==="json"?1:0);return `<iframe id="official-viewer" title="Read-only official MazeBench engine viewer" allow="fullscreen" src="/viewer.html?run=${encodeURIComponent(run.id)}&decision=${decision}"></iframe>`; }
function renderVideo(run,job) { if(job?.status==="running"||job?.status==="queued")return `<div class="video-cube">◇</div><b>Rendering the trajectory…</b><span>The previous video remains available until its replacement is complete.</span>`; if(job?.status==="failed")return `<div class="video-cube">!</div><b>Replay generation failed</b><span>${escapeHtml(job.error)}</span><code class="setup-note">See artifacts/replay.log for details.</code>`; if(run.artifacts.replay_video){const version=encodeURIComponent(run.artifacts.replay_version||"legacy");return `<video id="observation-video" controls preload="metadata" src="/api/runs/${encodeURIComponent(run.id)}/replay.mp4?v=${version}"></video>${run.replay_manifest?"":`<span class="video-sync-note">Regenerate once to enable exact action seeking.</span>`}`} return `<div class="video-cube">◇</div><b>No 3D replay generated yet</b><span>Generate it to pair the model input with the official engine view.</span>${!state.capabilities.replay_ready?`<code class="setup-note">One-time setup: scripts/setup-replay.sh</code>`:""}`; }

function bindDetail() {
  $("#generate-replay").onclick = generateReplay; $("#open-log").onclick = openLog; $("#open-interactions").onclick = openInteractions;
  if($("#stop-run")) $("#stop-run").onclick = stopCurrentRun;
  if($("#delete-run")) $("#delete-run").onclick = deleteCurrentRun;
  $("#frame-slider").oninput = event => { state.frame=Number(event.target.value); stopPlayback(); renderFrame(); };
  $("#frame-start").onclick=()=>{state.frame=0;stopPlayback();renderFrame()}; $("#frame-prev").onclick=()=>stepFrame(-1); $("#frame-next").onclick=()=>stepFrame(1); $("#frame-play").onclick=togglePlayback;
  $("#ascii-smaller").onclick=()=>setAsciiZoom(state.asciiZoom-.2); $("#ascii-fit").onclick=()=>setAsciiZoom(1); $("#ascii-larger").onclick=()=>setAsciiZoom(state.asciiZoom+.2);
  if($("#open-raw-model-input"))$("#open-raw-model-input").onclick=openRawModelInput;
  $$('[data-jump]').forEach(row => row.onclick=()=>{state.frame=Number(row.dataset.jump);stopPlayback();renderFrame();$("#ascii-board").scrollIntoView({behavior:"smooth",block:"center"})});
  $$('[data-chat-turn]').forEach(button=>button.onclick=()=>{const index=state.detail.metrics.timeline.findIndex(row=>Number(row.turn)===Number(button.dataset.chatTurn));if(index<0)return;state.frame=index;stopPlayback();renderFrame();$("#ascii-board").scrollIntoView({behavior:"smooth",block:"center"})});
  const video=$("#observation-video");
  if(video){video.addEventListener("loadedmetadata",()=>seekReplayToDecision(state.frame));video.addEventListener("timeupdate",()=>syncDecisionFromVideo(video));video.addEventListener("play",()=>{$("#frame-play").textContent="Ⅱ"});video.addEventListener("pause",()=>{$("#frame-play").textContent="▶"})}
  const viewer=$("#official-viewer");if(viewer)viewer.addEventListener("load",()=>syncOfficialViewer());
}

function setAsciiZoom(value){state.asciiZoom=Math.max(.6,Math.min(2,Math.round(value*10)/10));const board=$("#ascii-board");if(board)board.style.fontSize=`${10*state.asciiZoom}px`;const reset=$("#ascii-fit");if(reset)reset.textContent=`${Math.round(state.asciiZoom*100)}%`}

function replayDecisionSeconds(index) {
  const manifest=state.detail?.replay_manifest,fps=Number(manifest?.fps||0),actions=manifest?.actions;
  if(!fps||!Array.isArray(actions)||!actions.length)return null;
  if(index<=0)return Number(manifest.first_action_frame||0)/fps;
  const previous=actions.find(action=>Number(action.source_index)===index-1)||actions[index-1];
  return previous?Number(previous.frame_end||previous.frame_start||0)/fps:null;
}
function seekReplayToDecision(index){const video=$("#observation-video");if(!video||video.readyState<1)return;let seconds=replayDecisionSeconds(index);if(seconds===null){const count=state.detail?.metrics.timeline.length||1;seconds=(video.duration||0)*index/Math.max(1,count)}video.currentTime=Math.max(0,Math.min(seconds,video.duration||seconds))}
function syncDecisionFromVideo(video){
  const rows=state.detail?.metrics.timeline||[];if(!rows.length||!state.detail?.replay_manifest)return;
  let next=0;for(let index=1;index<rows.length;index++){const seconds=replayDecisionSeconds(index);if(seconds===null||video.currentTime+0.03<seconds)break;next=index}
  if(next!==state.frame){state.frame=next;renderFrame({seekVideo:false})}
}
function renderFrame({seekVideo=true} = {}) {
  const rows=state.detail?.metrics.timeline||[]; if(!rows.length)return; state.frame=Math.max(0,Math.min(rows.length-1,state.frame)); const row=rows[state.frame];
  const jsonMode=state.detail?.config?.observation_mode==="json",exact=Boolean(row.model_board),board=jsonMode?(row.board||"No engine ASCII snapshot retained for this action."):(row.model_board||row.board||"No model observation retained for this action.");
  $("#ascii-board").textContent=board; $("#model-input-caption").textContent=jsonMode?`engine result after #${row.turn}`:exact?`used to choose #${row.turn}`:`legacy post-action snapshot #${row.turn}`; $("#frame-slider").value=state.frame; $("#turn-label").textContent=`decision ${row.turn} / ${rows.length}`;
  setAsciiZoom(state.asciiZoom);
  $("#frame-meta").innerHTML=[["model chose",row.command],["result room",shortRoom(row.room)],["result position",`${row.x ?? "—"}, ${row.y ?? "—"}, z${row.elevation ?? "—"}`],["result",row.player_dead?"PLAYER DIED":row.novel_state?"NOVEL STATE":"REVISIT"]].map(([a,b])=>`<div><small>${a}</small><b>${escapeHtml(b)}</b></div>`).join("");
  const pathCaption=$("#path-caption");if(pathCaption)pathCaption.textContent=`${shortRoom(row.room)} · through #${row.turn}`;
  drawPath($("#path-canvas"),rows,row.room,state.frame);
  if(seekVideo)seekReplayToDecision(state.frame);
  syncOfficialViewer();
}
function openRawModelInput(){
  const row=state.detail?.metrics?.timeline?.[state.frame],raw=row?.model_board;if(!raw)return toast("No exact model input was retained for this turn",true);
  const win=window.open("","_blank");if(!win)return toast("The browser blocked the raw-input window",true);
  win.document.write(`<title>${escapeHtml(state.selected)} · turn ${number(row.turn)} model input</title><body style="margin:0;background:#0b0e0d;color:#dce7dc"><header style="position:sticky;top:0;padding:16px 24px;background:#111513;border-bottom:1px solid #273029;font:12px monospace"><b>Exact JSON model input</b> · ${escapeHtml(state.selected)} · decision ${number(row.turn)}</header><pre style="box-sizing:border-box;max-width:100%;margin:0;padding:24px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.55 ui-monospace,monospace">${escapeHtml(raw)}</pre></body>`);
  win.document.close();
}
function syncOfficialViewer(){const viewer=$("#official-viewer"),row=state.detail?.metrics.timeline?.[state.frame],jsonMode=state.detail?.config?.observation_mode==="json";if(viewer&&row)viewer.contentWindow?.postMessage({type:"mazebench-viewer-decision",decision:Number(row.turn)+(jsonMode?1:0)},location.origin)}
function stepFrame(delta){const rows=state.detail.metrics.timeline;state.frame=Math.max(0,Math.min(rows.length-1,state.frame+delta));renderFrame()}
function togglePlayback(){const video=$("#observation-video");if(video&&state.detail?.replay_manifest){if(video.paused)video.play().catch(error=>toast(error.message,true));else video.pause();return}if(state.timer){stopPlayback();return}$("#frame-play").textContent="Ⅱ";state.timer=setInterval(()=>{if(state.frame>=state.detail.metrics.timeline.length-1){stopPlayback();return}state.frame++;renderFrame()},700)}
function stopPlayback(){clearInterval(state.timer);state.timer=null;const video=$("#observation-video");if(video&&!video.paused)video.pause();if($("#frame-play"))$("#frame-play").textContent="▶"}

function canvasSize(canvas) { const dpr=window.devicePixelRatio||1, rect=canvas.getBoundingClientRect(); canvas.width=rect.width*dpr;canvas.height=rect.height*dpr;const ctx=canvas.getContext("2d");ctx.scale(dpr,dpr);return {ctx,w:rect.width,h:rect.height}; }
function drawLineChart(canvas,series,fixedMax=null){if(!canvas)return;const {ctx,w,h}=canvasSize(canvas),pad={l:30,r:10,t:10,b:20};ctx.clearRect(0,0,w,h);ctx.strokeStyle="#222a25";ctx.lineWidth=1;for(let i=0;i<4;i++){const y=pad.t+(h-pad.t-pad.b)*i/3;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(w-pad.r,y);ctx.stroke()}const all=series.flatMap(s=>s.values.map(Number).filter(Number.isFinite)),max=fixedMax??Math.max(1,...all);ctx.fillStyle="#657169";ctx.font="8px ui-monospace";ctx.fillText(String(max),3,pad.t+4);ctx.fillText("0",18,h-pad.b);series.forEach(s=>{if(!s.values.length)return;ctx.strokeStyle=s.color;ctx.lineWidth=1.7;ctx.beginPath();s.values.forEach((value,i)=>{const x=pad.l+(w-pad.l-pad.r)*(i/Math.max(1,s.values.length-1)),y=h-pad.b-(h-pad.t-pad.b)*(Number(value||0)/max);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()});ctx.fillStyle="#657169";ctx.fillText("action 1",pad.l,h-3);ctx.fillText(`action ${Math.max(1,all.length/series.length)}`,w-65,h-3)}
function drawPath(canvas,rows,room,frame){
  if(!canvas)return;
  const {ctx,w,h}=canvasSize(canvas),visible=rows.slice(0,frame+1),segments=[],breaks=[];
  let segment=null,pendingBreak=null;
  for(const row of visible){
    if(row.room!==room){segment=null;pendingBreak=null;continue}
    if(row.path_break_reason){segment=null;pendingBreak=row.path_break_reason}
    if(row.x===null||row.x===undefined||row.y===null||row.y===undefined)continue;
    const x=Number(row.x),y=Number(row.y);
    if(!Number.isFinite(x)||!Number.isFinite(y))continue;
    if(!segment){segment=[];segments.push(segment)}
    segment.push({...row,x,y});
    if(pendingBreak==="death"||pendingBreak==="reset")breaks.push({...row,x,y,path_break_reason:pendingBreak});
    pendingBreak=null;
  }
  const points=segments.flat();
  ctx.fillStyle="#0b0e0c";ctx.fillRect(0,0,w,h);ctx.strokeStyle="#18201b";ctx.lineWidth=1;
  for(let x=12;x<w;x+=18){ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,h);ctx.stroke()}
  for(let y=12;y<h;y+=18){ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke()}
  ctx.fillStyle="#657169";ctx.font="8px ui-monospace";ctx.fillText(shortRoom(room),8,h-8);
  if(!points.length)return;
  const xs=points.map(p=>p.x),ys=points.map(p=>p.y),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys),spanX=Math.max(1,maxX-minX),spanY=Math.max(1,maxY-minY),scale=Math.min((w-40)/spanX,(h-40)/spanY),plotW=spanX*scale,plotH=spanY*scale,ox=(w-plotW)/2,oy=(h-plotH)/2,sx=x=>ox+(x-minX)*scale,sy=y=>oy+(y-minY)*scale;
  ctx.strokeStyle="#66d9cf";ctx.lineWidth=2;ctx.lineJoin="round";ctx.lineCap="round";
  for(const path of segments){if(path.length<2)continue;ctx.beginPath();path.forEach((p,i)=>i?ctx.lineTo(sx(p.x),sy(p.y)):ctx.moveTo(sx(p.x),sy(p.y)));ctx.stroke()}
  points.forEach((p,i)=>{ctx.fillStyle=i===points.length-1?"#c8f26b":p.novel_state?"#66d9cf":"#3a4740";ctx.beginPath();ctx.arc(sx(p.x),sy(p.y),i===points.length-1?4:2.5,0,Math.PI*2);ctx.fill()});
  ctx.strokeStyle="#ee6b68";ctx.lineWidth=1.8;
  for(const point of breaks){const x=sx(point.x),y=sy(point.y),r=5;ctx.beginPath();ctx.arc(x,y,r+2,0,Math.PI*2);ctx.stroke();ctx.beginPath();ctx.moveTo(x-r,y-r);ctx.lineTo(x+r,y+r);ctx.moveTo(x+r,y-r);ctx.lineTo(x-r,y+r);ctx.stroke()}
}
const ASCII_PALETTE={" ":"#050608",H:"#050608",h:"#050608",A:"#d6bd94",a:"#d6bd94",E:"#d6bd94",e:"#d6bd94","8":"#d6bd94",W:"#23262c",w:"#23262c",I:"#a9d6f4",i:"#a9d6f4",K:"#a9d6f4",k:"#a9d6f4","~":"#a9d6f4","-":"#a9d6f4",O:"#b85f16",o:"#b85f16",Y:"#c75652",y:"#c75652",S:"#476b35",s:"#476b35",T:"#2f7d3f",t:"#2f7d3f",B:"#2a2d33",b:"#2a2d33",G:"#6cd7ff",g:"#6cd7ff",P:"#5aa95c",p:"#5aa95c",F:"#d6bd94",f:"#d6bd94","&":"#5b2f14","7":"#5b2f14","!":"#5b2f14","1":"#5b2f14","@":"#5b2f14","2":"#5b2f14","#":"#5b2f14","3":"#5b2f14","$":"#5b2f14","4":"#5b2f14","{":"#b59a2a","[":"#b59a2a",C:"#b59a2a",c:"#b59a2a",D:"#b59a2a",d:"#b59a2a",J:"#b59a2a",j:"#b59a2a","}":"#ef4444","]":"#ef4444",X:"#ef4444",x:"#ef4444",Q:"#ef4444",q:"#ef4444",Z:"#ef4444",z:"#ef4444",";":"#315991","_":"#315991",U:"#315991",u:"#315991"};
function drawAsciiBoard(canvas,board){if(!canvas)return;const rows=String(board||"").replaceAll("\r","").split("\n"),width=Math.max(1,...rows.map(row=>[...row].length)),height=Math.max(1,rows.length);canvas.width=width;canvas.height=height;const ctx=canvas.getContext("2d");ctx.imageSmoothingEnabled=false;ctx.fillStyle="#050608";ctx.fillRect(0,0,width,height);rows.forEach((row,y)=>[...row].forEach((glyph,x)=>{ctx.fillStyle=ASCII_PALETTE[glyph]||"#8a63d2";ctx.fillRect(x,y,1,1)}));}
function roomCoordinates(room){const match=String(room).match(/level_([A-Z]+)x([A-Z]+)$/i);if(!match)return null;const value=letters=>[...letters.toUpperCase()].reduce((n,c)=>n*26+c.charCodeAt(0)-64,0)-1;return [value(match[1]),value(match[2])]}
function drawWorldMap(canvas,visited){if(!canvas)return;const {ctx,w,h}=canvasSize(canvas),rooms=state.worldRooms.map(room=>({room,xy:roomCoordinates(room)})).filter(x=>x.xy),visitedMap=new Map(visited.map(room=>[room.room,room.actions])),maxX=Math.max(0,...rooms.map(x=>x.xy[0])),maxY=Math.max(0,...rooms.map(x=>x.xy[1])),gap=2,cell=Math.max(3,Math.min((w-28-(maxX*gap))/(maxX+1),(h-24-(maxY*gap))/(maxY+1))),gridW=(maxX+1)*cell+maxX*gap,gridH=(maxY+1)*cell+maxY*gap,ox=(w-gridW)/2,oy=(h-gridH)/2,maxActions=Math.max(1,...visitedMap.values());ctx.fillStyle="#0b0e0c";ctx.fillRect(0,0,w,h);rooms.forEach(({room,xy})=>{const count=visitedMap.get(room)||0,x=ox+xy[0]*(cell+gap),y=oy+xy[1]*(cell+gap);ctx.fillStyle=count?`rgba(200,242,107,${.35+.65*count/maxActions})`:"#161c18";ctx.strokeStyle=count?"#c8f26b":"#263029";ctx.lineWidth=1;ctx.fillRect(x,y,cell,cell);ctx.strokeRect(x+.5,y+.5,cell-1,cell-1)});ctx.fillStyle="#657169";ctx.font="8px ui-monospace";ctx.fillText(`${visited.length} / ${rooms.length} rooms`,8,h-7)}

async function generateReplay(){if(latestJob("replay",state.selected)?.status?.match(/^(queued|running)$/))return;try{const job=await api(`/api/runs/${encodeURIComponent(state.selected)}/replay`,{method:"POST",body:"{}"});state.jobs=state.jobs.filter(item=>item.id!==job.id);state.jobs.push(job);toast("3D replay job started");renderDetail()}catch(error){toast(error.message,true)}}
async function deleteCurrentRun(){const runId=state.selected;if(!confirm(`Delete ${runId}?\n\nThe run will be moved to runs/.trash and can be recovered manually.`))return;try{await api(`/api/runs/${encodeURIComponent(runId)}`,{method:"DELETE"});state.selected=null;state.detail=null;toast(`Moved ${runId} to trash`);await refresh({quiet:true})}catch(error){toast(error.message,true)}}
async function openLog(){try{const {log}=await api(`/api/runs/${encodeURIComponent(state.selected)}/log`);const win=window.open("","_blank");win.document.write(`<title>${escapeHtml(state.selected)} log</title><body style="background:#0b0e0d;color:#dce7dc;padding:24px"><pre style="white-space:pre-wrap;font:12px/1.5 monospace">${escapeHtml(log)}</pre></body>`)}catch(error){toast(error.message,true)}}
function interactionMessage(row){return row.response?.choices?.[0]?.message||{}}
function interactionReasoning(message){for(const key of ["reasoning_content","reasoning"]){if(typeof message?.[key]==="string")return message[key]}return message?.reasoning_details?JSON.stringify(message.reasoning_details,null,2):""}
function renderInteractionCard(row){const choice=row.response?.choices?.[0]||{},message=interactionMessage(row),reasoning=interactionReasoning(message),usage=row.response?.usage||{},cached=Number(usage.prompt_tokens_details?.cached_tokens||0),uncached=Math.max(0,Number(usage.prompt_tokens||0)-cached),latency=Number(row.latency_ms||0),metadata={id:row.response?.id,model:row.response?.model,created:row.response?.created,usage,timings:row.response?.timings};return `<article class="interaction-card">
  <header><div><span class="turn">CALL ${number(row.call)}</span><b>${escapeHtml(choice.finish_reason||row.error?.type||"unknown")}${row.inherited_from?` · inherited`:""}</b></div><div><span>${latency>=1000?`${(latency/1000).toFixed(1)} s`:`${number(Math.round(latency))} ms`}</span><span>${number(uncached)} new</span><span>${number(cached)} cached</span><span>${number(usage.completion_tokens||0)} out</span></div></header>
  ${row.error?`<div class="interaction-error"><b>${escapeHtml(row.error.type)}</b><span>${escapeHtml(row.error.message)}</span></div>`:""}
  <section><small>FINAL CONTENT</small><pre class="interaction-final">${escapeHtml(message.content??"No final content")}</pre></section>
  <details><summary>Reasoning trace · ${compact(reasoning.length)} characters</summary><pre>${escapeHtml(reasoning||"No separate reasoning content was returned.")}</pre></details>
  <details><summary>Prompt delta · ${number(row.request?.shared_prefix_messages||0)} shared / ${number(row.request?.message_count||0)} messages</summary><pre>${escapeHtml(JSON.stringify(row.request?.appended_messages||[],null,2))}</pre></details>
  <details><summary>Usage and provider timing</summary><pre>${escapeHtml(JSON.stringify(metadata,null,2))}</pre></details>
  <footer><span>${escapeHtml(row.started_at||"")}</span><code>${escapeHtml(row.request?.prompt_sha256||"")}</code></footer>
</article>`}
function renderCompactionCard(row,call){const usage=row.response_usage||{};return `<article class="interaction-card compaction-card"><header><div><span class="turn">COMPACTION ${number(row.compaction)}</span><b>${escapeHtml(row.status||"started")} · before call ${number(call)}${row.inherited_from?" · inherited":""}</b></div><div><span>${duration(Number(row.latency_ms||0)/1000)}</span><span>${number(row.working_tokens_before)} → ${number(row.working_tokens_after)}</span></div></header>${row.error?`<div class="interaction-error"><b>${escapeHtml(row.error.type)}</b><span>${escapeHtml(row.error.message)}</span></div>`:""}<section><small>GENERATED CONTINUATION SUMMARY</small><pre>${escapeHtml(row.summary||"Summary generation is still running.")}</pre></section>${row.partial_summary?`<section><small>REJECTED PARTIAL OUTPUT</small><pre>${escapeHtml(row.partial_summary)}</pre></section>`:""}<details><summary>Compacted source · ${number(row.raw_messages_compacted)} messages / ${number(row.source_tokens)} tokens</summary><pre>${escapeHtml(JSON.stringify(row.source_messages||[],null,2))}</pre></details><details><summary>Configuration and usage</summary><pre>${escapeHtml(JSON.stringify({mode:row.mode,trigger:row.trigger,recovery:row.recovery,retry_after_call:row.retry_after_call,recent_turns_retained:row.recent_turns_retained,summary_budget_tokens:row.summary_budget_tokens,response_finish_reason:row.response_finish_reason,response_usage:usage,response_timings:row.response_timings,repair_usage:row.repair_usage,repair_timings:row.repair_timings,source_sha256:row.source_sha256,summary_sha256:row.summary_sha256},null,2))}</pre></details><footer><span>${escapeHtml(row.started_at||"")}</span><code>${escapeHtml(row.summary_sha256||row.source_sha256||"")}</code></footer></article>`}
function renderInteractions(rows,compactions=[]){const items=rows.map(row=>({kind:"call",order:Number(row.call)*2,row}));normalizedCompactions(compactions).forEach(row=>{const call=compactionCall(row,rows);items.push({kind:"compaction",order:call*2-1,row,call})});items.sort((a,b)=>b.order-a.order);return items.length?items.map(item=>item.kind==="call"?renderInteractionCard(item.row):renderCompactionCard(item.row,item.call)).join(""):`<div class="empty-state">No model-call or compaction journal exists for this run.</div>`}
async function loadInteractions(){const list=$("#interaction-list");list.innerHTML=`<div class="empty-state">Loading model calls…</div>`;try{const {interactions,compactions}=await api(`/api/runs/${encodeURIComponent(state.selected)}/interactions`);list.innerHTML=renderInteractions(interactions||[],compactions||[])}catch(error){list.innerHTML=`<div class="empty-state">${escapeHtml(error.message)}</div>`;toast(error.message,true)}}
async function openInteractions(){$("#interactions-title").textContent=`Reasoning traces · ${state.selected}`;$("#interactions-dialog").showModal();await loadInteractions()}
async function stopCurrentRun(){const job=state.jobs.find(j=>j.run_id===state.selected&&j.kind==="trial"&&["queued","running","stopping"].includes(j.status));if(!job)return toast("No live process found",true);try{await api(`/api/jobs/${encodeURIComponent(job.id)}/stop`,{method:"POST",body:"{}"});toast("Stopping trial…")}catch(error){toast(error.message,true)}}

$("#new-run-button").onclick=()=>{renderTrialOptions();updateSystemPromptControls({reset:true});$("#new-run-dialog").showModal()}; $("#refresh-button").onclick=()=>refresh(); $("#run-search").oninput=renderRunList;
$("#fork-parent").onchange=updateForkControls;
$("#model-profile").onchange=updateModelControls;
$("#context-mode").onchange=updateContextControls;
$("#observation-mode").onchange=updateObservationControls;
$("#ascii-character-mode").onchange=updateObservationControls;
$("#ascii-seed").oninput=()=>updateSystemPromptControls();
$("#unofficial-system-prompt").onchange=()=>updateSystemPromptControls();
$("#close-interactions").onclick=()=>$("#interactions-dialog").close(); $("#refresh-interactions").onclick=loadInteractions;
$("#new-run-form").onsubmit=async event=>{if(event.submitter?.value==="cancel")return;event.preventDefault();if(!supportsExperimentControls()){toast("Restart the Control Center before launching with the new experiment controls",true);return}const form=new FormData(event.currentTarget),config=Object.fromEntries(form),parent=$("#fork-parent").value;config.actions=Number(config.actions);config.temperature=Number(config.temperature);config.ascii_character_mode=$("#ascii-character-mode").value;config.hide_names=config.observation_mode==="ascii"&&config.ascii_character_mode==="random";config.hide_names_seed=config.hide_names?$("#ascii-seed").value:"";config.thinking=form.has("thinking");config.thinking_budget=Number(config.thinking_budget);config.preserve_thinking=form.has("preserve_thinking");config.unofficial_system_prompt=$("#unofficial-system-prompt").checked;config.system_prompt=$("#system-prompt").value;config.fork_parent_run_id=parent;if(parent)config.fork_turn=Number($("#fork-turn").value);else{delete config.fork_turn;config.level=$("[name='level']").value}try{$("#launch-button").disabled=true;const job=await api("/api/runs",{method:"POST",body:JSON.stringify(config)});$("#new-run-dialog").close();state.jobs.push(job);toast(parent?`Started fork ${job.run_id}`:`Started ${job.run_id}`);await refresh({quiet:true});await selectRun(job.run_id)}catch(error){toast(error.message,true)}finally{$("#launch-button").disabled=!supportsExperimentControls()}};
$$('[data-scroll="analytics"]').forEach(button=>button.onclick=()=>$("#analytics")?.scrollIntoView({behavior:"smooth"}));
window.addEventListener("resize",()=>{if(!state.detail)return;requestAnimationFrame(()=>{const run=state.detail,timeline=run.metrics.timeline;drawLineChart($("#novelty-chart"),[{color:"#c8f26b",values:timeline.map(x=>x.novelty_rolling)}],1);drawLineChart($("#progress-chart"),[{color:"#66d9cf",values:timeline.map(x=>x.rooms_visited)},{color:"#f3a85e",values:timeline.map(x=>x.gems)}]);drawWorldMap($("#world-map"),run.metrics.rooms);renderFrame()})});
refresh({quiet:true}); setInterval(()=>{if(state.jobs.some(job=>["queued","running","stopping"].includes(job.status)))refresh({quiet:true})},2500);
