#!/usr/bin/env python3
"""Write S4c tasks and gold files.

The ``EXPECTED`` values below are the specification. They were derived by reading
the frozen source (named locations are recorded in SOURCE_NOTES), then the
matching oracle under ``oracles/`` was executed against the same frozen snapshot.
This script refuses to write anything unless the two agree exactly.
"""
from __future__ import annotations
import hashlib, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from reverpi.util import canonical, digest  # noqa: E402

DATA = ROOT / "research/s4c/data"

SOURCE_NOTES = {
    "attrs-define": ["attr/_make.py `evolve` reuses `getattr(inst, name)` by reference",
                     "attr/_make.py slotted-class builder adds `__weakref__` when weakref_slot is on",
                     "attr/_funcs.py `asdict`"],
    "attrs-validators": ["attr/validators.py `instance_of` documents TypeError",
                         "attr/validators.py `in_` documents ValueError",
                         "attr/validators.py `optional` and `deep_iterable`"],
    "dateutil-rrule": ["dateutil/rrule.py `bymonthday` defaulting and monthly frequency expansion"],
    "dateutil-relative": ["dateutil/relativedelta.py `min(calendar.monthrange(...)[1], ...)` clamping",
                          "dateutil/parser/_parser.py `dayfirst` and `default` handling"],
    "sortedcontainers-list": ["sortedcontainers/sortedlist.py `remove` removes one occurrence"],
    "sortedcontainers-dict": ["sortedcontainers/sorteddict.py `peekitem`, `irange`, `setdefault`",
                              "sortedcontainers/sortedset.py set algebra"],
    "boltons-multidict": ["boltons/dictutils.py `OMD` list-value semantics and `todict`"],
    "boltons-indexedset": ["boltons/setutils.py `IndexedSet` de-duplication and re-add ordering",
                           "boltons/dictutils.py `OMD.inverted`"],
    "tomlkit-roundtrip": ["tomlkit/container.py and tomlkit/items.py comment retention on assignment",
                          "tomlkit/api.py `dumps`"],
    "tomlkit-datetimes": ["tomlkit/items.py Date/Time/DateTime wrappers and `as_string`"],
    "humanize-numbers": ["humanize/number.py `intcomma`, `intword`, `metric`",
                         "humanize/filesize.py `naturalsize`", "humanize/time.py `naturaldelta`"],
    "humanize-time": ["humanize/time.py `naturaltime`, `precisedelta`",
                      "humanize/lists.py `natural_list`", "humanize/number.py `apnumber`, `ordinal`"],
    "more-itertools-window": ["more_itertools/more.py `windowed`, `chunked`, `batched`, `padded`, `intersperse`",
                              "more_itertools/recipes.py `iter_index`"],
    "more-itertools-recipes": ["more_itertools/recipes.py `flatten`, `consecutive_groups`, `run_length`, `pairwise`, `prepend`",
                               "more_itertools/recipes.py `unique_everseen`"],
    "tabulate-formats": ["tabulate/__init__.py table format constants and `_format_table`"],
    "tabulate-options": ["tabulate/__init__.py floatfmt, missingval, showindex, disable_numparse, numalign, colalign handling",
                         "tabulate/__init__.py `simple_separated_format`"],
    "pyparsing-parse": ["pyparsing/core.py `infix_notation` and `opAssoc`",
                        "pyparsing/results.py `ParseResults.as_list`",
                        "pyparsing/exceptions.py `ParseException`"],
    "pyparsing-helpers": ["pyparsing/helpers.py `delimited_list`, `nested_expr`",
                          "pyparsing/core.py `Suppress`, `Dict`, `search_string`"],
    "sqlparse-format": ["sqlparse/__init__.py `format` and `parse`",
                        "sqlparse/keywords.py token typing"],
    "sqlparse-grouping": ["sqlparse/sql.py `get_type` and statement classes",
                          "sqlparse/__init__.py `split`"],
    "idna-core": ["idna/core.py `encode`, `decode`, `alabel`, `ulabel`, `check_label`"],
    "idna-uts46": ["idna/core.py `uts46_remap` and the uts46/strict/std3 flags",
                   "idna/uts46data.py mapping table"],
    "markdown-blocks": ["markdown/core.py `markdown` convenience function",
                        "markdown/blockprocessors.py and inlinepatterns.py output shapes"],
    "markdown-extensions": ["markdown/extensions/tables.py, fenced_code.py, nl2br.py, attr_list.py, smarty.py, toc.py, footnotes.py"],
    "filelock-basics": ["filelock/_api.py `BaseFileLock` lock/unlock and `is_locked`",
                        "filelock/_api.py `lock_file` naming"],
    "filelock-contention": ["filelock/_api.py `acquire` timeout path raising `Timeout`",
                            "filelock/_soft.py or _unix.py `SoftFileLock` implementation"],
    "platformdirs-paths": ["platformdirs/api.py and platformdirs/unix.py property definitions"],
    "platformdirs-vendor": ["platformdirs/api.py `appauthor` handling per platform",
                            "platformdirs/__init__.py module-level convenience functions"],
    "six-python": ["six.py `PY2`/`PY3` and the `string_types`, `integer_types`, `class_types`, `binary_type` definitions"],
    "six-moves": ["six.py `raise_from`, `int2byte`, `byte_to_int`, `iterbytes`, `u`, `b`, `ensure_str`",
                  "six.py `_MovedItems` lazy module for `six.moves.xrange`"],
    "toml-parse": ["toml/decoder.py `TomlDecoder.loads` and table construction ordering"],
    "toml-roundtrip": ["toml/encoder.py `TomlEncoder.dumps` array and table formatting",
                       "toml/decoder.py inline table and array parsing"],
    "jinja-render": ["jinja2/environment.py `Environment.from_string` and compiled templates",
                     "jinja2/filters.py `join`, jinja2/lexer.py whitespace control"],
    "jinja-escapes": ["jinja2/runtime.py `Undefined` and `StrictUndefined`",
                      "jinja2/lexer.py raw-expression and comment handling"],
    "urllib3-url": ["urllib3/util/url.py `parse_url` and the `Url` class fields"],
    "urllib3-headers": ["urllib3/_collections.py `HTTPHeaderDict` multi-value semantics",
                        "urllib3/util/request.py `make_headers`"],
    "colorama-codes": ["colorama/ansi.py `AnsiFore`, `AnsiBack`, `AnsiStyle` code constants"],
    "colorama-ansi": ["colorama/ansitowin32.py `AnsiToWin32.get_win32_calls`",
                      "colorama/ansi.py `CSI` and `OSC` prefixes"],
    "docutils-tree": ["docutils/core.py `publish_doctree` with the null writer",
                      "docutils/parsers/rst/states.py section and bullet-list handling"],
    "docutils-nodes": ["docutils/nodes.py `Node.pformat` and `Element.astext`",
                       "docutils/utils/__init__.py `escape2null`, `unescape`, `column_width`"],
    "networkx-paths": ["networkx/algorithms/shortest_paths/weighted.py `dijkstra_path`, `bellman_ford_path`",
                       "networkx/algorithms/shortest_paths/generic.py `all_shortest_paths`"],
    "networkx-structure": ["networkx/algorithms/distance_measures.py and distance_regular.py",
                           "networkx/algorithms/traversal/breadth_first_search.py",
                           "networkx/algorithms/cluster.py", "networkx/algorithms/operators"],
    "pygments-lex": ["pygments/lexers/python.py `PythonLexer` and pygments/lexer.py token emission"],
    "pygments-tokens": ["pygments/token.py token hierarchy", "pygments/formatters/other.py `RawTokenFormatter`",
                        "pygments/styles/__init__.py `get_style_by_name`"],
    "rich-render": ["rich/console.py `Console.print` and markup handling",
                    "rich/markup.py tag parsing"],
    "rich-table": ["rich/table.py `Table` rendering and `rich/box.py` box drawing"],
    "pip-helpers": ["pip/_internal/utils/misc.py `format_size` and `normalize_path`",
                    "pip/_vendor/packaging/requirements.py and version.py"],
    "pip-package": ["pip/__init__.py and pip/__main__.py entry points"],
    "sqlalchemy-core": ["sqlalchemy/sql/compiler.py SQL rendering",
                        "sqlalchemy/sql/selectable.py select/where/order_by/limit"],
    "sqlalchemy-types": ["sqlalchemy/sql/sqltypes.py type objects",
                         "sqlalchemy/exc.py exception hierarchy"],
    "marshmallow-fields": ["marshmallow/fields.py String, Integer, Email, List and dump_default handling",
                           "marshmallow/schema.py load/dump/validate"],
    "marshmallow-behavior": ["marshmallow/schema.py unknown EXCLUDE/RAISE/INCLUDE policies",
                             "marshmallow/validate.py Length and Range"],
    "cerberus-basic": ["cerberus/validator.py validated/validate and default application",
                       "cerberus/errors.py error message construction"],
    "cerberus-errors": ["cerberus/validator.py required-field errors and normalized()",
                        "cerberus/errors.py ValidationError"],
    "pathspec-match": ["pathspec/pathspec.py PathSpec.match_file",
                       "pathspec/patterns/gitwildmatch.py negation and directory patterns"],
    "pathspec-util": ["pathspec/util.py normalize_file",
                      "pathspec/patterns/gitwildmatch.py anchoring rules"],
    "wheel-tags": ["wheel/wheelfile.py WheelFile", "wheel/util.py tag parsing helpers"],
    "wheel-metadata": ["wheel/metadata.py convert_requirements and related helpers"],
    "voluptuous-schema": ["voluptuous/schema_builder.py Schema and default filling",
                          "voluptuous/validators.py All, Length, Range"],
    "voluptuous-errors": ["voluptuous/error.py Invalid and MultipleInvalid",
                          "voluptuous/validators.py Any and Length"],
    "toolz-iter": ["toolz/itertoolz.py partition, sliding_window, interleave, unique, groupby"],
    "toolz-func": ["toolz/functoolz.py pipe, compose, curry, memoize",
                   "toolz/dicttoolz.py merge_with"],
    "funcy-seqs": ["funcy/seqs.py lmap, flatten, partition, chunks, cat, take"],
    "funcy-colls": ["funcy/colls.py group_by, where", "funcy/funcs.py compose",
                    "funcy/seqs.py first"],
    "dotenv-parse": ["dotenv/parser.py parse_stream binding rules for quoting, comments, exports"],
    "dotenv-stream": ["dotenv/main.py dotenv_values with stream and variable substitution",
                      "dotenv/parser.py Binding"],
    "xmltodict-parse": ["xmltodict.py parse and attribute prefixing"],
    "xmltodict-unparse": ["xmltodict.py unparse and list handling"],
    "jmespath-search": ["jmespath/parser.py expression parsing",
                        "jmespath/functions.py length, sort, filter projections"],
    "jmespath-parser": ["jmespath/parser.py Parser and error types",
                        "jmespath/visitor.py tree interpretation"],
}

