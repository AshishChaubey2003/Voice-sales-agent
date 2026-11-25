"""The turn_evaluations table: model, migration and their agreement. No database needed."""

import importlib.util
from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint

from api.models import TURN_EVALUATION_STATUSES, TurnEvaluation
from evals import live

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "migrations" / "versions" / "c4d2a91e7f35_add_turn_evaluations.py"


def load_migration(op_recorder=None):
    spec = importlib.util.spec_from_file_location("mig_c4d2", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_extends_the_current_single_head():
    module = load_migration()
    assert module.revision == "c4d2a91e7f35" and module.down_revision == "b7e1f0a2c3d4"
    others = {}
    for path in (ROOT / "migrations" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = next(line for line in text.splitlines() if line.startswith("revision"))
        down = next(line for line in text.splitlines() if line.startswith("down_revision"))
        others[revision.split("'")[1]] = down.split("'")[1] if "'" in down else None
    parents = set(others.values())
    heads = [rev for rev in others if rev not in parents]
    # c4d2a91e7f35 is no longer the head: d5e3b82a4c16 (sales reps and handoff) extends it. What this
    # test protects is unchanged: the history stays one linear chain with exactly one head.
    assert heads == ["d5e3b82a4c16"], f"expected one head, found {heads}"
    assert others["d5e3b82a4c16"] == "c4d2a91e7f35"


def test_migration_creates_exactly_the_model_columns_constraints_and_indexes(monkeypatch):
    calls = {"tables": {}, "indexes": [], "dropped": []}

    class Recorder:
        @staticmethod
        def f(name):
            return name

        @staticmethod
        def create_table(name, *items):
            calls["tables"][name] = items

        @staticmethod
        def create_index(name, table, columns, unique=False):
            calls["indexes"].append((name, table, tuple(columns)))

        @staticmethod
        def drop_index(name, table_name):
            calls["dropped"].append(name)

        @staticmethod
        def drop_table(name):
            calls["dropped"].append(name)

    module = load_migration()
    monkeypatch.setattr(module, "op", Recorder)
    module.upgrade()
    items = calls["tables"]["turn_evaluations"]
    migrated_columns = {i.name for i in items if i.__class__.__name__ == "Column"}
    assert migrated_columns == {c.name for c in TurnEvaluation.__table__.columns}

    constraint_names = {getattr(i, "name", None) for i in items if i.__class__.__name__ != "Column"}
    model_names = {c.name for c in TurnEvaluation.__table__.constraints if c.name}
    assert model_names <= {str(n) for n in constraint_names}, model_names - constraint_names

    model_indexes = {(i.name, "turn_evaluations", tuple(c.name for c in i.columns)) for i in TurnEvaluation.__table__.indexes}
    assert set(calls["indexes"]) == model_indexes

    module.downgrade()
    assert calls["dropped"][-1] == "turn_evaluations"  # only this table is dropped


def test_one_evaluation_per_message_and_evaluator_version_is_enforced_by_the_schema():
    uniques = [c for c in TurnEvaluation.__table__.constraints if isinstance(c, UniqueConstraint)]
    assert [(c.name, [col.name for col in c.columns]) for c in uniques] == [
        ("uq_turn_evaluations_message_version", ["message_id", "evaluator_version"])]


def test_status_and_correctness_values_are_constrained_and_match_the_evaluator():
    checks = {c.name: str(c.sqltext) for c in TurnEvaluation.__table__.constraints if isinstance(c, CheckConstraint)}
    assert set(checks) == {"ck_turn_evaluations_status_valid", "ck_turn_evaluations_correctness_valid"}
    for status in live.STATUSES:
        assert f"'{status}'" in checks["ck_turn_evaluations_status_valid"]
    assert set(TURN_EVALUATION_STATUSES) == set(live.STATUSES)
    for value in live.CORRECTNESS:
        assert f"'{value}'" in checks["ck_turn_evaluations_correctness_valid"]


def test_evaluations_are_tied_to_the_conversation_message_and_organization():
    columns = TurnEvaluation.__table__.columns
    assert not columns["organization_id"].nullable and not columns["message_id"].nullable
    targets = {tuple(fk.column.table.name + "." + fk.column.name for fk in c.elements)
               for c in TurnEvaluation.__table__.foreign_key_constraints}
    assert ("conversations.id", "conversations.organization_id") in targets
    assert ("messages.id",) in targets and ("organizations.id",) in targets
    assert "trace_id" not in columns  # no genuine trace id exists on a message today
