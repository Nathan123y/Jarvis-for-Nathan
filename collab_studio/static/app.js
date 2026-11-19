// Collab Studio: progressive enhancement only. Every form still works without this file.
(function () {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // reveal-on-scroll
  const io = "IntersectionObserver" in window ? new IntersectionObserver((es) => es.forEach((e) => {
    if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }), { threshold: 0.12 }) : null;
  $$(".reveal").forEach((el) => io ? io.observe(el) : el.classList.add("in"));

  // gallery tap-to-flip (hover handles desktop)
  $$(".card-flip").forEach((c) => {
    const t = () => c.classList.toggle("flipped");
    c.addEventListener("click", t);
    c.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); t(); } });
  });

  // front/back viewer
  $$("[data-viewer]").forEach((v) => {
    $$(".tabs button", v).forEach((b) => b.addEventListener("click", () => {
      $$(".tabs button", v).forEach((x) => x.classList.toggle("on", x === b));
      const back = b.dataset.view === "back";
      $("[data-front]", v).hidden = back;
      $("[data-back]", v).hidden = !back;
    }));
  });

  // zoom lightbox
  const lb = $("#lightbox");
  if (lb) {
    const img = $("img", lb);
    const close = () => { lb.hidden = true; img.classList.remove("big"); };
    $$("[data-zoom]").forEach((z) => z.addEventListener("click", () => {
      const shown = $$("img", z).find((i) => !i.hidden);
      if (!shown) return;
      img.src = shown.src; img.alt = shown.alt; lb.hidden = false; $(".lb-close", lb).focus();
    }));
    img.addEventListener("click", (e) => { e.stopPropagation(); img.classList.toggle("big"); });
    lb.addEventListener("click", close);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !lb.hidden) close(); });
  }

  // "Request Changes" toggles
  $$("[data-toggle]").forEach((b) => b.addEventListener("click", () => {
    const f = document.getElementById(b.dataset.toggle);
    if (f) { f.hidden = !f.hidden; if (!f.hidden) $("textarea", f).focus(); }
  }));

  // progress polling while a design job runs
  const poll = $("[data-poll]");
  if (poll && poll.dataset.jobStatus !== "failed") {
    const bar = $(".bar span", poll), stage = $(".stage", poll);
    const tick = () => fetch(poll.dataset.poll, { credentials: "same-origin" }).then((r) => r.json()).then((d) => {
      if (!d.job || d.job.status === "succeeded" || d.job.status === "failed") { location.reload(); return; }
      bar.style.width = d.job.progress + "%"; stage.textContent = d.job.stage || "In the queue";
      setTimeout(tick, 3000);
    }).catch(() => setTimeout(tick, 6000));
    setTimeout(tick, 3000);
  }

  // request builder: colour chips, reference previews, client checks, summary step
  const form = $("#builder");
  if (form) {
    const colors = $("#colors");
    $$(".chip[data-color]").forEach((c) => c.addEventListener("click", () => {
      const cur = colors.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (!cur.includes(c.dataset.color)) cur.push(c.dataset.color);
      colors.value = cur.join(", ");
    }));
    const refs = $("#references"), previews = $("#ref-previews");
    const MAX = 5, MAXB = 15 * 1024 * 1024, OK = ["image/png", "image/jpeg", "image/webp"];
    const refProblem = () => {
      const fs = Array.from(refs.files || []);
      if (fs.length > MAX) return `Up to ${MAX} reference images.`;
      const bad = fs.find((f) => !OK.includes(f.type) || f.size > MAXB);
      if (bad) return `${bad.name}: use a PNG, JPEG or WebP under 15 MB.`;
      return "";
    };
    refs.addEventListener("change", () => {
      previews.innerHTML = "";
      Array.from(refs.files || []).slice(0, MAX).forEach((f) => {
        if (!OK.includes(f.type)) return;
        const i = document.createElement("img"); i.alt = f.name; i.src = URL.createObjectURL(f); previews.appendChild(i);
      });
      const p = refProblem(); showErr(refs, p);
    });
    const showErr = (el, msg) => {
      let e = el.parentElement.querySelector(".err.js");
      if (!msg) { if (e) e.remove(); el.removeAttribute("aria-invalid"); return; }
      if (!e) { e = document.createElement("p"); e.className = "err js"; e.setAttribute("role", "alert"); el.insertAdjacentElement("afterend", e); }
      e.textContent = msg; el.setAttribute("aria-invalid", "true");
    };
    const val = (n) => { const el = form.elements[n]; return el ? (el.value || "").trim() : ""; };
    const labelOf = (n) => { const c = form.querySelector(`input[name=${n}]:checked`); return c ? c.closest("label").querySelector("strong").textContent : ""; };
    const check = () => {
      let first = null;
      const need = (ok, el, msg) => { showErr(el, ok ? "" : msg); if (!ok && !first) first = el; };
      need(val("idea").length >= 2, $("#idea"), "Tell us your idea.");
      need(!!labelOf("garment"), $$("input[name=garment]").pop(), "Choose a garment.");
      need(!!labelOf("style"), $$("input[name=style]").pop(), "Choose a style.");
      need(!val("back_name") || /^[A-Za-z][A-Za-z .'\-]{0,13}$/.test(val("back_name")), $("#back_name"), "Letters only, up to 14 characters.");
      need(!val("back_number") || /^\d{1,2}$/.test(val("back_number")), $("#back_number"), "0–99.");
      need(val("customer_name").length >= 1, $("#customer_name"), "Please add your name.");
      need(/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(val("email")), $("#email"), "Please add a valid email.");
      const rp = refProblem(); need(!rp, refs, rp);
      if (first) first.focus();
      return !first;
    };
    const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    $("#to-summary").addEventListener("click", () => {
      if (!check()) return;
      const rows = [["Idea", val("idea") + (form.elements.own_brand.checked ? " (my own brand)" : "")], ["Garment", labelOf("garment")],
        ["Style", labelOf("style")], ["Colours", val("colors") || "Designer’s choice"], ["Back name", (val("back_name") || "—").toUpperCase()],
        ["Number", val("back_number") || "—"], ["References", `${(refs.files || []).length} image(s)`],
        ["Notes", val("instructions") || "—"], ["Name", val("customer_name")], ["Email", val("email")]];
      $("#summary").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
      $("#step-edit").hidden = true; $("#step-summary").hidden = false; window.scrollTo({ top: 0, behavior: "smooth" });
    });
    $("#back-edit").addEventListener("click", () => { $("#step-summary").hidden = true; $("#step-edit").hidden = false; });
    form.addEventListener("submit", (e) => {
      if (!$("#step-summary").hidden || check()) { const b = $("#step-summary button[type=submit]"); if (b) { b.disabled = true; b.textContent = "Submitting…"; } return; }
      e.preventDefault();
    });
  }

  // approval form: live total preview (the server recalculates; this is only a preview)
  const ap = $(".approve-form");
  if (ap) {
    const q = $("#qty", ap);
    const hint = document.createElement("p"); hint.className = "fine"; q.closest(".row2").after(hint);
    const upd = () => {
      const n = Math.max(1, Math.min(20, parseInt(q.value || "1", 10)));
      const t = (+ap.dataset.unit * n + +ap.dataset.ship) / 100;
      hint.textContent = `Estimated before tax: $${t.toFixed(2)} including shipping.`;
    };
    q.addEventListener("input", upd); upd();
  }
})();
