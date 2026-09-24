"""Offline UI regression checks; rendering never starts analysts or trading."""
import json

from streamlit.testing.v1 import AppTest

from trader_engine.ui.net_edge import find_net_edge_root, specialist_coverage


def saved_report():
    return {
        "input_hash": "snapshot-1234",
        "reports": [
            {"role": "technical", "stance": "positive", "thesis": "Saved price observations.",
             "facts": [{"text": "SPY: saved-window return 1.250%.", "evidence_ids": ["prices"]}],
             "confidence": "low", "status": "ok", "missing_data": [], "risks": ["Momentum alone is not an edge."]},
            *[{"role": role, "stance": "insufficient_data", "thesis": "Evidence unavailable.",
               "facts": [], "confidence": "unknown", "status": "ok",
               "missing_data": [f"No usable {role} evidence"], "risks": []}
              for role in ("fundamental", "news", "macro")],
            {"role": "bear", "stance": "neutral", "thesis": "Challenge the supplied evidence.",
             "facts": [{"text": "SPY: saved-window return 1.250%.", "evidence_ids": ["prices"]}],
             "confidence": "low", "status": "ok", "missing_data": ["news: incomplete evidence"],
             "risks": ["Actual account exposures require review."]},
        ],
        "evidence_sources": {"prices": {"source_url": "https://example.com/prices", "available_at": "2026-09-23T20:00:00+00:00"}},
    }


def render_fixture(tmp_path, report):
    (tmp_path / "team_report.json").write_text(json.dumps(report))
    return AppTest.from_string(
        "from trader_engine.ui.net_edge import render_net_edge\n"
        f"render_net_edge({str(tmp_path)!r})\n"
    ).run()


def test_all_five_panels_show_evidence_or_missing_inputs(tmp_path):
    app = render_fixture(tmp_path, saved_report())
    assert not app.exception
    assert [t.label for t in app.tabs] == ["Technical analyst", "Fundamental analyst", "News analyst", "Macro analyst", "Bear / critic"]
    assert "SPY: saved-window return 1.250%." in "\n".join(m.value for m in app.tabs[0].markdown)
    for tab, expected in zip(app.tabs[1:4], ("ETF holdings", "cited headlines", "economic observations")):
        assert any("No usable evidence" in box.value for box in tab.info)
        assert expected in "\n".join(m.value for m in tab.markdown)
    assert "Actual account exposures require review." in "\n".join(m.value for m in app.tabs[4].markdown)
    overview = app.dataframe[0].value
    assert overview["Facts"].tolist() == [1, 0, 0, 0, 1]
    text = "\n".join(m.value for m in app.markdown)
    assert "do not run live analysis" in text
    assert "2026-09-23T20:00:00+00:00" in text


def test_missing_roles_are_explicit_and_failed_roles_are_not_neutral(tmp_path):
    report = saved_report()
    report["reports"] = [{"role": "technical", "status": "timeout", "facts": [], "confidence": "unknown"}]
    app = render_fixture(tmp_path, report)
    assert not app.exception
    statuses = app.dataframe[0].value["Evidence status"].tolist()
    assert statuses == ["Assessment timeout"] + ["Assessment not captured"] * 4
    assert any("Input coverage cannot be inferred" in box.value for box in app.tabs[0].warning)
    assert not app.tabs[0].info
    assert any("Assessment not captured" in box.value for box in app.tabs[1].info)


def test_dataclass_source_list_keeps_source_timestamp_visible(tmp_path):
    report = saved_report()
    report["evidence_sources"] = [dict(source_id=key, **value) for key, value in report["evidence_sources"].items()]
    app = render_fixture(tmp_path, report)
    assert not app.exception
    assert any("Available: 2026-09-23" in c.value for c in app.tabs[0].caption)


def test_no_report_renders_five_missing_assessments(tmp_path):
    (tmp_path / "registry.json").write_text("{}")
    app = AppTest.from_string(
        "from trader_engine.ui.net_edge import render_net_edge\n"
        f"render_net_edge({str(tmp_path)!r})\n"
    ).run()
    assert not app.exception
    assert len(app.tabs) == 5
    assert app.dataframe[0].value["Facts"].tolist() == [0] * 5
    assert any("No specialist assessment" in box.value for box in app.info)


def test_artifact_discovery_supports_root_latest_and_explicit_snapshot(tmp_path):
    root = tmp_path / "artifacts"
    snapshot = root / "net_edge_20260923"
    specialists = snapshot / "specialists"
    specialists.mkdir(parents=True)
    (specialists / "team_report.json").write_text(json.dumps(saved_report()))
    assert find_net_edge_root(root) == snapshot
    assert find_net_edge_root(root / "latest") == snapshot
    assert find_net_edge_root(snapshot) == snapshot


def test_coverage_does_not_invent_facts_for_missing_roles():
    assert all(row["Facts"] == 0 and row["Evidence status"] == "Assessment not captured"
               for row in specialist_coverage({}))