TASKS = [
    {
        "task_id": "attrs-define", "source_id": "attrs-26.1.0", "source_group": "hynek-attrs",
        "prompt": (
            "Using only the supplied attrs 26.1.0 source, determine the behaviour of this program:\n\n"
            "```python\nimport attrs\n\n@attrs.define\nclass P:\n"
            "    x: int\n    y: int = 5\n    tags: list = attrs.field(factory=list)\n\n"
            "p = P(1, 2)\nq = attrs.evolve(p, y=9)\nhash_ok = None\ntry:\n    hash(p)\n"
            "    hash_ok = True\nexcept TypeError:\n    hash_ok = False\n```\n\n"
            "Report:\n"
            "- `field_names`: the declaration-order list of field names.\n"
            "- `slots_tuple`: the list of entries in `P.__slots__`, in order.\n"
            "- `evolved_y`: the value of `q.y`.\n"
            "- `evolve_shares_untouched_list`: whether `q.tags` is the same object as `p.tags`.\n"
            "- `hashable`: whether `hash(p)` succeeded.\n"
            "- `asdict`: the object produced by `attrs.asdict(p)`.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "field_names": ["x", "y", "tags"],
            "slots_tuple": ["x", "y", "tags", "__weakref__"],
            "evolved_y": 9,
            "evolve_shares_untouched_list": True,
            "hashable": False,
            "asdict": {"tags": [], "x": 1, "y": 2},
        },
    },
    {
        "task_id": "attrs-validators", "source_id": "attrs-26.1.0", "source_group": "hynek-attrs",
        "prompt": (
            "Using only the supplied attrs 26.1.0 source, determine which exception type each "
            "statement raises (report the exception class name as a string, or null if no "
            "exception is raised):\n\n"
            "```python\nimport attrs\n\n@attrs.define\nclass C:\n"
            "    n: int = attrs.field(validator=attrs.validators.instance_of(int))\n"
            '    mode: str = attrs.field(validator=attrs.validators.in_(["a", "b"]))\n'
            "    opt: object = attrs.field(default=None, validator=attrs.validators.optional("
            "attrs.validators.instance_of(str)))\n"
            "    seq: list = attrs.field(factory=list, validator=attrs.validators.deep_iterable(\n"
            "        member_validator=attrs.validators.instance_of(int),\n"
            "        iterable_validator=attrs.validators.instance_of(list)))\n```\n\n"
            "Report:\n"
            "- `instance_of_error`: `C(n=\"x\", mode=\"a\")`\n"
            "- `in_error`: `C(n=1, mode=\"z\")`\n"
            "- `optional_ok`: whether `C(n=1, mode=\"a\", opt=None)` constructs (boolean).\n"
            "- `optional_error`: `C(n=1, mode=\"a\", opt=7)`\n"
            "- `deep_iterable_error`: `C(n=1, mode=\"a\", seq=[1, \"b\"])`\n"
            "- `deep_iterable_ok`: whether `C(n=1, mode=\"a\", seq=[1, 2]).seq == [1, 2]` (boolean).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "instance_of_error": "TypeError",
            "in_error": "ValueError",
            "optional_ok": True,
            "optional_error": "TypeError",
            "deep_iterable_error": "TypeError",
            "deep_iterable_ok": True,
        },
    },
    {
        "task_id": "dateutil-rrule", "source_id": "dateutil-2.9.0.post0",
        "source_group": "dateutil-python-dateutil",
        "prompt": (
            "Using only the supplied python-dateutil 2.9.0.post0 source, determine the datetimes "
            "produced by these recurrence rules. Report each list as ISO-8601 strings in emitted "
            "order, exactly as `datetime.isoformat()` renders a naive datetime.\n\n"
            "```python\nfrom datetime import datetime\n"
            "from dateutil.rrule import rrule, rrulestr, MONTHLY, WEEKLY, MO, WE\n\n"
            "monthly = rrule(MONTHLY, count=4, dtstart=datetime(2026, 1, 31), bymonthday=31)\n"
            "weekly = rrule(WEEKLY, count=3, dtstart=datetime(2026, 1, 5), byweekday=(MO, WE))\n"
            "daily = rrulestr(\"FREQ=DAILY;COUNT=3;INTERVAL=2\", dtstart=datetime(2026, 2, 27))\n"
            "bounded = rrule(MONTHLY, dtstart=datetime(2026, 1, 15), until=datetime(2026, 3, 16))\n```\n\n"
            "Report `monthly_dates`, `weekly_dates`, `daily_interval_dates` and `until_bound_dates`.\n\n"
            "Return exactly one JSON object with these four keys, no prose."),
        "expected": {
            "monthly_dates": ["2026-01-31T00:00:00", "2026-03-31T00:00:00",
                              "2026-05-31T00:00:00", "2026-07-31T00:00:00"],
            "weekly_dates": ["2026-01-05T00:00:00", "2026-01-07T00:00:00", "2026-01-12T00:00:00"],
            "daily_interval_dates": ["2026-02-27T00:00:00", "2026-03-01T00:00:00",
                                     "2026-03-03T00:00:00"],
            "until_bound_dates": ["2026-01-15T00:00:00", "2026-02-15T00:00:00",
                                  "2026-03-15T00:00:00"],
        },
    },
    {
        "task_id": "dateutil-relative", "source_id": "dateutil-2.9.0.post0",
        "source_group": "dateutil-python-dateutil",
        "prompt": (
            "Using only the supplied python-dateutil 2.9.0.post0 source, determine the results of "
            "these operations:\n\n"
            "```python\nfrom datetime import datetime\n"
            "from dateutil.relativedelta import relativedelta\nfrom dateutil.parser import parse\n\n"
            "d = datetime(2026, 1, 31)\n"
            "a = (d + relativedelta(months=1)).date()\n"
            "b = (d + relativedelta(months=1, days=-1)).date()\n"
            "delta = relativedelta(datetime(2026, 3, 1), datetime(2026, 1, 31))\n"
            "parsed = parse(\"2026-03-04T05:06:07+02:00\")\n"
            "c = parse(\"04/03/2026\", dayfirst=True).date()\n"
            "e = parse(\"March 4\", default=datetime(2000, 1, 9)).date()\n```\n\n"
            "Report:\n"
            "- `month_end_clamped`: `a` as an ISO date string.\n"
            "- `month_plus_days`: `b` as an ISO date string.\n"
            "- `delta_parts`: an object with integer `months` and `days` from `delta`.\n"
            "- `parse_offset_minutes`: the UTC offset of `parsed` in whole minutes, as an integer.\n"
            "- `parse_dayfirst`: `c` as an ISO date string.\n"
            "- `parse_default_year`: `e` as an ISO date string.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "month_end_clamped": "2026-02-28",
            "month_plus_days": "2026-02-27",
            "delta_parts": {"months": 1, "days": 1},
            "parse_offset_minutes": 120,
            "parse_dayfirst": "2026-03-04",
            "parse_default_year": "2000-03-04",
        },
    },
    {
        "task_id": "sortedcontainers-list", "source_id": "sortedcontainers-2.4.0",
        "source_group": "grantjenks-sortedcontainers",
        "prompt": (
            "Using only the supplied sortedcontainers 2.4.0 source, determine the results of this "
            "program:\n\n"
            "```python\nfrom sortedcontainers import SortedList, SortedListWithKey\n\n"
            "sl = SortedList([5, 1, 3, 3])\nsl.add(2)\nsl.remove(3)\n"
            "withkey = SortedListWithKey([(2, \"b\"), (1, \"a\"), (3, \"c\")], key=lambda item: -item[0])\n```\n\n"
            "Report:\n"
            "- `after_ops`: `list(sl)` after both mutations.\n"
            "- `index_of_three`: `sl.index(3)`.\n"
            "- `slice_middle`: `list(sl[1:3])`.\n"
            "- `bisect_left_of_three`: `sl.bisect_left(3)`.\n"
            "- `key_order`: `list(withkey)`.\n"
            "- `irange_two_to_four`: `list(sl.irange(2, 4))`.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "after_ops": [1, 2, 3, 5],
            "index_of_three": 2,
            "slice_middle": [2, 3],
            "bisect_left_of_three": 2,
            "key_order": [[3, "c"], [2, "b"], [1, "a"]],
            "irange_two_to_four": [2, 3],
        },
    },
    {
        "task_id": "sortedcontainers-dict", "source_id": "sortedcontainers-2.4.0",
        "source_group": "grantjenks-sortedcontainers",
        "prompt": (
            "Using only the supplied sortedcontainers 2.4.0 source, determine the results of this "
            "program:\n\n"
            "```python\nfrom sortedcontainers import SortedDict, SortedSet\n\n"
            "sd = SortedDict({3: \"c\", 1: \"a\", 2: \"b\"})\nsd.setdefault(4, \"d\")\n"
            "ss = SortedSet([3, 1, 2])\nunion = ss | {4}\nintersection = ss & {2, 3, 9}\n```\n\n"
            "Report:\n"
            "- `peek_first`: `list(sd.peekitem(0))`.\n"
            "- `peek_last`: `list(sd.peekitem(-1))`.\n"
            "- `irange_two_to_three`: `list(sd.irange(2, 3))`.\n"
            "- `keys_after_setdefault`: `list(sd.keys())` after the `setdefault` call.\n"
            "- `set_union`: `list(union)`.\n"
            "- `set_intersection`: `list(intersection)`.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "peek_first": [1, "a"],
            "peek_last": [4, "d"],
            "irange_two_to_three": [2, 3],
            "keys_after_setdefault": [1, 2, 3, 4],
            "set_union": [1, 2, 3, 4],
            "set_intersection": [2, 3],
        },
    },
]

BOLTONS_PROMPT = (
    "Using only the supplied boltons 26.2.0 source, determine the results of this program:\n\n"
    "```python\nfrom boltons.dictutils import OMD\nfrom boltons.setutils import IndexedSet\n\n"
    "omd = OMD([(\"a\", 1), (\"b\", 2), (\"a\", 3)])\nomd.add(\"c\", 4)\n"
    "iset = IndexedSet([3, 1, 2, 1])\nbefore = list(iset)\niset.add(1)\n"
    "inv = OMD([(\"a\", 1), (\"a\", 2)]).inverted()\n```\n\n"
)

TASKS.extend([
    {
        "task_id": "boltons-multidict", "source_id": "boltons-26.2.0",
        "source_group": "mahmoud-boltons",
        "prompt": BOLTONS_PROMPT + (
            "From `omd` report:\n"
            "- `getlist_a`: `omd.getlist(\"a\")`.\n"
            "- `scalar_a`: `omd[\"a\"]`.\n"
            "- `multi_items`: `[list(pair) for pair in omd.items(multi=True)]`.\n"
            "- `keys_after_add`: `list(omd.keys())` after `add`.\n"
            "- `todict`: `omd.todict(multi=False)`.\n"
            "- `distinct_len`: `len(omd)`.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "getlist_a": [1, 3], "scalar_a": 3,
            "multi_items": [["a", 1], ["b", 2], ["a", 3], ["c", 4]],
            "keys_after_add": ["a", "b", "c"], "todict": {"a": 3, "b": 2, "c": 4},
            "distinct_len": 3},
    },
    {
        "task_id": "boltons-indexedset", "source_id": "boltons-26.2.0",
        "source_group": "mahmoud-boltons",
        "prompt": BOLTONS_PROMPT + (
            "Report:\n"
            "- `deduplicated`: `before`.\n"
            "- `after_re_add`: `list(iset)` after `iset.add(1)`.\n"
            "- `index_of_two`: `iset.index(2)`.\n"
            "- `inverted_getlist`: `inv.getlist(2)`.\n"
            "- `sub_one_two`: `list(IndexedSet([3, 1, 2]) - IndexedSet([1]))`.\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "deduplicated": [3, 1, 2], "after_re_add": [3, 1, 2], "index_of_two": 2,
            "inverted_getlist": ["a"], "sub_one_two": [3, 2]},
    },
    {
        "task_id": "tomlkit-roundtrip", "source_id": "tomlkit-0.15.1",
        "source_group": "frostming-tomlkit",
        "prompt": (
            "Using only the supplied tomlkit 0.15.1 source, determine the results of this program:\n\n"
            "```python\nfrom tomlkit import parse, dumps\n\n"
            'doc = parse(\'title = "x"  # keep me\\n\\n[owner]\\nname = "a"\\n'
            'dob = 1979-05-27T07:32:00Z\\n\')\n'
            'doc["title"] = "y"\ndoc["owner"]["name"] = "b"\ntext = dumps(doc)\n```\n\n'
            "Report:\n"
            "- `comment_preserved`: whether the string `# keep me` survives in `text` (boolean).\n"
            "- `title_line`: the line of `text` that starts with `title`.\n"
            "- `name_line`: the line of `text` that starts with `name`.\n"
            "- `owner_is_table`: `type(doc[\"owner\"]).__name__`.\n"
            "- `dob_type`: `type(doc[\"owner\"][\"dob\"]).__name__`.\n"
            "- `stable_redump`: whether `dumps(parse(text)) == text` (boolean).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "comment_preserved": True, "title_line": 'title = "y"  # keep me',
            "name_line": 'name = "b"', "owner_is_table": "Table", "dob_type": "DateTime",
            "stable_redump": True},
    },
    {
        "task_id": "tomlkit-datetimes", "source_id": "tomlkit-0.15.1",
        "source_group": "frostming-tomlkit",
        "prompt": (
            "Using only the supplied tomlkit 0.15.1 source, determine the results of this program:\n\n"
            "```python\nfrom tomlkit import parse\n\n"
            'multi = parse("d = 1979-05-27\\nt = 07:32:00\\ndt = 1979-05-27T07:32:00\\n'
            'ldt = 1979-05-27T07:32:00.999999\\n")\n```\n\n'
            "Report `type(...).__name__` for `multi[\"d\"]`, `multi[\"t\"]` and `multi[\"dt\"]` as "
            "`date_type`, `time_type` and `datetime_type`; the value of `multi[\"ldt\"].microsecond` "
            "as `local_microsecond`; `multi[\"d\"].as_string()` as `date_as_string`; and whether "
            "`str(multi[\"dt\"]) == \"1979-05-27 07:32:00\"` as `offsets_equal`.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "date_type": "Date", "time_type": "Time", "datetime_type": "DateTime",
            "local_microsecond": 999999, "date_as_string": "1979-05-27", "offsets_equal": True},
    },
    {
        "task_id": "humanize-numbers", "source_id": "humanize-4.16.0",
        "source_group": "python-humanize-humanize",
        "prompt": (
            "Using only the supplied humanize 4.16.0 source, determine the results of this program:\n\n"
            "```python\nfrom datetime import timedelta\n"
            "from humanize import intcomma, intword, metric, naturalsize, naturaldelta\n\n"
            "a = intcomma(1234567.891)\nb = intword(1234567)\nc = naturalsize(3000, binary=True)\n"
            "d = metric(1234)\ne = naturaldelta(timedelta(seconds=5400))\n```\n\n"
            "Report the returned strings as `intcomma_value`, `intword_value`, "
            "`naturalsize_binary`, `metric_value` and `naturaldelta_hour` (for `e`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "intcomma_value": "1,234,567.891", "intword_value": "1.2 million",
            "naturalsize_binary": "2.9 KiB", "metric_value": "1.23 k",
            "naturaldelta_hour": "2 hours"},
    },
    {
        "task_id": "humanize-time", "source_id": "humanize-4.16.0",
        "source_group": "python-humanize-humanize",
        "prompt": (
            "Using only the supplied humanize 4.16.0 source, determine the results of this program:\n\n"
            "```python\nfrom datetime import datetime, timedelta\n"
            "from humanize import apnumber, natural_list, naturaltime, ordinal, precisedelta\n\n"
            "now = datetime(2026, 3, 4, 12, 0, 0)\n"
            "a = naturaltime(now - timedelta(seconds=1), when=now)\n"
            "b = naturaltime(now - timedelta(days=2), when=now)\n"
            "c = precisedelta(timedelta(seconds=5400))\nd = natural_list([\"a\", \"b\"])\n"
            "e = natural_list([\"a\", \"b\", \"c\"])\nf = apnumber(3)\ng = ordinal(2)\n```\n\n"
            "Report the returned strings as `naturaltime_second_ago`, `naturaltime_two_days`, "
            "`precisedelta_hour`, `natural_list_two`, `natural_list_three`, `apnumber_three` and `ordinal_two`.\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "naturaltime_second_ago": "a second ago", "naturaltime_two_days": "2 days ago",
            "precisedelta_hour": "1 hour and 30 minutes", "natural_list_two": "a and b",
            "natural_list_three": "a, b and c", "apnumber_three": "three", "ordinal_two": "2nd"},
    },
])


