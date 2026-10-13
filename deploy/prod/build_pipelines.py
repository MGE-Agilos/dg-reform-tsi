"""Genere deploy/prod/pipeline-*.yaml a partir de blocs communs (outils identiques dans les deux pipelines).

Lancer depuis la racine du depot : `python deploy/prod/build_pipelines.py`. Les composants `Code` de list_projects et
find_projects viennent de src/tsi_agent/platform_code/ (testes par tests/test_platform_code.py), inseres tels quels.
Ne pas modifier les YAML a la main : modifier ce script ou les modules, puis regenerer.
"""
from pathlib import Path

import yaml

CODE_DIR = Path("src/tsi_agent/platform_code")
ISO_COUNTRIES = ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT",
                 "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"]
POLICY_AREAS = ["competitiveness", "digital-transition", "financial-sector-and-access-finance", "green-transition",
                "health-and-long-term-care", "labour-market-and-social-protection", "migration-management",
                "public-administration-and-governance", "revenue-administration-and-public-financial-management",
                "skills-education-and-training"]
PROGRAMMES = ["TSI", "SRSP", "SRSS"]
STORE = {
    "type": "haystack_integrations.document_stores.opensearch.document_store.OpenSearchDocumentStore",
    "init_parameters": {"hosts": ["${OPENSEARCH_HOST}"], "http_auth": ["${OPENSEARCH_USER}", "${OPENSEARCH_PASSWORD}"],
                        "use_ssl": True, "verify_certs": False, "index": "tsi-corpus", "embedding_dim": 768,
                        "similarity": "cosine"},
}
EMBEDDER = {"type": "deepset_cloud_custom_nodes.embedders.nvidia.text_embedder.DeepsetNvidiaTextEmbedder",
            "init_parameters": {"model": "intfloat/multilingual-e5-base", "prefix": "query: ",
                                "normalize_embeddings": True}}


class _Dumper(yaml.SafeDumper):
    """Pas d'ancres ni d'alias YAML (&id001 / *id001) : la plateforme ne les resout pas (« Missing 'type' »)."""

    def ignore_aliases(self, data):
        return True


def _str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def _code(module: str) -> dict:
    return {"type": "deepset_cloud_custom_nodes.code.code_component.Code",
            "init_parameters": {"code": (CODE_DIR / f"{module}.py").read_text(encoding="utf-8")}}


def _filter_args() -> dict:
    return {
        "countries": {"type": "array", "items": {"type": "string", "enum": ISO_COUNTRIES},
                      "description": "ISO country codes (Greece is GR); a project matches if it involves any of them."},
        "year_from": {"type": "integer", "description": "Keep projects from this year on."},
        "year_to": {"type": "integer", "description": "Keep projects up to this year."},
        "programme": {"type": "string", "enum": PROGRAMMES,
                      "description": "TSI (2021 onwards), SRSP (2017-2020) or SRSS (pre-2017 assistance to Greece)."},
        "policy_area": {"type": "string", "enum": POLICY_AREAS, "description": "Official policy area."},
    }


def _tool_yaml(tool: dict) -> str:
    """Un outil d'agent (dict) en element de liste YAML, indente sous `tools:`."""
    text = yaml.dump([tool], Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=120)
    return "".join("      " + line if line.strip() else line for line in text.splitlines(True))


