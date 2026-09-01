"""Dataset and scenario manifests.

A benchmark result is only meaningful if the data behind it can be regenerated
exactly. The manifest is the record that makes that possible: it carries the
dataset id, the generator version, the seed, the full parameter set, the
generation timestamp and (for scenarios) the scenario id.

Two ids appear here and they answer different questions:

``dataset_id``   content-addressed from the generator config *and* the generator
                 version. Two datasets with the same id contain the same data.
``scenario_id``  content-addressed from the dataset id plus the scenario spec.
                 Identifies "this shock, on this network".

Because both are derived from content rather than assigned, a manifest cannot
drift out of sync with the thing it describes: change any parameter and the id
changes with it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lce import __version__
from lce.data.generator import GENERATOR_VERSION, GeneratorConfig
from lce.seeds import SeedBundle, config_hash

MANIFEST_FILENAME = "manifest.json"


@dataclass(slots=True)
class DatasetManifest:
    """Everything needed to regenerate a dataset byte-for-byte."""

    dataset_id: str
    generator_version: str
    code_version: str
    seed: int
    parameters: dict[str, Any]
    created_at: str
    scale: str | None = None
    seeds: dict[str, int] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    scenario_id: str | None = None
    notes: str = ""

    @classmethod
    def for_config(
        cls,
        config: GeneratorConfig,
        *,
        seeds: SeedBundle | None = None,
        scale: str | None = None,
        stats: dict[str, Any] | None = None,
        scenario_id: str | None = None,
        notes: str = "",
    ) -> DatasetManifest:
        return cls(
            dataset_id=config.dataset_version,
            generator_version=GENERATOR_VERSION,
            code_version=__version__,
            seed=config.seed,
            parameters=config.to_dict(),
            created_at=datetime.now(tz=UTC).isoformat(),
            scale=scale,
            seeds=seeds.to_dict() if seeds else {},
            stats=stats or {},
            scenario_id=scenario_id,
            notes=notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True, default=str)

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / MANIFEST_FILENAME
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, directory: Path) -> DatasetManifest:
        path = Path(directory) / MANIFEST_FILENAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(**payload)

    def rebuild_config(self) -> GeneratorConfig:
        """Reconstruct the generator config that produced this dataset.

        Parameters written by a *different* generator version are rejected
        rather than silently coerced: the same config under different generative
        semantics is a different dataset, and pretending otherwise is how
        benchmark results quietly stop being comparable.
        """
        if self.generator_version != GENERATOR_VERSION:
            raise ValueError(
                f"manifest was written by generator {self.generator_version}, "
                f"but this build is {GENERATOR_VERSION}; regenerate the dataset "
                "or check out the matching code version"
            )
        fields = set(GeneratorConfig.__dataclass_fields__)
        return GeneratorConfig(
            **{k: v for k, v in self.parameters.items() if k in fields}
        )

    def verify(self) -> None:
        """Confirm the recorded parameters really hash to the recorded id."""
        rebuilt = self.rebuild_config()
        if rebuilt.dataset_version != self.dataset_id:
            raise ValueError(
                f"manifest is inconsistent: parameters hash to "
                f"{rebuilt.dataset_version!r} but dataset_id is {self.dataset_id!r}"
            )


def make_scenario_id(dataset_id: str, spec_payload: dict[str, Any]) -> str:
    """Content-addressed id for a (dataset, scenario spec) pair."""
    return f"scn-{config_hash({'dataset': dataset_id, 'spec': spec_payload}, length=12)}"