GRID = "\n".join(["+--------+----------+", "| item   |      qty |", "+========+==========+",
                  "| spam   |  41.9999 |", "+--------+----------+", "| eggs   | 451      |",
                  "+--------+----------+"])
GITHUB = "\n".join(["| item   |      qty |", "|--------|----------|",
                    "| spam   |  41.9999 |", "| eggs   | 451      |"])
TABLE_HTML = "\n".join(["<table>", "<thead>", "<tr>", "<th>a</th>", "<th>b</th>", "</tr>",
                        "</thead>", "<tbody>", "<tr>", "<td>1</td>", "<td>2</td>", "</tr>",
                        "</tbody>", "</table>"])

TASKS.extend([
    {
        "task_id": "more-itertools-window", "source_id": "more-itertools-11.1.0",
        "source_group": "more-itertools",
        "prompt": (
            "Using only the supplied more-itertools 11.1.0 source, determine the results of this "
            "program:\n\n"
            "```python\nimport more_itertools as mi\n\n"
            "w = [list(x) for x in mi.windowed([1, 2, 3, 4, 5], 3)]\n"
            "c = [list(x) for x in mi.chunked([1, 2, 3, 4, 5], 2)]\n"
            "b = list(mi.batched([1, 2, 3, 4, 5], 2, strict=False))[-1]\n"
            "p = list(mi.padded([1, 2, 3], 0, 5))\n"
            "i = list(mi.iter_index([7, 3, 7, 7], 7))\n"
            "s = list(mi.intersperse(0, [1, 2, 3]))\n```\n\n"
            "Report `windowed` (`w`), `chunked` (`c`), `batched_last` (`b`), `padded` (`p`), "
            "`iter_index_hits` (`i`) and `intersperse` (`s`). Also report `strict_batched_error`: "
            "the class name of the exception raised by `list(mi.batched([1, 2, 3], 2, strict=True))`, "
            "or null if it raises nothing.\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "windowed": [[1, 2, 3], [2, 3, 4], [3, 4, 5]],
            "chunked": [[1, 2], [3, 4], [5]],
            "batched_last": [5],
            "padded": [1, 2, 3, 0, 0],
            "iter_index_hits": [0, 2, 3],
            "intersperse": [1, 0, 2, 0, 3],
            "strict_batched_error": "ValueError"},
    },
    {
        "task_id": "more-itertools-recipes", "source_id": "more-itertools-11.1.0",
        "source_group": "more-itertools",
        "prompt": (
            "Using only the supplied more-itertools 11.1.0 source, determine the results of this "
            "program:\n\n"
            "```python\nimport more_itertools as mi\n\n"
            "f = list(mi.flatten([[1, 2], [3], []]))\n"
            "g = [list(x) for x in mi.consecutive_groups([1, 2, 3, 7, 8, 10])]\n"
            "e = list(mi.run_length.encode(\"aabbbc\"))\n"
            "d = \"\".join(mi.run_length.decode([(\"a\", 2), (\"b\", 3), (\"c\", 1)]))\n"
            "w = [list(x) for x in mi.pairwise([1, 2, 3, 4])]\n"
            "u = list(mi.unique_everseen(\"AAAABBBCCDAABBB\"))\n"
            "p = list(mi.prepend(0, [1, 2]))\n```\n\n"
            "Report `flatten`, `consecutive_groups`, `run_length_encode`, `run_length_decode`, "
            "`pairwise`, `unique_everseen` and `prepend`.\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "flatten": [1, 2, 3],
            "consecutive_groups": [[1, 2, 3], [7, 8], [10]],
            "run_length_encode": [["a", 2], ["b", 3], ["c", 1]],
            "run_length_decode": "aabbbc",
            "pairwise": [[1, 2], [2, 3], [3, 4]],
            "unique_everseen": ["A", "B", "C", "D"],
            "prepend": [0, 1, 2]},
    },
    {
        "task_id": "tabulate-formats", "source_id": "tabulate-0.10.0",
        "source_group": "astanin-python-tabulate",
        "prompt": (
            "Using only the supplied tabulate 0.10.0 source, determine the exact strings returned "
            "by this program:\n\n"
            "```python\nimport tabulate as t\n\n"
            "rows = [[\"spam\", 41.9999], [\"eggs\", 451.0]]\n"
            "headers = [\"item\", \"qty\"]\n"
            "plain = t.tabulate(rows, headers=headers, tablefmt=\"plain\")\n"
            "simple = t.tabulate(rows, headers=headers, tablefmt=\"simple\")\n"
            "github = t.tabulate(rows, headers=headers, tablefmt=\"github\")\n"
            "grid = t.tabulate(rows, headers=headers, tablefmt=\"grid\")\n```\n\n"
            "Report the four rendered strings as `plain`, `simple`, `github` and `grid`, and "
            "`plain_first_line` for the first line of `plain`. Preserve spacing exactly.\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "plain": "item         qty\nspam     41.9999\neggs    451",
            "simple": "item         qty\n------  --------\nspam     41.9999\neggs    451",
            "github": GITHUB, "grid": GRID, "plain_first_line": "item         qty"},
    },
    {
        "task_id": "tabulate-options", "source_id": "tabulate-0.10.0",
        "source_group": "astanin-python-tabulate",
        "prompt": (
            "Using only the supplied tabulate 0.10.0 source, determine the exact strings returned "
            "by this program:\n\n"
            "```python\nimport tabulate as t\n\n"
            "rows = [[\"spam\", 41.9999], [\"eggs\", 451.0]]\n"
            "fmt = t.simple_separated_format(\"|\")\n"
            "a = t.tabulate([[1.23456789]], floatfmt=\".2f\")\n"
            "b = t.tabulate([[1, None]], missingval=\"?\")\n"
            "c = t.tabulate([[\"a\"], [\"b\"]], showindex=[\"x\", \"y\"], tablefmt=\"plain\")\n"
            "d = t.tabulate([[\"01\", \"2\"]], disable_numparse=True, tablefmt=\"plain\")\n"
            "e = t.tabulate([[1], [22]], tablefmt=\"plain\", numalign=\"left\")\n"
            "f = t.tabulate(rows, headers=[\"item\", \"qty\"], tablefmt=fmt)\n"
            "g = t.tabulate([[\"a\", 1]], tablefmt=\"plain\", colalign=(\"right\", \"left\"))\n```\n\n"
            "Report `float_fmt` (`a`), `missing_val` (`b`), `show_index` (`c`), `disable_numparse` "
            "(`d`), `numalign_left` (`e`), `separated_format` (first line of `f`) and "
            "`column_align` (`g`). Preserve spacing exactly.\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "float_fmt": "----\n1.23\n----", "missing_val": "-  -\n1  ?\n-  -",
            "show_index": "x  a\ny  b", "disable_numparse": "01  2", "numalign_left": "1\n22",
            "separated_format": "item  |     qty", "column_align": "a  1"},
    },
    {
        "task_id": "pyparsing-parse", "source_id": "pyparsing-3.3.3",
        "source_group": "pyparsing-pyparsing",
        "prompt": (
            "Using only the supplied pyparsing 3.3.3 source, determine the results of this "
            "program:\n\n"
            "```python\nimport pyparsing as pp\n\n"
            "integer = pp.Word(pp.nums).set_parse_action(lambda tokens: int(tokens[0]))\n"
            "op = pp.one_of(\"+ - * /\")\n"
            "expr = pp.infix_notation(integer, [(pp.one_of(\"* /\"), 2, pp.opAssoc.LEFT),\n"
            "                                   (op, 2, pp.opAssoc.LEFT)])\n"
            "parsed = expr.parse_string(\"3+4*2-1\")\n"
            "found = pp.Word(pp.alphas).search_string(\"ab cd ef\")[1].as_list()\n"
            "quoted = pp.QuotedString('\"').parse_string('\"hello world\"')[0]\n"
            "depth = len(pp.nested_expr(\"{\", \"}\").parse_string(\"{a {b c} d}\").as_list()[0])\n```\n\n"
            "Report `as_list` (`parsed.as_list()`), `repr` (`str(parsed)`), `strings_found` "
            "(`found`), `quoted_value` (`quoted`), `nested_depth` (`depth`), and "
            "`parse_error_class`: the class name of the exception raised by "
            "`expr.parse_string(\"3+\")`, or null if it raises nothing.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "as_list": [[3, "+", [4, "*", 2], "-", 1]],
            "repr": "[[3, '+', [4, '*', 2], '-', 1]]",
            "strings_found": ["cd"], "quoted_value": "hello world", "nested_depth": 3,
            "parse_error_class": "ParseException"},
    },
    {
        "task_id": "pyparsing-helpers", "source_id": "pyparsing-3.3.3",
        "source_group": "pyparsing-pyparsing",
        "prompt": (
            "Using only the supplied pyparsing 3.3.3 source, determine the results of this "
            "program:\n\n"
            "```python\nimport pyparsing as pp\n\n"
            "a = pp.DelimitedList(pp.Word(pp.alphas)).parse_string(\"a, b, c\").as_list()\n"
            "b = (pp.Suppress(\"(\") + pp.Word(pp.alphas) + pp.Suppress(\")\")).parse_string(\"(hi)\").as_list()\n"
            "c = str(pp.DelimitedList(pp.Word(pp.nums)).parse_string(\"1,2,3\"))\n"
            "d = [r.as_list() for r in pp.Word(pp.alphas).search_string(\"ab cd ef\")]\n"
            "e = pp.Dict(pp.Group(pp.Word(pp.alphas) + pp.Word(pp.nums))).parse_string(\"x 1 y 2\").as_dict()\n```\n\n"
            "Report `delimited_list` (`a`), `suppressed_result` (`b`), `delimited_string` (`c`), "
            "`search_all` (`d`) and `group_dict` (`e`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "delimited_list": ["a", "b", "c"], "suppressed_result": ["hi"],
            "delimited_string": "['1', '2', '3']",
            "search_all": [["ab"], ["cd"], ["ef"]], "group_dict": {"x": "1"}},
    },
    {
        "task_id": "sqlparse-format", "source_id": "sqlparse-0.6.0",
        "source_group": "andialbrecht-sqlparse",
        "prompt": (
            "Using only the supplied sqlparse 0.6.0 source, determine the results of this "
            "program:\n\n"
            "```python\nimport sqlparse\n\n"
            "stmt = sqlparse.parse('SELECT a, count(*) FROM t WHERE a > 1 GROUP BY a ORDER BY 2 DESC;')[0]\n"
            "types = [str(t.ttype) for t in stmt.flatten() if t.ttype is not None][:6]\n"
            "a = sqlparse.format(\"select a from t\", keyword_case=\"upper\")\n"
            "b = sqlparse.format(\"select AbC from T\", identifier_case=\"lower\")\n"
            "c = sqlparse.format(\"select 1 -- hi\\n\", strip_comments=True).strip()\n"
            "d = sqlparse.format(\"select a, b from t where a=1\", reindent=True, keyword_case=\"upper\").splitlines()\n```\n\n"
            "Report `statement_type` (`stmt.get_type()`), `token_types` (`types`), `keyword_case` "
            "(`a`), `identifier_case` (`b`), `strip_comments` (`c`) and `reindent_lines` (`d`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "statement_type": "SELECT",
            "token_types": ["Token.Keyword.DML", "Token.Text.Whitespace", "Token.Name",
                            "Token.Punctuation", "Token.Text.Whitespace", "Token.Name"],
            "keyword_case": "SELECT a FROM t", "identifier_case": "select abc from t",
            "strip_comments": "select 1",
            "reindent_lines": ["SELECT a,", "       b", "FROM t", "WHERE a=1"]},
    },
    {
        "task_id": "sqlparse-grouping", "source_id": "sqlparse-0.6.0",
        "source_group": "andialbrecht-sqlparse",
        "prompt": (
            "Using only the supplied sqlparse 0.6.0 source, determine the results of this "
            "program:\n\n"
            "```python\nimport sqlparse\nfrom sqlparse import sql, tokens as T\n\n"
            "a = len(sqlparse.split(\"select 1; select 2;\"))\n"
            "b = [sqlparse.parse(q)[0].get_type() for q in\n"
            "     [\"insert into t values (1)\", \"update t set a=1\", \"delete from t\"]]\n"
            "c = sqlparse.parse(\"create table t (a int)\")[0].get_type()\n"
            "d = sqlparse.parse(\"begin\")[0].get_type()\n"
            "e = [str(t) for t in sqlparse.parse(\"select abc from t\")[0].tokens\n"
            "     if isinstance(t, sql.Identifier)][:1]\n"
            "f = any(isinstance(t, sql.Where) for t in sqlparse.parse(\"select 1 where a=1\")[0].tokens)\n"
            "g = len([t for t in sqlparse.parse(\"select 1 /* c */\")[0].flatten() if t.ttype in T.Comment])\n```\n\n"
            "Report `split_count` (`a`), `dml_types` (`b`), `ddl_type` (`c`), `unknown_type` (`d`), "
            "`identifier_names` (`e`), `contains_where` (`f`) and `comments_count` (`g`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "split_count": 2, "dml_types": ["INSERT", "UPDATE", "DELETE"], "ddl_type": "CREATE",
            "unknown_type": "UNKNOWN", "identifier_names": ["abc"], "contains_where": True,
            "comments_count": 1},
    },
    {
        "task_id": "idna-core", "source_id": "idna-3.20",
        "source_group": "kjd-idna",
        "prompt": (
            "Using only the supplied idna 3.20 source, determine the results of this program. "
            "For the error fields report the exception class name, or null if no exception is "
            "raised:\n\n"
            "```python\nimport idna\n\n"
            "a = idna.encode(\"ドメイン.テスト\").decode()\n"
            "b = idna.alabel(\"テスト\").decode()\n"
            "c = idna.decode(\"xn--eckwd4c7c.xn--zckzah\")\n"
            "d = idna.ulabel(\"xn--eckwd4c7c\")\n"
            "# e: idna.encode(\"a..b\")\n"
            "# f: idna.encode(\"a\" * 64 + \".com\")\n"
            "g = type(idna.encode(\"example.com\")).__name__\n```\n\n"
            "Report `enc_domain` (`a`), `enc_label` (`b`), `dec_domain` (`c`), `dec_label` (`d`), "
            "`empty_label_error` (`e`), `too_long_error` (`f`) and `encode_type` (`g`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "enc_domain": "xn--eckwd4c7c.xn--zckzah", "enc_label": "xn--zckzah",
            "dec_domain": "ドメイン.テスト", "dec_label": "ドメイン",
            "empty_label_error": "IDNAError", "too_long_error": "IDNAError",
            "encode_type": "bytes"},
    },
    {
        "task_id": "idna-uts46", "source_id": "idna-3.20",
        "source_group": "kjd-idna",
        "prompt": (
            "Using only the supplied idna 3.20 source, determine the results of this program. "
            "For the error fields report the exception class name, or null if no exception is "
            "raised:\n\n"
            "```python\nimport idna\n\n"
            "a = idna.encode(\"Bücher.Example\", uts46=True).decode()\n"
            "b = idna.encode(\"Bücher\", uts46=True, transitional=True).decode()\n"
            "c = idna.uts46_remap(\"Bücher\")\n"
            "# d: idna.encode(\"a_b.com\", uts46=True, std3_rules=False)\n"
            "# e: idna.encode(\",abc\", uts46=True)\n"
            "f = idna.decode(\"xn--bcher-kva.example\", uts46=True)\n"
            "g = idna.encode(\"ExAmPle.COM\", uts46=True).decode()\n"
            "h = idna.alabel(\"münchen\").decode()\n```\n\n"
            "Report `lowercased_domain` (`a`), `transitional_label` (`b`), `remap_only` (`c`), "
            "`underscore_error_anyway` (`d`), `leading_comma_error` (`e`), `decoded_mapped` (`f`), "
            "`map_domain` (`g`) and `alabel_strict` (`h`).\n\n"
            "Return exactly one JSON object with these eight keys, no prose."),
        "expected": {
            "lowercased_domain": "xn--bcher-kva.example", "transitional_label": "xn--bcher-kva",
            "remap_only": "bücher", "underscore_error_anyway": "InvalidCodepoint",
            "leading_comma_error": "InvalidCodepoint", "decoded_mapped": "bücher.example",
            "map_domain": "example.com", "alabel_strict": "xn--mnchen-3ya"},
    },
    {
        "task_id": "markdown-blocks", "source_id": "markdown-3.10.3",
        "source_group": "python-markdown",
        "prompt": (
            "Using only the supplied Python-Markdown 3.10.3 source, determine the exact HTML "
            "strings returned by this program:\n\n"
            "```python\nimport markdown as md\n\n"
            "a = md.markdown(\"# Title\")\n"
            "b = md.markdown(\"*em* and **strong**\")\n"
            "c = md.markdown(\"- a\\n- b\\n\").strip()\n"
            "d = md.markdown(\"[text](https://example.com \\\"t\\\")\")\n"
            "e = md.markdown(\"    code\\n\").strip()\n"
            "f = md.markdown(\"> quoted\").strip()\n```\n\n"
            "Report `heading`, `emphasis`, `unordered_list`, `link`, `code_block` and `blockquote`. "
            "Preserve whitespace and newlines exactly.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "heading": "<h1>Title</h1>", "emphasis": "<p><em>em</em> and <strong>strong</strong></p>",
            "unordered_list": "<ul>\n<li>a</li>\n<li>b</li>\n</ul>",
            "link": '<p><a href="https://example.com" title="t">text</a></p>',
            "code_block": "<pre><code>code\n</code></pre>",
            "blockquote": "<blockquote>\n<p>quoted</p>\n</blockquote>"},
    },
    {
        "task_id": "markdown-extensions", "source_id": "markdown-3.10.3",
        "source_group": "python-markdown",
        "prompt": (
            "Using only the supplied Python-Markdown 3.10.3 source, determine the results of this "
            "program. Each extension is loaded by its full dotted name:\n\n"
            "```python\nfrom markdown import Markdown\n\n"
            "md = Markdown(extensions=[\"markdown.extensions.tables\", \"markdown.extensions.fenced_code\",\n"
            "                          \"markdown.extensions.nl2br\"])\n"
            "table = md.convert(\"| a | b |\\n|---|---|\\n| 1 | 2 |\\n\")\n"
            "fenced = md.convert(\"```python\\nprint(1)\\n```\\n\")\n"
            "br = md.convert(\"a\\nb\")\n"
            "attrs = Markdown(extensions=[\"markdown.extensions.attr_list\"]).convert(\"para\\n{: #my-id }\")\n"
            "smarty = Markdown(extensions=[\"markdown.extensions.smarty\"]).convert('\"quoted\" -- dash')\n"
            "toc = Markdown(extensions=[\"markdown.extensions.toc\"])\n"
            "toc.convert(\"# One\\n\\n## Two\\n\")\n"
            "foot = Markdown(extensions=[\"markdown.extensions.footnotes\"])\n"
            "foot_out = foot.convert(\"text[^1]\\n\\n[^1]: note\\n\")\n```\n\n"
            "Report `table_html` (`table`.strip()), `fenced_language` (first line of "
            "`fenced`.strip()), `break_tags` (`br`.strip()), `attr_list_id` (whether "
            "`attrs` contains `id=\"my-id\"`), `smarty_quotes` (`smarty`), `toc_first` (first line of "
            "`toc.toc.strip()`), `footnote_ref` and `footnote_body` (whether `foot_out` contains "
            "`fnref` / `class=\"footnote\"` respectively).\n\n"
            "Return exactly one JSON object with these eight keys, no prose."),
        "expected": {
            "table_html": TABLE_HTML,
            "fenced_language": '<pre><code class="language-python">print(1)',
            "break_tags": "<p>a<br />\nb</p>", "attr_list_id": True,
            "smarty_quotes": "<p>&ldquo;quoted&rdquo; &ndash; dash</p>",
            "toc_first": '<div class="toc">', "footnote_ref": True, "footnote_body": True},
    },
])


