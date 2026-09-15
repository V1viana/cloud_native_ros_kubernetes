#!/usr/bin/env python3
"""Render complete KubeROS update-demo manifests from named overrides."""

import argparse
import copy
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = PROJECT_ROOT / 'manifests' / 'kuberos' / 'update_demo'
BASE_PATH = DEMO_DIR / 'base.yaml'
VARIANTS_DIR = DEMO_DIR / 'variants'
GENERATED_DIR = DEMO_DIR / 'generated'


def load_yaml(path: Path) -> dict:
    with path.open(encoding='utf-8') as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError(f'{path} must contain a YAML object.')
    return data


def deep_merge(target: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def apply_named_overrides(items: list, overrides: dict, section: str) -> None:
    by_name = {item.get('name'): item for item in items}
    for name, override in overrides.items():
        if name not in by_name:
            raise ValueError(f'{section} override references unknown name: {name}')
        deep_merge(by_name[name], override)


def replace_project_root(value):
    if isinstance(value, dict):
        return {key: replace_project_root(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_project_root(item) for item in value]
    if isinstance(value, str):
        return value.replace('__PROJECT_ROOT__', str(PROJECT_ROOT))
    return value


def render_variant(base: dict, variant: dict) -> dict:
    manifest = copy.deepcopy(base)
    deep_merge(manifest['metadata'], variant.get('metadata', {}))
    apply_named_overrides(
        manifest.get('rosModules', []),
        variant.get('rosModules', {}),
        'rosModules',
    )
    apply_named_overrides(
        manifest.get('rosParamMap', []),
        variant.get('rosParamMap', {}),
        'rosParamMap',
    )
    return replace_project_root(manifest)


def rendered_manifests() -> dict:
    base = load_yaml(BASE_PATH)
    rendered = {}
    for variant_path in sorted(VARIANTS_DIR.glob('*.yaml')):
        variant = load_yaml(variant_path)
        output_name = variant.get('output')
        if not output_name:
            raise ValueError(f'{variant_path} does not define output.')
        if output_name in rendered:
            raise ValueError(f'Duplicate output manifest: {output_name}')
        rendered[output_name] = render_variant(base, variant)
    return rendered


def serialized_manifest(manifest: dict) -> str:
    return yaml.safe_dump(
        manifest,
        sort_keys=False,
        default_flow_style=False,
    )


def write_manifests(output_dir: Path, check: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    for output_name, manifest in rendered_manifests().items():
        destination = output_dir / output_name
        content = serialized_manifest(manifest)
        if check:
            if not destination.exists() or destination.read_text() != content:
                failures.append(str(destination))
        else:
            destination.write_text(content)

    if failures:
        raise SystemExit(
            'Generated manifests are stale or missing: ' + ', '.join(failures)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--output-dir', type=Path, default=GENERATED_DIR)
    args = parser.parse_args()
    write_manifests(args.output_dir, check=args.check)


if __name__ == '__main__':
    main()
