"""Composants `Code` de Haystack Enterprise Platform (list_projects, find_projects).

Chaque module definit exactement une classe `Code` decoree par @component, autonome (seulement la bibliotheque
standard et haystack) : deploy/prod/build_pipelines.py insere le texte du module tel quel dans le YAML
(`deepset_cloud_custom_nodes.code.code_component.Code`, init_parameters.code). Les tests les importent ici.
"""