TASKS.extend([
    {
        "task_id": "filelock-basics", "source_id": "filelock-4.0.3",
        "source_group": "tox-dev-filelock",
        "prompt": (
            "Using only the supplied filelock 4.0.3 source, determine the results of this program "
            "run on Linux:\n\n"
            "```python\nimport tempfile, pathlib\nfrom filelock import FileLock\n\n"
            "tmp = pathlib.Path(tempfile.mkdtemp())\np = tmp / \"demo.lock\"\n"
            "lock = FileLock(str(p))\nwith lock:\n    inside = lock.is_locked\n"
            "after = lock.is_locked\n```\n\n"
            "Report `is_locked_inside` (`inside`), `is_locked_after` (`after`), "
            "`lock_file_suffix` (the part of `lock.lock_file` after the last dot), "
            "`lock_file_matches` (whether `lock.lock_file == str(p)`), and `timeout_default` "
            "(`lock.timeout`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "is_locked_inside": True, "is_locked_after": False, "lock_file_suffix": "lock",
            "lock_file_matches": True, "timeout_default": -1},
    },
    {
        "task_id": "filelock-contention", "source_id": "filelock-4.0.3",
        "source_group": "tox-dev-filelock",
        "prompt": (
            "Using only the supplied filelock 4.0.3 source, determine the results of this program "
            "run on Linux. For error fields report the exception class name, or null if no "
            "exception is raised:\n\n"
            "```python\nimport tempfile, pathlib\nfrom filelock import FileLock, SoftFileLock\n\n"
            "tmp = pathlib.Path(tempfile.mkdtemp())\np = tmp / \"demo.lock\"\n"
            "contender = FileLock(str(p), timeout=0)\n"
            "with FileLock(str(p)):\n"
            "    try:\n        contender.acquire()\n        err = None\n"
            "    except Exception as exc:\n        err = type(exc).__name__\n"
            "soft = SoftFileLock(str(tmp / \"soft.lock\"))\n"
            "with soft:\n    soft_inside = soft.is_locked\n"
            "try:\n    with FileLock(str(p), timeout=1):\n        pass\n"
            "    reacquire = True\nexcept Exception:\n    reacquire = False\n```\n\n"
            "Report `zero_timeout_error` (`err`), `soft_is_locked_inside` (`soft_inside`) and "
            "`reacquire_after_release` (`reacquire`).\n\n"
            "Return exactly one JSON object with these three keys, no prose."),
        "expected": {
            "zero_timeout_error": "Timeout", "soft_is_locked_inside": True,
            "reacquire_after_release": True},
    },
    {
        "task_id": "platformdirs-paths", "source_id": "platformdirs-4.11.12",
        "source_group": "tox-dev-platformdirs",
        "prompt": (
            "Using only the supplied platformdirs 4.11.12 source, determine the strings returned by "
            "this program when run on Linux with HOME set to `/root`, no XDG environment variables "
            "set, and `XDG_DATA_DIRS` at its default:\n\n"
            "```python\nfrom platformdirs import PlatformDirs\n\n"
            "app = PlatformDirs(\"MyApp\", \"MyCompany\")\n```\n\n"
            "Report `user_data_dir`, `user_config_dir`, `user_cache_dir`, `user_log_dir`, "
            "`user_documents_dir`, `site_data_dir` and `data_dir_endswith` (whether "
            "`app.user_data_dir` ends with the string `MyApp`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "user_data_dir": "/root/.local/share/MyApp", "user_config_dir": "/root/.config/MyApp",
            "user_cache_dir": "/root/.cache/MyApp", "user_log_dir": "/root/.local/state/MyApp/log",
            "user_documents_dir": "/root/Documents", "site_data_dir": "/usr/share/gnome/MyApp",
            "data_dir_endswith": True},
    },
    {
        "task_id": "platformdirs-vendor", "source_id": "platformdirs-4.11.12",
        "source_group": "tox-dev-platformdirs",
        "prompt": (
            "Using only the supplied platformdirs 4.11.12 source, determine the results of this "
            "program when run on Linux:\n\n"
            "```python\nfrom platformdirs import PlatformDirs, user_cache_dir, user_config_dir\n\n"
            "app = PlatformDirs(\"MyApp\", \"MyCompany\")\nnovendor = PlatformDirs(\"MyApp\")\n"
            "cache_call = user_cache_dir(\"MyApp\", \"MyCompany\")\n"
            "config_call = user_config_dir(\"MyApp\", \"MyCompany\")\n"
            "roaming_other = PlatformDirs(\"MyApp\", \"MyCompany\", roaming=True).user_config_dir\n```\n\n"
            "Report `with_vendor_has_company` (whether `MyCompany` appears in `app.user_data_dir`), "
            "`without_vendor_has_company` (same test on `novendor`), `module_cache_matches_instance` "
            "(whether `cache_call == app.user_cache_dir`), `roaming_flag_changes_path` (whether "
            "`roaming_other != app.user_config_dir`), `expanduser_is_absolute` (whether "
            "`app.user_data_dir` is an absolute path), `dirs_type` (`type(app).__name__`) and "
            "`user_config_dir_call` (`config_call`) and `user_cache_dir_call` (`cache_call`).\n\n"
            "Return exactly one JSON object with these eight keys, no prose."),
        "expected": {
            "with_vendor_has_company": False, "without_vendor_has_company": False,
            "module_cache_matches_instance": True, "roaming_flag_changes_path": False,
            "expanduser_is_absolute": True, "dirs_type": "Unix",
            "user_config_dir_call": "/root/.config/MyApp",
            "user_cache_dir_call": "/root/.cache/MyApp"},
    },
    {
        "task_id": "six-python", "source_id": "six-1.17.0", "source_group": "benjaminp-six",
        "prompt": (
            "Using only the supplied six 1.17.0 source, determine the results of this program on "
            "Python 3:\n\n"
            "```python\nimport six\n\n"
            "strings = [t.__name__ for t in six.string_types]\n"
            "integers = [t.__name__ for t in six.integer_types]\n"
            "classes = [t.__name__ for t in six.class_types]\nbinary = six.binary_type.__name__\n```\n\n"
            "Report `is_python_two` (`six.PY2`), `is_python_three` (`six.PY3`), `maxsize_positive` (whether "
            "`six.MAXSIZE > 0`), `binary_type` (`binary`), `string_types` (`strings`), "
            "`integer_types` (`integers`) and `class_types` (`classes`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "is_python_two": False, "is_python_three": True, "maxsize_positive": True, "binary_type": "bytes",
            "string_types": ["str"], "integer_types": ["int"], "class_types": ["type"]},
    },
    {
        "task_id": "six-moves", "source_id": "six-1.17.0", "source_group": "benjaminp-six",
        "prompt": (
            "Using only the supplied six 1.17.0 source, determine the results of this program on "
            "Python 3:\n\n"
            "```python\nimport six\n\n"
            "try:\n    six.raise_from(ValueError(\"inner\"), KeyError(\"outer\"))\n"
            "    raised = None\nexcept BaseException as exc:\n"
            "    raised = {\"class\": type(exc).__name__,\n"
            "              \"cause\": type(exc.__cause__).__name__ if exc.__cause__ is not None else None}\n"
            "ib = repr(six.int2byte(65))\nb2i = six.byte2int(b\"AB\")\n"
            "itb = list(six.iterbytes(b\"AB\"))\nu = type(six.u(\"x\")).__name__\n"
            "b = type(six.b(\"x\")).__name__\nes = six.ensure_str(b\"x\")\n"
            "xr = six.moves.xrange is range\n```\n\n"
            "Report `raise_from` (`raised`), `int_to_byte` (`ib`), `byte_to_int` (`b2i`), "
            "`iterbytes` (`itb`), `u_type` (`u`), `b_type` (`b`), `ensure_str` (`es`) and "
            "`moves_xrange_is_range` (`xr`).\n\n"
            "Return exactly one JSON object with these eight keys, no prose."),
        "expected": {
            "raise_from": {"class": "ValueError", "cause": "KeyError"}, "int_to_byte": "b'A'",
            "byte_to_int": 65, "iterbytes": [65, 66], "u_type": "str", "b_type": "bytes",
            "ensure_str": "x", "moves_xrange_is_range": True},
    },
])


