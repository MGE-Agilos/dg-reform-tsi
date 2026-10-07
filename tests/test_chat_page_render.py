"""Teste le rendu Markdown->HTML cote page (fonctions pures du <script> de index.html), via Node.

On extrait uniquement les fonctions sans dependance au DOM (esc, inline, render, ...) : le reste du
script (ecouteurs d'evenements, document.getElementById) n'est pas execute ici.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX_HTML = Path(__file__).resolve().parent.parent / "src" / "tsi_agent" / "chat" / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node introuvable dans le PATH")


def _pure_js() -> str:
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    start = script.index("function esc(")
    end = script.index("function addMessage")
    return script[start:end]


def render_js(text: str, sources: list | None = None) -> str:
    payload = json.dumps({"text": text, "sources": sources or []})
    node_src = _pure_js() + f"\nconst input = {payload};\nprocess.stdout.write(render(input.text, input.sources));\n"
    result = subprocess.run(["node", "-e", node_src], capture_output=True, text=True, encoding="utf-8", check=True)
    return result.stdout


def test_render_heading_levels_map_to_h3_h4():
    assert render_js("# T1") == "<h3>T1</h3>"
    assert render_js("## T2") == "<h3>T2</h3>"
    assert render_js("### T3") == "<h4>T3</h4>"
    assert render_js("#### T4") == "<h4>T4</h4>"


def test_render_table_with_header_and_separator():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    assert render_js(md) == (
        '<div class="table-wrap"><table><thead><tr><th>A</th><th>B</th></tr></thead><tbody>'
        "<tr><td>1</td><td>2</td></tr></tbody></table></div>"
    )


def test_render_hr_and_blockquote():
    assert render_js("---") == "<hr>"
    assert render_js("> quoted") == "<blockquote>quoted</blockquote>"


def test_render_ordered_and_bullet_lists():
    assert render_js("1. a\n2. b") == "<ol><li>a</li><li>b</li></ol>"
    assert render_js("3. a\n4. b") == '<ol start="3"><li>a</li><li>b</li></ol>'
    assert render_js("- a\n- b") == "<ul><li>a</li><li>b</li></ul>"


def test_render_bold_italic_code():
    assert render_js("**bold**") == "<p><strong>bold</strong></p>"
    assert render_js("*italic*") == "<p><em>italic</em></p>"
    assert render_js("_italic_ word") == "<p><em>italic</em> word</p>"
    assert render_js("`code`") == "<p><code>code</code></p>"


def test_render_does_not_turn_underscores_inside_identifiers_into_italics():
    assert render_js("snake_case_var stays") == "<p>snake_case_var stays</p>"


def test_render_escapes_model_text_before_applying_markdown():
    html = render_js("<script>alert(1)</script> and **bold**")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "<strong>bold</strong>" in html


def test_render_links_only_http_and_https_schemes():
    html = render_js("See http://ok.example and javascript:alert(1) and data:text/html,x")
    assert '<a href="http://ok.example"' in html
    assert 'href="javascript' not in html
    assert 'href="data:' not in html


def test_render_ref_superscript_falls_back_to_hash_for_non_http_source_urls():
    sources = [{"ref": 1, "url": "https://ok.example"}, {"ref": 2, "url": "javascript:alert(1)"}]
    html = render_js("[1] and [2] and [3]", sources)
    assert '<sup><a href="https://ok.example"' in html
    assert '<sup><a href="#"' in html
    assert "[3]" in html


def _tags(html: str) -> list[tuple[str, list[tuple[str, str | None]]]]:
    from html.parser import HTMLParser

    tags = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append((tag, attrs))

    Collector().feed(html)
    return tags


def test_render_citation_after_autolink_cannot_inject_attributes():
    sources = [{"ref": 1, "url": "https://ok.example/p"},
               {"ref": 2, "url": "https://evil.example/x/onmouseover=alert(1)//"}]
    html = render_js("- [1] See https://a.example/[2] — x", sources)
    for tag, attrs in _tags(html):
        assert all(name in ("href", "target", "rel") for name, _ in attrs), (tag, attrs, html)
    anchors = [dict(attrs)["href"] for tag, attrs in _tags(html) if tag == "a"]
    assert anchors == ["https://ok.example/p", "https://a.example/", "https://evil.example/x/onmouseover=alert(1)//"]
    assert html.count("<a ") == html.count("</a>") == 3


def test_render_autolink_stops_at_brackets_and_quotes():
    html = render_js('Go to https://a.example/x"y and [https://b.example/z]')
    hrefs = [dict(attrs)["href"] for tag, attrs in _tags(html) if tag == "a"]
    assert hrefs == ["https://a.example/x", "https://b.example/z"]


def test_render_table_cell_with_escaped_pipe():
    md = "| A | B |\n|---|---|\n| a" + "\\" + "|b | c |"
    assert render_js(md) == (
        '<div class="table-wrap"><table><thead><tr><th>A</th><th>B</th></tr></thead><tbody>'
        "<tr><td>a|b</td><td>c</td></tr></tbody></table></div>"
    )


def eval_js(expr: str):
    node_src = _pure_js() + f"\nprocess.stdout.write(JSON.stringify({expr}));\n"
    result = subprocess.run(["node", "-e", node_src], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


def test_gauge_angle_and_impact_category():
    assert eval_js("[-2, -1, 0, 1, 2, 5].map(gaugeAngle)") == [-90, -45, 0, 45, 90, 90]
    infos = eval_js("[2, 1, 0, -1, -2, undefined, '2', 1.5].map(s => impactInfo({impact_score: s}))")
    assert [(i["category"], i["signed"]) for i in infos] == [
        ("positive", "+2"), ("positive", "+1"), ("undetermined", "0"), ("negative", "-1"), ("negative", "-2"),
        ("not_rated", ""), ("not_rated", ""), ("not_rated", "")]
    # aiguille : -2 tout a gauche, +2 tout a droite (meme hauteur que le centre)
    assert eval_js("[gaugePoint(gaugeAngle(-2), 10), gaugePoint(gaugeAngle(2), 10), gaugePoint(0, 10)]") == \
        [[100, 114], [120, 114], [110, 104]]


def test_impact_gauge_markup_and_escaping():
    html = eval_js('impactGauge({impact_score: 2, confidence: "high", '
                   'impact_rationale: "<img src=x onerror=alert(1)> Done [1]."}, '
                   '[{ref: 1, url: "https://ok.example"}])')
    assert 'role="img"' in html and 'aria-label="Impact: Positive, score +2 of -2 to +2, confidence high"' in html
    assert 'class="needle"' in html and "<img" not in html and "&lt;img" in html
    assert '<sup><a href="https://ok.example"' in html
    unrated = eval_js('impactGauge({confidence: "low"}, [])')
    assert 'class="needle"' not in unrated and "gauge unrated" in unrated
    assert "Not rated (run the analysis again to get a score)" in unrated
    assert eval_js('[impactPill({impact_score: -1}), impactPill({})]') == [
        '<span class="pill negative">Negative -1</span>', '<span class="pill not_rated">Not rated</span>']
