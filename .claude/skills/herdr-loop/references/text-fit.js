() => {
  // The kit's text-fit check (references/text-fit.js). Pass this whole file as the `function` of the browser tool's
  // `browser_evaluate` on a page you look at. It reports text that is cut off by an overflow with no ellipsis (`cut`),
  // drawn past the box that draws it, a border, background or shadow (`outside`), drawn over other text in the same
  // layer (`overlap`), cut by an ellipsis to fewer than 7 characters (`hard_ellipsis`), a field whose placeholder or
  // value is wider than the field (`input_cut`), a short value (24 characters or fewer: a date, a status, a label)
  // wrapping in a table cell, badge, pill or button (`short_wrap`), and horizontal scroll in pixels (`hscroll`).
  // `ellipsis` counts ellipses that leave 7 characters or more, which are intended. Each finding is outlined on the
  // page (red cut, orange outside, magenta overlap, gold hard ellipsis), so take the screenshot after it.
  // What it learned not to report, on the help-desk catalog program (2026-11-18): text in a closed <details>, a sheet or dialog
  // over the page, text an ellipsis already cut, a scroller's own content, and text a pan/zoom canvas clips at its
  // edge (a layer the size of its frame, moved or scaled by a transform, such as react-flow's viewport: panned out of
  // view, not cut). The character count of an ellipsis assumes a monospace face (0.6 em a character); in a
  // proportional face read `boxW` against the text instead.
  const HID = /hidden|clip/;
  const CLIPS = /hidden|clip|auto|scroll/; // a scroller clips what it scrolls
  const visible = (el) => {
    if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true, contentVisibilityAuto: true })) return false;
    const d = el.closest("details:not([open])");
    if (d && !el.closest("summary")) return false;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const sel = (el) => {
    const parts = [];
    for (let a = el, i = 0; a && a !== document.body && i < 3; a = a.parentElement, i++) {
      let s = a.tagName.toLowerCase();
      if (a.id) s += "#" + a.id;
      const c = [...a.classList].slice(0, 2);
      if (c.length) s += "." + c.join(".");
      parts.unshift(s);
    }
    return parts.join(" > ");
  };
  const mark = (el, color) => {
    el.style.outline = `2px solid ${color}`;
    el.style.outlineOffset = "-1px";
  };
  const paint = (c) => c && c !== "transparent" && !/rgba\([^)]*,\s*0\)$/.test(c);
  const drawsBox = (a) => {
    const s = getComputedStyle(a);
    const border = ["Top", "Right", "Bottom", "Left"].some((k) => parseFloat(s[`border${k}Width`]) > 0 && paint(s[`border${k}Color`]));
    return border || paint(s.backgroundColor) || (s.boxShadow && s.boxShadow !== "none");
  };
  const res = { cut: [], outside: [], overlap: [], hard_ellipsis: [], ellipsis: 0, input_cut: [], short_wrap: [], hscroll: 0 };
  const texts = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = w.nextNode())) {
    const t = n.nodeValue.replace(/\s+/g, " ").trim();
    if (t.length < 2) continue;
    const el = n.parentElement;
    if (!el || el.closest("script,style,noscript,template,svg,[hidden]") || !visible(el)) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    const raw = [...range.getClientRects()].filter((r) => r.width > 1 && r.height > 1);
    if (!raw.length) continue;
    // the part actually drawn: each line box cut to every ancestor that hides overflow
    let drawn = raw.map((r) => ({ left: r.left, right: r.right, top: r.top, bottom: r.bottom }));
    let hidden = null, layer = null;
    const moved = []; // transformed elements between the text and the box that hides it
    for (let a = el; a && a !== document.documentElement; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (HID.test(s.overflowX + s.overflowY)) hidden = hidden || a;
      else if (!hidden && s.transform && s.transform !== "none") moved.push(a);
      if (CLIPS.test(s.overflowX + s.overflowY)) {
        const b = a.getBoundingClientRect();
        drawn = drawn.map((r) => ({
          left: CLIPS.test(s.overflowX) ? Math.max(r.left, b.left) : r.left,
          right: CLIPS.test(s.overflowX) ? Math.min(r.right, b.right) : r.right,
          top: CLIPS.test(s.overflowY) ? Math.max(r.top, b.top) : r.top,
          bottom: CLIPS.test(s.overflowY) ? Math.min(r.bottom, b.bottom) : r.bottom,
        }));
      }
      if (!layer && (s.position === "fixed" || s.position === "sticky" || a.matches("dialog,[role=dialog],[aria-modal=true],[popover]"))) layer = a;
    }
    drawn = drawn.map((r) => ({ ...r, width: r.right - r.left, height: r.bottom - r.top })).filter((r) => r.width > 1 && r.height > 1);
    // a pan/zoom canvas: a layer the size of the frame that hides it, moved or scaled by a transform
    const panned = !!hidden && moved.some((m) => Math.abs(m.offsetWidth - hidden.clientWidth) <= 2 && Math.abs(m.offsetHeight - hidden.clientHeight) <= 2);
    texts.push({ t, el, raw, drawn, hidden, layer, panned });
  }
  const seenBox = new Set();
  for (const x of texts) {
    const es = getComputedStyle(x.el);
    // 1. cut: text runs past a box that hides overflow (a canvas's edge is no such box for what is panned under it)
    if (x.hidden && !x.panned && !seenBox.has(x.hidden)) {
      const s = getComputedStyle(x.hidden);
      const b = x.hidden.getBoundingClientRect();
      if (b.width > 2 && b.height > 2) {
        const cx = HID.test(s.overflowX) && x.raw.some((r) => r.right > b.right + 1.5 || r.left < b.left - 1.5);
        const cy = HID.test(s.overflowY) && x.raw.some((r) => r.bottom > b.bottom + 1.5 || r.top < b.top - 1.5);
        if (cx || cy) {
          seenBox.add(x.hidden);
          const ell = [es, s].some((q) => q.textOverflow === "ellipsis" || (q.webkitLineClamp && q.webkitLineClamp !== "none"));
          const full = Math.max(...x.raw.map((r) => r.right)) - Math.min(...x.raw.map((r) => r.left));
          const shown = Math.max(0, b.width);
          const row = { text: x.t.slice(0, 90), el: sel(x.el), boxW: Math.round(shown), fullW: Math.round(full) };
          if (!ell) {
            res.cut.push(row);
            mark(x.hidden, "red");
          } else {
            res.ellipsis++;
            const chars = Math.floor(shown / (parseFloat(es.fontSize) * 0.6)) - 1; // the face is monospace
            if (chars < 7 && x.t.length > chars + 2) {
              res.hard_ellipsis.push({ ...row, shows: x.t.slice(0, Math.max(chars, 0)) + "…" });
              mark(x.hidden, "gold");
            }
          }
        }
      }
    }
    // 2. outside: drawn text past the edge of an ancestor that draws a box (border, background or shadow)
    for (let a = x.el.parentElement, d = 0; a && a !== document.body && d < 12; a = a.parentElement, d++) {
      if (!drawsBox(a)) continue;
      const b = a.getBoundingClientRect();
      const out = x.drawn.some((r) => r.right > b.right + 1.5 || r.left < b.left - 1.5 || r.bottom > b.bottom + 1.5 || r.top < b.top - 1.5);
      if (out) {
        res.outside.push({ text: x.t.slice(0, 90), el: sel(x.el), box: sel(a), by: Math.round(Math.max(...x.drawn.map((r) => Math.max(r.right - b.right, b.left - r.left, r.bottom - b.bottom, b.top - r.top)))) });
        mark(a, "orange");
      }
      break; // only the nearest box that draws
    }
    // 3. short value wraps (a date, a status) in a table cell or a pill
    if (x.raw.length > 1 && x.t.length <= 24 && x.el.closest("td,th,[class*=badge],[class*=pill],button")) {
      res.short_wrap.push({ text: x.t, el: sel(x.el), lines: x.raw.length });
    }
  }
  // 4. text over text, in the same layer
  const flat = [];
  texts.forEach((x, i) => x.drawn.forEach((r) => flat.push({ i, r })));
  const pairs = new Set();
  for (let a = 0; a < flat.length; a++) {
    for (let b = a + 1; b < flat.length; b++) {
      const A = flat[a], B = flat[b];
      if (A.i === B.i) continue;
      const ix = Math.min(A.r.right, B.r.right) - Math.max(A.r.left, B.r.left);
      const iy = Math.min(A.r.bottom, B.r.bottom) - Math.max(A.r.top, B.r.top);
      if (ix <= 1 || iy <= 1) continue;
      const area = ix * iy, small = Math.min(A.r.width * A.r.height, B.r.width * B.r.height);
      if (area < 12 || area < 0.15 * small) continue;
      const ta = texts[A.i], tb = texts[B.i];
      if (ta.el.contains(tb.el) || tb.el.contains(ta.el) || ta.layer !== tb.layer) continue;
      const key = A.i + ":" + B.i;
      if (pairs.has(key)) continue;
      pairs.add(key);
      res.overlap.push({ a: ta.t.slice(0, 60), b: tb.t.slice(0, 60), ea: sel(ta.el), eb: sel(tb.el), px: Math.round(area) });
      mark(ta.el, "magenta");
      mark(tb.el, "magenta");
    }
  }
  // 5. a field's placeholder or value wider than the field
  const ctx = document.createElement("canvas").getContext("2d");
  for (const f of document.querySelectorAll("input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=range]):not([type=color]):not([type=file])")) {
    if (!visible(f)) continue;
    const s = getComputedStyle(f);
    const txt = f.tagName === "SELECT" ? f.selectedOptions[0]?.textContent?.trim() || "" : f.value || f.placeholder || "";
    if (!txt) continue;
    ctx.font = `${s.fontWeight} ${s.fontSize} ${s.fontFamily}`;
    const need = ctx.measureText(txt).width;
    const room = f.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight) - (f.tagName === "SELECT" ? 18 : 0);
    if (need > room + 2) {
      res.input_cut.push({ text: txt.slice(0, 60), el: sel(f), need: Math.round(need), room: Math.round(room) });
      mark(f, "red");
    }
  }
  res.hscroll = Math.max(0, document.documentElement.scrollWidth - window.innerWidth);
  const counts = Object.fromEntries(Object.entries(res).map(([k, v]) => [k, Array.isArray(v) ? v.length : v]));
  for (const k of Object.keys(res)) if (Array.isArray(res[k])) res[k] = res[k].slice(0, 15);
  return { width: window.innerWidth, counts, ...res };
}