TASKS.extend([
    {
        "task_id": "toml-parse", "source_id": "toml-0.10.2", "source_group": "uiri-toml",
        "prompt": (
            "Using only the supplied toml 0.10.2 source, determine the results of this program:\n\n"
            "```python\nimport toml\n\n"
            "doc = toml.loads('title = \"x\"\\n[owner]\\nname = \"b\"\\nports = [8001, 8002]\\nactive = true\\n')\n```\n\n"
            "Report `title` (`doc[\"title\"]`), `owner_name` (`doc[\"owner\"][\"name\"]`), `ports` "
            "(`doc[\"owner\"][\"ports\"]`), `active` (`doc[\"owner\"][\"active\"]`), `root_keys_sorted` "
            "(`sorted(doc)`) and `section_order` (`list(doc.keys())`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "title": "x", "owner_name": "b", "ports": [8001, 8002], "active": True,
            "root_keys_sorted": ["owner", "title"], "section_order": ["title", "owner"]},
    },
    {
        "task_id": "toml-roundtrip", "source_id": "toml-0.10.2", "source_group": "uiri-toml",
        "prompt": (
            "Using only the supplied toml 0.10.2 source, determine the results of this program:\n\n"
            "```python\nimport toml\n\n"
            "text = toml.dumps({\"b\": 1, \"a\": {\"x\": [1, 2], \"y\": True}})\n"
            "back = toml.loads(text)\n"
            "inline = toml.loads(\"point = { x = 1, y = 2 }\\nnames = [\\\"a\\\", \\\"b\\\"]\\n\")\n```\n\n"
            "Report `dumped_lines` (`text.strip().splitlines()`), `roundtrip_equal` (whether `back` "
            "equals `{\"b\": 1, \"a\": {\"x\": [1, 2], \"y\": True}}`), `inline_point` "
            "(`inline[\"point\"]`), `names` (`inline[\"names\"]`) and `dumps_type` "
            "(`type(toml.dumps({\"a\": 1})).__name__`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "dumped_lines": ["b = 1", "", "[a]", "x = [ 1, 2,]", "y = true"],
            "roundtrip_equal": True, "inline_point": {"x": 1, "y": 2}, "names": ["a", "b"],
            "dumps_type": "str"},
    },
    {
        "task_id": "jinja-render", "source_id": "jinja2-3.1.6", "source_group": "pallets-jinja",
        "prompt": (
            "Using only the supplied Jinja2 3.1.6 source, determine the strings returned by this "
            "program with a default `Environment()` (no autoescape):\n\n"
            "```python\nfrom jinja2 import Environment\n\nenv = Environment()\n"
            "a = env.from_string(\"Hello {{ name }}!\").render(name=\"World\")\n"
            "b = env.from_string(\"{% for i in items %}{{ i }},{% endfor %}\").render(items=[1, 2, 3])\n"
            "c = env.from_string(\"{{ items | join('-' ) }}\").render(items=[\"a\", \"b\"])\n"
            "d = env.from_string(\"{% if n > 2 %}big{% else %}small{% endif %}\").render(n=1)\n"
            "e = env.from_string(\"{%- for i in [1, 2] -%} {{ i }} {%- endfor -%}\").render()\n"
            "f = env.from_string(\"{{ v }}\").render(v=\"<b>\")\n```\n\n"
            "Report `interpolation` (`a`), `loop_lines` (`b`), `filter_join` (`c`), `condition` (`d`), "
            "`whitespace_trim` (`e`) and `autoescape_off` (`f`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "interpolation": "Hello World!", "loop_lines": "1,2,3,", "filter_join": "a-b",
            "condition": "small", "whitespace_trim": "12", "autoescape_off": "<b>"},
    },
    {
        "task_id": "jinja-escapes", "source_id": "jinja2-3.1.6", "source_group": "pallets-jinja",
        "prompt": (
            "Using only the supplied Jinja2 3.1.6 source, determine the results of this program with "
            "a default `Environment()`. For the error field report the exception class name, or null "
            "if no exception is raised:\n\n"
            "```python\nfrom jinja2 import Environment, StrictUndefined, UndefinedError\n\n"
            "env = Environment()\n"
            "a = env.from_string(\"{{ '{{' }}literal{{ '}}' }}\").render()\n"
            "b = env.from_string(\"{{ user.name }}\").render(user={\"name\": \"ada\"})\n"
            "c = env.from_string(\"{% for r in rows %}{% for c in r %}{{ c }}{% endfor %}|{% endfor %}\").render(\n"
            "    rows=[[1, 2], [3]])\n"
            "d = env.from_string(\"a{# hidden #}b\").render()\n"
            "e = isinstance(env.from_string(\"{{ nope }}\").render(), str)\n"
            "try:\n    Environment(undefined=StrictUndefined).from_string(\"{{ missing }}\").render()\n"
            "    err = None\nexcept UndefinedError as exc:\n    err = type(exc).__name__\n```\n\n"
            "Report `literal_braces` (`a`), `attribute_access` (`b`), `nested_loop_count` (`c`), "
            "`comment_removed` (`d`), `default_undefined_is_undefined` (`e`) and `strict_error_class` "
            "(`err`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "literal_braces": "{{literal}}", "attribute_access": "ada", "nested_loop_count": "12|3|",
            "comment_removed": "ab", "default_undefined_is_undefined": True,
            "strict_error_class": "UndefinedError"},
    },
    {
        "task_id": "urllib3-url", "source_id": "urllib3-2.8.0", "source_group": "urllib3-urllib3",
        "prompt": (
            "Using only the supplied urllib3 2.8.0 source, determine the results of this program "
            "(no network access is used):\n\n"
            "```python\nfrom urllib3.util import parse_url\n\n"
            "u = parse_url(\"http://user:pass@Example.COM:8080/a/b?q=1#frag\")\n```\n\n"
            "Report `scheme` (`u.scheme`), `host` (`u.host`), `port` (`u.port`), `path` (`u.path`), "
            "`query` (`u.query`), `fragment` (`u.fragment`), `request_uri` (`u.request_uri`) and "
            "`hostname_lowercased` (whether `u.host == \"example.com\"`).\n\n"
            "Return exactly one JSON object with these eight keys, no prose."),
        "expected": {
            "scheme": "http", "host": "example.com", "port": 8080, "path": "/a/b", "query": "q=1",
            "fragment": "frag", "request_uri": "/a/b?q=1", "hostname_lowercased": True},
    },
    {
        "task_id": "urllib3-headers", "source_id": "urllib3-2.8.0",
        "source_group": "urllib3-urllib3",
        "prompt": (
            "Using only the supplied urllib3 2.8.0 source, determine the results of this program "
            "(no network access is used):\n\n"
            "```python\nfrom urllib3.util import parse_url, make_headers\n"
            "from urllib3._collections import HTTPHeaderDict\n\n"
            "h = HTTPHeaderDict()\nh.add(\"X-Test\", \"a\")\nh.add(\"X-Test\", \"b\")\nh.add(\"Other\", \"c\")\n"
            "u2 = parse_url(\"https://example.com:443/x\")\n```\n\n"
            "Report `getlist` (`h.getlist(\"X-Test\")`), `scalar_get` (`h[\"x-test\"]`), `contains` "
            "(whether `\"other\" in h`), `items_sorted` (`sorted(h.items())`), `defaults_type` "
            "(`type(make_headers(accept_encoding=True)).__name__`), `default_port_kept` (`u2.port`) "
            "and `url_str` (`u2.url`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "getlist": ["a", "b"], "scalar_get": "a, b", "contains": True,
            "items_sorted": [["Other", "c"], ["X-Test", "a"], ["X-Test", "b"]],
            "defaults_type": "dict", "default_port_kept": 443,
            "url_str": "https://example.com:443/x"},
    },
    {
        "task_id": "colorama-codes", "source_id": "colorama-0.4.6",
        "source_group": "tartley-colorama",
        "prompt": (
            "Using only the supplied colorama 0.4.6 source, determine the results of this program on "
            "Linux:\n\n"
            "```python\nfrom colorama import Back, Fore, Style\n\n"
            "a = repr(Fore.RED)\nb = repr(Back.GREEN)\nc = repr(Style.BRIGHT)\n"
            "d = repr(Style.RESET_ALL)\ne = repr(Fore.RESET)\n"
            "names = sorted(k for k in vars(Fore) if not k.startswith(\"_\"))[:6]\n```\n\n"
            "Report `fore_red_repr` (`a`), `back_green_repr` (`b`), `style_bright_repr` (`c`), "
            "`style_reset_all_repr` (`d`), `fore_reset_repr` (`e`), `code_type` "
            "(`type(Fore.RED).__name__`) and `public_names` (`names`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "fore_red_repr": "'\\x1b[31m'", "back_green_repr": "'\\x1b[42m'",
            "style_bright_repr": "'\\x1b[1m'", "style_reset_all_repr": "'\\x1b[0m'",
            "fore_reset_repr": "'\\x1b[39m'", "code_type": "str",
            "public_names": ["BLACK", "BLUE", "CYAN", "GREEN", "LIGHTBLACK_EX", "LIGHTBLUE_EX"]},
    },
    {
        "task_id": "colorama-ansi", "source_id": "colorama-0.4.6",
        "source_group": "tartley-colorama",
        "prompt": (
            "Using only the supplied colorama 0.4.6 source, determine the results of this program on "
            "Linux:\n\n"
            "```python\nfrom colorama import AnsiToWin32, Fore, Style, ansi\n\n"
            "a = len(AnsiToWin32(Fore.RED, convert=True).get_win32_calls())\n"
            "b = type(AnsiToWin32(Fore.RED + \"x\", convert=True, strip=True).get_win32_calls()).__name__\n"
            "c = AnsiToWin32(\"\", convert=True).get_win32_calls()\n"
            "d = repr(ansi.CSI)\ne = repr(ansi.OSC)\n"
            "f = ansi.strip(Fore.RED + \"x\" + Style.RESET_ALL) if hasattr(ansi, \"strip\") else None\n```\n\n"
            "Report `red_call_count` (`a`), `converted_strip_calls_type` (`b`), "
            "`empty_string_calls` (`c`), `csi_repr` (`d`), `osc_repr` (`e`) and `strip_result` (`f`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "red_call_count": 0, "converted_strip_calls_type": "dict", "empty_string_calls": {},
            "csi_repr": "'\\x1b['", "osc_repr": "'\\x1b]'", "strip_result": None},
    },
    {
        "task_id": "docutils-tree", "source_id": "docutils-0.23", "source_group": "docutils-docutils",
        "prompt": (
            "Using only the supplied docutils 0.23 source, determine the results of this program "
            "(no file I/O and no writers):\n\n"
            "```python\nfrom docutils import nodes\nfrom docutils.core import publish_doctree\n\n"
            "tree = publish_doctree(\"Title\\n=====\\n\\nA *para* with `ref`_.\\n\\n- one\\n- two\\n\")\n"
            "a = [n.tagname for n in tree.children]\nb = tree.children[0].astext()\n"
            "c = len(list(tree.findall(nodes.section)))\n"
            "d = [n.astext() for n in tree.findall(nodes.list_item)][:3]\n"
            "e = [n.astext() for n in tree.findall(nodes.emphasis)]\n"
            "f = any(isinstance(n, nodes.docinfo) for n in tree.children)\n"
            "g = \"Title\" in tree.astext()\n```\n\n"
            "Report `child_types` (`a`), `title_text` (`b`), `section_count` (`c`), `bullet_items` "
            "(`d`), `emphasis_text` (`e`), `docinfo_present` (`f`) and `tree_astype_contains_title` "
            "(`g`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "child_types": ["title", "paragraph", "bullet_list"], "title_text": "Title",
            "section_count": 0, "bullet_items": ["one", "two"], "emphasis_text": ["para"],
            "docinfo_present": False, "tree_astype_contains_title": True},
    },
    {
        "task_id": "docutils-nodes", "source_id": "docutils-0.23",
        "source_group": "docutils-docutils",
        "prompt": (
            "Using only the supplied docutils 0.23 source, determine the results of this program "
            "(no file I/O and no writers):\n\n"
            "```python\nfrom docutils import nodes, utils\n\nnode = nodes.paragraph(\"\", \"hello\")\n"
            "a = node.pformat().splitlines()[0]\nb = node.astext()\nc = type(node).__name__\n"
            "d = \"paragraph\" in repr(node)\ne = isinstance(nodes.Text(\"x\"), nodes.Text)\n"
            "f = len(utils.escape2null(\"ab\"))\n"
            "g = utils.unescape(utils.escape2null(\"ab\\x00\"))[0]\n"
            "h = utils.column_width(\"ab\\tcd\")\n```\n\n"
            "Report `pformat_first_line` (`a`), `astype` (`b`), `node_type` (`c`), "
            "`repr_contains_paragraph` (`d`), `text_node_is_text` (`e`), `escaped_length` (`f`), "
            "`unescape_roundtrip` (`g`) and `column_widths` (`h`).\n\n"
            "Return exactly one JSON object with these eight keys, no prose."),
        "expected": {
            "pformat_first_line": "<paragraph>", "astype": "hello", "node_type": "paragraph",
            "repr_contains_paragraph": True, "text_node_is_text": True, "escaped_length": 2,
            "unescape_roundtrip": "a", "column_widths": 5},
    },
])


