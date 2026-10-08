import csv
import json
import re

import pytest
from haystack import component
from haystack.components.embedders import MockTextEmbedder
from haystack.dataclasses import ChatMessage

from conftest import make_vector_store
from tsi_agent import __main__ as cli
from tsi_agent.chat.generator import LoggedChatGenerator
from tsi_agent.config import Settings
from tsi_agent.countries import COUNTRIES, FRENCH_NAMES
from tsi_agent.eval import aggregation as agg
from tsi_agent.eval import aggregation_report as agg_report
from tsi_agent.impact.catalog import load_catalog
from tsi_agent.qdrant_local import open_store


def project(pid, area, countries, title=None, year=2022):
    return {"project_id": pid, "title": title or f"Project {pid}", "year": year, "programme": "TSI",
            "countries": countries, "policy_area": area, "detail_page": True}


def fake_catalog():
    cat = [project(f"h{i}", "health-and-long-term-care", ["HR"]) for i in range(4)]
    cat += [project("h-si", "health-and-long-term-care", ["SI", "HR"]),
            project("h-multi", "health-and-long-term-care", ["AT", "BE"])]
    cat += [project(f"g{i}", "green-transition", ["ES"]) for i in range(5)]
    cat += [project("g-pt", "green-transition", ["PT"]), project("none", None, ["FR"])]
    cat += [project(f"c{i}", "competitiveness", ["IT"]) for i in range(3)]
    cat += [project(f"d{i}", "digital-transition", ["EE"]) for i in range(2)]
    cat += [project(f"s{i}", "skills-education-and-training", ["LV"]) for i in range(3)]
    cat += [project(f"p{i}", "public-administration-and-governance", ["GR"]) for i in range(20)]
    cat += [project("l1", "labour-market-and-social-protection", ["CY"])]
    return cat


# --- pays dans le texte ---

def test_french_names_cover_all_member_states():
    assert set(FRENCH_NAMES) == set(COUNTRIES)
    assert "Pays-Bas" in FRENCH_NAMES["NL"] and "Grèce" in FRENCH_NAMES["GR"]


def test_detect_countries_names_codes_and_no_false_positive():
    text = ("Projects exist in Croatia, la Slovénie, les Pays-Bas, Czech Republic and Grece; also ES and PT. "
            "La Tchéquie aussi.")
    assert agg.detect_countries(text) == {"HR", "SI", "NL", "CZ", "GR", "ES", "PT"}
    # mots courants et codes en minuscules : rien
    assert agg.detect_countries("it is fine, pt. 3, de la reforme, se faire, es una") == set()
    # IT au sens informatique : pas l'Italie
    assert agg.detect_countries("The IT systems and IT-tools were upgraded.") == set()
    assert agg.detect_countries("Countries: IT, FR (France) and EL.") == {"IT", "FR", "GR"}
    assert agg.detect_countries("Italie et Allemagne ; Italy") == {"IT", "DE"}


def test_projects_mentioned_from_sources_titles_and_ids():
    cat = [project("pension-reform-croatia", "x", ["HR"], title="Supporting the pension reform in Croatia"),
           project("tax", "x", ["GR"], title="Tax"), project("other-project", "x", ["GR"], title="Another long project title")]
    answer = ("The project SUPPORTING THE PENSION REFORM IN CROATIA [1] and tax matters; see also (other-project) "
              "and Tax; other.")
    sources = [{"ref": 1, "doc_id": "from-source"}]
    assert agg.projects_mentioned(answer, sources, cat) == {"from-source", "pension-reform-croatia", "other-project"}


def test_parse_count():
    assert agg.parse_count("Croatia has 12 projects in health [1] (2021-2024).") == 12
    assert agg.parse_count("En 2022, la Croatie compte 7 projets [3].") == 7
    assert agg.parse_count("No projects found.") is None


def test_set_metrics():
    m = agg.set_metrics({"HR", "SI", "XX"}, {"HR", "SI", "AT", "BE"})
    assert m["recall"] == 0.5 and m["precision"] == pytest.approx(2 / 3) and m["f1"] == pytest.approx(4 / 7)
    assert m["missed"] == ["AT", "BE"] and m["extra"] == ["XX"]
    empty = agg.set_metrics(set(), {"HR"})
    assert empty["recall"] == 0.0 and empty["precision"] is None and empty["f1"] == 0.0


# --- questions sur les metadonnees ---

