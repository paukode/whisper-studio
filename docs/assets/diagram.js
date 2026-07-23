/* ================================================================
   Whisper Studio Docs - Interactive diagram engine (classic script)
   Exposes window.WSDiagram.mount(elementId, config)
   Click a node -> its whole downstream data-flow path lights up & flows.
   ================================================================ */
(function () {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Group hues - coherent with the app's category palette. The *active glow*
  // is always the gold accent; group hue only tints the node's accent bar + zone.
  var HUES = {
    browser: "#0284c7", transport: "#0d9488", server: "#c4841d",
    tools: "#7c3aed", agents: "#9333ea", security: "#dc2626",
    local: "#db2777", persist: "#0369a1", external: "#9a8f80",
    model: "#b45309", media: "#0d9488", index: "#7c3aed", "default": "#c4841d"
  };
  function hue(g) { return HUES[g] || HUES["default"]; }

  function svgEl(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function icon(paths, attrs) {
    var a = attrs || {};
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' +
      (a.sw || 2) + '" stroke-linecap="round" stroke-linejoin="round">' + paths + '</svg>';
  }

  // Intersection of the segment (center -> target) with a node's rectangle border.
  function borderPoint(n, tx, ty) {
    var cx = n.x + n.w / 2, cy = n.y + n.h / 2;
    var dx = tx - cx, dy = ty - cy;
    if (dx === 0 && dy === 0) return { x: cx, y: cy };
    var hw = n.w / 2, hh = n.h / 2;
    var sx = dx !== 0 ? hw / Math.abs(dx) : Infinity;
    var sy = dy !== 0 ? hh / Math.abs(dy) : Infinity;
    var s = Math.min(sx, sy);
    return { x: cx + dx * s, y: cy + dy * s };
  }
  function cubicPoint(p0, c0, c1, p1, t) {
    var u = 1 - t;
    var a = u * u * u, b = 3 * u * u * t, c = 3 * u * t * t, d = t * t * t;
    return { x: a * p0.x + b * c0.x + c * c1.x + d * p1.x, y: a * p0.y + b * c0.y + c * c1.y + d * p1.y };
  }

  // Grid auto-layout: nodes/zones may use {col,row} (+ zone {cols,rows})
  // instead of absolute {x,y}. Keeps configs terse and overlap-free.
  function normalize(cfg) {
    var g = cfg.grid || {};
    var NW = g.nodeW || 176, NH = g.nodeH || 60, GX = g.gapX || 50, GY = g.gapY || 40;
    var MX = g.marginX || 40, MY = g.marginY || ((cfg.zones && cfg.zones.length) ? 44 : 30);
    var maxX = 0, maxY = 0;
    (cfg.nodes || []).forEach(function (n) {
      if (n.w == null) n.w = NW;
      if (n.h == null) n.h = n.sub ? NH : NH - 10;
      if (n.x == null && n.col != null) n.x = MX + n.col * (NW + GX);
      if (n.y == null && n.row != null) n.y = MY + n.row * (NH + GY);
      if (n.x != null) maxX = Math.max(maxX, n.x + n.w);
      if (n.y != null) maxY = Math.max(maxY, n.y + n.h);
    });
    (cfg.zones || []).forEach(function (z) {
      if (z.x == null && z.col != null) {
        var zp = 13, zt = 24, cols = z.cols || 1, rows = z.rows || 1;
        z.x = MX + z.col * (NW + GX) - zp;
        z.y = MY + z.row * (NH + GY) - zt;
        z.w = cols * NW + (cols - 1) * GX + zp * 2;
        z.h = (rows - 1) * (NH + GY) + NH + zt + zp;
      }
      if (z.x != null) { maxX = Math.max(maxX, z.x + z.w); maxY = Math.max(maxY, z.y + z.h); }
    });
    if (cfg.w == null) cfg.w = Math.round(maxX + MX);
    if (cfg.h == null) cfg.h = Math.round(maxY + MY);
    return cfg;
  }

  function render(container, cfg, opts) {
    opts = opts || {};
    normalize(cfg);
    container.classList.add("diagram");
    container.innerHTML = "";
    var byId = {};
    (cfg.nodes || []).forEach(function (n) {
      n.w = n.w || 176; n.h = n.h || (n.sub ? 60 : 50);
      byId[n.id] = n;
    });

    // adjacency (downstream)
    var adj = {};
    (cfg.edges || []).forEach(function (e, i) {
      e._i = i;
      (adj[e.from] = adj[e.from] || []).push(e);
    });

    /* ---- toolbar ---- */
    var bar = document.createElement("div");
    bar.className = "diagram__bar";
    var hint = document.createElement("div");
    hint.className = "diagram__hint";
    hint.innerHTML = icon('<path d="M9 11.5 3.5 9 20.5 3.5 15 20.5l-2.5-5.5-3.5-3.5Z"/>') +
      "<span>Click a box to trace its downstream path</span>";
    bar.appendChild(hint);

    var legend = document.createElement("div");
    legend.className = "diagram__legend";
    var seen = {};
    (cfg.nodes || []).forEach(function (n) {
      var g = n.group || "default";
      if (seen[g]) return; seen[g] = 1;
      var label = (cfg.groups && cfg.groups[g] && cfg.groups[g].label) || (g[0].toUpperCase() + g.slice(1));
      var s = document.createElement("span");
      s.className = "lg";
      s.innerHTML = '<i style="background:' + hue(g) + '"></i>' + label;
      legend.appendChild(s);
    });
    if (Object.keys(seen).length > 1) bar.appendChild(legend);

    var spacer = document.createElement("div"); spacer.className = "diagram__spacer"; bar.appendChild(spacer);
    var reset = document.createElement("button");
    reset.className = "diagram__reset"; reset.type = "button";
    reset.innerHTML = icon('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>', { sw: 2 }) + "Reset";
    bar.appendChild(reset);
    if (opts.expandable !== false) {
      var expand = document.createElement("button");
      expand.className = "diagram__expand"; expand.type = "button";
      expand.setAttribute("aria-label", "Expand diagram to fullscreen");
      expand.innerHTML = icon('<path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M3 16v3a2 2 0 0 0 2 2h3"/>') + "Expand";
      expand.addEventListener("click", function (e) { e.stopPropagation(); openModal(cfg); });
      bar.appendChild(expand);
    }
    container.appendChild(bar);

    /* ---- svg canvas ---- */
    var svg = svgEl("svg", {
      "class": "diagram__canvas",
      viewBox: "0 0 " + cfg.w + " " + cfg.h,
      preserveAspectRatio: "xMidYMid meet",
      role: "group",
      "aria-label": cfg.title || "Architecture diagram"
    });
    container.appendChild(svg);

    var tip = document.createElement("div");
    tip.className = "diagram__tip";
    container.appendChild(tip);

    // zones
    (cfg.zones || []).forEach(function (z) {
      var g = svgEl("g", { "class": "zone" }, svg);
      g.style.setProperty("--zone-color", hue(z.group));
      svgEl("rect", { "class": "zone__box", x: z.x, y: z.y, width: z.w, height: z.h, rx: 14 }, g);
      if (z.label) svgEl("text", { "class": "zone__label", x: z.x + 12, y: z.y + 18 }, g).textContent = z.label;
    });

    // edges
    var edgeEls = [];
    (cfg.edges || []).forEach(function (e) {
      var A = byId[e.from], B = byId[e.to];
      if (!A || !B) return;
      var ac = { x: A.x + A.w / 2, y: A.y + A.h / 2 }, bc = { x: B.x + B.w / 2, y: B.y + B.h / 2 };
      var p0 = borderPoint(A, bc.x, bc.y);
      var p1 = borderPoint(B, ac.x, ac.y);
      var dx = p1.x - p0.x, dy = p1.y - p0.y;
      var c0, c1;
      if (Math.abs(dx) >= Math.abs(dy)) {
        c0 = { x: p0.x + dx * 0.42, y: p0.y }; c1 = { x: p1.x - dx * 0.42, y: p1.y };
      } else {
        c0 = { x: p0.x, y: p0.y + dy * 0.42 }; c1 = { x: p1.x, y: p1.y - dy * 0.42 };
      }
      // retract end slightly for the arrowhead
      var ang = Math.atan2(p1.y - c1.y, p1.x - c1.x);
      var gap = 7;
      var end = { x: p1.x - Math.cos(ang) * gap, y: p1.y - Math.sin(ang) * gap };
      var d = "M" + p0.x + " " + p0.y + " C" + c0.x + " " + c0.y + " " + c1.x + " " + c1.y + " " + end.x + " " + end.y;

      var g = svgEl("g", { "class": "edge" }, svg);
      svgEl("path", { "class": "edge__line", d: d }, g);
      svgEl("path", { "class": "edge__flow", d: d }, g);
      // arrowhead
      var L = 9, spread = 0.42;
      var b1 = { x: p1.x - Math.cos(ang - spread) * L, y: p1.y - Math.sin(ang - spread) * L };
      var b2 = { x: p1.x - Math.cos(ang + spread) * L, y: p1.y - Math.sin(ang + spread) * L };
      svgEl("polygon", { "class": "edge__arrow", points: p1.x + "," + p1.y + " " + b1.x + "," + b1.y + " " + b2.x + "," + b2.y }, g);
      if (e.label) {
        var m = cubicPoint(p0, c0, c1, end, 0.5);
        var bg = svgEl("rect", { "class": "edge__label-bg", rx: 4 }, g);
        var t = svgEl("text", { "class": "edge__label", x: m.x, y: m.y + 3, "text-anchor": "middle" }, g);
        t.textContent = e.label;
        var bb = t.getBBox ? null : null;
        // size bg after text is in DOM
        setTimeout(function () { try { var r = t.getBBox(); bg.setAttribute("x", r.x - 4); bg.setAttribute("y", r.y - 1); bg.setAttribute("width", r.width + 8); bg.setAttribute("height", r.height + 2); } catch (_) { } }, 0);
      }
      edgeEls.push({ e: e, g: g });
    });

    // nodes
    var nodeEls = {};
    var order = [];
    (cfg.nodes || []).forEach(function (n) {
      var g = svgEl("g", {
        "class": "node" + (n.kind ? " node--" + n.kind : ""),
        tabindex: "0", role: "button",
        "aria-label": (n.label || "").replace(/\n/g, " ") + (n.desc ? ". " + n.desc : "")
      }, svg);
      g.style.setProperty("--node-color", hue(n.group));
      svgEl("rect", { "class": "node__box", x: n.x, y: n.y, width: n.w, height: n.h, rx: 11 }, g);
      svgEl("rect", { "class": "node__accent", x: n.x + 1.5, y: n.y + 8, width: 4, height: n.h - 16, rx: 2 }, g);
      var cx = n.x + n.w / 2;
      var lines = String(n.label || "").split("\n");
      var lh = 15;
      var baseY = n.y + n.h / 2 - (lines.length - 1) * lh / 2 - (n.sub ? 5 : 0);
      var txt = svgEl("text", { "class": "node__label", x: cx, y: baseY }, g);
      lines.forEach(function (ln, i) {
        var ts = svgEl("tspan", { x: cx, dy: i === 0 ? 0 : lh }, txt);
        ts.textContent = ln;
      });
      if (n.sub) {
        svgEl("text", { "class": "node__sub", x: cx, y: n.y + n.h - 9 }, g).textContent = n.sub;
      }
      nodeEls[n.id] = g;
      order.push(n.id);

      g.addEventListener("click", function (ev) { ev.stopPropagation(); toggle(n.id); });
      g.addEventListener("mouseenter", function (ev) { showTip(n, ev); });
      g.addEventListener("mousemove", function (ev) { moveTip(ev); });
      g.addEventListener("mouseleave", hideTip);
      g.addEventListener("focus", function (ev) { showTip(n, ev); });
      g.addEventListener("blur", hideTip);
      g.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(n.id); }
        else if (ev.key === "Escape") { clear(); }
        else if (ev.key === "ArrowRight" || ev.key === "ArrowDown") { ev.preventDefault(); focusRel(n.id, 1); }
        else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") { ev.preventDefault(); focusRel(n.id, -1); }
      });
    });

    var activeId = null;
    function focusRel(id, dir) {
      var i = order.indexOf(id);
      var j = (i + dir + order.length) % order.length;
      nodeEls[order[j]].focus();
    }

    function clear() {
      activeId = null;
      container.classList.remove("is-active");
      Object.keys(nodeEls).forEach(function (id) { nodeEls[id].classList.remove("is-lit", "is-source"); });
      edgeEls.forEach(function (o) { o.g.classList.remove("is-lit"); });
    }
    function toggle(id) { if (activeId === id) clear(); else activate(id); }

    function activate(id) {
      clear();
      activeId = id;
      container.classList.add("is-active");
      nodeEls[id].classList.add("is-source");
      // BFS downstream
      var q = [id], seen = {}; seen[id] = 1;
      while (q.length) {
        var cur = q.shift();
        (adj[cur] || []).forEach(function (e) {
          // light the traversed edge
          edgeEls.forEach(function (o) { if (o.e._i === e._i) o.g.classList.add("is-lit"); });
          if (!seen[e.to]) { seen[e.to] = 1; if (nodeEls[e.to]) nodeEls[e.to].classList.add("is-lit"); q.push(e.to); }
        });
      }
    }

    /* ---- tooltip ---- */
    function showTip(n, ev) {
      if (!n.desc && !n.sub) { return; }
      tip.innerHTML = "<b>" + (n.label || "").replace(/\n/g, " ") + "</b>" + (n.desc ? "<br>" + n.desc : "");
      tip.classList.add("show");
      moveTip(ev);
    }
    function moveTip(ev) {
      var r = container.getBoundingClientRect();
      var x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
      var tw = tip.offsetWidth, th = tip.offsetHeight;
      if (x + tw > r.width - 6) x = r.width - tw - 6;
      if (y + th > r.height - 6) y = ev.clientY - r.top - th - 10;
      tip.style.left = Math.max(6, x) + "px";
      tip.style.top = Math.max(6, y) + "px";
    }
    function hideTip() { tip.classList.remove("show"); }

    svg.addEventListener("click", clear);
    reset.addEventListener("click", clear);
    container.addEventListener("keydown", function (ev) { if (ev.key === "Escape") clear(); });

    // Optional: auto-trace a node on load (config.autofocus)
    if (cfg.autofocus && byId[cfg.autofocus]) { /* left inactive by default for calm first paint */ }
  }

  /* ---- fullscreen zoom modal ("pop out") ---- */
  var _modal = null, _modalEsc = null;
  function closeModal() {
    if (!_modal) return;
    if (_modal._cleanup) _modal._cleanup();
    _modal.remove(); _modal = null;
    if (_modalEsc) { document.removeEventListener("keydown", _modalEsc); _modalEsc = null; }
    document.body.style.overflow = "";
  }
  function openModal(cfg) {
    closeModal();
    function ctrlBtn(paths, label) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "diagram-modal__btn";
      b.setAttribute("aria-label", label); b.innerHTML = icon(paths);
      return b;
    }
    var overlay = document.createElement("div");
    overlay.className = "diagram-modal";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", (cfg.title || "Diagram") + " (expanded)");

    var title = document.createElement("div");
    title.className = "diagram-modal__title";
    title.textContent = cfg.title || "Diagram";

    var hint = document.createElement("span");
    hint.className = "diagram-modal__hint";
    hint.textContent = "Click a box to trace · scroll to pan";

    var ctrls = document.createElement("div");
    ctrls.className = "diagram-modal__ctrls";
    var zoomOut = ctrlBtn('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2M8 11h6"/>', "Zoom out");
    var zoomIn = ctrlBtn('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2M11 8v6M8 11h6"/>', "Zoom in");
    var fit = ctrlBtn('<path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M3 16v3a2 2 0 0 0 2 2h3"/>', "Fit to screen");
    var close = ctrlBtn('<path d="M18 6 6 18M6 6l12 12"/>', "Close");
    ctrls.appendChild(hint); ctrls.appendChild(zoomOut); ctrls.appendChild(zoomIn); ctrls.appendChild(fit); ctrls.appendChild(close);

    var bar = document.createElement("div");
    bar.className = "diagram-modal__bar";
    bar.appendChild(title); bar.appendChild(ctrls);

    var stage = document.createElement("div");
    stage.className = "diagram-modal__stage";
    var content = document.createElement("div");
    content.className = "diagram-modal__content";
    var diag = document.createElement("div");
    content.appendChild(diag);
    stage.appendChild(content);

    overlay.appendChild(bar);
    overlay.appendChild(stage);
    document.body.appendChild(overlay);
    document.body.style.overflow = "hidden";

    // Render an independent, fully interactive copy (no nested Expand button).
    render(diag, cfg, { expandable: false });

    var zoom = 1; // 1 = fit the stage width
    function applyZoom() {
      var stageW = Math.max(240, stage.clientWidth - 48);
      diag.style.width = Math.round(stageW * zoom) + "px";
    }
    function setZoom(z) { zoom = Math.min(4, Math.max(0.5, z)); applyZoom(); }
    applyZoom();

    zoomIn.addEventListener("click", function () { setZoom(zoom * 1.25); });
    zoomOut.addEventListener("click", function () { setZoom(zoom / 1.25); });
    fit.addEventListener("click", function () { setZoom(1); stage.scrollTo(0, 0); });
    close.addEventListener("click", closeModal);
    overlay.addEventListener("click", function (e) { if (e.target === overlay || e.target === stage) closeModal(); });

    _modalEsc = function (e) {
      if (e.key === "Escape") closeModal();
      else if (e.key === "+" || e.key === "=") { e.preventDefault(); setZoom(zoom * 1.25); }
      else if (e.key === "-" || e.key === "_") { e.preventDefault(); setZoom(zoom / 1.25); }
      else if (e.key === "0") { setZoom(1); }
    };
    document.addEventListener("keydown", _modalEsc);
    window.addEventListener("resize", applyZoom);
    overlay._cleanup = function () { window.removeEventListener("resize", applyZoom); };

    if (window.requestAnimationFrame) requestAnimationFrame(function () { overlay.classList.add("open"); });
    else overlay.classList.add("open");
    close.focus();
    _modal = overlay;
  }

  /* ---- public API with DOM-ready queue ---- */
  var queue = [];
  var ready = false;
  function flush() { ready = true; queue.forEach(function (q) { doMount(q.id, q.cfg); }); queue = []; }
  function doMount(id, cfg) {
    var elm = typeof id === "string" ? document.getElementById(id) : id;
    if (!elm) { if (window.console) console.warn("[WSDiagram] no element:", id); return; }
    try { render(elm, cfg); } catch (err) { if (window.console) console.error("[WSDiagram] render failed", err); }
  }
  window.WSDiagram = {
    mount: function (id, cfg) { if (ready) doMount(id, cfg); else queue.push({ id: id, cfg: cfg }); },
    render: render
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", flush);
  else flush();
})();