TASKS.extend([
    {
        "task_id": "networkx-paths", "source_id": "networkx-3.7", "source_group": "networkx-networkx",
        "prompt": (
            "Using only the supplied networkx 3.7 source, determine the results of this program:\n\n"
            "```python\nimport networkx as nx\n\n"
            "g = nx.Graph()\n"
            "g.add_weighted_edges_from([(\"a\", \"b\", 1), (\"b\", \"c\", 2), (\"a\", \"c\", 5), (\"c\", \"d\", 1)])\n"
            "a = nx.dijkstra_path(g, \"a\", \"d\", weight=\"weight\")\n"
            "b = nx.dijkstra_path_length(g, \"a\", \"d\", weight=\"weight\")\n"
            "c = nx.bellman_ford_path(g, \"a\", \"d\", weight=\"weight\")\n"
            "d = sorted(nx.all_shortest_paths(g, \"a\", \"d\"))\n"
            "e = nx.has_path(g, \"a\", \"d\")\nf = nx.is_connected(g)\n```\n\n"
            "Report `dijkstra` (`a`), `dijkstra_length` (`b`), `bellman_ford` (`c`), "
            "`unweighted_paths` (`d`), `has_path` (`e`) and `is_connected` (`f`). Note that `d` uses "
            "unweighted hop counts while `a` uses the `weight` attribute.\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "dijkstra": ["a", "b", "c", "d"], "dijkstra_length": 4,
            "bellman_ford": ["a", "b", "c", "d"], "unweighted_paths": [["a", "c", "d"]],
            "has_path": True, "is_connected": True},
    },
    {
        "task_id": "networkx-structure", "source_id": "networkx-3.7",
        "source_group": "networkx-networkx",
        "prompt": (
            "Using only the supplied networkx 3.7 source, determine the results of this program:\n\n"
            "```python\nimport networkx as nx\n\n"
            "c = nx.Graph()\nc.add_edges_from([(i, i + 1) for i in range(6)])\n"
            "a = nx.diameter(c)\nb = nx.eccentricity(c, 0)\n"
            "d = list(nx.bfs_tree(c, 0).nodes())\ne = nx.clustering(c, 1)\n"
            "f = list(nx.cycle_basis(c))\ng = sorted((n, d) for n, d in c.degree())\n"
            "h = nx.complement(c).number_of_edges()\n```\n\n"
            "Report `diameter` (`a`), `eccentricity_zero` (`b`), `bfs_order_from_zero` (`d`), "
            "`clustering_of_path` (`e`), `cycle_basis` (`f`), `degree_sequence` (`g`) and "
            "`complement_edges` (`h`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "diameter": 6, "eccentricity_zero": 6,
            "bfs_order_from_zero": [0, 1, 2, 3, 4, 5, 6],
            "clustering_of_path": 0, "cycle_basis": [],
            "degree_sequence": [[0, 1], [1, 2], [2, 2], [3, 2], [4, 2], [5, 2], [6, 1]],
            "complement_edges": 15},
    },
    {
        "task_id": "pygments-lex", "source_id": "pygments-2.21.0",
        "source_group": "pygments-pygments",
        "prompt": (
            "Using only the supplied Pygments 2.21.0 source, determine the results of this program. "
            "Report token types as their string form, e.g. `Token.Keyword`:\n\n"
            "```python\nfrom pygments import lex\nfrom pygments.lexers import PythonLexer, TextLexer, get_lexer_by_name\n\n"
            "tokens = [(str(t), v) for t, v in lex(\"def f(x):\\n    return x + 1\\n\", PythonLexer())]\n"
            "text_tokens = [(str(t), v) for t, v in lex(\"plain text\", TextLexer())]\n"
            "name = get_lexer_by_name(\"python\").name\naliases = PythonLexer.aliases\n```\n\n"
            "Report `first_tokens` (the first four entries of `tokens`), `keyword_count` (how many "
            "entries have token type `Token.Keyword`), `name_count` (how many have `Token.Name`), "
            "`text_tokens` (the first two entries of `text_tokens`), `lexer_name` (`name`) and "
            "`aliases_contain_py` (whether `\"py\"` is in `aliases`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "first_tokens": [["Token.Keyword", "def"], ["Token.Text.Whitespace", " "],
                             ["Token.Name.Function", "f"], ["Token.Punctuation", "("]],
            "keyword_count": 2, "name_count": 2,
            "text_tokens": [["Token.Text", "plain text\n"]],
            "lexer_name": "Python", "aliases_contain_py": True},
    },
    {
        "task_id": "pygments-tokens", "source_id": "pygments-2.21.0",
        "source_group": "pygments-pygments",
        "prompt": (
            "Using only the supplied Pygments 2.21.0 source, determine the results of this program:\n\n"
            "```python\nfrom pygments import highlight\nfrom pygments.formatters import RawTokenFormatter\n"
            "from pygments.lexers import PythonLexer\nfrom pygments.styles import get_style_by_name\n"
            "from pygments.token import Comment, Keyword, Name, Number, String\n\n"
            "raw = highlight(\"x = 1\\n\", PythonLexer(), RawTokenFormatter())\n"
            "style = get_style_by_name(\"default\")\n"
            "n = str(Number.Integer)\ns = str(String.Double)\nc = str(Comment.Single)\n"
            "k = str(Keyword.Declaration)\nf = str(Name.Function)\n```\n\n"
            "Report `raw_first_line` (the first line of `raw`, decoded to text), `raw_line_count` "
            "(how many lines `raw` has), `number_token_str` (`n`), `string_token_str` (`s`), "
            "`comment_token_str` (`c`), `keyword_token_parent` (`k`), `name_token_parent` (`f`), "
            "`style_background` (`style.background_color`) and `style_class` "
            "(`type(style).__name__`).\n\n"
            "Return exactly one JSON object with these nine keys, no prose."),
        "expected": {
            "raw_first_line": "Token.Name\t'x'", "raw_line_count": 6,
            "number_token_str": "Token.Literal.Number.Integer", "string_token_str": "Token.Literal.String.Double",
            "comment_token_str": "Token.Comment.Single", "keyword_token_parent": "Token.Keyword.Declaration",
            "name_token_parent": "Token.Name.Function", "style_background": "#f8f8f8",
            "style_class": "StyleMeta"},
    },
    {
        "task_id": "rich-render", "source_id": "rich-15.0.0", "source_group": "textualize-rich",
        "prompt": (
            "Using only the supplied rich 15.0.0 source, determine the results of this program:\n\n"
            "```python\nfrom io import StringIO\nfrom rich.console import Console\n\n"
            "buf = StringIO()\n"
            "con = Console(file=buf, width=40, force_terminal=False, color_system=None)\n"
            "con.print(\"hello [bold]world[/bold]\", end=\"\\n\")\n"
            "out = buf.getvalue()\n```\n\n"
            "Report `plain_output` (`out.strip()`), `markup_stripped` (whether `\"[bold]\"` is absent "
            "from `out`), `console_width` (`con.width`) and `is_terminal` (`con.is_terminal`).\n\n"
            "Return exactly one JSON object with these four keys, no prose."),
        "expected": {
            "plain_output": "hello world", "markup_stripped": True, "console_width": 40,
            "is_terminal": False},
    },
    {
        "task_id": "rich-table", "source_id": "rich-15.0.0", "source_group": "textualize-rich",
        "prompt": (
            "Using only the supplied rich 15.0.0 source, determine the results of this program:\n\n"
            "```python\nfrom io import StringIO\nfrom rich.console import Console\nfrom rich.table import Table\n\n"
            "table = Table(title=\"T\")\ntable.add_column(\"a\")\ntable.add_column(\"b\")\ntable.add_row(\"1\", \"2\")\n"
            "buf = StringIO()\n"
            "Console(file=buf, width=20, force_terminal=False, color_system=None).print(table)\n"
            "lines = buf.getvalue().strip().splitlines()\n```\n\n"
            "Report `line_count` (`len(lines)`), `first_line` (`lines[0]`, exact string), "
            "`row_present` (whether some line contains both `\"1\"` and `\"2\"`) and `has_rule_chars` "
            "(whether some line contains a box-drawing rule or a hyphen).\n\n"
            "Return exactly one JSON object with these four keys, no prose."),
        "expected": {
            "line_count": 6, "first_line": "T    ", "row_present": True, "has_rule_chars": True},
    },
    {
        "task_id": "pip-helpers", "source_id": "pip-26.2.1", "source_group": "pypa-pip",
        "prompt": (
            "Using only the supplied pip 26.2.1 source, determine the results of this program (no "
            "network access and no installation):\n\n"
            "```python\nfrom pip._internal.utils.misc import format_size, get_prog, normalize_path\n"
            "from pip._vendor.packaging.requirements import Requirement\n"
            "from pip._vendor.packaging.version import parse as parse_version\n\n"
            "a = format_size(1024)\nb = format_size(5 * 1024 * 1024)\n"
            "c = get_prog().endswith(\"pip\")\nd = normalize_path(\"/tmp/../tmp/x\", resolve_symlinks=False)\n"
            "e = sorted(Requirement(\"demo[fast]>=1\").extras)\n"
            "f = parse_version(\"2.0\") > parse_version(\"1.9\")\ng = str(parse_version(\"1.0rc1\"))\n```\n\n"
            "Report `format_size_bytes` (`a`), `format_size_mb` (`b`), `prog_endswith_pip` (`c`), "
            "`normalize_path` (`d`), `vendor_requirement_extras` (`e`), `vendor_version_gt` (`f`) and "
            "`vendor_version_str` (`g`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "format_size_bytes": "1.0 kB", "format_size_mb": "5.2 MB", "prog_endswith_pip": False,
            "normalize_path": "/tmp/x", "vendor_requirement_extras": ["fast"],
            "vendor_version_gt": True, "vendor_version_str": "1.0rc1"},
    },
    {
        "task_id": "pip-package", "source_id": "pip-26.2.1", "source_group": "pypa-pip",
        "prompt": (
            "Using only the supplied pip 26.2.1 source, determine the results of this program (no "
            "network access and no installation):\n\n"
            "```python\nimport pip\n\n"
            "a = callable(getattr(pip, \"main\", None))\nb = len(pip.__version__.split(\".\"))\n"
            "c = pip.__version__[0].isdigit()\n```\n\n"
            "Report `module_has_main` (`a`), `version_tuple_len` (`b`) and `version_starts_digit` "
            "(`c`).\n\n"
            "Return exactly one JSON object with these three keys, no prose."),
        "expected": {"module_has_main": True, "version_tuple_len": 3, "version_starts_digit": True},
    },
    {
        "task_id": "sqlalchemy-core", "source_id": "sqlalchemy-2.0.54",
        "source_group": "sqlalchemy-sqlalchemy",
        "prompt": (
            "Using only the supplied SQLAlchemy 2.0.54 source, determine the results of this program "
            "(no database connection is opened):\n\n"
            "```python\nimport sqlalchemy as sa\n\nm = sa.MetaData()\n"
            "t = sa.Table(\"t\", m, sa.Column(\"id\", sa.Integer, primary_key=True), sa.Column(\"name\", sa.String(20)))\n"
            "sel = sa.select(t.c.name).where(t.c.id == 5).order_by(t.c.name.desc()).limit(10)\n"
            "compiled = sel.compile()\ninsert = t.insert().values(id=1, name=\"x\").compile()\n"
            "d = sa.create_engine(\"sqlite://\")\n```\n\n"
            "Report `select_has_where` (whether `\"WHERE\"` is in `str(compiled)`), "
            "`select_param_values` (the sorted string forms of `compiled.params.values()`), "
            "`insert_sql_prefix` (`str(insert).split(\"VALUES\")[0].strip()`), `table_columns`, "
            "`primary_key_cols`, `column_type_str` (`str(t.c.name.type)`), `dialect_name` "
            "(`d.dialect.name`) and `compiled_type` (`type(compiled).__name__`).\n\n"
            "Return exactly one JSON object with these eight keys, no prose."),
        "expected": {
            "select_has_where": True, "select_param_values": ["10", "5"],
            "insert_sql_prefix": "INSERT INTO t (id, name)", "table_columns": ["id", "name"],
            "primary_key_cols": ["id"], "column_type_str": "VARCHAR(20)", "dialect_name": "sqlite",
            "compiled_type": "StrSQLCompiler"},
    },
    {
        "task_id": "sqlalchemy-types", "source_id": "sqlalchemy-2.0.54",
        "source_group": "sqlalchemy-sqlalchemy",
        "prompt": (
            "Using only the supplied SQLAlchemy 2.0.54 source, determine the results of this program "
            "(no database connection is opened):\n\n"
            "```python\nimport sqlalchemy as sa\n\nm = sa.MetaData()\n"
            "t = sa.Table(\"t\", m, sa.Column(\"name\", sa.String(20)))\n"
            "a = sa.Integer().python_type.__name__\nb = t.c.name.type.length\n"
            "c = str(sa.text(\"x = :v\"))\nd = sa.exc.NoResultFound.__name__\n"
            "e = sa.__version__.split(\".\")[0]\nf = \":\" in str(sa.select(sa.literal(1)).compile())\n"
            "g = hasattr(sa, \"orm\")\n```\n\n"
            "Report `integer_python_type` (`a`), `string_length` (`b`), `text_clause_str` (`c`), "
            "`exc_name` (`d`), `version_major` (`e`), `literal_bind_has_param` (`f`) and "
            "`has_orm_attr` (`g`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "integer_python_type": "int", "string_length": 20, "text_clause_str": "x = :v",
            "exc_name": "NoResultFound", "version_major": "2", "literal_bind_has_param": True,
            "has_orm_attr": False},
    },
])


