"""Console Graph View structure guards (FR-7.8, design/07 §4).

Js-dom-free structure tests: they read the static assets as plain text and pin
the spec's verbatim surface — the nav entry, the vendored three.js (no CDN),
the hand-rolled instanced-layer architecture markers (THREE.Points custom
shader / InstancedMesh quads / canvas-sprite top-60 labels / Raycaster
picking / precomputed clustered layout), the decay/type/centrality/weight
encodings, the filters, the appendix-C degrade notice, and the CI-skip-safe
perf mode. Real wire behavior is covered by tests/test_console_api.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "src" / "mnemoseed" / "console" / "static"
APP_JS = STATIC / "app.js"
INDEX_HTML = STATIC / "index.html"
VENDOR_THREE = STATIC / "vendor" / "three.module.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------- nav + routing


def test_graph_nav_link_present(index_html: str) -> None:
    assert '<a href="#/graph" data-nav="graph" class="nav-link">Graph</a>' in index_html


def test_graph_route_parsed(app_js: str) -> None:
    assert 'if (hash.startsWith("/graph")) return { name: "graph" };' in app_js
    assert 'route.name === "graph"' in app_js


# ---------------------------------------------------------------- vendored three.js


def test_three_js_is_vendored_under_console() -> None:
    assert VENDOR_THREE.exists(), "three.module.js must be vendored under /console/vendor"
    head = VENDOR_THREE.read_text(encoding="utf-8")[:200]
    assert "Three.js Authors" in head and "SPDX-License-Identifier: MIT" in head


def test_three_js_loaded_from_console_vendor_never_a_cdn(app_js: str) -> None:
    assert 'import("/console/vendor/three.module.js")' in app_js
    for needle in ("unpkg.com", "cdn.jsdelivr", "cdnjs.cloudflare", "https://cdn."):
        assert needle not in app_js
        assert needle not in INDEX_HTML.read_text(encoding="utf-8")


# ------------------------------------------------- hand-rolled instanced architecture


def test_points_custom_shader_attributes(app_js: str) -> None:
    """One THREE.Points draw with per-point size/color/opacity/visibility."""
    assert "new THREE.Points" in app_js
    for attribute in ("aColor", "aSize", "aOpacity", "aVisible"):
        assert f'"{attribute}"' in app_js or f"'{attribute}'" in app_js
    assert "ShaderMaterial" in app_js
    assert "gl_PointSize" in app_js  # screen-space point size


def test_instanced_quad_edges_thickness_is_weight(app_js: str) -> None:
    assert "new THREE.InstancedMesh" in app_js
    assert "0.3 + edge.weight" in app_js  # quad thickness follows edge weight


def test_canvas_sprite_labels_top_sixty(app_js: str) -> None:
    assert "TOP_LABELS" in app_js
    assert "const TOP_LABELS = 60" in app_js
    assert 'createElement("canvas")' in app_js
    assert "new THREE.Sprite" in app_js


def test_raycaster_picking_and_detail_panel(app_js: str) -> None:
    assert "new THREE.Raycaster" in app_js
    assert "intersectObject(points, false)" in app_js
    assert "graphOpenDetail" in app_js  # click → Memory Detail side panel


def test_precomputed_clustered_layout(app_js: str) -> None:
    """No runtime force simulation: layout is a deterministic single pass."""
    assert "function graphLayout" in app_js
    assert "golden" in app_js  # golden-spiral sphere cluster centers


# ---------------------------------------------------------------- encodings + filters


def test_node_encodings_are_decay_type_centrality(app_js: str) -> None:
    """opacity = decay_weight, color = type, size = centrality (FR-7.8)."""
    assert "Math.max(0.05, n.decay_weight)" in app_js  # opacity = decay_weight
    assert "GRAPH_TYPE_RGB[n.node_type]" in app_js  # color = type
    assert "5 + (centrality.get(n.node_id) || 0) * 24" in app_js  # size = centrality


def test_filters_profile_type_time_tier_and_edge_kind(app_js: str) -> None:
    for needle in (
        'data-graph-filter="type"',
        'data-graph-filter="tier"',
        'data-graph-filter="time"',
        'data-graph-filter="kind"',
    ):
        assert needle in app_js
    assert "function applyGraphFilters" in app_js


def test_degrade_notice_is_explicit_not_faked(app_js: str) -> None:
    """The appendix-C degrade is surfaced honestly: the service declares the
    bulk view unavailable and the page renders that notice (never fake data)."""
    assert "graph.edge_list" in app_js
    assert "GRAPH_STATE.notice" in app_js
    service = (REPO / "src" / "mnemoseed" / "console" / "service.py").read_text(encoding="utf-8")
    assert "bulk edge view unavailable" in service
    assert "degrades to per-node edge fetching" in service or "per-node traversal" in service


def test_perf_mode_is_ci_skip_safe(app_js: str) -> None:
    assert "window.__GRAPH_PERF" in app_js
    assert 'has("perf")' in app_js
    assert "generatePerfGraph(5000)" in app_js
