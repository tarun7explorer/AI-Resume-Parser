/* Safe scorecard rendering. Only textContent / createElement are used; no innerHTML. */
(function () {
  "use strict";

  function el(tag, opts, children) {
    var node = document.createElement(tag);
    if (opts) {
      if (opts.cls) node.className = opts.cls;
      if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
      if (opts.attrs) Object.keys(opts.attrs).forEach(function (k) { node.setAttribute(k, String(opts.attrs[k])); });
    }
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  function num(v) { var n = Number(v); return isFinite(n) ? n : null; }
  function fmt(v, d) { var n = num(v); return n === null ? "—" : n.toFixed(d); }
  function strList(v) { return Array.isArray(v) ? v.filter(function (s) { return typeof s === "string" && s.trim(); }) : []; }

  function bulletSection(title, items, cls, emptyText) {
    var sec = el("section", { cls: "sc-section" }, [el("h4", { text: title })]);
    if (!items.length) {
      sec.appendChild(el("p", { cls: "muted-empty", text: emptyText }));
    } else {
      var ul = el("ul", { cls: "bullets " + cls });
      items.forEach(function (t) { ul.appendChild(el("li", { text: t })); });
      sec.appendChild(ul);
    }
    return sec;
  }

  function categoriesTable(cats) {
    var wrap = el("div", { cls: "table-wrap" });
    var table = el("table", { cls: "cat-table" });
    table.appendChild(el("caption", { text: "Category contributions" }));
    var head = el("tr", null, [
      el("th", { text: "Category", attrs: { scope: "col" } }),
      el("th", { cls: "num", text: "Score /10", attrs: { scope: "col" } }),
      el("th", { cls: "num", text: "Weight", attrs: { scope: "col" } }),
      el("th", { cls: "num", text: "Contribution", attrs: { scope: "col" } })
    ]);
    table.appendChild(el("thead", null, [head]));
    var tbody = el("tbody");
    cats.forEach(function (c) {
      if (!c || typeof c !== "object") return;
      var score = num(c.score);
      var bar = el("div", { cls: "bar", attrs: { "aria-hidden": "true" } });
      var fill = el("span");
      fill.style.width = (score === null ? 0 : Math.max(0, Math.min(10, score)) * 10) + "%";
      bar.appendChild(fill);
      var weight = num(c.weight);
      tbody.appendChild(el("tr", null, [
        el("th", { text: typeof c.name === "string" ? c.name : "Category", attrs: { scope: "row" } }),
        el("td", { cls: "num" }, [el("span", { text: fmt(c.score, 1) }), bar]),
        el("td", { cls: "num", text: weight === null ? "—" : Math.round(weight * 1000) / 10 + "%" }),
        el("td", { cls: "num", text: fmt(c.contribution, 2) })
      ]));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function evidenceSection(list) {
    var sec = el("section", { cls: "sc-section" }, [el("h4", { text: "Verified evidence" })]);
    var items = Array.isArray(list) ? list.filter(function (e) { return e && typeof e === "object" && typeof e.quote === "string" && e.quote; }) : [];
    if (!items.length) {
      sec.appendChild(el("p", { cls: "muted-empty", text: "No verified quotations were available for this resume." }));
      return sec;
    }
    var wrap = el("div", { cls: "evidence-list" });
    items.forEach(function (e) {
      var node = el("div", { cls: "evidence" });
      if (typeof e.claim === "string" && e.claim) node.appendChild(el("p", { cls: "evidence-claim", text: e.claim }));
      node.appendChild(el("blockquote", { text: e.quote }));
      var page = num(e.page);
      node.appendChild(el("p", { cls: "evidence-page", text: page !== null && page > 0 ? "Page " + page : "Page not reliably determined" }));
      wrap.appendChild(node);
    });
    sec.appendChild(wrap);
    return sec;
  }

  function coverageSection(value) {
    var n = num(value);
    if (n === null) return null;
    var pct = Math.max(0, Math.min(1, n)) * 100;
    var bar = el("div", { cls: "bar", attrs: { role: "img", "aria-label": "Evidence coverage " + Math.round(pct) + " percent" } });
    var fill = el("span"); fill.style.width = pct + "%"; bar.appendChild(fill);
    return el("section", { cls: "sc-section coverage" }, [
      el("h4", { text: "Evidence coverage: " + Math.round(pct) + "%" }),
      bar,
      el("p", { cls: "coverage-note", text: "How much of the assessment is backed by verified quotations. This is not a suitability score." })
    ]);
  }

  function scorecard(r, index) {
    var score = num(r.score_out_of_10);
    var percentage = num(r.percentage);
    var label = typeof r.candidate_label === "string" && r.candidate_label ? r.candidate_label : "Resume " + (index + 1);

    var ring = el("div", { cls: "ring", attrs: { "aria-hidden": "true" } }, [el("div", { cls: "ring-inner", text: fmt(score, 1) })]);
    ring.style.setProperty("--pct", String(percentage === null ? (score === null ? 0 : score * 10) : percentage));

    var scoreText = el("div", null, [
      el("p", { cls: "score-big" }, [document.createTextNode(fmt(score, 1)), el("small", { text: " / 10" })]),
      el("p", { cls: "score-pct", text: percentage === null ? "" : Math.round(percentage) + "%" })
    ]);

    var head = el("div", { cls: "sc-head" }, [
      el("div", null, [
        el("h3", { cls: "sc-title", text: label }),
        el("p", { cls: "sc-sub", text: "Candidate " + (index + 1) })
      ]),
      el("div", { cls: "sc-score" }, [ring, scoreText])
    ]);

    var body = el("div", { cls: "sc-body" });
    if (Array.isArray(r.categories) && r.categories.length) body.appendChild(categoriesTable(r.categories));
    body.appendChild(el("div", { cls: "cols-2" }, [
      bulletSection("Strengths", strList(r.strengths), "good", "No evidence-backed strengths were reported."),
      bulletSection("Missing or uncertain evidence", strList(r.missing_or_uncertain_evidence), "warn", "Nothing was flagged as missing or uncertain.")
    ]));
    body.appendChild(evidenceSection(r.evidence));
    var cov = coverageSection(r.evidence_coverage);
    if (cov) body.appendChild(cov);

    var lims = strList(r.limitations);
    if (lims.length) {
      var ul = el("ul");
      lims.forEach(function (t) { ul.appendChild(el("li", { text: t })); });
      body.appendChild(el("div", { cls: "limits" }, [ul]));
    }

    var card = el("article", { cls: "scorecard", attrs: { "aria-label": "Scorecard for " + label } }, [head, body]);
    return card;
  }

  function render(container, results) {
    while (container.firstChild) container.removeChild(container.firstChild);
    if (!Array.isArray(results) || !results.length) {
      container.appendChild(el("p", { cls: "empty", text: "No results were returned for this analysis." }));
      return;
    }
    results.forEach(function (r, i) {
      if (!r || typeof r !== "object") return;
      if (r.status === "failed") {
        var label = typeof r.candidate_label === "string" && r.candidate_label ? r.candidate_label : "Resume " + (i + 1);
        var message = typeof r.error_message === "string" ? r.error_message : "This resume could not be processed.";
        container.appendChild(el("article", { cls: "scorecard scorecard-failed", attrs: { "aria-label": "Processing failed for " + label } }, [
          el("h3", { cls: "sc-title", text: label }),
          el("p", { attrs: { role: "status" }, text: message })
        ]));
      } else {
        container.appendChild(scorecard(r, i));
      }
    });
  }

  window.RPResults = { render: render };
})();