TASKS.extend([
    {
        "task_id": "marshmallow-fields", "source_id": "marshmallow-4.3.1",
        "source_group": "marshmallow-code-marshmallow",
        "prompt": (
            "Using only the supplied marshmallow 4.3.1 source, determine the results of this "
            "program. Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom marshmallow import Schema, fields, validate\n\n"
            "class User(Schema):\n"
            "    name = fields.String(required=True, validate=validate.Length(min=2, max=5))\n"
            "    age = fields.Integer(load_default=18, validate=validate.Range(min=0, max=120))\n"
            "    email = fields.Email()\n    tags = fields.List(fields.String())\n"
            "    role = fields.String(load_default=\"user\", dump_default=\"user\")\n\n"
            "s = User()\n"
            "loaded = s.load({\"name\": \"ada\", \"age\": 36, \"email\": \"a@b.com\", \"tags\": [\"x\"]})\n"
            "dumped = s.dump({\"name\": \"ada\", \"age\": 36, \"tags\": [\"x\"]})\n"
            "errors = s.validate({\"name\": \"a\", \"age\": 200})\n```\n\n"
            "Report `loaded`, `dumped`, `error_keys` (`sorted(errors)`), `name_error_type` "
            "(`type(errors[\"name\"][0]).__name__`) and `age_error_first` (`errors[\"age\"][0]`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "loaded": {"age": 36, "email": "a@b.com", "name": "ada", "role": "user", "tags": ["x"]},
            "dumped": {"age": 36, "name": "ada", "role": "user", "tags": ["x"]},
            "error_keys": ["age", "name"], "name_error_type": "str",
            "age_error_first": "Must be greater than or equal to 0 and less than or equal to 120."},
    },
    {
        "task_id": "marshmallow-behavior", "source_id": "marshmallow-4.3.1",
        "source_group": "marshmallow-code-marshmallow",
        "prompt": (
            "Using only the supplied marshmallow 4.3.1 source, determine the results of this "
            "program. Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom marshmallow import EXCLUDE, INCLUDE, RAISE, Schema, ValidationError, fields\n\n"
            "class U(Schema):\n"
            "    name = fields.String()\n"
            "    age = fields.Integer(load_default=18)\n"
            "    role = fields.String(load_default=\"user\")\n\n"
            "excluded = U().load({\"name\": \"ada\", \"extra\": 1}, unknown=EXCLUDE)\n"
            "included = U(unknown=INCLUDE).load({\"name\": \"ada\", \"extra\": 1})\n"
            "try:\n    U().load({\"name\": \"ada\", \"extra\": 1}, unknown=RAISE)\n    raised = None\n"
            "except ValidationError as exc:\n    raised = sorted(exc.messages)\n"
            "meta = hasattr(Schema, \"Meta\")\nfields_module = hasattr(__import__(\"marshmallow\"), \"fields\")\n```\n\n"
            "Report `exclude_extra_keys` (`sorted(excluded)`), `raise_error_keys` (`raised`), "
            "`include_extra_keys` (`sorted(included)`), `has_meta` (`meta`), `has_fields_module` "
            "(`fields_module`) and `validation_error_name` (`ValidationError.__name__`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "exclude_extra_keys": ["age", "name", "role"], "raise_error_keys": ["extra"],
            "include_extra_keys": ["age", "extra", "name", "role"], "has_meta": True,
            "has_fields_module": True, "validation_error_name": "ValidationError"},
    },
    {
        "task_id": "cerberus-basic", "source_id": "cerberus-1.3.8",
        "source_group": "pyeclipse-cerberus",
        "prompt": (
            "Using only the supplied cerberus 1.3.8 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom cerberus import Validator\n\n"
            "schema = {\"name\": {\"type\": \"string\", \"minlength\": 2, \"required\": True},\n"
            "          \"age\": {\"type\": \"integer\", \"min\": 0, \"max\": 120, \"default\": 18},\n"
            "          \"tags\": {\"type\": \"list\", \"schema\": {\"type\": \"string\"}}}\n"
            "v = Validator(schema)\n"
            "good = v.validated({\"name\": \"ada\", \"tags\": [\"x\"]})\n"
            "bad = v.validate({\"name\": \"a\", \"age\": 200, \"tags\": [1]})\n```\n\n"
            "Report `validated_default_age` (`good[\"age\"]`), `bad_result` (`bad`), "
            "`error_fields` (`sorted(v.errors)`), `name_error_message` (`v.errors[\"name\"][0]`) and "
            "`age_error_message` (`v.errors[\"age\"][0]`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "validated_default_age": 18, "bad_result": False,
            "error_fields": ["age", "name", "tags"], "name_error_message": "min length is 2",
            "age_error_message": "max value is 120"},
    },
    {
        "task_id": "cerberus-errors", "source_id": "cerberus-1.3.8",
        "source_group": "pyeclipse-cerberus",
        "prompt": (
            "Using only the supplied cerberus 1.3.8 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom cerberus import Validator\nfrom cerberus.errors import ValidationError\n\n"
            "v2 = Validator({\"name\": {\"type\": \"string\", \"required\": True}})\n"
            "missing = not v2.validate({})\n"
            "v = Validator({\"name\": {\"type\": \"string\", \"minlength\": 2}, \"age\": {\"type\": \"integer\", \"max\": 120}})\n"
            "v.validate({\"name\": \"a\", \"age\": 200})\n"
            "norm = v.normalized({\"name\": \"ada\"}) if hasattr(v, \"normalized\") else {}\n"
            "types = sorted({type(v2.errors[\"name\"][0]).__name__, type(v.errors[\"age\"][0]).__name__})\n```\n\n"
            "Report `missing_required` (`missing`), `missing_error_field` (`sorted(v2.errors)`), "
            "`required_error_type` (`type(v2.errors[\"name\"][0]).__name__`), `validation_error_name` "
            "(`ValidationError.__name__`), `error_value_types` (`types`) and `normalized_default` "
            "(`norm`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "missing_required": True, "missing_error_field": ["name"], "required_error_type": "str",
            "validation_error_name": "ValidationError", "error_value_types": ["str"],
            "normalized_default": {"age": 18, "name": "ada"}},
    },
    {
        "task_id": "pathspec-match", "source_id": "pathspec-1.1.1",
        "source_group": "cpburnz-pathspec",
        "prompt": (
            "Using only the supplied pathspec 1.1.1 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom pathspec import PathSpec\n\n"
            "spec = PathSpec.from_lines(\"gitwildmatch\", [\"*.py\", \"!test_*.py\", \"build/\"])\n"
            "a = spec.match_file(\"pkg/main.py\")\nb = spec.match_file(\"pkg/test_main.py\")\n"
            "c = spec.match_file(\"build/out.o\")\nd = spec.match_file(\"README.md\")\n"
            "e = spec.match_file(\"build/nested/x.py\")\nf = len(spec.patterns)\n```\n\n"
            "Report `main_py` (`a`), `test_py` (`b`), `build_file` (`c`), `readme` (`d`), "
            "`nested_build` (`e`) and `patterns_len` (`f`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "main_py": True, "test_py": False, "build_file": True, "readme": False,
            "nested_build": True, "patterns_len": 3},
    },
    {
        "task_id": "pathspec-util", "source_id": "pathspec-1.1.1",
        "source_group": "cpburnz-pathspec",
        "prompt": (
            "Using only the supplied pathspec 1.1.1 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom pathspec import PathSpec\nfrom pathspec.util import normalize_file\n\n"
            "other = PathSpec.from_lines(\"gitwildmatch\", [\"docs/*\", \"/root.txt\"])\n"
            "a = other.match_file(\"docs/index.md\")\nb = other.match_file(\"docs/sub/page.md\")\n"
            "c = other.match_file(\"root.txt\")\nd = other.match_file(\"sub/root.txt\")\n"
            "e = normalize_file(\"dir\\\\file.py\")\n```\n\n"
            "Report `docs_index` (`a`), `docs_nested` (`b`), `root_txt` (`c`), `sub_root_txt` (`d`), "
            "`normalize` (`e`) and `version_major` (`__import__(\"pathspec\").__version__.split(\".\")[0]`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "docs_index": True, "docs_nested": True, "root_txt": True, "sub_root_txt": False,
            "normalize": "dir\\file.py", "version_major": "1"},
    },
    {
        "task_id": "wheel-tags", "source_id": "wheel-0.48.0", "source_group": "pypa-wheel",
        "prompt": (
            "Using only the supplied wheel 0.48.0 source, determine the results of this program "
            "(nothing is built or installed):\n\n"
            "```python\nimport wheel\nfrom wheel.wheelfile import WheelFile\n\n"
            "tag = \"py3-none-any\"\n"
            "parts = tag.split(\"-\")\n```\n\n"
            "Report `wheelfile_class` (`WheelFile.__name__`), `tag_string` (`tag`), `parts` (`parts`), "
            "`python_tag` (`parts[0]`), `abi_tag` (`parts[1]`), `platform_tag` (`parts[2]`), "
            "`version_major` (`wheel.__version__.split(\".\")[0]`) and `has_wheelfile_attr` (whether "
            "`WheelFile` exposes a method whose name starts with `get_zipinfo`).\n\n"
            "Return exactly one JSON object with these eight keys, no prose."),
        "expected": {
            "wheelfile_class": "WheelFile", "tag_string": "py3-none-any",
            "parts": ["py3", "none", "any"], "python_tag": "py3", "abi_tag": "none",
            "platform_tag": "any", "version_major": "0", "has_wheelfile_attr": True},
    },
    {
        "task_id": "wheel-metadata", "source_id": "wheel-0.48.0", "source_group": "pypa-wheel",
        "prompt": (
            "Using only the supplied wheel 0.48.0 source, determine the results of this program "
            "(nothing is built or installed). The `wheel.metadata` module is private in this "
            "version but still importable:\n\n"
            "```python\nfrom wheel.metadata import convert_requirements\nfrom wheel.wheelfile import WheelFile\n"
            "import wheel.metadata as meta\n\n"
            "a = list(convert_requirements([\"demo[fast]>=1\"]))\n"
            "b = list(convert_requirements([\"demo\"]))\n```\n\n"
            "Report `converted_requirements` (`a`), `converted_plain` (`b`), `wheelfile_methods` "
            "(the first four names of `sorted(n for n in dir(WheelFile) if not n.startswith(\"_\"))`), "
            "`has_metadata_module` (boolean true), and `metadata_functions` (the first four names of "
            "`sorted(n for n in dir(meta) if not n.startswith(\"_\"))`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "converted_requirements": ["demo[fast] >=1"], "converted_plain": ["demo"],
            "wheelfile_methods": ["close", "comment", "extract", "extractall"],
            "has_metadata_module": True,
            "metadata_functions": ["convert_requirements", "generate_requirements",
                                   "pkginfo_to_metadata", "requires_to_requires_dist"]},
    },
    {
        "task_id": "voluptuous-schema", "source_id": "voluptuous-0.16.0",
        "source_group": "alecthomas-voluptuous",
        "prompt": (
            "Using only the supplied voluptuous 0.16.0 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nimport voluptuous as vol\n"
            "from voluptuous import All, Length, Required, Schema\n\n"
            "schema = Schema({Required(\"name\"): All(str, Length(min=2)),\n"
            "                 \"age\": All(int, vol.Range(min=0, max=120)),\n"
            "                 vol.Optional(\"role\", default=\"user\"): str})\n"
            "good = schema({\"name\": \"ada\", \"age\": 36})\n```\n\n"
            "Report `applied_defaults` (`good`), `role_default` (`good[\"role\"]`), "
            "`valid_output_keys` (`sorted(good)`) and `schema_class` (`Schema.__name__`).\n\n"
            "Return exactly one JSON object with these four keys, no prose."),
        "expected": {
            "applied_defaults": {"age": 36, "name": "ada", "role": "user"}, "role_default": "user",
            "valid_output_keys": ["age", "name", "role"], "schema_class": "Schema"},
    },
    {
        "task_id": "voluptuous-errors", "source_id": "voluptuous-0.16.0",
        "source_group": "alecthomas-voluptuous",
        "prompt": (
            "Using only the supplied voluptuous 0.16.0 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nimport voluptuous as vol\nfrom voluptuous import Any, Invalid, Length, MultipleInvalid, Schema\n\n"
            "schema = Schema({\"name\": vol.All(str, Length(min=2)), \"age\": vol.All(int, vol.Range(max=120))})\n"
            "try:\n    schema({\"name\": \"a\", \"age\": 200})\n    paths = None\n"
            "except MultipleInvalid as exc:\n    paths = sorted(str(e.path[0]) for e in exc.errors)\n"
            "try:\n    Length(min=5)(\"ab\")\n    length_error = None\nexcept Invalid as exc:\n"
            "    length_error = type(exc).__name__\n```\n\n"
            "Report `error_paths` (`paths`), `length_error` (`length_error`), `invalid_class` "
            "(`Invalid.__name__`), `multiple_invalid_class` (`MultipleInvalid.__name__`), "
            "`schema_class` (`Schema.__name__`), `version_major` "
            "(`vol.__version__.split(\".\")[0]`) and `any_result` (`Any(\"x\", \"y\")(\"y\")`).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "error_paths": ["age", "name"], "length_error": "LengthInvalid", "invalid_class": "Invalid",
            "multiple_invalid_class": "MultipleInvalid", "schema_class": "Schema",
            "version_major": "0", "any_result": "y"},
    },
])


