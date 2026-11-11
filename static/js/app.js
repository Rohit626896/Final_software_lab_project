/* Progressive enhancement: the core workflow also works without JavaScript. */
"use strict";

document.addEventListener("DOMContentLoaded", () => {
    const form = document.querySelector("[data-search]");
    const history = document.getElementById("searchHistory");
    const historyContainer = document.getElementById("recent-searches");
    let searches = [];
    try {
        const stored = JSON.parse(localStorage.getItem("paperlens.searches") || "[]");
        if (Array.isArray(stored)) searches = stored.filter(item => typeof item ===
            "string").slice(0, 5);
        const query = new URLSearchParams(window.location.search).get("q");
        if (window.location.pathname === "/search" && query && query.length <= 200) {
            searches = [query, ...searches.filter(item => item !== query)].slice(0, 5);
            localStorage.setItem("paperlens.searches", JSON.stringify(searches));
        }
    } catch (_) {
        /* Browser storage may be unavailable. */ }
    if (history && historyContainer && searches.length) {
        historyContainer.hidden = false;
        for (const query of searches) {
            const link = document.createElement("a");
            link.className = "tag";
            link.textContent = query;
            link.href = "/search?" + new URLSearchParams({
                q: query
            });
            history.appendChild(link);
        }
    }
    if (form) {
        form.addEventListener("submit", () => {
            const button = form.querySelector("button[type=submit]");
            button.disabled = true;
            button.textContent = "Searching…";
        });
    }
    document.querySelectorAll("form[data-busy]").forEach(item => {
        item.addEventListener("submit", () => {
            const button = item.querySelector("button[type=submit]");
            if (button) {
                button.disabled = true;
                button.textContent = "Working…";
            }
        });
    });
    window.addEventListener("pageshow", event => {
        if (event.persisted) window.location.reload();
    });
    document.querySelectorAll("[data-print]").forEach(button => {
        button.addEventListener("click", () => window.print());
    });
    const stats = document.querySelector("[data-library-stats]");
    if (stats) {
        let updating = false;
        async function refreshStats() {
            if (document.hidden || updating) return;
            updating = true;
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 10000);
            try {
                const response = await fetch(stats.dataset.libraryStats, {
                    headers: { "Accept": "application/json" },
                    cache: "no-store",
                    signal: controller.signal
                });
                if (!response.ok) return;
                const counts = await response.json();
                for (const key of ["total", "analyzed"]) {
                    if (Number.isInteger(counts[key]) && counts[key] >= 0) {
                        stats.querySelector(`[data-stat="${key}"]`).textContent = counts[key];
                    }
                }
            } catch (_) {
                /* Keep the last known counts and retry on the next update. */
            } finally {
                clearTimeout(timer);
                updating = false;
            }
        }
        setInterval(refreshStats, 5000);
        document.addEventListener("visibilitychange", refreshStats);
        window.addEventListener("focus", refreshStats);
        refreshStats();
    }
    const progress = document.getElementById("analysis-progress");
    if (progress) {
        let failures = 0;
        async function poll() {
            if (document.hidden) {
                setTimeout(poll, 4000);
                return;
            }
            try {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 10000);
                let response;
                try {
                    response = await fetch(progress.dataset.statusUrl, {
                        headers: {
                            "Accept": "application/json"
                        },
                        signal: controller.signal
                    });
                } finally {
                    clearTimeout(timer);
                }
                if (!response.ok) throw new Error("Status unavailable");
                const data = await response.json();
                if (!["queued", "running"].includes(data.job?.status)) {
                    window.location.reload();
                    return;
                }
                document.getElementById("job-stage").textContent = data.job.stage;
                failures = 0;
            } catch (_) {
                failures++;
                document.getElementById("job-stage").textContent =
                    "Connection interrupted. Retrying… You can refresh this page.";
            }
            setTimeout(poll, Math.min(15000, 2000 + failures * 2000));
        }
        setTimeout(poll, 1500);
    }
    const resourceFilter = document.getElementById("resource-filter");
    if (resourceFilter) {
        resourceFilter.addEventListener("change", () => {
            let shown = 0;
            document.querySelectorAll(".resource-card").forEach(card => {
                card.hidden = Boolean(resourceFilter.value && card.dataset
                    .category !== resourceFilter.value);
                if (!card.hidden) shown++;
            });
            document.getElementById("resource-filter-empty").hidden = shown > 0;
        });
    }
    const chartData = document.getElementById("chart-data");
    if (chartData && window.Plotly) {
        try {
            JSON.parse(chartData.textContent).forEach((figure, index) => {
                window.Plotly.newPlot("chart-" + (index + 1), figure.data, figure
                    .layout, {
                        responsive: true,
                        displayModeBar: false
                    });
            });
        } catch (_) {
            /* Accessible comparison tables remain available. */ }
    }
});
