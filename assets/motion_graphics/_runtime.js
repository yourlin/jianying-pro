/*
 * 动态图形模板公共运行时 (由 scripts/motion_graphics.py 内联进每个模板)。
 *
 * 约定 (rules/web-vfx.md 的录制协议):
 *   - window.MG_CONFIG: Python 端校验过的配置 {template, width, height, duration, hold, background, theme, data}
 *   - 自动播放: 按真实时间播放, 到 duration 后设 window.animationFinished = true (web_recorder 录制模式)
 *   - 逐帧模式: CFG.manual = true 时不自动播放, 由录制端调用 window.__mgSeek(t) 渲染第 t 秒的画面
 *     (window.__mgReady = true 表示可以开始), 帧率稳定且支持透明背景
 * 模板只写 MG.run(function setup(cfg) { ...; return function update(t) { ... }; })。
 * 所有文字都用 textContent 写入, 不解析 HTML。
 */
(function () {
  "use strict";
  var CFG = window.MG_CONFIG || {};
  var W = CFG.width || 1920;
  var H = CFG.height || 1080;
  var NS = "http://www.w3.org/2000/svg";
  var theme = CFG.theme || {};
  var MG = (window.MG = {
    cfg: CFG,
    data: CFG.data || {},
    W: W,
    H: H,
    U: (Math.min(W, H) / 1080) * (H > W * 1.05 ? 1.25 : 1), // 字号/线宽按短边缩放, 竖屏放大 (手机上看)
    portrait: H > W * 1.05,
    theme: theme,
    duration: CFG.duration || 5,
    hold: CFG.hold == null ? 1 : CFG.hold,
  });
  MG.A = Math.max(0.3, MG.duration - MG.hold); // 动画时长 (之后静止 hold 秒)
  MG.pad = Math.round(Math.min(W, H) * 0.08);

  MG.clamp = function (v, a, b) {
    return v < a ? a : v > b ? b : v;
  };
  MG.lerp = function (a, b, k) {
    return a + (b - a) * k;
  };
  MG.ease = {
    linear: function (k) {
      return k;
    },
    outQuad: function (k) {
      return 1 - (1 - k) * (1 - k);
    },
    outCubic: function (k) {
      return 1 - Math.pow(1 - k, 3);
    },
    inOutCubic: function (k) {
      return k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
    },
    outBack: function (k) {
      var c1 = 1.70158,
        c3 = c1 + 1;
      return 1 + c3 * Math.pow(k - 1, 3) + c1 * Math.pow(k - 1, 2);
    },
    outExpo: function (k) {
      return k >= 1 ? 1 : 1 - Math.pow(2, -10 * k);
    },
  };
  /* t 在 [start, start+dur] 内的进度 0~1 (带缓动) */
  MG.prog = function (t, start, dur, ease) {
    var k = dur > 0 ? MG.clamp((t - start) / dur, 0, 1) : t >= start ? 1 : 0;
    return (ease || MG.ease.outCubic)(k);
  };
  MG.fmt = function (v, decimals, sep) {
    var d = decimals || 0;
    var s = Math.abs(v).toFixed(d);
    if (sep !== false) {
      var parts = s.split(".");
      parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      s = parts.join(".");
    }
    return (v < 0 ? "-" : "") + s;
  };
  MG.color = function (i, explicit) {
    if (explicit) return explicit;
    var pal = theme.palette || [theme.primary || "#4F8CFF"];
    return pal[i % pal.length];
  };

  MG.el = function (tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    for (var k in attrs || {}) {
      if (attrs[k] != null) e.setAttribute(k, attrs[k]);
    }
    (parent || MG.root).appendChild(e);
    return e;
  };
  MG.text = function (str, attrs, parent) {
    var a = {
      fill: theme.text,
      "font-family": theme.font,
      "font-weight": 600,
      "dominant-baseline": "middle",
    };
    for (var k in attrs || {}) a[k] = attrs[k];
    var e = MG.el("text", a, parent);
    e.textContent = str == null ? "" : String(str);
    return e;
  };
  /* 文字超过 maxWidth 时缩小字号 */
  MG.fit = function (textEl, maxWidth) {
    try {
      var w = textEl.getComputedTextLength();
      if (w > maxWidth && w > 0) {
        var fs = parseFloat(textEl.getAttribute("font-size")) || 32;
        textEl.setAttribute("font-size", Math.max(8, (fs * maxWidth) / w).toFixed(1));
      }
    } catch (e) {
      /* 未挂载时忽略 */
    }
    return textEl;
  };
  /* 文字后面加半透明圆角底, 保证在任何画面上可读; 返回 {g, rect, text} */
  MG.pill = function (str, x, y, fs, opts) {
    opts = opts || {};
    var g = MG.el("g", {}, opts.parent);
    var rect = MG.el("rect", { rx: fs * 0.45, fill: opts.bg || theme.label_bg }, g);
    var t = MG.text(str, { x: x, y: y, "font-size": fs, "text-anchor": opts.anchor || "middle", fill: opts.color || theme.text }, g);
    try {
      var bb = t.getBBox();
      var px = fs * 0.5,
        py = fs * 0.25;
      rect.setAttribute("x", bb.x - px);
      rect.setAttribute("y", bb.y - py);
      rect.setAttribute("width", bb.width + 2 * px);
      rect.setAttribute("height", bb.height + 2 * py);
    } catch (e) {
      /* ignore */
    }
    return { g: g, rect: rect, text: t };
  };
  /* 页面标题 (可选), 返回标题占用的高度 */
  MG.title = function (str) {
    if (!str) return 0;
    var fs = Math.round(64 * MG.U);
    var t = MG.text(str, { x: W / 2, y: MG.pad + fs * 0.6, "font-size": fs, "text-anchor": "middle", "font-weight": 800 });
    MG.fit(t, W - 2 * MG.pad);
    MG._title = t;
    return fs * 1.9;
  };

  function init() {
    var de = document.documentElement;
    de.style.margin = document.body.style.margin = "0";
    de.style.overflow = document.body.style.overflow = "hidden";
    var bg = CFG.background || "transparent";
    de.style.background = document.body.style.background = bg === "transparent" ? "transparent" : bg;
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("width", W);
    svg.setAttribute("height", H);
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.style.display = "block";
    document.body.appendChild(svg);
    MG.svg = svg;
    MG.defs = MG.el("defs", {}, svg);
    if (bg === "transparent") {
      /* 透明叠加时给内容加柔和阴影, 浅色画面上也看得清 */
      var f = MG.el("filter", { id: "mg-shadow", x: "-20%", y: "-20%", width: "140%", height: "140%" }, MG.defs);
      MG.el("feDropShadow", { dx: 0, dy: 2 * MG.U, stdDeviation: 4 * MG.U, "flood-color": "#000", "flood-opacity": 0.45 }, f);
    }
    MG.root = MG.el("g", bg === "transparent" ? { filter: "url(#mg-shadow)" } : {}, svg);
  }

  MG.run = function (setup) {
    init();
    var update = setup(CFG) || function () {};
    var title = MG._title;
    var draw = function (t) {
      t = MG.clamp(t, 0, MG.duration);
      if (title) {
        var k = MG.prog(t, 0, 0.5);
        title.setAttribute("opacity", k);
        title.setAttribute("transform", "translate(0," + ((1 - k) * 20 * MG.U).toFixed(2) + ")");
      }
      update(t);
    };
    window.__mgSeek = function (t) {
      draw(+t || 0);
      return true;
    };
    draw(0);
    window.__mgReady = true;
    if (CFG.manual) return;
    var t0 = null;
    var loop = function (now) {
      if (t0 === null) t0 = now;
      var t = (now - t0) / 1000;
      draw(t);
      if (t >= MG.duration) {
        window.animationFinished = true;
        return;
      }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  };
})();
