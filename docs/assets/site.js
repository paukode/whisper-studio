/* ================================================================
   Whisper Studio Docs - site chrome (classic script)
   Builds header, sidebar, TOC, breadcrumb, prev/next, search,
   theme toggle, code copy buttons, heading anchors, mobile drawer.
   ================================================================ */
(function () {
  "use strict";
  var NAV = window.WS_NAV || [];
  var THEME_KEY = "ws-docs-theme";

  function h(tag, attrs, kids) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "class") e.className = attrs[k];
      else if (k === "html") e.innerHTML = attrs[k];
      else if (k === "text") e.textContent = attrs[k];
      else e.setAttribute(k, attrs[k]);
    }
    (kids || []).forEach(function (c) { if (c) e.appendChild(c); });
    return e;
  }
  var I = {
    sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/></svg>',
    menu: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg>',
    wave: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 12h1M8 8v8M12 4v16M16 8v8M20 12h-1"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>'
  };

  /* ---------- theme ---------- */
  function currentTheme() { return document.documentElement.getAttribute("data-theme") || "light"; }
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(THEME_KEY, t); } catch (e) { }
  }

  /* ---------- current page ---------- */
  var page = (location.pathname.split("/").pop() || "index.html");
  if (page.indexOf(".html") === -1) page = "index.html";
  var flat = [];
  NAV.forEach(function (g) { g.items.forEach(function (it) { flat.push({ t: it.t, h: it.h, d: it.d || "", group: g.title }); }); });
  var here = null, hereIdx = -1;
  flat.forEach(function (f, i) { if (f.h === page) { here = f; hereIdx = i; } });

  function build() {
    var main = document.querySelector("main.doc");
    if (!main) return;

    /* ----- header ----- */
    var brand = h("a", { class: "brand", href: "index.html", "aria-label": "Whisper Studio docs home" }, [
      h("span", { class: "brand__mark", html: I.wave }),
      h("span", { class: "brand__name", text: "Whisper Studio" }),
      h("span", { class: "brand__tag", text: "Docs" })
    ]);

    var searchInput = h("input", { type: "text", placeholder: "Search docs", "aria-label": "Search docs", autocomplete: "off", spellcheck: "false" });
    var searchResults = h("div", { class: "search-results", role: "listbox" });
    var search = h("div", { class: "header-search" }, [
      h("span", { class: "search-ico", html: I.search }),
      searchInput,
      h("kbd", { class: "hint", text: "/" }),
      searchResults
    ]);

    var themeBtn = h("button", { class: "icon-btn", type: "button", "aria-label": "Toggle color theme" });
    themeBtn.innerHTML = currentTheme() === "dark" ? I.sun : I.moon;
    themeBtn.addEventListener("click", function () {
      var t = currentTheme() === "dark" ? "light" : "dark";
      setTheme(t); themeBtn.innerHTML = t === "dark" ? I.sun : I.moon;
    });

    var menuBtn = h("button", { class: "icon-btn menu-toggle", type: "button", "aria-label": "Toggle navigation", html: I.menu });
    menuBtn.addEventListener("click", function () { document.body.classList.toggle("nav-open"); });

    var header = h("header", { class: "site-header" }, [
      menuBtn, brand, h("div", { class: "header-spacer" }), search,
      h("div", { class: "header-actions" }, [themeBtn])
    ]);

    /* ----- sidebar ----- */
    var nav = h("nav", { class: "nav", "aria-label": "Documentation" });
    NAV.forEach(function (g) {
      var grp = h("div", { class: "nav-group" }, [h("div", { class: "nav-group__title", text: g.title })]);
      g.items.forEach(function (it) {
        var a = h("a", { class: "nav-link" + (it.h === page ? " active" : ""), href: it.h, text: it.t });
        if (it.h === page) a.setAttribute("aria-current", "page");
        grp.appendChild(a);
      });
      nav.appendChild(grp);
    });
    var sidebar = h("aside", { class: "sidebar" }, [nav]);

    /* ----- breadcrumb ----- */
    if (here) {
      var crumb = h("div", { class: "breadcrumb" }, [
        h("a", { href: "index.html", text: "Docs" }),
        h("span", { class: "sep", text: "/" }),
        h("span", { text: here.group }),
        h("span", { class: "sep", text: "/" }),
        h("span", { text: here.t })
      ]);
      main.insertBefore(crumb, main.firstChild);
    }

    /* ----- prev/next ----- */
    if (hereIdx >= 0) {
      var prev = hereIdx > 0 ? flat[hereIdx - 1] : null;
      var next = hereIdx < flat.length - 1 ? flat[hereIdx + 1] : null;
      var pn = h("nav", { class: "page-nav", "aria-label": "Pagination" });
      if (prev) pn.appendChild(h("a", { href: prev.h, class: "prev" }, [h("small", { text: "← Previous" }), h("b", { text: prev.t })]));
      else pn.appendChild(h("span"));
      if (next) pn.appendChild(h("a", { href: next.h, class: "next" }, [h("small", { text: "Next →" }), h("b", { text: next.t })]));
      main.appendChild(pn);
    }

    /* ----- TOC ----- */
    var toc = h("aside", { class: "toc" });
    var heads = main.querySelectorAll("h2, h3");
    var tocLinks = [];
    if (heads.length) {
      toc.appendChild(h("div", { class: "toc__title", text: "On this page" }));
      var tnav = h("nav");
      heads.forEach(function (hd) {
        if (!hd.id) hd.id = slug(hd.textContent);
        // anchor link on heading
        var anchor = h("a", { class: "heading-anchor", href: "#" + hd.id, "aria-hidden": "true", text: "#" });
        hd.insertBefore(anchor, hd.firstChild);
        var a = h("a", { href: "#" + hd.id, class: hd.tagName === "H3" ? "lvl-3" : "lvl-2", text: hd.textContent.replace(/^#/, "") });
        tnav.appendChild(a); tocLinks.push({ a: a, el: hd });
      });
      toc.appendChild(tnav);
    }

    /* ----- assemble ----- */
    var scrim = h("div", { class: "scrim" });
    scrim.addEventListener("click", function () { document.body.classList.remove("nav-open"); });
    var layout = h("div", { class: "layout" }, [sidebar, h("div", { class: "main" }, [main]), toc]);
    var footer = h("footer", { class: "site-footer" }, [
      h("span", { html: 'Whisper Studio &middot; local-first AI workspace' }),
      h("span", { html: 'Built to run fully offline &middot; <a href="ref-glossary.html">Glossary</a>' }),
      h("span", { class: "site-footer__legal", html: 'A personal project, not affiliated with, endorsed by, or sponsored by Amazon. Amazon, AWS, and Amazon Bedrock are trademarks of Amazon.com, Inc. or its affiliates, used here for identification only. Whisper Studio calls Amazon Bedrock, a paid AWS service billed to your own AWS account per token.' })
    ]);

    document.body.insertBefore(header, document.body.firstChild);
    document.body.appendChild(scrim);
    document.body.appendChild(layout);
    document.body.appendChild(footer);

    enhanceCode(main);
    wireSearch(searchInput, searchResults);
    wireScrollSpy(tocLinks);
    // scroll active nav link into view
    var activeLink = sidebar.querySelector(".nav-link.active");
    if (activeLink) activeLink.scrollIntoView({ block: "center" });
    // close drawer on nav click (mobile)
    nav.addEventListener("click", function (e) { if (e.target.closest("a")) document.body.classList.remove("nav-open"); });
  }

  /* ---------- helpers ---------- */
  function slug(s) {
    return s.toLowerCase().replace(/^#/, "").trim().replace(/[^\w\s-]/g, "").replace(/\s+/g, "-").replace(/-+/g, "-");
  }

  function enhanceCode(main) {
    main.querySelectorAll("pre").forEach(function (pre) {
      var wrap = pre.parentElement;
      if (!wrap || !wrap.classList.contains("code-block")) {
        wrap = h("div", { class: "code-block" });
        pre.parentNode.insertBefore(wrap, pre);
        wrap.appendChild(pre);
      }
      if (wrap.querySelector(".copy-btn")) return;
      var btn = h("button", { class: "copy-btn", type: "button", "aria-label": "Copy code" });
      btn.innerHTML = I.copy + "<span>Copy</span>";
      btn.addEventListener("click", function () {
        var code = pre.innerText;
        var done = function () { btn.classList.add("done"); btn.querySelector("span").textContent = "Copied"; setTimeout(function () { btn.classList.remove("done"); btn.querySelector("span").textContent = "Copy"; }, 1400); };
        if (navigator.clipboard) navigator.clipboard.writeText(code).then(done, fallbackCopy);
        else fallbackCopy();
        function fallbackCopy() { try { var ta = document.createElement("textarea"); ta.value = code; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); document.body.removeChild(ta); done(); } catch (e) { } }
      });
      wrap.appendChild(btn);
    });
  }

  function wireSearch(input, box) {
    var active = -1, matches = [];
    function close() { box.classList.remove("open"); active = -1; }
    function run() {
      var q = input.value.toLowerCase().trim();
      box.innerHTML = ""; active = -1;
      if (!q) { close(); return; }
      matches = flat.filter(function (f) {
        return f.t.toLowerCase().indexOf(q) > -1 || f.d.toLowerCase().indexOf(q) > -1 || f.group.toLowerCase().indexOf(q) > -1;
      }).slice(0, 8);
      if (!matches.length) { box.innerHTML = '<div class="empty">No matches for "' + escapeHtml(q) + '"</div>'; box.classList.add("open"); return; }
      matches.forEach(function (m, i) {
        var a = h("a", { href: m.h, role: "option" });
        a.innerHTML = "<span>" + escapeHtml(m.t) + "</span><small>" + escapeHtml(m.group + " - " + m.d) + "</small>";
        a.addEventListener("mouseenter", function () { setActive(i); });
        box.appendChild(a);
      });
      box.classList.add("open");
    }
    function setActive(i) {
      var links = box.querySelectorAll("a");
      links.forEach(function (l) { l.classList.remove("active"); });
      active = i;
      if (i >= 0 && links[i]) { links[i].classList.add("active"); }
    }
    input.addEventListener("input", run);
    input.addEventListener("focus", function () { if (input.value.trim()) run(); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); setActive(Math.min(active + 1, matches.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
      else if (e.key === "Enter") { if (active >= 0 && matches[active]) location.href = matches[active].h; else if (matches[0]) location.href = matches[0].h; }
      else if (e.key === "Escape") { close(); input.blur(); }
    });
    document.addEventListener("click", function (e) { if (!e.target.closest(".header-search")) close(); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== input && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
        e.preventDefault(); input.focus();
      }
    });
  }
  function escapeHtml(s) { return s.replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }

  function wireScrollSpy(links) {
    if (!links.length || !("IntersectionObserver" in window)) return;
    var map = {};
    links.forEach(function (l) { map[l.el.id] = l.a; });
    var visible = {};
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { visible[en.target.id] = en.isIntersecting ? en.intersectionRatio : 0; });
      var best = null, bestR = 0;
      links.forEach(function (l) { var r = visible[l.el.id] || 0; if (r > bestR) { bestR = r; best = l.el.id; } });
      links.forEach(function (l) { l.a.classList.toggle("active", l.el.id === best); });
    }, { rootMargin: "-70px 0px -70% 0px", threshold: [0, 0.5, 1] });
    links.forEach(function (l) { obs.observe(l.el); });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", build);
  else build();
})();