TASKS.extend([
    {
        "task_id": "toolz-iter", "source_id": "toolz-1.1.0", "source_group": "pytoolz-toolz",
        "prompt": (
            "Using only the supplied toolz 1.1.0 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom toolz import groupby, interleave, partition, sliding_window, unique\n\n"
            "a = [list(x) for x in partition(2, [1, 2, 3, 4, 5])]\n"
            "b = [list(x) for x in sliding_window(3, [1, 2, 3, 4])]\n"
            "c = list(interleave([[1, 2], [3, 4]]))\n"
            "d = list(unique([1, 1, 2, 3, 2]))\n"
            "e = {k: list(v) for k, v in groupby(lambda x: x % 2, [1, 2, 3, 4]).items()}\n```\n\n"
            "Report `partition` (`a`), `sliding_window` (`b`), `interleave` (`c`), `unique` (`d`) "
            "and `groupby_parity` (`e`, with string keys).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "partition": [[1, 2], [3, 4]], "sliding_window": [[1, 2, 3], [2, 3, 4]],
            "interleave": [1, 3, 2, 4], "unique": [1, 2, 3],
            "groupby_parity": {"0": [2, 4], "1": [1, 3]}},
    },
    {
        "task_id": "toolz-func", "source_id": "toolz-1.1.0", "source_group": "pytoolz-toolz",
        "prompt": (
            "Using only the supplied toolz 1.1.0 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom toolz import curry, merge_with, pipe\nfrom toolz.functoolz import compose, memoize\n\n"
            "a = pipe(3, lambda x: x + 1, lambda x: x * 2)\n"
            "b = compose(lambda x: x * 2, lambda x: x + 1)(3)\n"
            "c = curry(lambda p, q: p + q)(1)(2)\n"
            "d = merge_with(sum, [{\"a\": 1, \"b\": 2}, {\"a\": 3}])\n"
            "e = memoize(lambda n: n * n)(4)\n```\n\n"
            "Report `pipe_result` (`a`), `compose_result` (`b`), `curry_add_one` (`c`), "
            "`merge_with_sum` (`d`), `memoize_calls` (`e`) and `has_curry` "
            "(`callable(curry)`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "pipe_result": 8, "compose_result": 8, "curry_add_one": 3,
            "merge_with_sum": {"a": 4, "b": 2}, "memoize_calls": 16, "has_curry": True},
    },
    {
        "task_id": "funcy-seqs", "source_id": "funcy-2.1", "source_group": "suor-funcy",
        "prompt": (
            "Using only the supplied funcy 2.1 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom funcy import cat, chunks, flatten, lmap, partition, take\n\n"
            "a = lmap(lambda x: x * 2, [1, 2, 3])\n"
            "b = list(flatten([[1, 2], [3]]))\n"
            "c = [list(x) for x in partition(2, [1, 2, 3, 4, 5])]\n"
            "d = [list(x) for x in chunks(2, [1, 2, 3, 4, 5])]\n"
            "e = list(cat([[1, 2], [3]]))\nf = list(take(2, [1, 2, 3]))\n```\n\n"
            "Report `lmap` (`a`), `flatten` (`b`), `partition` (`c`), `chunks` (`d`), `cat` (`e`) "
            "and `take` (`f`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "lmap": [2, 4, 6], "flatten": [1, 2, 3], "partition": [[1, 2], [3, 4]],
            "chunks": [[1, 2], [3, 4], [5]], "cat": [1, 2, 3], "take": [1, 2]},
    },
    {
        "task_id": "funcy-colls", "source_id": "funcy-2.1", "source_group": "suor-funcy",
        "prompt": (
            "Using only the supplied funcy 2.1 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nimport funcy\nfrom funcy import compose, even, first, group_by, where\n\n"
            "a = {k: list(v) for k, v in group_by(even, [1, 2, 3, 4]).items()}\n"
            "b = list(where([{\"a\": 1}, {\"a\": 2}], a=2))\n"
            "c = compose(lambda x: x * 2, lambda x: x + 1)(3)\n"
            "d = first([9, 8, 7])\n```\n\n"
            "Report `group_by` (`a`, with string keys), `where` (`b`), `compose` (`c`), `first` (`d`) "
            "and `has_odd` (`callable(getattr(funcy, \"odd\", None))`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "group_by": {"false": [1, 3], "true": [2, 4]}, "where": [{"a": 2}], "compose": 8,
            "first": 9, "has_odd": True},
    },
    {
        "task_id": "dotenv-parse", "source_id": "python-dotenv-1.2.3",
        "source_group": "theskumar-python-dotenv",
        "prompt": (
            "Using only the supplied python-dotenv 1.2.3 source, determine the results of this "
            "program. Answer every field, consulting the source where needed:\n\n"
            "```python\nimport io\nfrom dotenv.main import parse_stream\n\n"
            "text = 'PLAIN=value\\nQUOTED=\"a b\"\\nSINGLE=\\'c d\\'\\nEMPTY=\\nEXPORT=1\\n"
            "export EXPORTED=2\\n# comment\\nHASH=no#comment\\n'\n"
            "parsed = {b.key: b.value for b in parse_stream(io.StringIO(text)) if b.key is not None}\n```\n\n"
            "Report `has_plain` (`parsed.get(\"PLAIN\")`), `quoted_value`, `single_value`, "
            "`empty_value`, `hash_value`, `exported_value` and `comment_absent` (whether "
            "`parsed.get(\"# comment\")` is None).\n\n"
            "Return exactly one JSON object with these seven keys, no prose."),
        "expected": {
            "has_plain": "value", "quoted_value": "a b", "single_value": "c d", "empty_value": "",
            "hash_value": "no#comment", "exported_value": "2", "comment_absent": True},
    },
    {
        "task_id": "dotenv-stream", "source_id": "python-dotenv-1.2.3",
        "source_group": "theskumar-python-dotenv",
        "prompt": (
            "Using only the supplied python-dotenv 1.2.3 source, determine the results of this "
            "program. Answer every field, consulting the source where needed:\n\n"
            "```python\nimport io\nimport dotenv\nfrom dotenv import dotenv_values\nfrom dotenv.main import DotEnv\nfrom dotenv.parser import Binding\n\n"
            "values = dict(dotenv_values(stream=io.StringIO('A=1\\nB=\"${A}2\"\\n')))\n"
            "has_values = callable(dotenv.dotenv_values)\nhas_find = callable(dotenv.find_dotenv)\n```\n\n"
            "Report `stream_values` (`values`), `dotenv_class` (`DotEnv.__name__`), `binding_class` "
            "(`Binding.__name__`), `has_dotenv_values` (`has_values`) and `has_find_dotenv` "
            "(`has_find`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "stream_values": {"A": "1", "B": "12"}, "dotenv_class": "DotEnv",
            "binding_class": "Binding", "has_dotenv_values": True, "has_find_dotenv": True},
    },
    {
        "task_id": "xmltodict-parse", "source_id": "xmltodict-1.0.4",
        "source_group": "martinblech-xmltodict",
        "prompt": (
            "Using only the supplied xmltodict 1.0.4 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nimport xmltodict\n\n"
            "doc = xmltodict.parse(\"<root><a>1</a><b x='y'><c>2</c></b></root>\")\n"
            "empty = xmltodict.parse(\"<r><e/></r>\")[\"r\"][\"e\"]\n"
            "listing = xmltodict.parse(\"<r><i>1</i><i>2</i></r>\")[\"r\"][\"i\"]\n```\n\n"
            "Report `parsed` (`doc`), `root_keys` (`sorted(doc[\"root\"])`), `attr_dict` "
            "(`doc[\"root\"][\"b\"]`), `text_value` (`doc[\"root\"][\"a\"]`), `empty_tag` (`empty`) and "
            "`list_handling` (`listing`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "parsed": {"root": {"a": "1", "b": {"@x": "y", "c": "2"}}}, "root_keys": ["a", "b"],
            "attr_dict": {"@x": "y", "c": "2"}, "text_value": "1", "empty_tag": None,
            "list_handling": ["1", "2"]},
    },
    {
        "task_id": "xmltodict-unparse", "source_id": "xmltodict-1.0.4",
        "source_group": "martinblech-xmltodict",
        "prompt": (
            "Using only the supplied xmltodict 1.0.4 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nimport xmltodict\n\n"
            "text = xmltodict.unparse({\"root\": {\"a\": \"1\", \"b\": {\"@x\": \"y\", \"c\": \"2\"}}})\n```\n\n"
            "Report `unparsed` (`text`), `starts_with_root` (whether `text.startswith(\"<root>\")`), "
            "`contains_attr` (whether `'x=\"y\"'` is in `text`), `has_expat` "
            "(`hasattr(xmltodict, \"expat\")`) and `has_unparse` (`callable(xmltodict.unparse)`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "unparsed": "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<root><a>1</a><b x=\"y\"><c>2</c></b></root>",
            "starts_with_root": False, "contains_attr": True, "has_expat": True, "has_unparse": True},
    },
    {
        "task_id": "jmespath-search", "source_id": "jmespath-1.1.0",
        "source_group": "jmespath-jmespath",
        "prompt": (
            "Using only the supplied jmespath 1.1.0 source, determine the results of this program. "
            "Answer every field, consulting the source where needed:\n\n"
            "```python\nfrom jmespath import search\n\n"
            "data = {\"people\": [{\"name\": \"ada\", \"age\": 36}, {\"name\": \"bob\", \"age\": 20}],\n"
            "        \"meta\": {\"count\": 2}}\n"
            "a = search(\"people[*].name\", data)\n"
            "b = search(\"people[0].name\", data)\n"
            "c = search(\"people[?age > `25`].name\", data)\n"
            "d = search(\"length(people)\", data)\n"
            "e = search(\"meta.count\", data)\n"
            "f = search(\"sort(people[*].name)\", data)\n"
            "Report `names` (`a`), `first_name` (`b`), `filtered` (`c`), `length` (`d`), `count` (`e`), "
            "`sorted_names` (`f`).\n\n"
            "Return exactly one JSON object with these six keys, no prose."),
        "expected": {
            "names": ["ada", "bob"], "first_name": "ada", "filtered": ["ada"], "length": 2,
            "count": 2, "sorted_names": ["ada", "bob"]},
    },
    {
        "task_id": "jmespath-parser", "source_id": "jmespath-1.1.0",
        "source_group": "jmespath-jmespath",
        "prompt": (
            "Using only the supplied jmespath 1.1.0 source, determine the results of this program. "
            "For the error field report the exception class name, or null if no exception is "
            "raised:\n\n"
            "```python\nimport jmespath\nfrom jmespath import search\nfrom jmespath.parser import Parser\n\n"
            "data = {\"people\": [{\"name\": \"ada\"}]}\n"
            "try:\n    search(\"people[\", data)\n    err = None\n"
            "except Exception as exc:\n    err = type(exc).__name__\n"
            "compiled = jmespath.compile(\"people[0].name\")\n"
            "result = compiled.search(data)\n```\n\n"
            "Report `parse_error_class` (`err`), `parser_class` (`Parser.__name__`), `has_compile` "
            "(`callable(jmespath.compile)`), `compiled_result` (`result`) and `version_major` "
            "(`jmespath.__version__.split(\".\")[0]`).\n\n"
            "Return exactly one JSON object with these five keys, no prose."),
        "expected": {
            "parse_error_class": "IncompleteExpressionError", "parser_class": "Parser",
            "has_compile": True, "compiled_result": "ada", "version_major": "1"},
    },
])


def snapshot_manifest(source_id: str) -> dict[str, str]:
    root = DATA / "sources" / source_id
    files = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        name = p.relative_to(root).as_posix()
        assert "__pycache__" not in name and not name.endswith(".pyc"), \
            f"snapshot polluted by generated bytecode: {source_id}/{name}"
        assert name.endswith(".py") or name.startswith("LICENSES/"), \
            f"unexpected file type in snapshot: {source_id}/{name}"
        raw = p.read_bytes()
        raw.decode("utf-8")  # the actor workspace requires UTF-8 source
        files[name] = hashlib.sha256(raw).hexdigest()
    return files


def oracle_values(name: str) -> dict:
    raw = json.loads((ROOT / "research/s4c/oracle_output" / f"{name}.json").read_text())
    return {k: v for k, v in raw.items() if k not in {"loaded_from", "version"}}


def main() -> int:
    (DATA / "tasks").mkdir(parents=True, exist_ok=True)
    (DATA / "controller").mkdir(parents=True, exist_ok=True)
    checked = 0
    for spec in TASKS:
        assert all(re.fullmatch(r"[a-z_]+", k) for k in spec["expected"]), \
            f"answer keys must match the frozen [a-z_]+ contract in {spec['task_id']}"
        files = snapshot_manifest(spec["source_id"])
        assert "LICENSES/LICENSE" in files, "license missing from snapshot"
        body = {
            "schema": "reverpi.s4.task.v1", "task_id": spec["task_id"], "source_id": spec["source_id"],
            "source_group": spec["source_group"], "split": "development", "inspected": True,
            "origin": "self_authored_tasks_on_pypi_wheel_sources", "official_benchmark": False,
            "snapshot_sha256": digest(files), "files": files, "prompt": spec["prompt"],
            "answer_keys": sorted({k for k in spec["expected"]}), "artifact_version": 1,
            "replay_scope": "readonly_snapshot", "projected_answer_hidden_requirement": False,
        }
        task = {**body, "task_sha256": digest(body)}
        gold = {"schema": "reverpi.s4.gold.v1", "task_sha256": task["task_sha256"],
                "expected": spec["expected"], "source_notes": SOURCE_NOTES[spec["task_id"]]}
        (DATA / "tasks" / f"{spec['task_id']}.json").write_text(canonical(task) + "\n")
        (DATA / "controller" / f"{spec['task_id']}.gold.json").write_text(
            json.dumps(gold, indent=1, sort_keys=True) + "\n")
        print(f"wrote {spec['task_id']}: {len(files)} files, {len(spec['expected'])} keys")

    seen = set()
    for path in sorted((ROOT / "research/s4c/oracle_output").glob("*.json")):
        for task_id, got in oracle_values(path.stem).items():
            want = next(s["expected"] for s in TASKS if s["task_id"] == task_id)
            assert canonical(got) == canonical(want), f"oracle/spec mismatch in {task_id}"
            seen.add(task_id)
            checked += 1
    assert seen == {s["task_id"] for s in TASKS}, f"tasks without an oracle: {sorted({s['task_id'] for s in TASKS} - seen)}"
    print(f"oracle agreement: {checked}/{len(TASKS)} tasks identical to the written specification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
