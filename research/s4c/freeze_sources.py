#!/usr/bin/env python3
"""Freeze downloaded wheels into read-only source snapshots for S4c.

Selection rule (declared, applied identically to every source): every ``*.py``
module shipped by the wheel's importable packages, plus the wheel's own license
text. Type stubs, metadata and RECORD are not placed in the actor workspace; the
RECORD is kept in the controller provenance file and used here to verify every
copied byte.
"""
from __future__ import annotations
import base64, csv, hashlib, io, json, shutil, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "research/s4c/data"
WHEELS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/root/reverpi/s4c_work/wheels")

SOURCES = {
    "attrs-26.1.0": {"wheel": "attrs-26.1.0-py3-none-any.whl", "group": "hynek-attrs",
                     "packages": ["attr", "attrs"],
                     "license": "attrs-26.1.0.dist-info/licenses/LICENSE"},
    "dateutil-2.9.0.post0": {"wheel": "python_dateutil-2.9.0.post0-py2.py3-none-any.whl",
                             "group": "dateutil-python-dateutil",
                             "packages": ["dateutil"],
                             "license": "python_dateutil-2.9.0.post0.dist-info/LICENSE"},
    "sortedcontainers-2.4.0": {"wheel": "sortedcontainers-2.4.0-py2.py3-none-any.whl",
                               "group": "grantjenks-sortedcontainers",
                               "packages": ["sortedcontainers"],
                               "license": "sortedcontainers-2.4.0.dist-info/LICENSE"},
    "boltons-26.2.0": {"wheel": "boltons-26.2.0-py3-none-any.whl",
                       "group": "mahmoud-boltons", "packages": ["boltons"],
                       "license": "boltons-26.2.0.dist-info/licenses/LICENSE"},
    "tomlkit-0.15.1": {"wheel": "tomlkit-0.15.1-py3-none-any.whl",
                       "group": "frostming-tomlkit", "packages": ["tomlkit"],
                       "license": "tomlkit-0.15.1.dist-info/licenses/LICENSE"},
    "humanize-4.16.0": {"wheel": "humanize-4.16.0-py3-none-any.whl",
                        "group": "python-humanize-humanize", "packages": ["humanize"],
                        "license": "humanize-4.16.0.dist-info/licenses/LICENCE"},
    "more-itertools-11.1.0": {"wheel": "more_itertools-11.1.0-py3-none-any.whl",
                              "group": "more-itertools", "packages": ["more_itertools"],
                              "license": "more_itertools-11.1.0.dist-info/licenses/LICENSE"},
    "tabulate-0.10.0": {"wheel": "tabulate-0.10.0-py3-none-any.whl",
                        "group": "astanin-python-tabulate", "packages": ["tabulate"],
                        "license": "tabulate-0.10.0.dist-info/licenses/LICENSE"},
    "pyparsing-3.3.3": {"wheel": "pyparsing-3.3.3-py3-none-any.whl",
                        "group": "pyparsing-pyparsing", "packages": ["pyparsing"],
                        "license": "pyparsing-3.3.3.dist-info/licenses/LICENSE"},
    "sqlparse-0.6.0": {"wheel": "sqlparse-0.6.0-py3-none-any.whl",
                       "group": "andialbrecht-sqlparse", "packages": ["sqlparse"],
                       "license": "sqlparse-0.6.0.dist-info/licenses/LICENSE"},
    "idna-3.20": {"wheel": "idna-3.20-py3-none-any.whl",
                  "group": "kjd-idna", "packages": ["idna"],
                  "license": "idna-3.20.dist-info/licenses/LICENSE.md"},
    "markdown-3.10.3": {"wheel": "markdown-3.10.3-py3-none-any.whl",
                        "group": "python-markdown", "packages": ["markdown"],
                        "license": "markdown-3.10.3.dist-info/licenses/LICENSE.md"},
    "filelock-4.0.3": {"wheel": "filelock-4.0.3-py3-none-any.whl",
                       "group": "tox-dev-filelock", "packages": ["filelock"],
                       "license": "filelock-4.0.3.dist-info/licenses/LICENSE"},
    "platformdirs-4.11.12": {"wheel": "platformdirs-4.11.12-py3-none-any.whl",
                             "group": "tox-dev-platformdirs", "packages": ["platformdirs"],
                             "license": "platformdirs-4.11.12.dist-info/licenses/LICENSE"},
    "six-1.17.0": {"wheel": "six-1.17.0-py2.py3-none-any.whl",
                   "group": "benjaminp-six", "packages": ["six"],
                   "license": "six-1.17.0.dist-info/LICENSE"},
    "jinja2-3.1.6": {"wheel": "jinja2-3.1.6-py3-none-any.whl",
                     "group": "pallets-jinja", "packages": ["jinja2"],
                     "license": "jinja2-3.1.6.dist-info/licenses/LICENSE.txt"},
    "urllib3-2.8.0": {"wheel": "urllib3-2.8.0-py3-none-any.whl",
                      "group": "urllib3-urllib3", "packages": ["urllib3"],
                      "license": "urllib3-2.8.0.dist-info/licenses/LICENSE.txt"},
    "colorama-0.4.6": {"wheel": "colorama-0.4.6-py2.py3-none-any.whl",
                       "group": "tartley-colorama", "packages": ["colorama"],
                       "license": "colorama-0.4.6.dist-info/licenses/LICENSE.txt"},
    "docutils-0.23": {"wheel": "docutils-0.23-py3-none-any.whl",
                      "group": "docutils-docutils", "packages": ["docutils"],
                      "license": "docutils-0.23.dist-info/licenses/COPYING.rst"},
    "toml-0.10.2": {"wheel": "toml-0.10.2-py2.py3-none-any.whl",
                    "group": "uiri-toml", "packages": ["toml"],
                    "license": "toml-0.10.2.dist-info/LICENSE"},
    "pygments-2.21.0": {"wheel": "pygments-2.21.0-py3-none-any.whl",
                        "group": "pygments-pygments", "packages": ["pygments"],
                        "license": "pygments-2.21.0.dist-info/licenses/LICENSE"},
    "rich-15.0.0": {"wheel": "rich-15.0.0-py3-none-any.whl",
                    "group": "textualize-rich", "packages": ["rich"],
                    "license": "rich-15.0.0.dist-info/licenses/LICENSE"},
    "networkx-3.7": {"wheel": "networkx-3.7-py3-none-any.whl",
                     "group": "networkx-networkx", "packages": ["networkx"],
                     "license": "networkx-3.7.dist-info/licenses/LICENSE.txt"},
    "pip-26.2.1": {"wheel": "pip-26.2.1-py3-none-any.whl",
                   "group": "pypa-pip", "packages": ["pip"],
                   "license": "pip-26.2.1.dist-info/licenses/LICENSE.txt"},
    "sqlalchemy-2.0.54": {"wheel": "sqlalchemy-2.0.54-py3-none-any.whl",
                          "group": "sqlalchemy-sqlalchemy", "packages": ["sqlalchemy"],
                          "license": "sqlalchemy-2.0.54.dist-info/licenses/LICENSE"},
    "voluptuous-0.16.0": {"wheel": "voluptuous-0.16.0-py3-none-any.whl",
                          "group": "alecthomas-voluptuous", "packages": ["voluptuous"],
                          "license": "voluptuous-0.16.0.dist-info/COPYING"},
    "wheel-0.48.0": {"wheel": "wheel-0.48.0-py3-none-any.whl",
                     "group": "pypa-wheel", "packages": ["wheel"],
                     "license": "wheel-0.48.0.dist-info/licenses/LICENSE.txt"},
    "marshmallow-4.3.1": {"wheel": "marshmallow-4.3.1-py3-none-any.whl",
                          "group": "marshmallow-code-marshmallow", "packages": ["marshmallow"],
                          "license": "marshmallow-4.3.1.dist-info/licenses/LICENSE"},
    "cerberus-1.3.8": {"wheel": "cerberus-1.3.8-py3-none-any.whl",
                       "group": "pyeclipse-cerberus", "packages": ["cerberus"],
                       "license": "cerberus-1.3.8.dist-info/licenses/LICENSE"},
    "pathspec-1.1.1": {"wheel": "pathspec-1.1.1-py3-none-any.whl",
                       "group": "cpburnz-pathspec", "packages": ["pathspec"],
                       "license": "pathspec-1.1.1.dist-info/licenses/LICENSE"},
    "toolz-1.1.0": {"wheel": "toolz-1.1.0-py3-none-any.whl",
                    "group": "pytoolz-toolz", "packages": ["toolz"],
                    "license": "toolz-1.1.0.dist-info/licenses/LICENSE.txt"},
    "funcy-2.1": {"wheel": "funcy-2.1-py3-none-any.whl",
                  "group": "suor-funcy", "packages": ["funcy"],
                  "license": "funcy-2.1.dist-info/licenses/LICENSE"},
    "python-dotenv-1.2.3": {"wheel": "python_dotenv-1.2.3-py3-none-any.whl",
                            "group": "theskumar-python-dotenv", "packages": ["dotenv"],
                            "license": "python_dotenv-1.2.3.dist-info/licenses/LICENSE"},
    "xmltodict-1.0.4": {"wheel": "xmltodict-1.0.4-py3-none-any.whl",
                        "group": "martinblech-xmltodict", "packages": ["xmltodict"],
                        "license": "xmltodict-1.0.4.dist-info/licenses/LICENSE"},
    "jmespath-1.1.0": {"wheel": "jmespath-1.1.0-py3-none-any.whl",
                       "group": "jmespath-jmespath", "packages": ["jmespath"],
                       "license": "jmespath-1.1.0.dist-info/LICENSE"},
}