def test_metadata_questions_and_truths():
    cat = fake_catalog()
    qs = agg.metadata_questions(cat, seed=3)
    assert qs == agg.metadata_questions(cat, seed=3)  # reproductible
    kinds = [q["kind"] for q in qs]
    assert kinds.count("countries") == 6 and kinds.count("projects") == 3 and kinds.count("count") == 3
    assert {q["lang"] for q in qs} == {"fr", "en"}
    assert len({q["id"] for q in qs}) == len(qs) and all(q["source"] == "metadata" for q in qs)
    for q in (q for q in qs if q["kind"] == "countries"):
        area = q["filters"]["policy_area"]
        assert set(q["truth_countries"]) == {c for p in cat if p["policy_area"] == area for c in p["countries"]}
        assert agg.readable_area(area, q["lang"]) in q["question"]
    areas = [q["filters"]["policy_area"] for q in qs if q["kind"] == "countries"]
    assert areas[0] == "health-and-long-term-care" and len(set(areas)) == 6  # sante toujours interrogee
    assert agg.readable_area("health-and-long-term-care") == "health and long-term care"
    assert agg.readable_area("health-and-long-term-care", "fr") == "santé et soins de longue durée"
    for q in qs:
        area, country = q["filters"]["policy_area"], q["filters"].get("country")
        matching = [p for p in cat if p["policy_area"] == area and (not country or country in p["countries"])]
        if q["kind"] == "projects":
            assert 3 <= len(q["truth_projects"]) <= 15
            assert set(q["truth_projects"]) == {p["project_id"] for p in matching}
        if q["kind"] == "count":
            assert q["truth_count"] == len(matching) >= 1
        assert "-" not in area or area not in q["question"]  # nom lisible, pas le slug


def test_metadata_questions_restricted_to_known_areas():
    qs = agg.metadata_questions(fake_catalog(), seed=1, areas=["green-transition", "health-and-long-term-care"])
    assert {q["filters"]["policy_area"] for q in qs} <= {"green-transition", "health-and-long-term-care"}


# --- verite des themes libres ---

@component
class ScriptedJudge:
    """Juge factice : pertinent si l'id du candidat est dans `relevant` ; garde les invites recues."""

    def __init__(self, relevant):
        self.relevant = set(relevant)
        self.prompts = []

    @component.output_types(replies=list[ChatMessage])
    def run(self, messages):
        prompt = messages[-1].text
        self.prompts.append(prompt)
        refs = re.findall(r"^\[(\d+)\] id: (\S+)", prompt, flags=re.M)
        decisions = [{"ref": int(n), "relevant": pid in self.relevant} for n, pid in refs]
        meta = {"model": "judge", "usage": {}, "cost_usd": 0.02}
        return {"replies": [ChatMessage.from_assistant(json.dumps({"decisions": decisions}), meta=meta)]}


ENERGY = "energy-regulation-academy"
TAX = "srsp-2016-technical-support-for-the-property-tax-reform"
TOPIC = {"id": "energy", "label_en": "energy regulation", "label_fr": "la régulation de l'énergie",
         "query": "energy regulators training", "keywords": ["energy", "regulator"]}


def topic_setup(tmp_path):
    vdir = make_vector_store(tmp_path)
    projects_dir = tmp_path / "output" / "projects"
    return open_store(str(vdir), 8), load_catalog(projects_dir), projects_dir


def test_topic_truth_with_judge(tmp_path):
    store, cat, projects_dir = topic_setup(tmp_path)
    judge = ScriptedJudge([ENERGY, TAX])
    truth = agg.build_topic_truth([TOPIC], cat, store, MockTextEmbedder(dimension=8), judge, projects_dir,
                                  batch_size=2)
    t = truth[0]
    # candidats : mots-cles (titre) + projets des morceaux semantiques (tout le petit corpus ici)
    assert ENERGY in t["candidates"] and len(t["candidates"]) == len(set(t["candidates"])) == 4
    assert len(judge.prompts) == 2  # 4 candidats par lots de 2
    assert "Energy Regulation Academy" in "".join(judge.prompts)
    assert sorted(t["relevant"]) == sorted([ENERGY, TAX])
    assert set(t["countries"]) == set(COUNTRIES)  # l'academie couvre les 27 pays
    assert t["unjudged"] == 0
    qs = agg.questions_from_truth(truth)
    assert [q["kind"] for q in qs] == ["countries", "projects"]
    assert qs[1]["truth_projects"] == sorted([ENERGY, TAX]) and all(q["source"] == "topic" for q in qs)
    assert {q["lang"] for q in qs} == {"fr", "en"}


def test_topic_candidates_by_keyword_only(tmp_path):
    store, cat, projects_dir = topic_setup(tmp_path)
    texts = agg.load_project_texts(projects_dir)
    found = agg.topic_candidates({**TOPIC, "keywords": ["property tax"]}, cat, texts, store,
                                 MockTextEmbedder(dimension=8), top_chunks=0)
    assert found == [TAX]
    assert texts[ENERGY]["excerpt"] and len(texts[ENERGY]["excerpt"]) <= agg.EXCERPT_CHARS