def list_projects_tool() -> str:
    params = {"type": "object", "properties": {
        **_filter_args(),
        "detail_only": {"type": "boolean", "description": "Only projects with a detail page."},
        "title_contains": {"type": "string", "description": "Word or phrase that must appear in the project title."},
        "csr": {"type": "string", "description": "CSR id or prefix, e.g. csr-HR-2022-1, csr-HR-2022-1.2 or "
                                                 "csr-HR-2022: only projects linked to it (AI-assessed links)."},
        "limit": {"type": "integer", "description": "Maximum number of projects to list (default 20); the totals "
                                                    "always cover every matching project."},
    }}
    return _tool_yaml({"type": "haystack.tools.pipeline_tool.PipelineTool", "data": {
        "name": "list_projects",
        "description": ("Count and list distinct projects matching filters, over the WHOLE corpus (exact, not a "
                        "similarity search). Gives the total and the counts by programme, year, country and policy "
                        "area, then the project list truncated to `limit`, each with its URL. Use it for any number "
                        "or list of projects by country, policy area, programme or year."),
        "parameters": params,
        "input_mapping": {"countries": ["filters.countries", "summary.countries"],
                          "year_from": ["filters.year_from"], "year_to": ["filters.year_to"],
                          "programme": ["filters.programme"],
                          "policy_area": ["filters.policy_area", "summary.policy_area"],
                          "detail_only": ["filters.detail_only"], "title_contains": ["summary.title_contains"],
                          "csr": ["summary.csr"], "limit": ["summary.limit"]},
        "output_mapping": {"summary.result": "result"},
        "outputs_to_string": {"source": "result"},
        "is_pipeline_async": False,
        "pipeline": {
            "components": {
                "filters": _code("filters"),
                "retriever": {"type": "haystack.components.retrievers.filter_retriever.FilterRetriever",
                              "init_parameters": {"document_store": STORE}},
                "summary": _code("list_summary"),
            },
            "connections": [{"sender": "filters.catalog_filters", "receiver": "retriever.filters"},
                            {"sender": "retriever.documents", "receiver": "summary.documents"}],
            "max_runs_per_component": 100, "metadata": {}},
    }})


def find_projects_tool() -> str:
    params = {"type": "object", "properties": {
        "topic": {"type": "string", "description": "English description of the topic, e.g. 'anti-money laundering "
                                                   "and counter-terrorist financing'."},
        "keywords": {"type": "array", "items": {"type": "string"},
                     "description": "3-8 English keywords, synonyms or acronyms searched in project titles, e.g. "
                                    "['money laundering', 'AML', 'CFT', 'financial intelligence', 'FIU']."},
        **_filter_args(),
    }, "required": ["topic"]}

    def retriever(top_k):
        return {"type": "haystack_integrations.components.retrievers.opensearch.embedding_retriever."
                        "OpenSearchEmbeddingRetriever",
                "init_parameters": {"document_store": STORE, "top_k": top_k, "filter_policy": "replace"}}

    return _tool_yaml({"type": "haystack.tools.pipeline_tool.PipelineTool", "data": {
        "name": "find_projects",
        "description": ("List CANDIDATE projects for a free topic, over the whole corpus, for you to judge: the "
                        "projects whose texts best match the topic, those whose deliverables best match it, and those "
                        "whose title contains one of the keywords. No relevance threshold: many candidates are "
                        "off-topic. Keep only those whose title or snippet is really about the topic, then count "
                        "them yourself."),
        "parameters": params,
        "input_mapping": {"topic": ["embedder.text", "aggregate.topic"], "keywords": ["aggregate.keywords"],
                          "countries": ["filters.countries", "aggregate.countries"],
                          "year_from": ["filters.year_from"], "year_to": ["filters.year_to"],
                          "programme": ["filters.programme"], "policy_area": ["filters.policy_area"]},
        "output_mapping": {"aggregate.result": "result"},
        "outputs_to_string": {"source": "result"},
        "is_pipeline_async": False,
        "pipeline": {
            "components": {
                "filters": _code("filters"),
                "embedder": EMBEDDER,
                "catalog": {"type": "haystack.components.retrievers.filter_retriever.FilterRetriever",
                            "init_parameters": {"document_store": STORE}},
                "project_search": retriever(400),
                "deliverable_search": retriever(300),
                "aggregate": _code("find_aggregate"),
            },
            "connections": [
                {"sender": "filters.catalog_filters", "receiver": "catalog.filters"},
                {"sender": "filters.project_filters", "receiver": "project_search.filters"},
                {"sender": "filters.deliverable_filters", "receiver": "deliverable_search.filters"},
                {"sender": "embedder.embedding", "receiver": "project_search.query_embedding"},
                {"sender": "embedder.embedding", "receiver": "deliverable_search.query_embedding"},
                {"sender": "catalog.documents", "receiver": "aggregate.catalog"},
                {"sender": "project_search.documents", "receiver": "aggregate.project_hits"},
                {"sender": "deliverable_search.documents", "receiver": "aggregate.deliverable_hits"},
            ],
            "max_runs_per_component": 100, "metadata": {}},
    }})

