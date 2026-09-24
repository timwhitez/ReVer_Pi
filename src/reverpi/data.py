from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from .config import StrictModel
from .errors import LabError
from .memory import Record
from .util import atomic_write, canonical, digest, bytes_digest, source_manifest, strict_json_loads


class Question(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1)
    category: Literal["G", "C", "V", "E"]
    tag: str | None = None  # Only used by MOCK fixtures, not an answer/gold value.


class Checkpoint(StrictModel):
    id: str
    split: Literal["search", "dev", "gate", "external"]
    source_group: str = Field(min_length=1)
    provenance: dict[str, str]
    task: str
    records: list[Record] = Field(min_length=1)
    questions: list[Question] = Field(min_length=1)
    synthetic: bool = False

    @model_validator(mode="after")
    def unique_components(self):
        if len({x.id for x in self.records}) != len(self.records) or len({x.id for x in self.questions}) != len(self.questions):
            raise ValueError("Record and question identifiers must be unique within each task")
        return self

    def source_keys(self) -> set[str]:
        return source_identity_keys(self.source_group, self.provenance)


def source_identity_keys(group: str, provenance: dict[str, str]) -> set[str]:
    """Only identity-bearing fields, not generic license/version labels, define overlap."""
    norm = {k: v.strip().lower().rstrip("/") for k,v in provenance.items() if v.strip()}
    repo = norm.get("repository_family", norm.get("repo_family", norm.get("repository", norm.get("repo", ""))))
    # Normalize well-defined host/transport aliases, not guessed fork ancestry.
    # Arbitrary curator family identifiers retain their original semantics.
    for host in ("github.com", "gitlab.com", "bitbucket.org"):
        for prefix in (f"git@{host}:", f"ssh://git@{host}/", f"git://{host}/", f"http://{host}/", f"{host}/"):
            if repo.startswith(prefix):
                repo = f"https://{host}/" + repo[len(prefix):]
                break
    if repo.endswith(".git"):
        repo = repo[:-4]
    keys = {"group:" + group.strip().lower()}
    if repo:
        keys.add("repo:" + repo)
    for k in ("patch_sha", "commit", "template", "benchmark", "upstream_family"):
        if k in norm:
            keys.add(k + ":" + norm[k])
    for k in ("issue", "pr"):
        if k in norm:
            # Numeric issue/PR ids are meaningful only in the normalized repository.
            if not repo:
                raise ValueError("issue/pr provenance requires a normalized repository identity")
            keys.add(k + ":" + repo + ":" + norm[k])
    return keys


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with Path(path).open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                value = strict_json_loads(line)
            except ValueError as exc:
                raise ValueError(f"Invalid JSON at line {lineno} in {path.name}") from exc
            if not isinstance(value, dict):
                raise ValueError("JSONL records must be objects")
            rows.append(value)
    return rows


def checkpoints(path: Path) -> list[Checkpoint]:
    rows = [Checkpoint.model_validate(r) for r in load_jsonl(path)]
    ids = [r.id for r in rows]
    if not rows or len(set(ids)) != len(ids):
        raise ValueError("Empty dataset or duplicate task identifiers")
    for r in rows:
        qids = [q.id for q in r.questions]
        if not qids or len(set(qids)) != len(qids):
            raise ValueError("Empty/duplicate question identifiers")
        if not r.source_group or not r.provenance:
            raise ValueError("Source grouping/provenance is mandatory")
    return rows


def audit(paths: list[Path]) -> dict:
    all_rows = [r for p in paths for r in checkpoints(p)]
    id_owner: dict[str, Checkpoint] = {}
    keys: dict[str, list[Checkpoint]] = {}
    texts: dict[str, Checkpoint] = {}
    conflicts = []
    for r in all_rows:
        if r.id in id_owner:
            conflicts.append({"kind": "duplicate_id", "left": r.id, "right": r.id})
        id_owner[r.id] = r
        fingerprint = digest({"task": r.task, "records": [x.model_dump(exclude={"id"}) for x in r.records]})
        if fingerprint in texts and texts[fingerprint].split != r.split:
            conflicts.append({"kind": "identical_content", "left": texts[fingerprint].id, "right": r.id})
        texts[fingerprint] = r
        for key in r.source_keys():
            for other in keys.get(key, []):
                if other.split != r.split:
                    conflicts.append({"kind": "shared_source", "key": key, "left": other.id, "right": r.id})
            keys.setdefault(key, []).append(r)
    # Approximate text matching produces REVIEW FLAGS, never proof of no contamination.
    flagged = []
    bags = [set(re.findall(r"\w+", (r.task + " " + " ".join(x.text for x in r.records)).lower())) for r in all_rows]
    if len(all_rows) <= 2000:
        for i, a in enumerate(all_rows):
            for j in range(i):
                b = all_rows[j]
                if a.split == b.split:
                    continue
                union = bags[i] | bags[j]
                similarity = len(bags[i] & bags[j]) / max(1, len(union))
                if similarity >= .85:
                    flagged.append({"left": a.id, "right": b.id, "jaccard": similarity})
    return {"tasks": len(all_rows), "conflicts": conflicts, "near_duplicate_review_flags": flagged,
            "near_duplicate_scan": "executed" if len(all_rows) <= 2000 else "not_executed_dataset_too_large",
            "splits_present": sorted({r.split for r in all_rows}),
            "project_disjoint": (not conflicts and not flagged) if len({r.split for r in all_rows}) >= 2 and len(all_rows) <= 2000 else None,
            "pretraining_contamination": "unknown",
            "source_metadata_quality": "curator-supplied; repository ancestry is not inferred automatically"}