def test_judge_unreadable_batch_is_counted_unjudged(tmp_path):
    store, cat, projects_dir = topic_setup(tmp_path)

    @component
    class Broken:
        @component.output_types(replies=list[ChatMessage])
        def run(self, messages):
            return {"replies": [ChatMessage.from_assistant("not json")]}
    relevant, unjudged = agg.judge_candidates(TOPIC, [ENERGY, TAX], cat, agg.load_project_texts(projects_dir),
                                              Broken(), batch_size=25)
    assert relevant == [] and unjudged == 2


# --- execution et rapport ---

def fake_ask(answers):
    def ask(pipeline, question, history, *, logs_dir):
        assert history == []
        text, sources, tools = answers[question]
        return {"answer": text, "sources": sources, "duration_s": 1.5, "cost_usd": 0.05, "run_id": "r",
                "steps": [{"tool": t, "arguments": {"policy_area": "x"}, "summary": "", "error": False}
                          for t in tools]}
    return ask


QUESTIONS = [
    {"id": "m01", "source": "metadata", "kind": "countries", "lang": "en", "question": "Which countries?",
     "truth_countries": ["HR", "SI", "AT"], "truth_projects": [], "truth_count": None, "filters": {}},
    {"id": "m02", "source": "metadata", "kind": "count", "lang": "fr", "question": "Combien ?",
     "truth_countries": [], "truth_projects": [], "truth_count": 7, "filters": {}},
    {"id": "t01", "source": "topic", "kind": "projects", "lang": "en", "question": "Which projects?",
     "truth_countries": [], "truth_projects": ["a", "b"], "truth_count": None, "filters": {}, "topic": "cancer"},
    {"id": "t02", "source": "topic", "kind": "countries", "lang": "fr", "question": "Boom ?",
     "truth_countries": ["HR"], "truth_projects": [], "truth_count": None, "filters": {}, "topic": "cancer"},
]
ANSWERS = {"Which countries?": ("Croatia and Slovenia [1].", [{"doc_id": "x"}], ["list_projects"]),
           "Combien ?": ("La Croatie compte 6 projets.", [], ["list_projects", "list_projects"]),
           "Which projects?": ("See [1].", [{"doc_id": "a"}, {"doc_id": "z"}], ["find_projects", "search_projects"])}


def test_run_aggregation_scores_each_question(tmp_path):
    ask = fake_ask(ANSWERS)

    def ask_or_fail(pipeline, question, history, *, logs_dir):
        if question == "Boom ?":
            raise RuntimeError("cli down")
        return ask(pipeline, question, history, logs_dir=logs_dir)
    rows = agg.run_aggregation(Settings(logs_dir=str(tmp_path)), object(), QUESTIONS, ask_fn=ask_or_fail,
                               catalog=[], progress=None)
    m01, m02, t01, t02 = rows
    assert m01["found"] == ["HR", "SI"] and m01["recall"] == pytest.approx(2 / 3) and m01["precision"] == 1.0
    assert m01["missed"] == ["AT"] and m01["tools"] == ["list_projects"] and m01["cost_usd"] == 0.05
    assert m02["predicted_count"] == 6 and m02["abs_error"] == 1 and m02["exact"] is False
    assert t01["found"] == ["a", "z"] and t01["recall"] == 0.5 and t01["precision"] == 0.5
    assert t02["error"] and t02["recall"] is None