# LLM du client : passerelle EC GPT de la Commission (API compatible OpenAI), cle dans le secret GPT_EC du workspace.
# OpenAIChatGenerator (et non OpenAIGenerator) : seul le generateur de chat sait appeler des outils.
LLM_BASE_URL = "https://api.tech.ec.europa.eu/ecgpt/v1"
LLM_KEY_ENV = "GPT_EC"
LLM_MODEL = "gpt-5.1"
GENERATOR = """      chat_generator:
        type: haystack.components.generators.chat.openai.OpenAIChatGenerator
        init_parameters:
          api_base_url: {base_url}
          api_key:
            type: env_var
            env_vars:
            - {key_env}
            strict: false
          model: {model}
          timeout: 180
          max_retries: 2
          generation_kwargs:
            max_completion_tokens: {max_tokens}
"""

BUILDER_TEMPLATE = """                    {%- if existing_documents is not none -%}
                    {%- set existing_doc_len = existing_documents|length -%}
                    {%- else -%}
                    {%- set existing_doc_len = 0 -%}
                    {%- endif -%}
                    {%- for doc in docs %}
                    <document reference="{{loop.index + existing_doc_len}}">
                    {{ doc.content }}
                    </document>
                    {% endfor -%}
"""


def search_tool(name, description, top_k, filter_value):
    if isinstance(filter_value, list):
        flt = ("                    field: meta.doc_type\n                    operator: in\n                    value:\n"
               + "".join(f"                    - {v}\n" for v in filter_value))
    else:
        flt = (f"                    field: meta.doc_type\n                    operator: '=='\n"
               f"                    value: {filter_value}\n")
    return f"""      - type: haystack.tools.pipeline_tool.PipelineTool
        data:
          name: {name}
          description: >-
{description}
          input_mapping:
            query:
            - retriever.query
            documents:
            - builder.existing_documents
          output_mapping:
            builder.prompt: formatted_docs
            retriever.documents: documents
          inputs_from_state:
            documents: documents
          outputs_to_state:
            documents:
              source: documents
          outputs_to_string:
            source: formatted_docs
          parameters:
          is_pipeline_async: false
          pipeline:
            components:
              retriever:
                type: haystack_integrations.components.retrievers.opensearch.open_search_hybrid_retriever.OpenSearchHybridRetriever
                init_parameters:
                  document_store:
                    type: haystack_integrations.document_stores.opensearch.document_store.OpenSearchDocumentStore
                    init_parameters:
                      hosts:
                      - ${{OPENSEARCH_HOST}}
                      http_auth:
                      - ${{OPENSEARCH_USER}}
                      - ${{OPENSEARCH_PASSWORD}}
                      use_ssl: true
                      verify_certs: false
                      index: tsi-corpus
                      embedding_dim: 768
                      similarity: cosine
                  top_k: {top_k}
                  fuzziness: 0
                  filters_bm25:
{flt}                  filters_embedding:
{flt}                  embedder:
                    type: deepset_cloud_custom_nodes.embedders.nvidia.text_embedder.DeepsetNvidiaTextEmbedder
                    init_parameters:
                      model: intfloat/multilingual-e5-base
                      prefix: 'query: '
                      normalize_embeddings: true
              builder:
                type: haystack.components.builders.prompt_builder.PromptBuilder
                init_parameters:
                  required_variables:
                  - existing_documents
                  - docs
                  template: |-
{BUILDER_TEMPLATE}            connections:
            - sender: retriever.documents
              receiver: builder.docs
            max_runs_per_component: 100
            metadata: {{}}
"""