def freeze(root: Path, config: Path, public: Path, gold: Path, all_public: list[Path], out: Path):
    if out.exists():
        raise ValueError("Freeze exists; create a new explicit generation instead of overwriting")
    if public.resolve() not in {p.resolve() for p in all_public}:
        raise LabError("audit_missing_target", "The target dataset must be included in all-public audit inputs")
    if gold.resolve() in {public.resolve(), *(p.resolve() for p in all_public)}:
        raise LabError('gold_public_alias', 'Evaluator gold and public-source inputs must be distinct files')
    report = audit(all_public)
    if report["conflicts"] or report["near_duplicate_review_flags"] or report["near_duplicate_scan"] != "executed":
        raise LabError("dataset_overlap", "Resolve source conflicts/near-duplicate flags before freezing")
    rows = checkpoints(public)
    if any(r.synthetic for r in rows) and any(r.split == "external" for r in rows):
        raise LabError("synthetic_external", "Bundled/generated fixtures cannot be marketed as independent external benchmarks")
    if any(r.split in {"gate", "external"} for r in rows) and not {"search", "dev"}.intersection(report["splits_present"]):
        raise LabError("audit_scope", "Held-out freeze requires the development-source inventory, not only the target split")
    manifest = {"schema": 1, "code_sha": digest(source_manifest(root)), "config_sha": bytes_digest(config.read_bytes()),
                "public_sha": bytes_digest(public.read_bytes()), "gold_sha": bytes_digest(gold.read_bytes()),
                "source_files": {str(p.resolve()): bytes_digest(p.read_bytes()) for p in all_public},
                "audit": report, "task_ids": [r.id for r in rows]}
    atomic_write(out, canonical(manifest))
    return manifest


def check_freeze(path: Path, root: Path, config: Path, public: Path, gold: Path):
    expected = json.loads(path.read_text())
    actual = {"code_sha": digest(source_manifest(root)), "config_sha": bytes_digest(config.read_bytes()),
              "public_sha": bytes_digest(public.read_bytes()), "gold_sha": bytes_digest(gold.read_bytes())}
    if any(expected.get(k) != v for k, v in actual.items()):
        raise LabError("freeze_changed", "Code/config/data differ from the frozen manifest")
    for p, sha in expected.get("source_files", {}).items():
        if not Path(p).exists() or bytes_digest(Path(p).read_bytes()) != sha:
            raise LabError("freeze_changed", "A source-audit input has changed or moved; freeze must be portable-rebased explicitly")
    return expected


def make_fixtures(folder: Path, n: int = 12):
    """Search-only fixtures. No fabricated external benchmark or hidden sample count."""
    if any((folder/name).exists() for name in ("public.jsonl", "gold.jsonl")):
        raise ValueError("Fixture outputs already exist; use a new directory")
    folder.mkdir(parents=True, exist_ok=True)
    public, gold = [], []
    for i in range(n):
        taskid = f"fixture-{i:03}"
        records = [
            Record(id="goal", kind="goal", text=f"GOAL=deliver_{i} Keep all requirements while completing the task."),
            Record(id="rule", kind="constraint", text=f"RULE=no_network_{i} Do not access the internet."),
            Record(id="initial", kind="edit", text="Working tree initialized at version A", updates={"workspace": "A"}),
            Record(id="old", kind="verification", text=f"Tests passed on workspace A. EVIDENCE=old_{i}", dependencies={"workspace": "A"}),
        ]
        records += [Record(id=f"noise-{j}", kind="observation", text=(f"Routine build progress section {j}.\n" * 30)) for j in range(6)]
        records += [Record(id="edit", kind="edit", text="Dependencies changed; previous passing result is not current. LIVE=unknown", updates={"workspace": "B"}),
                    Record(id="obligation", kind="obligation", text=f"EVIDENCE=log_{i} Re-run checks before claiming current success.", dependencies={"workspace": "B"})]
        qs = [Question(id="goal", text="What is the GOAL identifier?", category="G", tag="GOAL"),
              Question(id="rule", text="What is the active RULE identifier?", category="C", tag="RULE"),
              Question(id="validity", text="What is the LIVE current validation state?", category="V", tag="LIVE"),
              Question(id="evidence", text="What is the most recent EVIDENCE identifier?", category="E", tag="EVIDENCE")]
        public.append(Checkpoint(id=taskid, split="search", source_group="synthetic-revision-template-v1",
                                 provenance={"template": "rever-owned-revision-v1"}, task="Continue the revision-sensitive task.",
                                 records=records, questions=qs, synthetic=True).model_dump())
        gold.append({"id": taskid, "answers": {"goal": [f"deliver_{i}"], "rule": [f"no_network_{i}"], "validity": ["unknown"], "evidence": [f"log_{i}"]}})
    atomic_write(folder / "public.jsonl", "\n".join(canonical(x) for x in public) + "\n")
    atomic_write(folder / "gold.jsonl", "\n".join(canonical(x) for x in gold) + "\n")
    atomic_write(folder / "MANIFEST.json", canonical({"purpose": "SEARCH/PROTOCOL FIXTURES ONLY", "independent_benchmark": False,
                                                     "tasks": n, "source_groups": 1, "public_sha": bytes_digest((folder/'public.jsonl').read_bytes())}))
