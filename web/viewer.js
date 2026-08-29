(() => {
  const params = new URLSearchParams(location.search);
  const runId = params.get("run") || "";
  const canvas = document.getElementById("maze-canvas");
  const stage = document.getElementById("viewer-stage");
  const loading = document.getElementById("viewer-loading");
  const errorBox = document.getElementById("viewer-error");
  const statusBox = document.getElementById("viewer-status");
  let app = null;
  let currentDecision = Math.max(1, Number(params.get("decision")) || 1);
  let requestSerial = 0;
  // The official renderer's own default: almost top-down, but inclined enough
  // for wall height, holes, actors, and shadows to remain visually distinct.
  const DEFAULT_CAMERA_TILT = 0.22;

  async function json(path) {
    const response = await fetch(path, { cache: "no-store" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function applySnapshot(levelState, renderState) {
    const state = structuredClone(levelState);
    state.actors = structuredClone(renderState.actors || state.actors || []);
    for (const override of renderState.terrain_overrides || []) {
      const index = Number(override.index);
      const x = index % state.width;
      const y = Math.floor(index / state.width);
      const cell = state.terrain?.[y]?.[x];
      if (!cell) continue;
      if (override.type) {
        state.terrain[y][x] = {
          elevation: 0, imageUrl: null, label: String(override.type).replaceAll("_", " "),
          layers: null, raised: Boolean(override.raised), type: override.type, underlay: null
        };
      } else {
        cell.raised = Boolean(override.raised);
        for (const layer of cell.layers || []) {
          if (layer?.type === "player_lift") layer.raised = Boolean(override.raised);
        }
      }
    }
    return state;
  }

  async function initialize(levelState, renderState) {
    const modules = window.PlayModules || {};
    const state = applySnapshot(levelState, renderState);
    state.hostFullBleedView = true;
    state.disableHorizontalNeighborFetches = true;
    state.ignoreSavedGemProgress = true;
    app = modules.createPlayCore({
      playData: state,
      canvas,
      playShell: stage,
      playStage: stage,
      mazeFrame: stage,
      enableCameraControls: true
    });
    modules.registerRenderFunctions(app);
    window.__PIXEL_GAME_APP__ = app;
    app.syncPlayLayout();
    app.setupCanvas();
    app.syncCameraTarget(true);
    app.syncNoiseTicker();
    app.syncFloatingFloorTicker();
    await app.preloadImages();
    await Promise.resolve(app.threeRendererReady).catch(() => {});
    app.threeRenderer?.setDebugCameraView?.({
      yaw: (Number(renderState.yaw) || 0) * Math.PI / 2,
      tilt: DEFAULT_CAMERA_TILT,
      zoom: 1,
      mode: "perspective"
    });
    app.render();
  }

  async function replaceState(levelState, renderState) {
    const state = applySnapshot(levelState, renderState);
    app.applyLevelState(state, {
      deferRender: true,
      immediateCamera: true,
      resetHistory: true,
      resetLevelEntry: true
    });
    await Promise.resolve(app.preloadImagesForLevelState?.(state)).catch(() => {});
    await Promise.resolve(app.threeRendererReady).catch(() => {});
    app.render();
  }

  async function showDecision(decision) {
    if (!runId) throw new Error("No run id was supplied to the viewer");
    const serial = ++requestSerial;
    currentDecision = Math.max(1, Number(decision) || 1);
    const snapshot = await json(`/api/runs/${encodeURIComponent(runId)}/viewer-snapshot?decision=${currentDecision}`);
    if (serial !== requestSerial) return;
    const renderState = snapshot.render_state;
    if (!renderState?.level_id) throw new Error("The run has no replayable official render state");
    const levelState = await json(`/api/play/maze/${encodeURIComponent(renderState.level_id)}`);
    if (serial !== requestSerial) return;
    if (!app) await initialize(levelState, renderState);
    else await replaceState(levelState, renderState);
    statusBox.textContent = `decision ${snapshot.decision} · ${String(renderState.level_id).replace("level_", "")}`;
    loading.hidden = true;
    errorBox.hidden = true;
  }

  function cameraView(patch) {
    const renderer = app?.threeRenderer;
    if (!renderer) return;
    renderer.setDebugCameraView({
      yaw: patch.yaw ?? renderer.getDebugCameraYaw?.() ?? 0,
      tilt: patch.tilt ?? renderer.getDebugCameraTilt?.() ?? DEFAULT_CAMERA_TILT,
      zoom: patch.zoom ?? renderer.getDebugCameraZoom?.() ?? 1,
      mode: "perspective",
      animate: true
    });
  }

  document.querySelectorAll("[data-camera]").forEach((button) => {
    button.addEventListener("click", () => {
      const renderer = app?.threeRenderer;
      if (!renderer) return;
      const direction = button.dataset.camera;
      const yaw = renderer.getDebugCameraYaw?.() ?? 0;
      const tilt = renderer.getDebugCameraTilt?.() ?? DEFAULT_CAMERA_TILT;
      if (direction === "left") cameraView({ yaw: yaw - Math.PI / 2 });
      if (direction === "right") cameraView({ yaw: yaw + Math.PI / 2 });
      if (direction === "up") cameraView({ tilt: Math.max(0, tilt - Math.PI / 12) });
      if (direction === "down") cameraView({ tilt: Math.min(Math.PI / 2, tilt + Math.PI / 12) });
    });
  });

  document.querySelectorAll("[data-zoom]").forEach((button) => {
    button.addEventListener("click", () => {
      const renderer = app?.threeRenderer;
      if (!renderer) return;
      const action = button.dataset.zoom;
      const zoom = renderer.getDebugCameraZoom?.() ?? 1;
      if (action === "in") cameraView({ zoom: zoom * 1.2 });
      if (action === "out") cameraView({ zoom: zoom / 1.2 });
      if (action === "reset") cameraView({ yaw: 0, tilt: DEFAULT_CAMERA_TILT, zoom: 1 });
    });
  });

  document.getElementById("viewer-fullscreen").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else stage.requestFullscreen();
  });

  window.addEventListener("message", (event) => {
    if (event.data?.type === "mazebench-viewer-decision") {
      showDecision(event.data.decision).catch(showError);
    }
  });
  window.addEventListener("resize", () => {
    if (!app) return;
    app.syncPlayLayout();
    app.setupCanvas();
    app.syncCameraTarget(true);
    app.render();
  });

  function showError(error) {
    loading.hidden = true;
    errorBox.hidden = false;
    errorBox.textContent = error.message || String(error);
  }

  showDecision(currentDecision).catch(showError);
})();