def test_render_report_and_csv(tmp_path):
    rows = agg.run_aggregation(Settings(logs_dir=str(tmp_path)), object(), QUESTIONS[:3], ask_fn=fake_ask(ANSWERS),
                               catalog=[], progress=None)
    md = agg_report.render_report(rows, {"date": "2026-10-08", "chat_model": "sonnet", "judge_model": "sonnet",
                                         "truth_file": "t.json", "topics": [{"id": "cancer", "candidates": 40,
                                                                             "relevant": ["a", "b"], "unjudged": 0}]})
    assert md.startswith("# Banc d'agrégation — 2026-10-08")
    assert "| Métadonnées | Pays | 1 | 67 % | 100 % | 80 % |" in md
    assert "| Métadonnées | Comptage | 1 |" in md and "0/1" in md
    assert "| Thèmes libres | Projets | 1 | 50 % | 50 % | 50 % |" in md
    assert "Which countries?" in md and "AT" in md and "list_projects ×2" in md
    assert "cancer" in md and "40" in md
    lecture = md.split("## Lecture")[1]
    assert "list_projects" in lecture and "recommand" not in lecture.lower()
    assert ("Thèmes libres : list_projects appelé dans 0/1 questions, find_projects dans 1/1, une recherche "
            "sémantique dans 1/1.") in lecture
    assert "find_projects dans 0/2" in lecture
    path = tmp_path / "a.csv"
    agg_report.write_csv(rows, path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        out = list(csv.DictReader(fh))
    assert len(out) == 3 and out[0]["missed"] == "AT" and out[1]["predicted_count"] == "6"


def test_run_eval_aggregation_end_to_end(tmp_path):
    store, cat, projects_dir = topic_setup(tmp_path)
    settings = Settings(output_dir=str(tmp_path / "output"), vectors_dir=str(tmp_path / "output" / "vectors"),
                        embedding_dim=8, logs_dir=str(tmp_path / "logs"))
    inner = ScriptedJudge([ENERGY])
    judge = LoggedChatGenerator(chat_generator=inner)
    asked = []

    def ask(pipeline, question, history, *, logs_dir):
        asked.append(question)
        return {"answer": "Spain (ES) [1]", "sources": [{"doc_id": ENERGY}], "steps": [], "duration_s": 1.0,
                "cost_usd": 0.1, "run_id": "x"}
    out_dir = tmp_path / "eval"
    kw = dict(out_dir=out_dir, store=store, text_embedder=MockTextEmbedder(dimension=8), judge=judge,
              pipeline=object(), ask_fn=ask, topics=[TOPIC], today="2026-10-08", progress=None)
    out = agg.run_eval_aggregation(settings, **kw)
    assert out["report"] == str(out_dir / "aggregation-2026-10-08.md")
    assert (out_dir / "aggregation-2026-10-08.csv").exists()
    truth = json.loads((out_dir / "aggregation_truth.json").read_text(encoding="utf-8"))
    assert truth["topics"][0]["relevant"] == [ENERGY]
    assert out["questions"] == len(asked) == len(agg.metadata_questions(cat)) + 2
    assert out["cost_usd"] == pytest.approx(len(asked) * 0.1 + 0.02 * len(inner.prompts))
    n_prompts = len(inner.prompts)
    # deuxieme run : verite reutilisee (aucun appel au juge) ; themes seuls
    asked.clear()
    out2 = agg.run_eval_aggregation(settings, **{**kw, "topics_only": True})
    assert len(inner.prompts) == n_prompts and out2["questions"] == len(asked) == 2
    # --regenerate : le juge est rappele ; --metadata-only n'en a pas besoin
    agg.run_eval_aggregation(settings, **{**kw, "topics_only": True, "regenerate": True})
    assert len(inner.prompts) == 2 * n_prompts
    asked.clear()
    agg.run_eval_aggregation(settings, **{**kw, "metadata_only": True, "regenerate": True})
    assert len(inner.prompts) == 2 * n_prompts and len(asked) == len(agg.metadata_questions(cat))


def test_run_eval_aggregation_locked_store(tmp_path):
    class Locked:
        def check_lock(self):
            raise RuntimeError("deja ouverte")
    with pytest.raises(RuntimeError, match="serveur du chatbot"):
        agg.run_eval_aggregation(Settings(logs_dir=str(tmp_path / "l")), out_dir=tmp_path / "eval", store=Locked(),
                                 progress=None)
    assert not (tmp_path / "eval").exists()


def test_cli_eval_aggregation(monkeypatch, capsys, tmp_path):
    calls = {}

    def fake(settings, **kw):
        calls.update(kw)
        return {"report": "a.md", "csv": "a.csv", "truth_file": "t.json", "questions": 28, "cost_usd": 1.5,
                "run_id": "x"}
    monkeypatch.setattr(agg, "run_eval_aggregation", fake)
    assert cli.main(["eval-aggregation", "--regenerate", "--topics-only", "--out-dir", str(tmp_path / "e")]) == 0
    assert calls["regenerate"] and calls["topics_only"] and not calls["metadata_only"]
    assert calls["out_dir"] == str(tmp_path / "e") and "health-and-long-term-care" in calls["policy_areas"]
    printed = capsys.readouterr().out
    assert "a.md" in printed and "a.csv" in printed and "1.5" in printed
    with pytest.raises(SystemExit):
        cli.main(["eval-aggregation", "--topics-only", "--metadata-only"])

    def boom(*a, **k):
        raise RuntimeError("kaput")
    monkeypatch.setattr(agg, "run_eval_aggregation", boom)
    assert cli.main(["eval-aggregation"]) == 2
    assert "kaput" in capsys.readouterr().err