def indent(text, n):
    return "".join(" " * n + line if line.strip() else line for line in text.splitlines(True))


PROJECTS = indent("""Semantic and keyword search in the texts of TSI and SRSP projects and in the TSI country and policy
area factsheets (not the deliverables). Returns numbered passages. Write a precise English query naming the
country, the institution or the reform.""", 12)
DELIVERABLES = indent("""Semantic and keyword search in the full text of the projects' deliverables (reports, studies,
roadmaps, presentations). Returns numbered passages with the document and the page. Write a precise English query
with the project title or topic and the country.""", 12)
CSRS = indent("""Semantic and keyword search in the country-specific recommendations (CSRs) addressed to EU Member
States in the European Semester (2016-2025), with their sub-parts and the Commission's latest assessment of their
implementation. Returns numbered passages with the CSR id. Write a precise English query naming the country and
the policy topic.""", 12)

WEB_SEARCH_OPENAI = """      - type: haystack.tools.component_tool.ComponentTool
        data:
          name: web_search
          description: >-
            Search the web (OpenAI web search) and return the main findings, each with its source URL. Use one
            precise English query per call, naming the country, the institution and the reform.
          inputs_from_state:
          parameters:
            type: object
            properties:
              query:
                type: string
                description: Precise English web search query.
            required:
            - query
          component:
            type: haystack.core.super_component.super_component.SuperComponent
            init_parameters:
              input_mapping:
                query:
                - prompt.query
              output_mapping:
                adapter.output: result
              is_pipeline_async: false
              pipeline:
                components:
                  prompt:
                    type: haystack.components.builders.chat_prompt_builder.ChatPromptBuilder
                    init_parameters:
                      required_variables:
                      - query
                      template: |-
                        {% message role="user" %}
                        Search the web for: {{ query }}

                        Report the most relevant findings for an impact assessment (laws, decisions, indicators,
                        evaluations, criticism), most authoritative sources first: official, OECD, World Bank,
                        European Commission, then press. One line per finding: the finding with its date, then the
                        source URL in parentheses. Only report what the sources say; at most 200 words, in English.
                        If nothing relevant is found, say so.
                        {% endmessage %}
                  searcher:
                    type: haystack.components.generators.chat.openai_responses.OpenAIResponsesChatGenerator
                    init_parameters:
                      api_key:
                        type: env_var
                        env_vars:
                        - OPENAI_API_KEY
                        strict: false
                      model: gpt-5.4
                      tools:
                      - type: web_search
                        search_context_size: low
                      generation_kwargs:
                        reasoning:
                          effort: low
                  adapter:
                    type: haystack.components.converters.output_adapter.OutputAdapter
                    init_parameters:
                      template: '{{ replies[-1].text }}'
                      output_type: str
                      custom_filters: {}
                      unsafe: false
                connections:
                - sender: prompt.prompt
                  receiver: searcher.messages
                - sender: searcher.replies
                  receiver: adapter.replies
                max_runs_per_component: 100
                metadata: {}
"""

TAIL = r"""
  adapter:
    type: haystack.components.converters.output_adapter.OutputAdapter
    init_parameters:
      template: '{{ [(messages|last).text] }}'
      output_type: list[str]
      custom_filters: {}
      unsafe: false

  answer_builder:
    type: haystack.components.builders.answer_builder.AnswerBuilder
    init_parameters:
      reference_pattern: \[(\d+)\]
      last_message_only: false
      return_only_referenced_documents: true

connections:
- sender: agent.messages
  receiver: adapter.messages
- sender: adapter.output
  receiver: answer_builder.replies
- sender: agent.documents
  receiver: answer_builder.documents

inputs:
  messages:
  - agent.messages
  query:
  - answer_builder.query

outputs:
  answers: answer_builder.answers
  documents: agent.documents

pipeline_output_type: chat

max_runs_per_component: 100

metadata: {}
"""


