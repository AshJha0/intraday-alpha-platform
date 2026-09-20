"""Golden group: schemas/ files and tests/golden/expected_contracts_examples.json.

Pins: every schema file parses, is draft 2020-12, carries an ``$id`` and
``x-version`` consistent with its path and ``iap.contracts.versions``; every
example instance validates against its schema; the golden document is
reproduced byte-for-byte by ``iap.contracts.examples``; the ``explain()``
rendering, the trace id and the canonical_json known answer are pinned.
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from iap.contracts.examples import (
    GOLDEN_X_VERSION,
    all_examples,
    example_trace,
    golden_document,
    render_golden,
)
from iap.contracts.ids import make_trace_id
from iap.contracts.types import DecisionTrace, explain
from iap.contracts.validate import (
    ContractValidationError,
    load_schema,
    schema_registry,
    validate,
    validate_typed,
)
from iap.contracts.versions import (
    SCHEMA_BASE_URI,
    SCHEMA_VERSIONS,
    canonical_json,
    content_hash,
    schema_dir,
)

GOLDEN_NAME = "expected_contracts_examples.json"

EXPECTED_EXPLAIN = """Order 12345
Alpha:      EQ03  expected return = +4.2 bps  confidence = 0.81
Portfolio:  target = +20,000 shares
Risk:       ALLOW
Execution:  POV 15%
SOR:        XV1 = 45%  XV2 = 35%  XV3 = 20%
Fills:      18,000 / 20,000 (90.0%)
TCA:        IS = 2.1 bps
Attribution: alpha = +6.2 bps  spread = -0.8 bps  impact = -2.1 bps  fees = -0.4 bps"""

EXPECTED_TRACE_ID = "8b9fed6896d01463e64c4de915b0614b"

NEW_SCHEMAS = (
    "portfolio/portfolio_target.schema.json",
    "order/parent_order.schema.json",
    "order/child_order.schema.json",
    "execution/venue_decision.schema.json",
    "tca/tca_result.schema.json",
    "research/experiment_spec.schema.json",
    "research/experiment_result.schema.json",
    "alpha/lifecycle_transition.schema.json",
    "trace/decision_trace.schema.json",
    "risk/risk_decision.schema.json",
)


@pytest.fixture(scope="module")
def golden(golden_dir):
    with open(golden_dir / GOLDEN_NAME, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("relpath", sorted(SCHEMA_VERSIONS))
def test_schema_file_is_consistent(relpath):
    path = schema_dir() / relpath
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["$id"] == SCHEMA_BASE_URI + relpath
    assert doc["x-version"] == SCHEMA_VERSIONS[relpath]
    assert doc["type"] == "object"
    assert doc["additionalProperties"] is False
    assert list(doc["properties"]) == doc["required"]
    assert doc["title"] and doc["description"]
    Draft202012Validator.check_schema(doc)
    for name, sub in doc.get("$defs", {}).items():
        assert sub["type"] == "object" and sub["additionalProperties"] is False, name
        assert list(sub["properties"]) == sub["required"], name


@pytest.mark.parametrize("relpath", NEW_SCHEMAS)
def test_phase0_schemas_exist_at_version_1(relpath):
    assert load_schema(relpath)["x-version"] == 1


def test_integer_fields_pin_int64_domains():
    """Every integer property is bounded: an explicit minimum/maximum
    inside the i64/u64 domain, or a closed enum."""
    for relpath in SCHEMA_VERSIONS:
        doc = load_schema(relpath)
        objects = [doc] + list(doc.get("$defs", {}).values())
        for obj in objects:
            for name, prop in obj["properties"].items():
                if prop.get("type") != "integer":
                    continue
                if "enum" in prop:
                    assert all(isinstance(v, int) for v in prop["enum"]), (relpath, name)
                    continue
                assert "minimum" in prop and "maximum" in prop, (relpath, name)
                assert prop["minimum"] >= -(2**63), (relpath, name)
                assert prop["maximum"] <= 2**64 - 1, (relpath, name)


def test_registry_resolves_every_schema_id():
    registry = schema_registry()
    for relpath in SCHEMA_VERSIONS:
        assert registry.get_or_retrieve(SCHEMA_BASE_URI + relpath).value


@pytest.mark.parametrize("name", list(all_examples()))
def test_example_validates_against_schema(name):
    inst = all_examples()[name]
    validate_typed(inst)


def test_decision_trace_refs_resolve_from_disk():
    """A wrong nested field deep inside a $ref'd sibling schema fails."""
    trace = example_trace().to_dict()
    bad = json.loads(json.dumps(trace))
    bad["stages"]["signal"][0]["confidence"] = "high"
    with pytest.raises(ContractValidationError) as info:
        validate(bad, DecisionTrace.SCHEMA)
    assert "$.stages.signal[0].confidence" in info.value.errors[0]
    bad = json.loads(json.dumps(trace))
    bad["stages"]["tca"][0]["latency_ns"]["p99"] = -1
    with pytest.raises(ContractValidationError):
        validate(bad, DecisionTrace.SCHEMA)
    bad = json.loads(json.dumps(trace))
    bad["stages"]["portfolio"] = 5
    with pytest.raises(ContractValidationError):
        validate(bad, DecisionTrace.SCHEMA)
    bad = json.loads(json.dumps(trace))
    bad["stages"]["portfolio"] = None
    bad["stages"]["attribution"] = None
    validate(bad, DecisionTrace.SCHEMA)


def test_golden_document_reproduced_byte_for_byte(golden_dir):
    expected = (golden_dir / GOLDEN_NAME).read_bytes()
    actual = render_golden(golden_document()).encode("utf-8")
    assert actual == expected


def test_golden_examples_match_and_load(golden):
    assert golden["x-version"] == GOLDEN_X_VERSION
    examples = all_examples()
    assert list(golden["examples"]) == list(examples)
    for name, entry in golden["examples"].items():
        inst = examples[name]
        assert entry["schema"] == inst.SCHEMA
        assert entry["x_version"] == inst.x_version
        assert entry["value"] == inst.to_dict()
        assert type(inst).from_dict(entry["value"]) == inst
        validate(entry["value"], inst.SCHEMA)


def test_explain_matches_pinned_block(golden):
    names = {int(k): v for k, v in golden["explain"]["venue_names"].items()}
    text = explain(example_trace(), names)
    assert text == EXPECTED_EXPLAIN
    assert golden["explain"]["text"] == EXPECTED_EXPLAIN
    loaded = DecisionTrace.from_dict(golden["examples"]["DecisionTrace"]["value"])
    assert explain(loaded, names) == EXPECTED_EXPLAIN


def test_trace_id_pinned(golden):
    t = golden["trace_id"]
    assert make_trace_id(t["session_id"], t["instrument_id"], t["event_ts"],
                         t["sequence"]) == t["expected"] == EXPECTED_TRACE_ID
    assert example_trace().trace_id == EXPECTED_TRACE_ID


def test_canonical_json_known_answer(golden):
    c = golden["canonical_json"]
    assert canonical_json(c["input"]) == c["text"]
    assert content_hash(c["input"]) == c["sha256"]
    with pytest.raises(ValueError):
        canonical_json({"nan": float("nan")})
