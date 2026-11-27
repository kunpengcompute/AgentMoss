"""Regression guards for the dashboard page (``v1/server/dashboard.py``).

The page is one HTML string served at ``/dashboard/``.  Every bug guarded here
was reproduced in a real Chromium (Electron) against a running server and then
re-measured after the fix — the numbers in the docstrings are the actual
measurements, not estimates.

These assertions are source-level rather than DOM-level: exercising the layout
needs a browser, which CI does not have.  Each one names the exact regression
it prevents, so an accidental revert is still caught.
"""

import shutil
import subprocess

import pytest

from agent_gate.shared.server.dashboard import DASHBOARD_HTML


def _script_block() -> str:
    return DASHBOARD_HTML.split("<script>", 1)[1].rsplit("</script>", 1)[0]


def test_page_script_parses_as_javascript(tmp_path):
    """The embedded <script> must be syntactically valid JavaScript.

    It lives inside a Python string, so backslash escapes are a trap: in a
    NON-raw string ``\\t`` / ``\\r`` / ``\\n`` are turned into real control
    characters *before* the browser ever sees them.  A regex written as
    ``/[\\t\\r\\n]+/`` therefore arrived as ``/[<TAB><CR><LF>]+/`` and the
    whole script died on "Invalid regular expression: missing /" — every
    handler on the page silently stopped working (measured: ``setCopyMode``
    was undefined, the copy toolbar did nothing).  Hence the module uses a raw
    string; this test is what catches it if that ever regresses.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")

    script_path = tmp_path / "dashboard.js"
    script_path.write_text(_script_block(), encoding="utf-8")

    proc = subprocess.run(
        [node, "--check", str(script_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_copy_records_is_wired_up():
    """The copy feature's entry, toolbar and payload builder must all exist.

    Guards against a partial revert leaving, say, the button present but the
    handler gone (which fails silently in the browser).
    """
    for element_id in ("enter-copy", "copy-bar", "copy-selected", "copy-page", "copy-count"):
        assert f'id="{element_id}"' in DASHBOARD_HTML, element_id

    assert "function buildCopyPayload(" in DASHBOARD_HTML
    assert "navigator.clipboard.writeText" in DASHBOARD_HTML
    # 复制读原始字段、不读 DOM —— 否则会被列宽省略号截断
    assert "_currentPageRows" in DASHBOARD_HTML


def test_scrollbar_gutter_is_reserved():
    """Switching tabs must not resize or shift the stat cards.

    Tab heights differ a lot (Recent Calls ≈1230px vs Timeline ≈873px), so the
    vertical scrollbar appeared and disappeared.  That moved the content width
    between 1585 and 1600, and the ``auto-fit`` card grid re-laid itself out —
    measured: card width 182↔184, and the last card's x jumped 1186↔1197, so
    the whole row visibly slid.  Reserving the gutter keeps the width constant.
    """
    assert "scrollbar-gutter: stable" in DASHBOARD_HTML


def test_timeline_empty_state_is_dom_text_not_canvas():
    """The empty state must be real text, not pixels painted on a canvas.

    It used to be ``ctx.fillText()`` on a canvas that was never resized (the
    bitmap stayed at the 300×150 default) while CSS stretched it to the
    container — measured 300×150 stretched to 1518×246, a ~5× blow-up of a 14px
    font.  It read as a blurry, stretched image rather than text, and could not
    be selected.
    """
    assert 'id="timeline-empty"' in DASHBOARD_HTML
    assert "No data in the last 60 minutes" in DASHBOARD_HTML
    assert "fillText('No data" not in DASHBOARD_HTML


def test_canvas_bitmap_tracks_display_size():
    """The chart must not be a low-res bitmap stretched by CSS.

    The bitmap has to be sized to the CSS box × devicePixelRatio, otherwise the
    chart itself (not just the empty state) renders blurry.
    """
    assert "devicePixelRatio" in DASHBOARD_HTML


def test_refresh_does_not_clobber_text_selection():
    """The 3s refresh must not wipe an in-progress selection.

    ``refresh()`` rebuilds the table with ``innerHTML = …``, which destroys the
    selection.  Measured: selecting a command cell and waiting ~1s left the
    selection empty, so nothing in the table could be selected or copied at
    all.  The guards skip the rebuild when the data is unchanged or the user is
    mid-selection.
    """
    assert "_userIsSelecting" in DASHBOARD_HTML
    assert "_lastRecentKey" in DASHBOARD_HTML
    assert "_lastPageSig" in DASHBOARD_HTML


def test_recent_table_shows_reason_column():
    """记录表必须展示「原因」列（与 TS V1 看板对齐）。"""
    header = DASHBOARD_HTML.split('<table id="recent-calls">', 1)[1].split("</thead>", 1)[0]
    assert 'data-i18n="reason">Reason</th>' in header
    assert "reason-cell" in DASHBOARD_HTML
    assert "escapeHtml(r.reason" in DASHBOARD_HTML


def test_delete_records_is_wired_up():
    """删除记录：入口、多选工具栏、确认框和按 ids 删除的请求体都要存在。"""
    for element_id in (
        "enter-delete", "delete-selected", "delete-all", "select-all",
    ):
        assert f'id="{element_id}"' in DASHBOARD_HTML, element_id

    assert "function setSelectMode(" in DASHBOARD_HTML
    assert "mode-delete" in DASHBOARD_HTML
    assert "确定删除所选" in DASHBOARD_HTML
    assert "确定删除全部" in DASHBOARD_HTML
    assert "JSON.stringify({ ids: ids })" in DASHBOARD_HTML
    # 删除全部沿用无 body 的 DELETE，保持后端兼容语义
    assert "await doDelete(null)" in DASHBOARD_HTML