def agent(max_tokens, steps, prompt):
    return f"""components:
  agent:
    type: haystack.components.agents.agent.Agent
    init_parameters:
{GENERATOR.format(max_tokens=max_tokens, base_url=LLM_BASE_URL, key_env=LLM_KEY_ENV, model=LLM_MODEL)}      exit_conditions:
      - text
      max_agent_steps: {steps}
      raise_on_tool_invocation_failure: false
      state_schema:
        documents:
          type: list[haystack.Document]
      streaming_callback: deepset_cloud_custom_nodes.callbacks.streaming.streaming_callback
      system_prompt: |-
{indent(prompt, 8)}
      tools:
"""



def _block(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


# Recherche web SearchApi : meme bloc que le trial (valide sur la plateforme : ~30 s par analyse).
WEB_SEARCH_SEARCHAPI = _block(Path("deploy/trial/pipeline-tsi-impact.yaml").read_text(encoding="utf-8"),
                              "      - type: haystack.tools.component_tool.ComponentTool\n", "\n  adapter:\n") + "\n"

CHAT_PROMPT = """You answer questions about technical support projects of the European Commission (DG REFORM): the
Technical Support Instrument (TSI, 2021 onwards) and its predecessor, the Structural Reform Support
Programme (SRSP, 2017-2020). Some older projects (SRSS, 2012-2016) are pre-SRSP technical assistance to
Greece. You only know what the tools return.

Tools:
- search_projects: project texts and factsheets. Each passage starts with "project title — programme year —
  countries — section" and ends with the project id, policy area, results and source URL.
- search_deliverables: full text of the projects' deliverables. Each passage starts with "publication title —
  document, p. N — programme year — countries" and ends with the deliverable id, the related project ids and
  the document URL.
- search_csrs: EU country-specific recommendations (CSRs, European Semester 2016-2025). Each passage starts with
  "country year CSR n — year — country — section" and ends with the CSR id and the source URL.
- list_projects: exact counts and lists of projects over the whole corpus, with filters (countries, years,
  programme, policy area, detail page, title words, csr).
- find_projects: CANDIDATE projects on a free topic, over the whole corpus (best matches in project texts and
  deliverables plus keywords in titles, no relevance threshold), for you to judge.

Rules:
1. Answer in the language of the user's question.
2. Use only information returned by the tools in this conversation. If it is not there, say so plainly.
3. Cite every factual statement from search_projects or search_deliverables with the passage number, like [n].
   Projects returned by list_projects or find_projects are not numbered passages: cite them with a Markdown link
   to their URL. End with a "Sources" list grouped by project or publication: one line per project or publication
   with all its cited numbers, its title (and the document and page for a deliverable) and its URL, e.g.
   "[2][5][9] Project title — URL". Never put two projects on one line.
4. When a passage is marked as an AI-generated summary, say that the information comes from an AI-generated
   summary rather than from the official text.
5. For any number, count or list of projects by country, policy area, programme or year, call list_projects and
   report its figures; never estimate. For "which countries", "how many per country" or "which policy areas"
   questions, read its "By country" / "By policy area" lines: they are exact over the whole corpus. Do not infer
   countries or areas from the listed projects: that list is truncated to `limit` (raise `limit` when the user
   wants the full list). When you announce a number of projects, it must match what you report.
6. For "which projects", "which countries" or "how many projects" questions about a free topic (not an official
   policy area), call find_projects with an English topic and 3-8 keywords (synonyms, acronyms, English terms),
   e.g. topic "anti-money laundering and counter-terrorist financing", keywords ["money laundering", "AML",
   "CFT", "financial intelligence", "FIU"]. It returns candidates, not answers: go through EVERY candidate and keep
   only those clearly about the topic (judge from the title and snippet; when unsure, check with search_projects).
   Then compute the countries and counts yourself from the kept projects only, list the kept projects with their
   links, and say that the list comes from a candidate search judged by you and may not be exhaustive.
7. For a comparison, search each compared item separately (for example one search per country). For descriptive
   questions, run 2 to 3 searches with different phrasings of the topic (synonyms and related terms).
8. Projects without a detail page ("Detail page: no") are only known by their title, country and year: say so.
9. For follow-up questions, reuse the context of the conversation (countries, topics, projects cited).
10. You have a limited number of steps: after a few tool calls, answer with what you found.
11. Be concise: at most about 250 words unless the user asks for details or a full list; use a short table only
    for comparisons. Use headings sparingly (at most one level).
12. Use search_projects, list_projects and find_projects to identify projects; use search_deliverables for details
    from the reports (figures, methods, recommendations, detailed results), and cite the document and page.
13. Always write tool queries (search queries, find_projects topic and keywords) in English, naming the country,
    the institution and the topic, whatever the language of the question.
14. For questions about country-specific recommendations (CSRs) or the European Semester, call search_csrs and
    cite the CSR id (e.g. csr-FR-2023-1) with its passage number; say that assessments come from the
    Commission's CSR database. Do not state that a project implements a CSR unless a tool result says so.
    To list the projects linked to a CSR, call list_projects with csr (a CSR id or a prefix such as
    csr-HR-2022); these links are assessed by an AI model: say so.

Filter values: countries are ISO codes (AT Austria, BE Belgium, BG Bulgaria, HR Croatia, CY Cyprus, CZ Czechia,
DK Denmark, EE Estonia, FI Finland, FR France, DE Germany, GR Greece, HU Hungary, IE Ireland, IT Italy, LV Latvia,
LT Lithuania, LU Luxembourg, MT Malta, NL Netherlands, PL Poland, PT Portugal, RO Romania, SK Slovakia, SI Slovenia,
ES Spain, SE Sweden). Programmes: TSI, SRSP, SRSS. Policy areas: competitiveness, digital-transition,
financial-sector-and-access-finance, green-transition, health-and-long-term-care,
labour-market-and-social-protection, migration-management, public-administration-and-governance,
revenue-administration-and-public-financial-management, skills-education-and-training."""


CHAT_PROMPT_SEARCH_ONLY = """You answer questions about technical support projects of the European Commission (DG REFORM): the
Technical Support Instrument (TSI, 2021 onwards) and its predecessor, the Structural Reform Support
Programme (SRSP, 2017-2020). Some older projects (SRSS, 2012-2016) are pre-SRSP technical assistance to
Greece. You only know what the tools return.

Tools:
- search_projects: project texts and factsheets. Each passage starts with "project title — programme year —
  countries — section" and ends with the project id, policy area, results and source URL.
- search_deliverables: full text of the projects' deliverables. Each passage starts with "publication title —
  document, p. N — programme year — countries" and ends with the deliverable id, the related project ids and
  the document URL.
- search_csrs: EU country-specific recommendations (CSRs, European Semester 2016-2025). Each passage starts with
  "country year CSR n — year — country — section" and ends with the CSR id and the source URL.

Rules:
1. Answer in the language of the user's question.
2. Use only information returned by the tools in this conversation. If it is not there, say so plainly.
3. Cite every factual statement with the passage number, like [n]. End with a "Sources" list grouped by project or
   publication: one line per project or publication with all its cited numbers, its title (and the document and
   page for a deliverable) and its URL, e.g. "[2][5][9] Project title — URL". Never put two projects on one line.
4. When a passage is marked as an AI-generated summary, say that the information comes from an AI-generated
   summary rather than from the official text.
5. Counts and lists of projects come from a search, not from a complete database: give the projects you found
   with their citations and say that the list may not be exhaustive. Never estimate a total; when you announce a
   number of projects, it must match the projects you list.
6. For a list or a comparison, run 2 to 3 searches with different phrasings of the topic (synonyms and related
   terms, e.g. "e-government", "digital public services", "interoperability", "digitalisation of justice"), and
   search each compared item separately (for example one search per country).
7. Projects without a detail page ("Detail page: no") are only known by their title, country and year: say so.
8. For follow-up questions, reuse the context of the conversation (countries, topics, projects cited).
9. You have a limited number of steps: after a few searches, answer with what you found.
10. Be concise: at most about 250 words unless the user asks for details or a full list; use a short table only
    for comparisons. Use headings sparingly (at most one level).
11. Use search_projects to identify projects; use search_deliverables for details from the reports (figures,
    methods, recommendations, detailed results), and cite the document and page.
12. Always write search queries in English, naming the country, the institution and the topic, whatever the
    language of the question.
13. For questions about country-specific recommendations (CSRs) or the European Semester, call search_csrs and
    cite the CSR id (e.g. csr-FR-2023-1) with its passage number; say that assessments come from the
    Commission's CSR database. Do not state that a project implements a CSR unless a tool result says so."""


def impact_prompt(web_tool: str, limit: str) -> str:
    return f"""You are an impact analyst for the European Commission's DG REFORM. You assess the impact of one Technical
Support Instrument (TSI) or SRSP project on its country, on Europe and on the world, against the project's own
objectives. The user gives a project title or id.

Method:
1. Call search_projects with the project title (and country) to read the project: identify its objectives and the
   actions carried out. Passages end with the project id and its source URL. Use search_deliverables for details
   from its reports (recommendations, figures, outputs).
2. Search the web with web_search ({web_tool}) for evidence of impact: adoption and implementation of
   the reform (laws, decrees, action plans), indicators before and after, evaluations, OECD / World Bank / European
   Commission documents (European Semester country reports, recovery and resilience plans), government sites, press
   articles, criticism. Use precise English queries naming the country, the institution and the reform. {limit}

Rules:
- Cite project and deliverable passages with their number [n]; cite web sources with a Markdown link to the source
  URL given by web_search, e.g. ([OECD 2024](https://...)). Never invent a source, a URL, a figure or an impact.
- Distinguish correlation from causation: say how directly the evidence can be attributed to the project (causal
  link established, plausible, or unclear).
- When evidence is missing or too recent, say so: a short, honest report is better than a speculative one.
- Write in English, concise and factual.

Answer with a Markdown report with these sections:
**Project overview** (objectives and actions), **Evidence of positive impact**, **Evidence of negative impact or
limitations**, **Impact by level** (country, Europe, world), **Objectives** (a table: objective | verdict:
achieved, partly achieved, not achieved or insufficient evidence | rationale), **Overall verdict** (one or two
sentences).
End with exactly one line: "Impact score: X/10 (strong | moderate | weak) — Confidence: high | medium | low".
The score rates the POSITIVE impact documented in your report, on outcomes rather than outputs:
- 0: no evidence of positive impact (outputs not used, reform abandoned, contradicted or reversed).
- 1-3 (weak): outputs delivered (reports, recommendations, trainings) but little or no evidence of uptake; a reform
  adopted outside the project or going against its objectives counts as no uptake.
- 4-6 (moderate): the supported reform was adopted or partly implemented, attribution to the project plausible but
  partial or early.
- 7-8 (strong): documented results or outcomes plausibly attributable to the project.
- 9-10 (strong): well-documented, sustained outcomes clearly attributable to the project.
An objective whose intended reform did not happen, or went the other way, is "not achieved", not "insufficient
evidence"."""


CHAT_HEADER = """# Pipeline "tsi-chat" (production) : chatbot RAG sur l'index tsi-corpus, gpt-5.1 via EC GPT. GENERE par
# deploy/prod/build_pipelines.py : ne pas modifier a la main.
# search_projects (projets et fiches programme) et search_deliverables (texte integral des livrables) : recherche
# hybride filtree par la metadonnee doc_type, numerotation [n] continue (etat documents).
# list_projects : comptes et listes exacts sur tout le corpus (FilterRetriever + composants Code).
# find_projects : candidats pour un sujet libre (400 morceaux projet + 300 morceaux livrable regroupes par projet,
# plus mots-cles des titres), juges par l'agent. Historique de la conversation par l'entree `messages`.
"""
IMPACT_HEADER = """# Pipeline "tsi-impact" (production) : analyse d'impact d'un projet, gpt-5.1 via EC GPT (secret GPT_EC).
# Outils : search_projects, search_deliverables (index tsi-corpus) et web_search (SearchApi : 5 resultats, pages lues
# par l'outil ; 4 recherches au plus). Valide sur le trial : ~30 s par analyse, sous la limite de 2 minutes d'une
# recherche (Search). Variante avec la recherche web integree d'OpenAI : pipeline-tsi-impact-openai-search.yaml.
# Score 0-10 donne par l'agent dans la derniere ligne du rapport. Requete attendue : le titre ou l'id du projet.
"""
IMPACT_OPENAI_HEADER = """# Pipeline "tsi-impact", VARIANTE recherche web OpenAI integree (non retenue par defaut) : web_search est un
# sous-pipeline qui appelle OpenAI (Responses API) avec son outil web_search et renvoie constats + URL (un Agent
# Haystack ne peut pas melanger cet outil integre et ses propres outils). Chaque recherche prend plusieurs dizaines
# de secondes : sur le trial, l'analyse a ete coupee apres 2 a 3 minutes (CancelledError), meme allegee (3
# recherches, contexte reduit). A n'utiliser que si le plan du client permet une duree plus longue ou les Jobs, et
# avec un acces direct a OpenAI (OPENAI_API_KEY, API Responses) : la passerelle EC GPT n'est pas prevue pour cet outil.
"""
CHAT_UI_HEADER = """# Pipeline "tsi-chat", version SANS list_projects ni find_projects (gpt-5.1 via EC GPT). GENERE par
# deploy/prod/build_pipelines.py. A utiliser quand les fichiers sont televerses par l'interface : un lot ne porte que
# des metadonnees communes (Topic, corpus, doc_type), pas les pays, annees ou id de chaque projet dont ces deux outils
# ont besoin. search_projects / search_deliverables filtrent sur doc_type : ils fonctionnent.
"""

out = Path("deploy/prod")
tools_impact = (search_tool("search_projects", PROJECTS, 8, ["project", "programme"])
                + search_tool("search_deliverables", DELIVERABLES, 6, "deliverable"))
(out / "pipeline-tsi-chat.yaml").write_text(
    CHAT_HEADER + agent(4096, 15, CHAT_PROMPT) + search_tool("search_projects", PROJECTS, 10, ["project", "programme"])
    + search_tool("search_deliverables", DELIVERABLES, 8, "deliverable")
    + search_tool("search_csrs", CSRS, 6, "csr") + list_projects_tool() + find_projects_tool()
    + TAIL, encoding="utf-8", newline="\n")
(out / "pipeline-tsi-chat-ui.yaml").write_text(
    CHAT_UI_HEADER + agent(4096, 12, CHAT_PROMPT_SEARCH_ONLY)
    + search_tool("search_projects", PROJECTS, 10, ["project", "programme"])
    + search_tool("search_deliverables", DELIVERABLES, 8, "deliverable")
    + search_tool("search_csrs", CSRS, 6, "csr") + TAIL, encoding="utf-8", newline="\n")
(out / "pipeline-tsi-impact.yaml").write_text(
    IMPACT_HEADER + agent(8000, 12, impact_prompt("it returns, for each result, its URL and the page text",
                                                  "At most 4 web searches: you have a strict time limit."))
    + tools_impact + WEB_SEARCH_SEARCHAPI + TAIL, encoding="utf-8", newline="\n")
(out / "pipeline-tsi-impact-openai-search.yaml").write_text(
    IMPACT_OPENAI_HEADER + agent(8000, 12, impact_prompt("it returns findings with their source URLs",
                                                         "At most 3 web searches, each one slow: you have a strict "
                                                         "time limit."))
    + tools_impact + WEB_SEARCH_OPENAI + TAIL, encoding="utf-8", newline="\n")
print("ok")
