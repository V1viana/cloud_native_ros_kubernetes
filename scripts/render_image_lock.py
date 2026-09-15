#!/usr/bin/env python3
"""Replace project image aliases with immutable references from a release lock."""

import argparse
import json
import os
import tempfile
from pathlib import Path

import yaml


POD_TEMPLATE_PATHS = {
    "Deployment": ("spec", "template", "spec"),
    "StatefulSet": ("spec", "template", "spec"),
    "DaemonSet": ("spec", "template", "spec"),
    "ReplicaSet": ("spec", "template", "spec"),
    "Job": ("spec", "template", "spec"),
    "CronJob": ("spec", "jobTemplate", "spec", "template", "spec"),
    "Pod": ("spec",),
}


def load_aliases(lock_path):
    lock = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1:
        raise ValueError("unsupported image lock schema")

    aliases = {}
    for item in lock.get("images", []):
        immutable = item.get("immutable_reference", "")
        if "@sha256:" not in immutable:
            raise ValueError(f"invalid immutable reference for {item.get('name')}")
        for alias in item.get("aliases", []):
            previous = aliases.setdefault(alias, immutable)
            if previous != immutable:
                raise ValueError(f"alias maps to multiple digests: {alias}")
    if not aliases:
        raise ValueError("image lock has no aliases")
    return lock, aliases


def replace_images(value, aliases):
    replacements = []
    if isinstance(value, dict):
        rendered = {}
        for key, item in value.items():
            if key == "image" and isinstance(item, str) and item in aliases:
                rendered[key] = aliases[item]
                replacements.append((item, aliases[item]))
            else:
                rendered[key], nested = replace_images(item, aliases)
                replacements.extend(nested)
        return rendered, replacements
    if isinstance(value, list):
        rendered = []
        for item in value:
            new_item, nested = replace_images(item, aliases)
            rendered.append(new_item)
            replacements.extend(nested)
        return rendered, replacements
    return value, replacements


def nested_mapping(document, path):
    current = document
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, dict) else None


def add_pull_secret(document, secret_name):
    pod_spec = nested_mapping(document, POD_TEMPLATE_PATHS.get(document.get("kind"), ()))
    if pod_spec is None:
        return
    pull_secrets = pod_spec.setdefault("imagePullSecrets", [])
    if not any(item.get("name") == secret_name for item in pull_secrets):
        pull_secrets.append({"name": secret_name})


def add_kuberos_registry(document, secret_name):
    registries = document.setdefault("containerRegistry", [])
    registry = next(
        (item for item in registries if item.get("name") == "default"), None
    )
    if registry is None:
        registry = {"name": "default"}
        registries.append(registry)
    registry["imagePullSecretName"] = secret_name
    registry["imagePullPolicy"] = "Always"

    for module in document.get("rosModules", []):
        if "@sha256:" in str(module.get("image", "")):
            module["imagePullPolicy"] = "Always"


def render_documents(documents, aliases, secret_name):
    rendered = []
    replacements = []
    for document in documents:
        new_document, document_replacements = replace_images(document, aliases)
        if document_replacements:
            if new_document.get("kind") == "ApplicationDeployment":
                add_kuberos_registry(new_document, secret_name)
            else:
                add_pull_secret(new_document, secret_name)
        rendered.append(new_document)
        replacements.extend(document_replacements)
    return rendered, replacements


def load_documents(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [document for document in yaml.safe_load_all(stream) if document]


def write_documents(path, documents):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump_all(
                documents, stream, sort_keys=False, explicit_start=True
            )
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def render(input_path, output_path, lock_path, secret_name):
    lock, aliases = load_aliases(lock_path)
    documents, replacements = render_documents(
        load_documents(input_path), aliases, secret_name
    )
    write_documents(output_path, documents)
    return {
        "release": lock.get("release"),
        "repository_commit": lock.get("repository_commit"),
        "replacements": len(replacements),
        "images": sorted({new for _, new in replacements}),
    }


def main(args=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--pull-secret", required=True)
    parsed = parser.parse_args(args)
    summary = render(
        parsed.input, parsed.output, parsed.lock, parsed.pull_secret
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
