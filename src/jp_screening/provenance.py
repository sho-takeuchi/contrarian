"""Versioned provenance shared with optional analysis, without optional dependencies."""
from hashlib import sha256
from pathlib import Path


def enrich_manifest(manifest, folder, scope=None):
    source = Path(__file__).parent
    rules = sha256(b"".join((source / name).read_bytes() for name in
                          ("core.py", "weekly.py", "daily.py"))).hexdigest()
    manifest.update(format_version=2, rules_version=rules,
                    source_scope=scope or manifest.get("source_scope", "unknown"),
                    artifact_hashes={p.name:sha256(p.read_bytes()).hexdigest()
                                     for p in sorted(Path(folder).glob("*.csv"))})
    return manifest
