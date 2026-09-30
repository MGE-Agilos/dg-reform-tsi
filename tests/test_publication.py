from conftest import fixture_bytes
from tsi_agent.models import PublicationFile, PublicationParse
from tsi_agent.parsers.publication import files_key, parse_publication, publication_links, select_files

SITE = "https://reforms-investments.ec.europa.eu"


def test_publication_links_from_detail_pages():
    assert publication_links(fixture_bytes("detail_with_publication.html")) == [
        f"{SITE}/publications-0/developing-ehealth-healthier-society-slovenia_en"]
    assert publication_links(fixture_bytes("detail_hems_croatia.html")) == [
        f"{SITE}/publications-0/establishment-helicopter-emergency-medical-services-croatia_en"]


def test_parse_one_file():
    p = parse_publication(fixture_bytes("pub_one_file.html"))
    assert p.title == "Developing eHealth for a healthier society in Slovenia"
    assert p.date == "17 October 2023" and p.meta["Country"] == "Slovenia"
    assert len(p.documents) == 1 and len(p.documents[0]) == 1
    f = p.documents[0][0]
    assert f.document == "Final - deliverable" and f.language == "en"
    assert f.url == f"{SITE}/document/download/6ab9d519-2de1-499d-870b-77ae9a1458ca_en?filename=Final%20-%20deliverable.pdf"
    assert f.filename == "Final - deliverable.pdf" and f.size_label == "(569.01 KB - PDF)"


def test_parse_translations_and_select_english():
    p = parse_publication(fixture_bytes("pub_two_languages.html"))
    assert [v.language for v in p.documents[0]] == ["en", "sk"]
    assert [f.language for f in select_files(p)] == ["en"]


def test_parse_three_documents():
    p = parse_publication(fixture_bytes("pub_three_files.html"))
    assert [d[0].document for d in p.documents] == ["Final Report", "Final Presentation", "Project Factsheet"]
    assert len(select_files(p)) == 3


def test_select_main_version_when_no_english():
    sk = PublicationFile(document="R", url="https://x/d/1_sk", language="sk")
    cs = PublicationFile(document="R", url="https://x/d/1_cs", language="cs")
    assert select_files(PublicationParse(title="t", documents=[[sk, cs]])) == [sk]


def test_files_key_is_order_independent():
    a = PublicationFile(document="A", url="https://x/1_en")
    b = PublicationFile(document="B", url="https://x/2_en")
    assert files_key([a, b]) == files_key([b, a]) != files_key([a])