def record_map(zf: zipfile.ZipFile) -> dict[str, tuple[str, int]]:
    # Only the wheel's own top-level dist-info RECORD; vendored packages ship their own.
    name = next(n for n in zf.namelist() if n.count("/") == 1 and n.endswith(".dist-info/RECORD"))
    out = {}
    rows = csv.reader(io.StringIO(zf.read(name).decode("utf-8")))
    for row in rows:
        if len(row) == 3 and row[1].startswith("sha256=") and row[2].isdigit():
            out[row[0]] = (row[1].split("=", 1)[1], int(row[2]))
    return out


def record_digest(data: bytes) -> str:
    """Wheel RECORD stores urlsafe base64 without padding, not hex."""
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def main() -> int:
    for source_id, spec in SOURCES.items():
        wheel = WHEELS / spec["wheel"]
        raw = wheel.read_bytes()
        zf = zipfile.ZipFile(wheel)
        records = record_map(zf)
        assert spec["license"] in records, "RECORD parse produced no entry for the wheel license"
        target = DATA / "sources" / source_id
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        chosen, verified = [], 0
        for name in sorted(zf.namelist()):
            if name.endswith("/") or not name.endswith(".py"):
                continue
            if not any(name.startswith(pkg + "/") or name == pkg + ".py" for pkg in spec["packages"]):
                continue
            data = zf.read(name)
            digest, size = records[name]
            assert record_digest(data) == digest and len(data) == size, f"RECORD mismatch {name}"
            verified += 1
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            data.decode("utf-8")  # actor workspace must be UTF-8 decodable
            chosen.append(name)
        lic = zf.read(spec["license"])
        assert record_digest(lic) == records[spec["license"]][0], "license RECORD mismatch"
        (target / "LICENSES").mkdir()
        (target / "LICENSES/LICENSE").write_bytes(lic)
        chosen.append("LICENSES/LICENSE")
        files = {n: hashlib.sha256((target / n).read_bytes()).hexdigest() for n in chosen}
        provenance = {
            "schema": "reverpi.s4c.provenance.v1", "source_id": source_id,
            "source_group": spec["group"], "wheel_filename": spec["wheel"],
            "wheel_sha256": hashlib.sha256(raw).hexdigest(), "wheel_bytes": len(raw),
            "acquisition": "downloaded from PyPI (pip download --no-deps) in this session; not an installed-distribution subset",
            "record_verified_files": verified,
            "license_wheel_path": spec["license"],
            "selection_rule": "all *.py of the importable packages listed above, plus the wheel license text",
            "files": files,
        }
        (DATA / "controller").mkdir(parents=True, exist_ok=True)
        out = DATA / "controller" / f"{source_id}.provenance.json"
        out.write_text(json.dumps(provenance, indent=1, sort_keys=True) + "\n")
        print(f"{source_id}: {len(files)} files, RECORD-verified {verified}, wheel {hashlib.sha256(raw).hexdigest()[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
